"""Fixtures for the test suite."""

from __future__ import annotations

if True:
    import warnings

    warnings.filterwarnings("ignore", category=DeprecationWarning)

import logging
import os
import random
from typing import TYPE_CHECKING

import pytest
from testcontainers.postgres import PostgresContainer

from api.settings import settings

if TYPE_CHECKING:
    from collections.abc import Generator

logging.basicConfig(
    force=True,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


@pytest.fixture(scope="session", name="postgres_container", autouse=True)
def postgres_container_fixture() -> Generator[None]:
    """Fixture to start and stop a postgres container for the session."""
    exposed_port = random.randint(1024, 49151)
    container = PostgresContainer(
        image="postgres:18",
        username="testuser",
        password="testpass",  # noqa: S106
        dbname="testdb",
    ).with_bind_ports(5432, exposed_port)
    container.start()
    os.environ.update(
        {
            "PGDATABASE": "testdb",
            "PGHOST": container.get_container_host_ip(),
            "PGPASSWORD": "testpass",
            "PGPORT": str(container.get_exposed_port(5432)),
            "PGUSER": "testuser",
        },
    )
    yield
    container.stop()


@pytest.fixture
def enable_cache() -> None:
    """Fixture to enable caching for specific tests."""
    original_setting = settings.enable_cache
    settings.enable_cache = True
    yield
    settings.enable_cache = original_setting
