"""Tests for assembling the server-rendered HTML pages."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

import falcon

from api.api_resource import APIResource
from api.tests.support import mock_app_context
from api.utils import page_rendering
from api.utils.page_rendering import serialize_embedded_json


class TestServeStaticFile(unittest.TestCase):
    """serve_static_file reads a file under STATIC_DIR and writes it to the response."""

    def test_reads_file_content(self) -> None:
        mock_response = MagicMock()

        with patch("api.utils.page_rendering._read_static_text", return_value="file content"):
            page_rendering.serve_static_file(filename="test.html", falcon_response=mock_response)

        assert mock_response.text == "file content"

    def test_missing_file_serves_404(self) -> None:
        mock_response = MagicMock()

        page_rendering.serve_static_file(filename="does-not-exist.html", falcon_response=mock_response)

        assert mock_response.status == falcon.HTTP_404
        assert "does-not-exist.html" in mock_response.text

    def test_content_is_read_from_disk_only_once(self) -> None:
        """_read_static_text is process-lifetime cached: a second call skips the disk read."""
        mock_response_1 = MagicMock()
        mock_response_2 = MagicMock()

        with patch("pathlib.Path.read_text", return_value="cached content") as mock_read_text:
            page_rendering.serve_static_file(filename="__test_cache_marker__.html", falcon_response=mock_response_1)
            page_rendering.serve_static_file(filename="__test_cache_marker__.html", falcon_response=mock_response_2)

        assert mock_response_1.text == "cached content"
        assert mock_response_2.text == "cached content"
        mock_read_text.assert_called_once()


class TestReadStaticBytes(unittest.TestCase):
    """read_static_bytes is process-lifetime cached, like _read_static_text."""

    def test_content_is_read_from_disk_only_once(self) -> None:
        with patch("pathlib.Path.read_bytes", return_value=b"cached bytes") as mock_read_bytes:
            first = page_rendering.read_static_bytes("__test_bytes_cache_marker__.ico")
            second = page_rendering.read_static_bytes("__test_bytes_cache_marker__.ico")

        assert first == b"cached bytes"
        assert second == b"cached bytes"
        mock_read_bytes.assert_called_once()


class TestHtmlMinification(unittest.TestCase):
    """_minify_html reduces page weight.

    Must not corrupt the per-request placeholders that _build_base_html's cached output still
    needs substituted afterward (SERVER_SIDE_RESULTS, SERVER_SIDE_EMBEDDED_DATA).
    """

    def setUp(self) -> None:
        self.app_context = mock_app_context()
        self.mock_conn_pool = self.app_context.reader_pool
        self.api_resource = APIResource(app_context=self.app_context)

    def test_minifies_whitespace_by_default(self) -> None:
        # minify_html also drops the redundant closing </p> (valid HTML5 tag-omission), hence
        # "<div><p>x</div>" rather than a literal whitespace-only collapse.
        assert page_rendering._minify_html("<div>   <p>x</p>   </div>") == "<div><p>x</div>"

    def test_disabled_flag_returns_input_unchanged(self) -> None:
        original = page_rendering._MINIFY_HTML_ENABLED
        page_rendering._MINIFY_HTML_ENABLED = False
        try:
            html = "<div>   <p>x</p>   </div>"
            assert page_rendering._minify_html(html) == html
        finally:
            page_rendering._MINIFY_HTML_ENABLED = original

    def test_server_side_placeholders_survive_minification(self) -> None:
        mock_response = MagicMock()
        self.api_resource._root(falcon_response=mock_response)
        assert "<!-- SERVER_SIDE_RESULTS -->" in mock_response.text
        assert "<!-- SERVER_SIDE_EMBEDDED_DATA -->" in mock_response.text

    def test_search_results_still_embed_after_minification(self) -> None:
        mock_response = MagicMock()
        mock_search_results = {
            "cards": [{"name": "Elvish Mystic", "set_code": "m14", "collector_number": "1"}],
            "total_cards": 1,
            "query": "elf",
        }
        with patch.object(self.api_resource, "_search", return_value=mock_search_results):
            self.api_resource._root(falcon_response=mock_response, q="elf")
        assert "window.EMBEDDED_SEARCH_RESULTS = {" in mock_response.text
        assert "Elvish Mystic" in mock_response.text

    def test_hostile_query_in_search_results_escapes_script_breakout(self) -> None:
        mock_response = MagicMock()
        hostile_query = "</script><script>alert('xss')</script>"
        mock_search_results = {
            "cards": [{"name": "<img src=x onerror=alert(1)>", "set_code": "m14", "collector_number": "1"}],
            "total_cards": 1,
            "query": hostile_query,
        }
        with patch.object(self.api_resource, "_search", return_value=mock_search_results):
            self.api_resource._root(falcon_response=mock_response, q=hostile_query)

        # The embedded script assignment must not contain literal closing script tags or < characters
        assert "window.EMBEDDED_SEARCH_RESULTS = " in mock_response.text
        # Extract the script content containing the embedded data
        script_marker = "window.EMBEDDED_SEARCH_RESULTS = "
        script_start = mock_response.text.index(script_marker) + len(script_marker)
        script_end = mock_response.text.index(";", script_start)
        embedded_json = mock_response.text[script_start:script_end]

        assert "<" not in embedded_json
        assert "</script>" not in embedded_json

        # Hydration parity: parsing the escaped JSON yields the exact original payload
        hydrated = json.loads(embedded_json)
        assert hydrated == mock_search_results


class TestSerializeEmbeddedJson(unittest.TestCase):
    """serialize_embedded_json produces HTML-script-safe JSON by escaping literal `<`."""

    def test_escapes_literal_less_than(self) -> None:
        payload = {"tag": "<script>", "arrow": "a < b", "nested": [{"key": "<value>"}]}
        serialized = serialize_embedded_json(payload)

        assert "<" not in serialized
        assert r"\u003c" in serialized
        assert json.loads(serialized) == payload

    def test_hostile_script_closing_payload(self) -> None:
        payload = {
            "query": "</script><script>alert('breakout')</script>",
            "comment": "<!-- comment -->",
        }
        serialized = serialize_embedded_json(payload)

        assert "<" not in serialized
        assert "</script>" not in serialized
        assert "<!--" not in serialized
        assert json.loads(serialized) == payload

    def test_preserves_complex_datatypes_and_unicode(self) -> None:
        payload = {
            "string": "Lim-Dûl's Vault",
            "escapes": 'quote: ", backslash: \\, newline: \n, tab: \t',
            "numbers": [0, 42, -3.14, 1e-5],
            "booleans": [True, False],
            "null": None,
            "html_entities": "&amp; &lt; &gt; &quot;",
            "script_tags": '</script><script src="evil.js"></script>',
        }
        serialized = serialize_embedded_json(payload)

        assert "<" not in serialized
        assert json.loads(serialized) == payload


class TestAppScriptFallsBackToTheUnminifiedSource(unittest.TestCase):
    """index.html names app.min.js, a gitignored build artifact; a checkout without it must still ship JS.

    Neither hash is read per request (both are computed at import), so the tests patch the module
    constants rather than the filesystem. The cache is off in the suite, so build_base_html is
    rebuilt per call.
    """

    def _routes(self) -> set[str]:
        return set(APIResource(app_context=mock_app_context()).routes)

    def test_without_the_minified_file_the_page_loads_app_js(self) -> None:
        with (
            patch.object(page_rendering, "_APP_MIN_JS_HASH", None),
            patch.object(page_rendering, "_APP_JS_HASH", "abc123def456"),
        ):
            html = page_rendering.build_base_html("", "Site")
        assert "/static/app.js?v=abc123def456" in html
        assert "/static/app.min.js" not in html
        # The route table serves what the page references.
        assert "static/app.js" in self._routes()

    def test_with_the_minified_file_the_page_loads_it(self) -> None:
        with (
            patch.object(page_rendering, "_APP_MIN_JS_HASH", "0123456789ab"),
            patch.object(page_rendering, "_APP_JS_HASH", "abc123def456"),
        ):
            html = page_rendering.build_base_html("", "Site")
        assert "/static/app.min.js?v=0123456789ab" in html
        assert "/static/app.js?v=" not in html
        assert "static/app.min.js" in self._routes()

    def test_with_neither_hash_the_page_still_names_a_servable_script(self) -> None:
        with (
            patch.object(page_rendering, "_APP_MIN_JS_HASH", None),
            patch.object(page_rendering, "_APP_JS_HASH", None),
        ):
            html = page_rendering.build_base_html("", "Site")
        assert "/static/app.js" in html

    def test_the_committed_source_always_has_a_hash(self) -> None:
        """app.js is committed, so the fallback is never hashless in a real checkout."""
        assert page_rendering._APP_JS_HASH is not None
