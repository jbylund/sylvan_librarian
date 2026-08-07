"""Tests for assembling the server-rendered HTML pages."""

from __future__ import annotations

import multiprocessing
import time
import unittest
from unittest.mock import MagicMock, patch

from api.api_resource import APIResource
from api.utils import page_rendering


class TestHtmlMinification(unittest.TestCase):
    """_minify_html reduces page weight.

    Must not corrupt the per-request placeholders that _build_base_html's cached output still
    needs substituted afterward (SERVER_SIDE_RESULTS, SERVER_SIDE_EMBEDDED_DATA).
    """

    def setUp(self) -> None:
        self.mock_conn_pool = MagicMock()
        self.api_resource = APIResource(
            last_import_time=multiprocessing.Value("d", time.time(), lock=True),
        )
        self.api_resource._conn_pool = self.mock_conn_pool

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
