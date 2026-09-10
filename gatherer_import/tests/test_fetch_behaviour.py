"""Paging termination, timeouts, set-code validation, array extraction and the fetch-all CLI.

None of these touch the network: the fetcher's session is replaced with a fake whose `get` returns
scripted responses.
"""

from __future__ import annotations

import json
import sys
from typing import Any
from unittest.mock import Mock, patch

import pytest
import requests

from gatherer_import import __main__ as cli
from gatherer_import import fetch_gatherer_data
from gatherer_import.fetch_gatherer_data import REQUEST_TIMEOUT, GathererFetcher, validate_set_code


def _embed(items: list) -> str:
    """Render `items` the way a Gatherer page does: JSON, then JSON-string-escaped, inside a page."""
    escaped = json.dumps(json.dumps({"items": items}, separators=(",", ":")))[1:-1]  # {\"items\":[...]}
    _, _, after_items = escaped.partition(r"\"items\":")
    return f'<script>self.__next_f.push([1,"{{\\"page\\":1,\\"items\\":{after_items}"])</script>'


def _page(items: list) -> Mock:
    response = Mock()
    response.text = _embed(items)
    response.raise_for_status = Mock()
    return response


def _not_found() -> Mock:
    return Mock(raise_for_status=Mock(side_effect=requests.HTTPError(response=Mock(status_code=404))))


def _fetcher_with(*responses: Any) -> GathererFetcher:
    fetcher = GathererFetcher()
    fetcher.session = Mock()
    fetcher.session.get.side_effect = list(responses)
    return fetcher


class TestFetchSetTermination:
    def test_stops_on_a_200_with_no_items(self) -> None:
        """An empty page ends the listing; previously only a 404 did, and this looped forever."""
        fetcher = _fetcher_with(_page([{"id": 1}]), _page([]), _page([{"id": "never requested"}]))

        assert fetcher.fetch_set("TEST") == [{"id": 1}]
        assert fetcher.session.get.call_count == 2

    def test_page_cap_raises_instead_of_spinning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fetch_gatherer_data, "MAX_PAGES", 3)
        fetcher = GathererFetcher()
        fetcher.session = Mock()
        fetcher.session.get.return_value = _page([{"id": 1}])

        with pytest.raises(RuntimeError, match="did not end within 3 pages"):
            fetcher.fetch_set("TEST")
        assert fetcher.session.get.call_count == 3

    def test_every_request_carries_a_timeout(self) -> None:
        fetcher = _fetcher_with(_page([{"id": 1}]), _not_found(), _page([{"setCode": "AAA"}]), _not_found())

        fetcher.fetch_set("TEST")
        fetcher.fetch_all_sets()

        assert fetcher.session.get.call_count == 4
        for call in fetcher.session.get.call_args_list:
            assert call.kwargs["timeout"] == REQUEST_TIMEOUT


class TestSetCodeValidation:
    @pytest.mark.parametrize("code", ["TDM", "war_2", "abc-1", "M21", "8ed"])
    def test_accepts_plain_codes(self, code: str) -> None:
        assert validate_set_code(code) == code

    @pytest.mark.parametrize("code", ["../etc", "a/b", "TDM.json", "T DM", "", "..", "x\\y", "a?b=1"])
    def test_rejects_path_and_url_characters(self, code: str) -> None:
        with pytest.raises(ValueError, match="Invalid set code"):
            validate_set_code(code)

    def test_fetch_set_rejects_before_any_request(self) -> None:
        fetcher = GathererFetcher()
        fetcher.session = Mock()

        with pytest.raises(ValueError, match="Invalid set code"):
            fetcher.fetch_set("../sets")
        fetcher.session.get.assert_not_called()

    def test_save_set_rejects_before_touching_the_filesystem(self, tmp_path) -> None:
        fetcher = GathererFetcher()
        fetcher.fetch_set = Mock(return_value=[])

        with pytest.raises(ValueError, match="Invalid set code"):
            fetcher.save_set_to_json("../escape", output_dir=str(tmp_path / "out"))
        fetcher.fetch_set.assert_not_called()
        assert not (tmp_path / "out").exists()


class TestExtractItems:
    def test_brackets_and_quotes_inside_strings_do_not_end_the_array(self) -> None:
        """Bracket counting stopped at the first `]` in a card's text; a JSON decoder does not."""
        items = [
            {"name": "Ach! Hans, Run]", "text": 'Say "run]" [twice] and \\ backslash'},
            {"name": "Second", "text": "]]]"},
        ]

        assert GathererFetcher()._extract_items_from_response(_embed(items)) == items

    def test_only_the_leading_array_is_read(self) -> None:
        """Whatever follows the array inside the same string literal (the rest of the page state) is ignored."""
        items = [{"id": 1}]
        page = _embed(items).replace('"])</script>', ',\\"totalPages\\":7,\\"items\\":[]}"])</script>')

        assert GathererFetcher()._extract_items_from_response(page) == items

    def test_truncated_array_raises(self) -> None:
        page = _embed([{"id": 1}, {"id": 2}])
        truncated = page[: page.index(r"\"id\":2") + 5]

        with pytest.raises(ValueError, match="Could not find end of items array"):
            GathererFetcher()._extract_items_from_response(truncated)

    def test_non_array_items_raises(self) -> None:
        page = _embed([]).replace(r"\"items\":[]", r"\"items\":{}")

        with pytest.raises(ValueError, match="not an array"):
            GathererFetcher()._extract_items_from_response(page)


class TestFetchAllCommand:
    def _run(self, fetcher: Mock, tmp_path) -> int:
        with (
            patch.object(cli, "GathererFetcher", return_value=fetcher),
            patch.object(sys, "argv", ["gatherer_import", "fetch-all", "--output", str(tmp_path)]),
        ):
            return cli.main()

    def test_fetches_every_set_code_string(self, tmp_path) -> None:
        """fetch_all_sets returns strings; the old loop wanted dicts and silently fetched nothing."""
        fetcher = Mock()
        fetcher.fetch_all_sets.return_value = ["AAA", "BBB", "CCC"]
        fetcher.save_set_to_json.return_value = tmp_path / "x.json"

        assert self._run(fetcher, tmp_path) == 0
        assert [c.args[0] for c in fetcher.save_set_to_json.call_args_list] == ["AAA", "BBB", "CCC"]

    def test_a_failed_set_is_logged_skipped_and_fails_the_run(self, tmp_path, caplog: pytest.LogCaptureFixture) -> None:
        fetcher = Mock()
        fetcher.fetch_all_sets.return_value = ["AAA", "BBB", "CCC"]

        def save(set_code: str, _output: str) -> str:
            if set_code == "BBB":
                msg = "boom"
                raise requests.ConnectionError(msg)
            return f"{set_code}.json"

        fetcher.save_set_to_json.side_effect = save

        with caplog.at_level("INFO"):
            assert self._run(fetcher, tmp_path) == 1

        assert fetcher.save_set_to_json.call_count == 3  # CCC still fetched after BBB failed
        assert "BBB failed" in caplog.text
        assert "Failed sets: BBB" in caplog.text
        assert "Fetched 2 of 3 sets" in caplog.text
