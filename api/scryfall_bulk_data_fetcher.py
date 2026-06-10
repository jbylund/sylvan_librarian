"""Fetcher for Scryfall bulk data."""

import io
import logging
import pathlib
import time
from collections.abc import Iterator
from enum import StrEnum

import orjson
import requests
import zstandard as zstd
from cachebox import TTLCache
from cachebox import cached as cachebox_cached

logger = logging.getLogger(__name__)
MINUTE = 60


class BulkDataKey(StrEnum):
    """Key for Scryfall bulk data."""

    ALL_CARDS = "all_cards"
    DEFAULT_CARDS = "default_cards"
    ORACLE_CARDS = "oracle_cards"
    RULINGS = "rulings"
    UNIQUE_ARTWORK = "unique_artwork"


class ScryfallBulkDataFetcher:
    """Fetches bulk data from Scryfall."""

    def __init__(self) -> None:
        """Initialize the fetcher."""
        self.cache_directory = pathlib.Path("/data/api")
        if not self.cache_directory.exists():
            self.cache_directory = pathlib.Path("/tmp/api")  # noqa: S108
            self.cache_directory.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()

    @cachebox_cached(cache=TTLCache(maxsize=2, global_ttl=5 * MINUTE))
    def list_bulk_data(self) -> dict[BulkDataKey, dict]:
        """Fetch bulk data from Scryfall."""
        return {
            BulkDataKey(r["type"]): r for r in self.session.get("https://api.scryfall.com/bulk-data", timeout=20).json()["data"]
        }

    def get_download_uri_for_key(self, data_key: BulkDataKey) -> str:
        """Get the download URI for a given data key."""
        return self.list_bulk_data()[data_key]["download_uri"]

    def get_data_for_key(self, data_key: BulkDataKey) -> list[dict]:
        """Get the data for a given data key."""

        def _load_data(decompressed_data: bytes) -> list[dict]:
            """Load data from a bytes object."""
            before = time.monotonic()
            data = orjson.loads(decompressed_data)
            logger.info(
                "Parsed %d bytes to objects in %.3f seconds using orjson",
                len(decompressed_data),
                time.monotonic() - before,
            )
            return data

        download_uri = self.get_download_uri_for_key(data_key)
        suffix = download_uri.rpartition("/")[-1]
        cache_file_path = self.cache_directory / data_key / suffix
        cache_file_path = cache_file_path.with_suffix(".json.zstd")
        cache_file_path.parent.mkdir(parents=True, exist_ok=True)
        # if it exists, load and return
        try:
            with cache_file_path.open("rb") as f:
                before = time.monotonic()
                compressed_data = f.read()
                logger.info(
                    "Read %d bytes from %s in %.3f seconds",
                    len(compressed_data),
                    cache_file_path,
                    time.monotonic() - before,
                )

            # decompress
            before = time.monotonic()
            decompressed_data = zstd.decompress(compressed_data)
            logger.info(
                "Decompressed %d bytes from %s in %.3f seconds",
                len(decompressed_data),
                cache_file_path,
                time.monotonic() - before,
            )
            return _load_data(decompressed_data)
        except FileNotFoundError:
            pass

        # prune other files from the directory - they've been superseded
        for ifile in cache_file_path.parent.iterdir():
            ifile.unlink()

        # if it doesn't exist, download and cache
        before = time.monotonic()
        response = self.session.get(download_uri, timeout=30)
        response.raise_for_status()
        logger.info(
            "Downloaded %d bytes from %s in %.3f seconds",
            len(response.content),
            download_uri,
            time.monotonic() - before,
        )
        compressed_data = zstd.compress(data=response.content)
        with cache_file_path.open("wb") as f:
            f.write(compressed_data)

        return _load_data(response.content)

    def _cache_file_path_for_key(self, data_key: BulkDataKey) -> pathlib.Path:
        download_uri = self.get_download_uri_for_key(data_key)
        suffix = download_uri.rpartition("/")[-1]
        cache_file_path = self.cache_directory / data_key / suffix
        return cache_file_path.with_suffix(".json.zstd")

    def _ensure_cached(self, data_key: BulkDataKey, cache_file_path: pathlib.Path) -> None:
        """Download and cache the bulk data file if not already present."""
        cache_file_path.parent.mkdir(parents=True, exist_ok=True)
        if cache_file_path.exists():
            return

        for ifile in cache_file_path.parent.iterdir():
            if ifile.is_file():
                ifile.unlink()

        download_uri = self.get_download_uri_for_key(data_key)
        before = time.monotonic()
        tmp_path = cache_file_path.with_suffix(".zstd.tmp")
        cctx = zstd.ZstdCompressor(write_content_size=True)
        try:
            with self.session.get(download_uri, stream=True, timeout=60) as response:
                response.raise_for_status()
                with tmp_path.open("wb") as f, cctx.stream_writer(f) as compressor:
                    for chunk in response.iter_content(chunk_size=65536):
                        compressor.write(chunk)
            tmp_path.rename(cache_file_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        logger.info("Downloaded and cached %s in %.3f seconds", cache_file_path, time.monotonic() - before)

    def stream_data_for_key(self, data_key: BulkDataKey) -> Iterator[dict]:
        """Yield card dicts one at a time without loading the full file into memory.

        Reads the cached .json.zstd file line-by-line; downloads and caches first if needed.
        Each line that starts with '{' is treated as one card JSON object (trailing comma stripped).
        Unparseable lines are logged and skipped rather than raising.
        """
        cache_file_path = self._cache_file_path_for_key(data_key)
        self._ensure_cached(data_key, cache_file_path)

        before = time.monotonic()
        card_count = 0
        dctx = zstd.ZstdDecompressor()
        with (
            cache_file_path.open("rb") as f,
            dctx.stream_reader(f) as reader,
            io.TextIOWrapper(io.BufferedReader(reader), encoding="utf-8") as text_reader,
        ):
            for raw_line in text_reader:
                stripped = raw_line.strip().rstrip(",")
                if not stripped.startswith("{"):
                    continue
                try:
                    yield orjson.loads(stripped)
                    card_count += 1
                except orjson.JSONDecodeError:
                    logger.warning("Skipping unparseable line: %.120s", stripped)
        logger.info("Streamed %d cards from %s in %.3f seconds", card_count, cache_file_path, time.monotonic() - before)


def main() -> None:
    """Main function."""
    logging.basicConfig(level=logging.INFO)
    fetcher = ScryfallBulkDataFetcher()
    fetcher.get_data_for_key(BulkDataKey.DEFAULT_CARDS)
    fetcher.get_data_for_key(BulkDataKey.DEFAULT_CARDS)
    fetcher.get_data_for_key(BulkDataKey.DEFAULT_CARDS)


if __name__ == "__main__":
    main()
