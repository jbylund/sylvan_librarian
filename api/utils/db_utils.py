"""Database utility functions for the API."""

import atexit
import functools
import hashlib
import logging
import os
import pathlib
import random
import time

import docker
import docker.errors
import orjson
import psycopg
import psycopg.conninfo
import psycopg.types.json
import psycopg_pool

logger = logging.getLogger(__name__)
CONFLICT = 409


class CredentialRedactingFilter(logging.Filter):
    """Mask credentials in a log record's args before any handler formats them.

    Install on the logger of the module holding the credentials, not on a handler: a logger's
    filters survive `logging.basicConfig(force=True)` and do not depend on how any entry point
    configured logging. Every call site in that module is covered, including ones added later.

    Reaches `record.args` only. `logger.info(f"...{secret}")` has already formatted the secret into
    `record.msg`, where no structure is left to find it by.
    """

    REDACTED = "[REDACTED]"
    SECRET_KEYS = frozenset({"password", "sslpassword", "passfile"})
    # Prefix-matched rather than `"://" in text`, which would catch every ordinary URL -- the
    # conninfo parser rejects those, and they would be replaced wholesale below.
    DSN_SCHEMES = ("postgresql://", "postgres://")

    def filter(self, record: logging.LogRecord) -> bool:
        """Replace the record's args with a masked copy. Always keeps the record."""
        if record.args:
            record.args = self._redact(record.args)  # type: ignore[assignment]
        return True

    def _redact(self, value: object) -> object:
        if isinstance(value, dict):
            return self._mask({k: self._redact(v) for k, v in value.items()})
        if isinstance(value, tuple):
            return tuple(self._redact(v) for v in value)
        if isinstance(value, str):
            return self._redact_connection_string(value)
        return value

    def _redact_connection_string(self, text: str) -> str:
        """Mask the password in a conninfo string or DSN, and leave every other string alone."""
        if not (text.startswith(self.DSN_SCHEMES) or any(f"{k}=" in text for k in self.SECRET_KEYS)):
            return text
        try:
            params = psycopg.conninfo.conninfo_to_dict(text)
        except psycopg.ProgrammingError:
            return "<redacted: unparseable connection string>"
        # _mask, not _redact: these values are atomic, and re-inspecting them mangles a legitimate
        # `sslrootcert=file:///...`, which parses as a connection string in its own right.
        return " ".join(f"{k}={v}" for k, v in sorted(self._mask(params).items()))

    def _mask(self, params: dict[str, object]) -> dict[str, object]:
        return {k: (self.REDACTED if k in self.SECRET_KEYS else v) for k, v in params.items()}


# Installed once, at import. Guarded by class name rather than isinstance: a reload of this module
# rebuilds the class, and the filter already on the logger would be an instance of the old one.
if not any(type(f).__name__ == CredentialRedactingFilter.__name__ for f in logger.filters):
    logger.addFilter(CredentialRedactingFilter())


def get_pg_creds() -> dict[str, str]:
    """Get postgres credentials from the environment."""
    mapping = {
        "database": "dbname",
    }
    unmapped = {k[2:].lower(): v for k, v in os.environ.items() if k.startswith("PG")}
    return {mapping.get(k, k): v for k, v in unmapped.items()}


def get_testcontainers_creds() -> dict[str, str]:
    """Get postgres credentials from the testcontainers environment."""
    logger.warning("Using an ephemeral postgres container...")
    from testcontainers.postgres import PostgresContainer  # noqa: PLC0415

    exposed_port = random.randint(1024, 49151)
    container = (
        PostgresContainer(
            image="postgres:18",
            username="testuser",
            password="testpass",  # noqa: S106
            dbname="testdb",
        )
        .with_bind_ports(5432, exposed_port)
        .with_name("postgres-test")
    )

    connection_info = {
        "dbname": "testdb",
        "password": "testpass",
        "user": "testuser",
    }
    connection_info["host"] = "localhost"
    try:
        container.start()
    except docker.errors.APIError as oops:
        if oops.status_code != CONFLICT:
            raise
        docker_client = docker.from_env()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            containers = docker_client.containers.list(
                filters={"name": "postgres-test"},
            )
            if containers:
                break
        else:
            msg = "Ephemeral postgres container not found"
            raise RuntimeError(msg)
        the_container = containers[0]
        container_attrs = the_container.attrs
        network_settings = container_attrs["NetworkSettings"]
        connection_info["port"] = network_settings["Ports"].popitem()[1][0]["HostPort"]
    else:
        connection_info["port"] = container.get_exposed_port(5432)
    logger.info("Connection info in pid %d: %s", os.getpid(), connection_info)
    return connection_info


