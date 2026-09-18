"""Fetcher for Scryfall bulk data."""

import io
import itertools
import logging
import os
import pathlib
import secrets
import time
import zlib
from collections.abc import Iterable, Iterator
from enum import StrEnum

import orjson
import requests
import zstandard as zstd
from cachebox import TTLCache
from cachebox import cached as cachebox_cached
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from api.utils.http_utils import make_user_agent

logger = logging.getLogger(__name__)
MINUTE = 60

# A healthy dump is one card object per line, so nearly every byte belongs to a successfully
# parsed card. Files at least this large must reach the coverage threshold or streaming raises.
_PARSE_COVERAGE_MIN_CHARS = 1_000_000
_PARSE_COVERAGE_THRESHOLD = 0.8

# .tmp files younger than this may be another worker's in-flight download; only prune older ones.
_TMP_PRUNE_AGE_SECONDS = 15 * MINUTE

# Log at most this many individual unparseable lines; beyond that only the final summary reports them.
_MAX_UNPARSEABLE_LINE_LOGS = 5

# Field on each /bulk-data record holding the dump URL. Scryfall served plain-JSON `download_uri`
# until it moved the dumps to gzipped JSONL under this name; the old field no longer exists.
_DOWNLOAD_URI_FIELD = "jsonl_download_uri"

# Scryfall serves the dumps as `.jsonl.gz` with Content-Type: application/gzip and *no*
# Content-Encoding header, so requests hands back raw gzip bytes rather than decompressing.
_GZIP_MAGIC = b"\x1f\x8b"
# zlib needs this window-bits offset to parse a gzip container rather than a raw deflate stream.
_GZIP_WBITS = 16 + zlib.MAX_WBITS


class BulkDataParseError(Exception):
    """Raised when a bulk data file yields implausibly little card data for its size."""


class BulkDataFormatError(Exception):
    """Raised when Scryfall's /bulk-data response does not have the shape we expect."""


