"""Fetcher for Scryfall bulk data."""

import logging
import pathlib
import time
from enum import StrEnum

import orjson
import requests
import zstandard as zstd
from cachebox import TTLCache
from cachebox import cached as cachebox_cached

logger = logging.getLogger(__name__)
MINUTE = 60
MAX_FETCH_ATTEMPTS = 5


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
        # Scryfall's API requires a User-Agent and Accept header; requests without
        # them may be rejected. https://scryfall.com/docs/api
        self.session.headers.update(
            {
                "User-Agent": "arcane-tutor-bulk-fetcher/1.0",
                "Accept": "application/json",
            }
        )

    def _get_with_retries(self, url: str, *, timeout: int) -> requests.Response:
        """GET a URL, retrying with backoff and logging the response on failure.

        Args:
            url: The URL to fetch.
            timeout: Per-attempt request timeout in seconds.

        Returns:
            The successful (2xx) response.

        Raises:
            RuntimeError: If all attempts fail.
        """
        last_error: Exception | None = None
        for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
            try:
                response = self.session.get(url, timeout=timeout)
            except requests.RequestException as err:
                last_error = err
                logger.warning("GET %s failed (attempt %d/%d): %s", url, attempt, MAX_FETCH_ATTEMPTS, err)
            else:
                if response.ok:
                    return response
                last_error = requests.HTTPError(f"HTTP {response.status_code} for {url}", response=response)
                logger.warning(
                    "GET %s returned HTTP %d (attempt %d/%d), body[:500]=%r",
                    url,
                    response.status_code,
                    attempt,
                    MAX_FETCH_ATTEMPTS,
                    response.text[:500],
                )
            if attempt < MAX_FETCH_ATTEMPTS:
                time.sleep(min(2**attempt, 30))
        msg = f"Failed to GET {url} after {MAX_FETCH_ATTEMPTS} attempts"
        raise RuntimeError(msg) from last_error

    @cachebox_cached(cache=TTLCache(maxsize=2, global_ttl=5 * MINUTE))
    def list_bulk_data(self) -> dict[BulkDataKey, dict]:
        """Fetch bulk data from Scryfall."""
        response = self._get_with_retries("https://api.scryfall.com/bulk-data", timeout=20)
        payload = response.json()
        if "data" not in payload:
            logger.error("Scryfall bulk-data listing had no 'data' key, body[:500]=%r", response.text[:500])
            msg = "Scryfall bulk-data listing response missing 'data'"
            raise RuntimeError(msg)
        listing: dict[BulkDataKey, dict] = {}
        for record in payload["data"]:
            try:
                listing[BulkDataKey(record["type"])] = record
            except ValueError:
                # Scryfall occasionally adds new bulk data types (e.g. art_tags);
                # ignore ones we don't use rather than failing the whole listing.
                logger.info("Ignoring unrecognized Scryfall bulk data type %r", record.get("type"))
        return listing

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
        response = self._get_with_retries(download_uri, timeout=30)
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


def main() -> None:
    """Main function."""
    logging.basicConfig(level=logging.INFO)
    fetcher = ScryfallBulkDataFetcher()
    fetcher.get_data_for_key(BulkDataKey.DEFAULT_CARDS)
    fetcher.get_data_for_key(BulkDataKey.DEFAULT_CARDS)
    fetcher.get_data_for_key(BulkDataKey.DEFAULT_CARDS)


if __name__ == "__main__":
    main()