def configure_connection(conn: psycopg.Connection) -> None:
    """Configure a connection to use dict_row as the row factory."""
    conn.row_factory = psycopg.rows.dict_row


def set_statement_timeout(cursor: psycopg.Cursor, statement_timeout: int) -> None:
    """Validate and set the statement timeout for a database cursor.

    PostgreSQL SET commands don't support parameterized values, so we must
    validate the value before using it in string interpolation.

    Args:
        cursor: Database cursor to execute the SET command on
        statement_timeout: The statement timeout value in milliseconds

    Raises:
        ValueError: If statement_timeout is not a non-negative integer
    """
    if not isinstance(statement_timeout, int) or statement_timeout < 0:
        msg = f"statement_timeout must be a non-negative integer, got: {statement_timeout}"
        raise ValueError(msg)
    cursor.execute(f"set statement_timeout = {statement_timeout}")


def make_pool() -> psycopg_pool.ConnectionPool:
    """Create and return a psycopg3 ConnectionPool for PostgreSQL connections."""
    creds = get_pg_creds()
    if not creds:
        creds = get_testcontainers_creds()
    conninfo = " ".join(f"{k}={v}" for k, v in creds.items())
    pool_args = {
        "configure": configure_connection,
        "conninfo": conninfo,
        "max_size": 2,
        "min_size": 1,
        "open": True,
    }
    # The conninfo carries PGPASSWORD. Passed as an arg, not formatted in, so CredentialRedactingFilter
    # masks it before any handler sees the record.
    logger.info("Pool args: %s", pool_args)
    pool = psycopg_pool.ConnectionPool(**pool_args)

    def cleanup() -> None:
        pool.close()

    atexit.register(cleanup)
    return pool


def get_migrations() -> list[dict[str, str]]:
    """Get the migrations from the filesystem.

    Returns:
    -------
        List[Dict[str, str]]: List of migration metadata dictionaries.

    """
    # generate migrations + their hashes
    here = pathlib.Path(__file__).parent.parent
    migrations_dir = here / "db"
    migrations = []
    for dirname, _, child_files in migrations_dir.walk():
        for ichild in sorted(child_files):
            if not ichild.lower().endswith(".sql"):
                continue
            fullpath = dirname / ichild
            with pathlib.Path(fullpath).open() as filehandle:
                contents = filehandle.read().strip()
            migrations.append(
                {
                    "file_contents": contents,
                    "file_sha256": hashlib.sha256(contents.encode()).hexdigest(),
                    "file_name": ichild,
                },
            )
    return migrations


@functools.cache
def read_sql(filename: str) -> str:
    """Read a query from api/sql/<filename>.sql.

    Memoized for the process lifetime: these files ship with the code and do not change at runtime.

    Args:
        filename: Bare stem of the file, without directory components or the .sql extension.

    Returns:
        The file's contents, stripped.

    Raises:
        ValueError: If filename is not a bare stem. Callers pass literals, so a value that could
            traverse out of the directory is a bug rather than input to sanitize — reject it instead
            of joining it. `Path(x).name == x` alone is not enough: `".."` and `""` both satisfy it.
    """
    if filename in {"", ".", ".."} or pathlib.Path(filename).name != filename:
        msg = f"read_sql expects a bare file stem, got {filename!r}"
        raise ValueError(msg)
    sql_file = pathlib.Path(__file__).parent.parent / "sql" / f"{filename}.sql"
    with sql_file.open(encoding="utf-8") as filehandle:
        return filehandle.read().strip()


class IntArray(list):
    """A list that psycopg sends as a native PostgreSQL integer array, not JSONB."""


def maybe_json(v: object) -> object:
    """Wrap plain list/dict values in Jsonb, but pass IntArray through unchanged."""
    if isinstance(v, IntArray):
        return v
    if isinstance(v, list | dict):
        return psycopg.types.json.Jsonb(v)
    return v


def orjson_dumps(obj: object) -> str:
    """Dump an object to a string using orjson."""
    return orjson.dumps(obj).decode("utf-8")


# Register for dumping (adapting Python -> DB)
psycopg.types.json.set_json_dumps(dumps=orjson_dumps)
psycopg.types.json.set_json_loads(loads=orjson.loads)
