"""Tests for ScryfallBulkDataFetcher streaming methods."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import orjson
import pytest
import zstandard as zstd

from api.scryfall_bulk_data_fetcher import BulkDataKey, ScryfallBulkDataFetcher

if TYPE_CHECKING:
    import pathlib

_FAKE_URI = "https://data.scryfall.io/default-cards/default-cards-test.json"
_FAKE_BULK_DATA = {BulkDataKey.DEFAULT_CARDS: {"download_uri": _FAKE_URI}}


def _make_fetcher(cache_directory: pathlib.Path) -> ScryfallBulkDataFetcher:
    fetcher = ScryfallBulkDataFetcher.__new__(ScryfallBulkDataFetcher)
    fetcher.cache_directory = cache_directory
    fetcher.session = MagicMock()
    return fetcher


def _write_cache_file(cache_dir: pathlib.Path, raw_json: bytes) -> pathlib.Path:
    cache_file = cache_dir / "default_cards" / "default-cards-test.json.zstd"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_bytes(zstd.compress(raw_json))
    return cache_file


def _scryfall_json(cards: list[dict]) -> bytes:
    """Produce a Scryfall-style newline-delimited JSON array."""
    lines = ["[\n"]
    for i, card in enumerate(cards):
        comma = "," if i < len(cards) - 1 else ""
        lines.append(orjson.dumps(card).decode() + comma + "\n")
    lines.append("]\n")
    return "".join(lines).encode()


class TestStreamDataForKeyHappyPath:
    @pytest.fixture
    def fetcher(self, tmp_path: pathlib.Path) -> ScryfallBulkDataFetcher:
        return _make_fetcher(tmp_path)

    def test_streams_all_cards_from_cache(self, fetcher: ScryfallBulkDataFetcher, tmp_path: pathlib.Path) -> None:
        cards = [{"id": "c1", "name": "Lightning Bolt"}, {"id": "c2", "name": "Counterspell"}]
        _write_cache_file(tmp_path, _scryfall_json(cards))

        with patch.object(fetcher, "list_bulk_data", return_value=_FAKE_BULK_DATA):
            result = list(fetcher.stream_data_for_key(BulkDataKey.DEFAULT_CARDS))

        assert len(result) == 2
        assert result[0] == {"id": "c1", "name": "Lightning Bolt"}
        assert result[1] == {"id": "c2", "name": "Counterspell"}

    def test_single_card_file(self, fetcher: ScryfallBulkDataFetcher, tmp_path: pathlib.Path) -> None:
        _write_cache_file(tmp_path, _scryfall_json([{"id": "only", "name": "Dark Ritual"}]))

        with patch.object(fetcher, "list_bulk_data", return_value=_FAKE_BULK_DATA):
            result = list(fetcher.stream_data_for_key(BulkDataKey.DEFAULT_CARDS))

        assert len(result) == 1
        assert result[0]["name"] == "Dark Ritual"

    def test_empty_array_yields_nothing(self, fetcher: ScryfallBulkDataFetcher, tmp_path: pathlib.Path) -> None:
        _write_cache_file(tmp_path, _scryfall_json([]))

        with patch.object(fetcher, "list_bulk_data", return_value=_FAKE_BULK_DATA):
            result = list(fetcher.stream_data_for_key(BulkDataKey.DEFAULT_CARDS))

        assert result == []


class TestStreamDataForKeyLineParsing:
    @pytest.fixture
    def fetcher(self, tmp_path: pathlib.Path) -> ScryfallBulkDataFetcher:
        return _make_fetcher(tmp_path)

    def test_skips_opening_and_closing_bracket_lines(self, fetcher: ScryfallBulkDataFetcher, tmp_path: pathlib.Path) -> None:
        """[ and ] delimiter lines must not be parsed as cards."""
        raw = b'[\n{"id":"c1","name":"A"}\n]\n'
        _write_cache_file(tmp_path, raw)

        with patch.object(fetcher, "list_bulk_data", return_value=_FAKE_BULK_DATA):
            result = list(fetcher.stream_data_for_key(BulkDataKey.DEFAULT_CARDS))

        assert result == [{"id": "c1", "name": "A"}]

    def test_strips_trailing_comma_from_card_lines(self, fetcher: ScryfallBulkDataFetcher, tmp_path: pathlib.Path) -> None:
        """Trailing commas on all-but-last card lines must be stripped before parsing."""
        raw = b'[\n{"id":"c1","name":"A"},\n{"id":"c2","name":"B"},\n{"id":"c3","name":"C"}\n]\n'
        _write_cache_file(tmp_path, raw)

        with patch.object(fetcher, "list_bulk_data", return_value=_FAKE_BULK_DATA):
            result = list(fetcher.stream_data_for_key(BulkDataKey.DEFAULT_CARDS))

        assert [r["name"] for r in result] == ["A", "B", "C"]

    def test_skips_malformed_lines_and_logs_warning(
        self, fetcher: ScryfallBulkDataFetcher, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Malformed JSON lines are skipped; a WARNING is emitted and parsing continues."""
        raw = b'[\n{"id":"good1","name":"Good"},\n{bad json here},\n{"id":"good2","name":"Also Good"}\n]\n'
        _write_cache_file(tmp_path, raw)

        with patch.object(fetcher, "list_bulk_data", return_value=_FAKE_BULK_DATA), caplog.at_level(logging.WARNING):
            result = list(fetcher.stream_data_for_key(BulkDataKey.DEFAULT_CARDS))

        assert [r["name"] for r in result] == ["Good", "Also Good"]
        assert any("Skipping unparseable" in r.message for r in caplog.records)

    def test_ignores_blank_lines(self, fetcher: ScryfallBulkDataFetcher, tmp_path: pathlib.Path) -> None:
        raw = b'[\n\n{"id":"c1","name":"A"}\n\n]\n'
        _write_cache_file(tmp_path, raw)

        with patch.object(fetcher, "list_bulk_data", return_value=_FAKE_BULK_DATA):
            result = list(fetcher.stream_data_for_key(BulkDataKey.DEFAULT_CARDS))

        assert len(result) == 1


