"""Shared fixtures for api integration tests."""

from __future__ import annotations

import multiprocessing
import time
from typing import TYPE_CHECKING

import pytest

from api.api_resource import APIResource
from api.settings import settings

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture(name="engine_enabled")
def engine_enabled_fixture() -> Generator[None]:
    """Enable the engine feature gate (ENABLE_ENGINE) for the duration of a test."""
    saved = settings.enable_engine
    settings.enable_engine = True
    yield
    settings.enable_engine = saved


@pytest.fixture(scope="module")
def api_resource(postgres_container: None) -> Generator[APIResource]:
    """APIResource wired to the session-scoped postgres container, with the schema set up.

    The root conftest's session container exports the PG* env vars, so the database is shared
    across the whole test session: tests using this fixture must make assertions only about
    rows they created themselves (unique card names / oracle_ids), never about global counts.
    """
    api = APIResource(
        last_import_time=multiprocessing.Value("d", time.time(), lock=True),
        schema_setup_event=multiprocessing.Event(),
    )
    api._setup_complete = lambda: True
    api._import_recent = lambda: True
    api.setup_schema()
    yield api
    api._conn_pool.close()
