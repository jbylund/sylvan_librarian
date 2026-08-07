"""Shared fixtures for api integration tests."""

from __future__ import annotations

import multiprocessing
import time
from typing import TYPE_CHECKING

import pytest

from api.api_resource import APIResource
from api.settings import settings
from api.tests.support import override_attr

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture(name="engine_enabled")
def engine_enabled_fixture() -> Generator[None]:
    """Enable the engine feature gate (ENABLE_ENGINE) for the duration of a test."""
    saved = settings.enable_engine
    settings.enable_engine = True
    yield
    settings.enable_engine = saved


@pytest.fixture(name="stub_api_resource")
def stub_api_resource_fixture() -> Generator[APIResource]:
    """A fresh APIResource per test, readiness stubbed, connection pool closed afterward.

    Function-scoped on purpose: it replaces per-class `setup_method` construction, and tests using it
    mutate the instance (`_engine`, feature gates), so sharing one across a module would couple them.

    Two things it deliberately does *not* do:

    - It does not stub `_import_recent`. What keeps `__init__`'s own `import_data()` call on its fast
      path is `last_import_time` being now — an override applied after construction would be too late
      for that anyway. Tests that go on to exercise an import path override it themselves, where it is
      visible: it guards a network fetch, and a silently ineffective override there means the suite
      starts doing real Scryfall work while still passing.
    - It does not set up a schema or touch the database. Tests needing that want the `api_resource`
      fixture below instead.
    """
    api = APIResource(last_import_time=multiprocessing.Value("d", time.time(), lock=True))
    override_attr(api, "_setup_complete", lambda: True)
    yield api
    api._conn_pool.close()


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
    override_attr(api, "_setup_complete", lambda: True)
    override_attr(api.admin, "_import_recent", lambda: True)
    api.admin.setup_schema()
    yield api
    api._conn_pool.close()
