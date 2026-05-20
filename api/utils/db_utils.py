"""Database utility functions for the API."""

import atexit
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
import psycopg.types.json
import psycopg_pool

logger = logging.getLogger(__name__)
CONFLICT = 409


class UUIDToStringLoader(psycopg.adapt.Loader):
    """Loader that converts UUID data from PostgreSQL to strings."""

    def load(self, data: memoryview) -> str:
        """Convert UUID bytes to string representation."""
        # UUID data comes as bytes, convert to string
        return data.tobytes().decode("utf-8")


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

    exposed_port = random.randint(1024, 49151)  # noqa: S311
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
    # Register UUID loader to convert UUID data to strings
    # UUID type OID in PostgreSQL is 2950
    psycopg.adapters.register_loader(2950, UUIDToStringLoader)


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


class IntArray(list):
    """A list that psycopg sends as a native PostgreSQL integer array, not JSONB."""


def maybe_json(v: object) -> object:
    """Wrap a value in a Jsonb object if it is a list or dict."""
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
