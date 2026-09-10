"""Tests for reconciling the migrations table against the files on disk (AdminResource.setup_schema)."""

from __future__ import annotations

import hashlib
import multiprocessing
from unittest.mock import MagicMock, patch

import pytest

from api.admin_resource import AdminContext, AdminResource, plan_migrations
from api.tests.support import mock_app_context


def _migration(name: str, contents: str) -> dict[str, str]:
    return {
        "file_name": name,
        "file_sha256": hashlib.sha256(contents.encode()).hexdigest(),
        "file_contents": contents,
    }


def _applied(migration: dict[str, str]) -> dict[str, str]:
    """The row the migrations table holds for a file: name and hash only."""
    return {"file_name": migration["file_name"], "file_sha256": migration["file_sha256"]}


A = _migration("2025-01-01-a.sql", "CREATE TABLE a (id int)")
B = _migration("2025-01-02-b.sql", "CREATE TABLE b (id int)")
B_PRIME = _migration("2025-01-02-b.sql", "CREATE TABLE b (id int, edited text)")
C = _migration("2025-01-03-c.sql", "CREATE TABLE c (id int)")
ON_DISK = [A, B, C]


class TestPlanMigrations:
    def test_matching_history_skips_every_applied_file(self) -> None:
        skip, reset = plan_migrations([_applied(A), _applied(B)], ON_DISK)
        assert skip == {A["file_sha256"], B["file_sha256"]}
        assert reset is False

    def test_nothing_applied_applies_everything(self) -> None:
        assert plan_migrations([], ON_DISK) == (set(), False)

    def test_divergence_resets_and_skips_nothing(self) -> None:
        """[A, B', C] applied against [A, B, C] on disk: the schema is rebuilt, so all three must run.

        The old loop cleared the skip set at B' and kept walking, so C -- which matched -- went back
        into the skip set and was never re-applied after its table had been dropped with the schema.
        """
        skip, reset = plan_migrations([_applied(A), _applied(B_PRIME), _applied(C)], ON_DISK)
        assert reset is True
        assert skip == set()

    def test_divergence_forgets_the_rows_before_it_too(self) -> None:
        """A's objects are dropped with the schema, so A must run again as well."""
        skip, _reset = plan_migrations([_applied(A), _applied(B_PRIME)], ON_DISK)
        assert A["file_sha256"] not in skip


class TestSetupSchemaAppliesEverythingAfterADivergence:
    """The apply loop, driven end to end against a recording cursor."""

    @pytest.fixture(name="admin")
    def admin_fixture(self) -> tuple[AdminResource, MagicMock]:
        cursor = MagicMock()
        cursor.__iter__.return_value = iter([_applied(A), _applied(B_PRIME), _applied(C)])
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        writer_pool = MagicMock()
        writer_pool.connection.return_value.__enter__.return_value = conn
        admin = AdminResource(
            app_context=mock_app_context(writer_pool=writer_pool),
            admin_context=AdminContext(schema_setup_event=multiprocessing.Event()),
        )
        return admin, cursor

    def test_every_file_is_applied(self, admin: tuple[AdminResource, MagicMock]) -> None:
        resource, cursor = admin
        with patch("api.admin_resource.db_utils.get_migrations", return_value=ON_DISK):
            resource.setup_schema()

        executed = [call.args[0] for call in cursor.execute.call_args_list]
        assert "DROP SCHEMA IF EXISTS magic CASCADE" in executed
        for migration in ON_DISK:
            assert migration["file_contents"] in executed, f"{migration['file_name']} was not re-applied"
        # Exactly one reset, however many rows diverged.
        assert executed.count("DELETE FROM migrations") == 1
        assert resource.admin_context.schema_setup_event.is_set()
