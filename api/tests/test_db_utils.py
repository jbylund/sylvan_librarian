"""Tests for api/utils/db_utils.py."""

from __future__ import annotations

import logging
import os
import pathlib
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from api.utils import db_utils

if TYPE_CHECKING:
    from api.api_resource import APIResource

SENTINEL_PASSWORD = "hunter2-do-not-log-me"


class TestCredentialRedaction:
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
        # ... and the log line named the connection without it.
        assert SENTINEL_PASSWORD not in caplog.text
        assert "Pool args" in caplog.text
        assert "host=db.example.internal" in caplog.text
        assert "dbname=magic_db" in caplog.text
        assert f"password={db_utils.REDACTED}" in caplog.text

    def test_redact_conninfo_masks_a_quoted_password(self) -> None:
        rendered = db_utils.redact_conninfo("host=h dbname=d user=u password='sp ace'")
        assert "sp ace" not in rendered
        assert rendered == f"dbname=d host=h password={db_utils.REDACTED} user=u"

    def test_redact_conninfo_does_not_echo_an_unparseable_string(self) -> None:
        assert SENTINEL_PASSWORD not in db_utils.redact_conninfo(f"password='{SENTINEL_PASSWORD}")

    def test_redact_credentials_leaves_non_secrets_alone(self) -> None:
        params = {"host": "localhost", "port": "5432", "password": SENTINEL_PASSWORD}
        assert db_utils.redact_credentials(params) == {"host": "localhost", "port": "5432", "password": db_utils.REDACTED}
        assert params["password"] == SENTINEL_PASSWORD, "the caller's dict is not mutated"


def test_importing_db_utils_does_not_import_the_docker_sdk() -> None:
    """The docker SDK is only for the testcontainers fallback; production workers must not pay for it.

    Checked in a subprocess: this test process has already imported docker via testcontainers.
    """
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, "-c", "import sys, api.utils.db_utils; sys.exit(1 if 'docker' in sys.modules else 0)"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


class TestStatementTimeoutIsTransactionLocal:
    """set_statement_timeout guards the statement that follows it, and only that transaction.

    Against the real container: a session-level SET on a pooled connection persisted past commit,
    so a writer connection kept a backfill's 600 s timeout for every later statement, and the reader
    pool re-asserted its 10 s on every SQL search.
    """

    def test_timeout_applies_to_the_following_statement(self, api_resource: APIResource) -> None:
        with api_resource.app_context.writer_pool.connection() as conn, conn.cursor() as cursor:
            db_utils.set_statement_timeout(cursor, 50)
            with pytest.raises(psycopg.errors.QueryCanceled):
                cursor.execute("SELECT pg_sleep(1)")
            conn.rollback()

    def test_timeout_does_not_survive_a_commit_on_the_same_connection(self, api_resource: APIResource) -> None:
        with api_resource.app_context.writer_pool.connection() as conn, conn.cursor() as cursor:
            db_utils.set_statement_timeout(cursor, 12_345)
            cursor.execute("SHOW statement_timeout")
            assert cursor.fetchone()["statement_timeout"] == "12345ms", "in effect inside the transaction"
            conn.commit()
            cursor.execute("SHOW statement_timeout")
            assert cursor.fetchone()["statement_timeout"] == "0", "gone once the transaction ended"

    def test_next_borrower_of_the_pool_starts_clean(self, api_resource: APIResource) -> None:
        pool = api_resource.app_context.writer_pool
        with pool.connection() as conn, conn.cursor() as cursor:
            db_utils.set_statement_timeout(cursor, 12_345)
        with pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SHOW statement_timeout")
            assert cursor.fetchone()["statement_timeout"] == "0"

    def test_rejects_a_non_integer(self) -> None:
        with pytest.raises(ValueError, match="non-negative integer"):
            db_utils.set_statement_timeout(MagicMock(), "10; DROP TABLE x")  # type: ignore[arg-type]
