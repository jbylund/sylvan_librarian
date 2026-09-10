"""Tests for the /ready probe."""

from __future__ import annotations

from unittest.mock import MagicMock

import falcon
import pytest

from api.api_resource import APIResource
from api.settings import settings
from api.tests.support import mock_app_context, override_attr


@pytest.fixture(name="resource")
def resource_fixture() -> APIResource:
    return APIResource(app_context=mock_app_context(reader_pool=MagicMock(), writer_pool=MagicMock()))


@pytest.fixture(name="engine_setting")
def engine_setting_fixture() -> None:
    saved = settings.enable_engine
    yield
    settings.enable_engine = saved


def _probe(resource: APIResource, *, setup_complete: bool, engine_size: int, enable_engine: bool) -> tuple[str, dict]:
    override_attr(resource.app_context, "setup_complete", lambda: setup_complete)
    resource.app_context.engine.size.return_value = engine_size
    settings.enable_engine = enable_engine
    resp = falcon.Response()
    body = resource.ready(falcon_response=resp)
    return resp.status, body


@pytest.mark.usefixtures("engine_setting")
class TestReady:
    """/get_pid is 200 on a stack with zero cards; /ready is what a health check should poll."""

    def test_ready_when_imported_and_engine_loaded(self, resource: APIResource) -> None:
        status, body = _probe(resource, setup_complete=True, engine_size=90_001, enable_engine=True)
        assert status == falcon.HTTP_200
        assert body == {"ready": True, "checks": {"setup_complete": True, "engine_loaded": True}, "failed": []}

    def test_not_ready_before_the_import_finishes(self, resource: APIResource) -> None:
        status, body = _probe(resource, setup_complete=False, engine_size=90_001, enable_engine=True)
        assert status == falcon.HTTP_503
        assert body["ready"] is False
        assert body["failed"] == ["setup_complete"]

    def test_not_ready_while_the_engine_store_is_empty(self, resource: APIResource) -> None:
        status, body = _probe(resource, setup_complete=True, engine_size=0, enable_engine=True)
        assert status == falcon.HTTP_503
        assert body["failed"] == ["engine_loaded"]

    def test_an_empty_engine_is_fine_when_the_engine_is_disabled(self, resource: APIResource) -> None:
        status, body = _probe(resource, setup_complete=True, engine_size=0, enable_engine=False)
        assert status == falcon.HTTP_200
        assert body["ready"] is True

    def test_both_failures_are_named(self, resource: APIResource) -> None:
        _status, body = _probe(resource, setup_complete=False, engine_size=0, enable_engine=True)
        assert body["failed"] == ["setup_complete", "engine_loaded"]

    def test_never_cached(self, resource: APIResource) -> None:
        override_attr(resource.app_context, "setup_complete", lambda: True)
        resource.app_context.engine.size.return_value = 1
        resp = falcon.Response()
        resource.ready(falcon_response=resp)
        assert resp.get_header("Cache-Control") == "no-store"

    def test_is_advertised(self, resource: APIResource) -> None:
        assert "ready" in resource._not_found_routes