class TestStreamDataForKeyCacheMiss:
    @pytest.fixture
    def fetcher(self, tmp_path: pathlib.Path) -> ScryfallBulkDataFetcher:
        return _make_fetcher(tmp_path)

    def test_downloads_and_caches_on_miss(self, fetcher: ScryfallBulkDataFetcher, tmp_path: pathlib.Path) -> None:
        """When no cache file exists, the file is downloaded and written as zstd-compressed cache."""
        raw_json = _scryfall_json([{"id": "dl-1", "name": "Downloaded Card"}])
        mock_response = MagicMock()
        mock_response.iter_content.return_value = [raw_json]
        fetcher.session.get.return_value = mock_response

        with patch.object(fetcher, "list_bulk_data", return_value=_FAKE_BULK_DATA):
            result = list(fetcher.stream_data_for_key(BulkDataKey.DEFAULT_CARDS))

        cache_file = tmp_path / "default_cards" / "default-cards-test.json.zstd"
        assert cache_file.exists(), "cache file should have been written"
        # The streaming compressor omits content size, so use ZstdDecompressor instead of zstd.decompress()
        dctx = zstd.ZstdDecompressor()
        decompressed = dctx.decompress(cache_file.read_bytes(), max_output_size=10 * 1024 * 1024)
        assert b"Downloaded Card" in decompressed
        assert result == [{"id": "dl-1", "name": "Downloaded Card"}]

    def test_second_call_does_not_re_download(self, fetcher: ScryfallBulkDataFetcher, tmp_path: pathlib.Path) -> None:
        """Once the cache file exists, subsequent calls must not make HTTP requests."""
        _write_cache_file(tmp_path, _scryfall_json([{"id": "c1", "name": "Cached"}]))

        with patch.object(fetcher, "list_bulk_data", return_value=_FAKE_BULK_DATA):
            list(fetcher.stream_data_for_key(BulkDataKey.DEFAULT_CARDS))
            list(fetcher.stream_data_for_key(BulkDataKey.DEFAULT_CARDS))

        fetcher.session.get.assert_not_called()

    def test_prunes_stale_cache_files_before_download(self, fetcher: ScryfallBulkDataFetcher, tmp_path: pathlib.Path) -> None:
        """Stale cache files in the directory are deleted before writing a new one."""
        stale = tmp_path / "default_cards" / "old-file.json.zstd"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"stale data")

        raw_json = _scryfall_json([{"id": "new", "name": "New Card"}])
        uri = "https://data.scryfall.io/default-cards/default-cards-new.json"
        mock_response = MagicMock()
        mock_response.iter_content.return_value = [raw_json]
        fetcher.session.get.return_value = mock_response

        bulk_data = {BulkDataKey.DEFAULT_CARDS: {"download_uri": uri}}
        with patch.object(fetcher, "list_bulk_data", return_value=bulk_data):
            list(fetcher.stream_data_for_key(BulkDataKey.DEFAULT_CARDS))

        assert not stale.exists(), "stale file should have been deleted"
