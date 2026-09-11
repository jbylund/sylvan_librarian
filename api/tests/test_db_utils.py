"""Tests for api/utils/db_utils.py."""

from __future__ import annotations

import io
import logging
import os
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from api.utils import db_utils

if TYPE_CHECKING:
    import pytest

SENTINEL_PASSWORD = "hunter2-do-not-log-me"
REDACTED = db_utils.CredentialRedactingFilter.REDACTED


def filtered_message(msg: str, *args: object) -> str:
    """Build a record the way `logger.info(msg, *args)` would, run it through the filter, and format it."""
    record = logging.LogRecord(db_utils.logger.name, logging.INFO, __file__, 0, msg, args, None)
    assert db_utils.CredentialRedactingFilter().filter(record) is True, "the filter masks; it never drops a record"
    return record.getMessage()


class TestMakePool:
    """Pool construction logs its arguments; the password in the conninfo must not be among them."""

    def test_make_pool_does_not_log_the_password(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        for name in [k for k in os.environ if k.startswith("PG")]:
            monkeypatch.delenv(name)
        monkeypatch.setenv("PGHOST", "db.example.internal")
        monkeypatch.setenv("PGDATABASE", "magic_db")
        monkeypatch.setenv("PGUSER", "magic_user")
        monkeypatch.setenv("PGPASSWORD", SENTINEL_PASSWORD)

        with (
            patch.object(db_utils.psycopg_pool, "ConnectionPool", return_value=MagicMock()) as pool_cls,
            patch.object(db_utils.atexit, "register"),
            caplog.at_level(logging.DEBUG, logger="api.utils.db_utils"),
        ):
            db_utils.make_pool()

        # The real pool still received the real secret ...
        assert f"password={SENTINEL_PASSWORD}" in pool_cls.call_args.kwargs["conninfo"]
        # ... and the log line named the connection without it. caplog's handler sits on the root
        # logger; the module logger's filter runs before the record propagates there, so this is the
        # masked record.
        assert SENTINEL_PASSWORD not in caplog.text
        assert "Pool args" in caplog.text
        assert "host=db.example.internal" in caplog.text
        assert "dbname=magic_db" in caplog.text
        assert "user=magic_user" in caplog.text
        assert f"password={REDACTED}" in caplog.text


class TestCredentialRedactingFilter:
    def test_is_installed_on_the_module_logger(self) -> None:
        assert any(isinstance(f, db_utils.CredentialRedactingFilter) for f in db_utils.logger.filters)

    def test_masks_a_dict_arg_without_mutating_it(self) -> None:
        params = {"host": "localhost", "port": "5432", "password": SENTINEL_PASSWORD}
        rendered = filtered_message("creds: %s", params)
        assert SENTINEL_PASSWORD not in rendered
        assert "'host': 'localhost'" in rendered
        assert f"'password': '{REDACTED}'" in rendered
        assert params["password"] == SENTINEL_PASSWORD, "the caller goes on to connect with this dict"

    def test_masks_a_conninfo_string_arg(self) -> None:
        rendered = filtered_message("conninfo: %s", f"host=h dbname=d user=u password={SENTINEL_PASSWORD}")
        assert SENTINEL_PASSWORD not in rendered
        assert rendered == f"conninfo: dbname=d host=h password={REDACTED} user=u"

    def test_masks_a_postgresql_dsn_arg(self) -> None:
        rendered = filtered_message("dsn: %s", f"postgresql://magic_user:{SENTINEL_PASSWORD}@db.example.internal/magic_db")
        assert SENTINEL_PASSWORD not in rendered
        assert "user=magic_user" in rendered
        assert "host=db.example.internal" in rendered
        assert "dbname=magic_db" in rendered
        assert f"password={REDACTED}" in rendered

    def test_masks_a_quoted_password_whole(self) -> None:
        rendered = filtered_message("conninfo: %s", "host=h dbname=d user=u password='sp ace'")
        assert "sp ace" not in rendered
        assert rendered == f"conninfo: dbname=d host=h password={REDACTED} user=u"

    def test_does_not_echo_an_unparseable_connection_string(self) -> None:
        rendered = filtered_message("conninfo: %s", f"password='{SENTINEL_PASSWORD}")
        assert SENTINEL_PASSWORD not in rendered
        assert rendered == "conninfo: <redacted: unparseable connection string>"

    def test_masks_inside_a_tuple_of_args(self) -> None:
        rendered = filtered_message("Connection info in pid %d: %s", 4242, {"host": "localhost", "password": SENTINEL_PASSWORD})
        assert SENTINEL_PASSWORD not in rendered
        assert rendered.startswith("Connection info in pid 4242: ")
        assert f"'password': '{REDACTED}'" in rendered

    def test_leaves_an_ordinary_url_untouched(self) -> None:
        url = "https://example.com/a=b"
        assert filtered_message("fetching %s", url) == f"fetching {url}"

    def test_does_not_mangle_an_sslrootcert_file_url(self) -> None:
        rendered = filtered_message("conninfo: %s", f"host=h password={SENTINEL_PASSWORD} sslrootcert=file:///etc/ssl/ca.pem")
        assert SENTINEL_PASSWORD not in rendered
        assert "sslrootcert=file:///etc/ssl/ca.pem" in rendered

    def test_an_fstring_formatted_secret_is_not_caught(self, caplog: pytest.LogCaptureFixture) -> None:
        # The known limit of a filter on record.args, called out in the class docstring: an f-string has
        # already formatted the secret into record.msg, where no structure is left to find it by. This
        # test pins that behavior so the limit is documented rather than assumed away; the fix for such a
        # call site is to pass the value as an arg, as make_pool does.
        with caplog.at_level(logging.DEBUG, logger="api.utils.db_utils"):
            db_utils.logger.info(f"conninfo: password={SENTINEL_PASSWORD}")
        assert SENTINEL_PASSWORD in caplog.text

    def test_survives_basic_config_force(self) -> None:
        # basicConfig(force=True) removes every root handler, caplog's included, so this test captures
        # through the stream basicConfig itself installs, and puts the root logger back afterwards.
        root = logging.getLogger()
        saved_handlers, saved_level = root.handlers[:], root.level
        stream = io.StringIO()
        try:
            logging.basicConfig(force=True, stream=stream, level=logging.DEBUG, format="%(message)s")
            assert any(isinstance(f, db_utils.CredentialRedactingFilter) for f in db_utils.logger.filters)
            db_utils.logger.info("Pool args: %s", {"conninfo": f"host=h password={SENTINEL_PASSWORD}", "max_size": 2})
        finally:
            for handler in root.handlers[:]:
                root.removeHandler(handler)
            for handler in saved_handlers:
                root.addHandler(handler)
            root.setLevel(saved_level)
        assert SENTINEL_PASSWORD not in stream.getvalue()
        assert f"host=h password={REDACTED}" in stream.getvalue()
        assert "'max_size': 2" in stream.getvalue()