def _gunzip_if_needed(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Decompress a byte stream if it is gzipped, else pass it through unchanged.

    Sniffs the gzip magic bytes rather than trusting the URL suffix or headers, so this keeps
    working whether Scryfall serves gzip as the payload (today) or as a Content-Encoding that
    requests transparently decompresses for us.

    Args:
        chunks: Byte chunks as returned by requests' iter_content.

    Yields:
        Decompressed bytes, not aligned to any particular boundary.

    Raises:
        BulkDataFormatError: If a gzip stream ends mid-member, i.e. the download was truncated.
    """
    nonempty = (chunk for chunk in chunks if chunk)  # iter_content emits empty keep-alive chunks
    first = next(nonempty, b"")
    if not first:
        return
    if not first.startswith(_GZIP_MAGIC):
        yield first
        yield from nonempty
        return

    decompressor = zlib.decompressobj(_GZIP_WBITS)
    for chunk in itertools.chain([first], nonempty):
        pending = chunk
        while pending:
            if out := decompressor.decompress(pending):
                yield out
            if not decompressor.eof:
                break
            pending = decompressor.unused_data
            if pending:
                # Concatenated members are one logical stream; decode the next one too, or a
                # multi-member dump would silently truncate to its first member.
                decompressor = zlib.decompressobj(_GZIP_WBITS)
    if not decompressor.eof:
        msg = "Bulk data gzip stream ended mid-member; the download was truncated"
        raise BulkDataFormatError(msg)


class BulkDataKey(StrEnum):
    """Key for Scryfall bulk data."""

    ALL_CARDS = "all_cards"
    ART_TAGS = "art_tags"
    DEFAULT_CARDS = "default_cards"
    ORACLE_CARDS = "oracle_cards"
    ORACLE_TAGS = "oracle_tags"
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
        # Scryfall rejects default HTTP-library User-Agents (400 generic_user_agent),
        # and asks for an explicit Accept header.
        self.session.headers.update({"User-Agent": make_user_agent(), "Accept": "application/json"})
        # Retry connection errors and retryable statuses with backoff at the
        # transport layer. raise_on_status=False returns the last response after
        # retries are exhausted so _get() can log its body before raising.
        retry = Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _get(self, url: str, *, timeout: int, **kwargs: object) -> requests.Response:
        """GET a URL via the retrying session, logging the response body on HTTP errors.

        Args:
            url: The URL to fetch.
            timeout: Per-attempt request timeout in seconds.
            **kwargs: Extra arguments passed through to session.get (e.g. stream=True).

        Returns:
            The successful (2xx) response.

        Raises:
            requests.HTTPError: If the final response after retries is not 2xx.
            requests.RequestException: If the request fails at the transport level.
        """
        response = self.session.get(url, timeout=timeout, **kwargs)
        if not response.ok:
            logger.error("GET %s returned HTTP %d, body[:500]=%r", url, response.status_code, response.text[:500])
        response.raise_for_status()
        return response

    def fetch_api_json(self, path: str, *, timeout: int = 30) -> dict:
        """GET one Scryfall API endpoint and decode it.

        The reference data — sets, catalogs, symbology — is published as ordinary API responses
        rather than as bulk dumps, and is small enough to fetch whole. It goes through this session
        so it inherits the same retry policy and error logging the dumps get, rather than opening a
        second HTTP client with its own behaviour.

        Args:
            path: Path below the API root, with no leading slash (e.g. "catalog/creature-types").
            timeout: Per-attempt request timeout in seconds.

        Returns:
            The decoded response body.

        Raises:
            requests.HTTPError: If the final response after retries is not 2xx.
            requests.RequestException: If the request fails at the transport level.
        """
        return self._get(f"https://api.scryfall.com/{path}", timeout=timeout).json()

    @cachebox_cached(cache=TTLCache(maxsize=2, global_ttl=5 * MINUTE))
    def list_bulk_data(self) -> dict[BulkDataKey, dict]:
        """Fetch bulk data from Scryfall, ignoring bulk data types we don't recognize."""
        response = self._get("https://api.scryfall.com/bulk-data", timeout=20)
        known_types = {key.value for key in BulkDataKey}
        records = response.json()["data"]
        unknown_types = sorted({r["type"] for r in records} - known_types)
        if unknown_types:
            logger.info("Ignoring unrecognized bulk data types: %s", unknown_types)
        return {BulkDataKey(r["type"]): r for r in records if r["type"] in known_types}

    def get_download_uri_for_key(self, data_key: BulkDataKey) -> str:
        """Get the download URI for a given data key.

        Args:
            data_key: Which bulk data dump to look up.

        Returns:
            The URL of the dump file.

        Raises:
            BulkDataFormatError: If the record lacks the download URI field, which means Scryfall
                renamed or removed it again.
        """
        record = self.list_bulk_data()[data_key]
        if _DOWNLOAD_URI_FIELD not in record:
            msg = (
                f"Scryfall bulk data record for {data_key} has no {_DOWNLOAD_URI_FIELD!r} field "
                f"(present fields: {sorted(record)}); the /bulk-data schema has changed"
            )
            raise BulkDataFormatError(msg)
        return record[_DOWNLOAD_URI_FIELD]

    def _cache_file_path_for_key(self, data_key: BulkDataKey) -> pathlib.Path:
        download_uri = self.get_download_uri_for_key(data_key)
        remote_name = download_uri.rpartition("/")[-1]
        # We store decompressed-then-zstd-recompressed content, so drop any upstream .gz suffix.
        return self.cache_directory / data_key / f"{remote_name.removesuffix('.gz')}.zstd"

    def _ensure_cached(self, data_key: BulkDataKey, cache_file_path: pathlib.Path) -> None:
        """Download and cache the bulk data file if not already present.

        Safe to call concurrently from multiple worker processes: each writes to its own
        uniquely-named .tmp file and atomically renames it into place, so racers waste a
        duplicate download but never corrupt the cache file.
        """
        cache_file_path.parent.mkdir(parents=True, exist_ok=True)
        if cache_file_path.exists():
            return

        for ifile in cache_file_path.parent.iterdir():
            try:
                if not ifile.is_file():
                    continue
                if ifile.suffix == ".tmp" and time.time() - ifile.stat().st_mtime < _TMP_PRUNE_AGE_SECONDS:
                    continue  # likely another worker's in-flight download
                ifile.unlink(missing_ok=True)
            except FileNotFoundError:
                continue  # another worker pruned it between iterdir() and stat()

        download_uri = self.get_download_uri_for_key(data_key)
        before = time.monotonic()
        tmp_path = cache_file_path.with_name(f"{cache_file_path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        cctx = zstd.ZstdCompressor(write_content_size=True)
        try:
            with self._get(download_uri, stream=True, timeout=60) as response:
                with tmp_path.open("wb") as f, cctx.stream_writer(f) as compressor:
                    for chunk in _gunzip_if_needed(response.iter_content(chunk_size=65536)):
                        compressor.write(chunk)
            tmp_path.rename(cache_file_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        logger.info("Downloaded and cached %s in %.3f seconds", cache_file_path, time.monotonic() - before)

    def stream_data_for_key(self, data_key: BulkDataKey) -> Iterator[dict]:
        """Yield card dicts one at a time without loading the full file into memory.

        Reads the cached .zstd file line-by-line; downloads and caches first if needed. The dumps
        are JSONL, so every non-blank line must be one JSON object. Lines that are not — malformed
        JSON, or valid JSON of some other type such as a whole array on one line — are logged and
        skipped. But if a non-trivially-sized file ends up with less than _PARSE_COVERAGE_THRESHOLD
        of its content parsed into cards (e.g. the dump is no longer one-object-per-line),
        BulkDataParseError is raised instead of silently under-yielding.

        Note: the coverage check runs after the last line is read, so it only fires for consumers
        that iterate to exhaustion. A consumer that stops early (break, islice, etc.) gets no
        integrity guarantee about the portion it consumed.
        """
        cache_file_path = self._cache_file_path_for_key(data_key)
        self._ensure_cached(data_key, cache_file_path)

        before = time.monotonic()
        card_count = 0
        total_chars = 0
        parsed_chars = 0
        skipped_lines = 0
        dctx = zstd.ZstdDecompressor()
        try:
            with (
                cache_file_path.open("rb") as f,
                dctx.stream_reader(f) as reader,
                io.TextIOWrapper(io.BufferedReader(reader), encoding="utf-8") as text_reader,
            ):
                for raw_line in text_reader:
                    total_chars += len(raw_line)
                    stripped = raw_line.strip()
                    if not stripped:
                        continue
                    try:
                        card = orjson.loads(stripped)
                    except orjson.JSONDecodeError:
                        card = None
                    # A non-dict line means the layout is not JSONL; skipping keeps the yielded
                    # type honest and lets the coverage check below turn it into a hard failure.
                    if not isinstance(card, dict):
                        skipped_lines += 1
                        if skipped_lines <= _MAX_UNPARSEABLE_LINE_LOGS:
                            logger.warning("Skipping unparseable line: %.120s", stripped)
                        continue
                    card_count += 1
                    parsed_chars += len(raw_line)
                    yield card
        except (zstd.ZstdError, UnicodeDecodeError):
            # ZstdError: not a valid zstd stream. UnicodeDecodeError: valid zstd whose
            # decompressed bytes are not valid UTF-8 (e.g. truncated mid-character).
            logger.exception("Corrupt cache file %s; deleting it so the next call re-downloads", cache_file_path)
            cache_file_path.unlink(missing_ok=True)
            raise
        if skipped_lines:
            logger.warning("Skipped %d unparseable lines total in %s", skipped_lines, cache_file_path)
        if total_chars >= _PARSE_COVERAGE_MIN_CHARS and parsed_chars < _PARSE_COVERAGE_THRESHOLD * total_chars:
            msg = (
                f"Parsed only {card_count} cards covering {parsed_chars} of {total_chars} characters "
                f"in {cache_file_path}; the bulk data file format may have changed"
            )
            raise BulkDataParseError(msg)
        logger.info("Streamed %d cards from %s in %.3f seconds", card_count, cache_file_path, time.monotonic() - before)


def main() -> None:
    """Stream and count cards from the default bulk data dump."""
    logging.basicConfig(level=logging.INFO)
    fetcher = ScryfallBulkDataFetcher()
    count = sum(1 for _ in fetcher.stream_data_for_key(BulkDataKey.DEFAULT_CARDS))
    logger.info("Total cards: %d", count)


if __name__ == "__main__":
    main()
