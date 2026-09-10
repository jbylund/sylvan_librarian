"""Tests for CompressionMiddleware's decision of what to compress."""

from __future__ import annotations

import os

import falcon
import falcon.testing
import pytest

from api.middlewares.compression import CompressionMiddleware
from api.middlewares.compression.compression_mod import is_compressible_content_type

# Well above MIN_SIZE, and incompressible on its own so a wrong decision cannot hide behind "it was
# tiny anyway".
_PAYLOAD = os.urandom(100 * 1024)


class _Static:
    def __init__(self, content_type: str, data: bytes = _PAYLOAD) -> None:
        self._content_type = content_type
        self._data = data

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        del req
        resp.data = self._data
        resp.content_type = self._content_type


def _get(handler: object) -> falcon.testing.Result:
    app = falcon.App(middleware=[CompressionMiddleware()])
    app.add_route("/x", handler)
    return falcon.testing.TestClient(app).simulate_get("/x", headers={"Accept-Encoding": "gzip"})


class TestImagesAreNotCompressed:
    """Raster images carry their own compression; recompressing the WebP social preview was pure CPU."""

    @pytest.mark.parametrize(argnames=["content_type"], argvalues=[("image/webp",), ("image/png",), ("image/jpeg; q=0.9",)])
    def test_raster_image_is_served_as_is(self, content_type: str) -> None:
        result = _get(_Static(content_type))
        assert result.status == falcon.HTTP_200
        assert result.headers.get("Content-Encoding") is None
        assert result.content == _PAYLOAD

    @pytest.mark.parametrize(
        argnames=["content_type"],
        argvalues=[("image/svg+xml",), ("image/x-icon",), ("image/vnd.microsoft.icon",)],
    )
    def test_svg_and_favicon_types_still_compress(self, content_type: str) -> None:
        result = _get(_Static(content_type, data=b"<svg>" * 10_000))
        assert result.headers.get("Content-Encoding") == "gzip"

    @pytest.mark.parametrize(
        argnames=["content_type"], argvalues=[("application/json",), ("text/html; charset=utf-8",), ("text/css",)]
    )
    def test_text_types_still_compress(self, content_type: str) -> None:
        result = _get(_Static(content_type, data=b'{"cards": []}' * 1_000))
        assert result.headers.get("Content-Encoding") == "gzip"


@pytest.mark.parametrize(
    argnames=["content_type", "expected"],
    argvalues=[
        ("image/webp", False),
        ("IMAGE/PNG", False),
        ("image/gif; foo=bar", False),
        ("image/svg+xml", True),
        ("image/x-icon", True),
        ("image/vnd.microsoft.icon", True),
        ("application/json", True),
        ("", True),
        (None, True),
    ],
)
def test_is_compressible_content_type(content_type: str | None, expected: bool) -> None:
    assert is_compressible_content_type(content_type) is expected
