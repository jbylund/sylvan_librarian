"""Script to copy card images to S3.

This script:
1. Fetches card data from the database (set_code, collector_number, image_location_uuid)
2. Downloads PNG images from Scryfall
3. Converts them to WebP at 4 different sizes (280, 388, 538, 745)
4. Uploads to S3 using a face-aware path structure:
   s3://biblioplex/img/{set_code}/{collector_number}/{face}/{width}.webp
   (for now, single-faced cards use face = "1")
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import botocore.session
import psycopg
import requests
from botocore.exceptions import ClientError

from api.utils.db_utils import configure_connection, get_pg_creds
from api.utils.http_utils import make_user_agent

logger = logging.getLogger(__name__)

# Image size configuration
ORIGINAL_WIDTH = 745  # this seems to be the size of the pngs that scryfall returns
# Four sizes uniformly spread between 280 and 745
XLARGE_WIDTH = 745  # Full resolution width in pixels
LARGE_WIDTH = 538  # Large resolution width in pixels
MEDIUM_WIDTH = 388  # Medium resolution width in pixels
SMALL_WIDTH = 280  # Small resolution width in pixels

# WebP quality setting
WEBP_QUALITY = 75

XLARGE_KEY = "745"
LARGE_KEY = "538"
MEDIUM_KEY = "388"
SMALL_KEY = "280"

ORIGINAL_KEY = "o"

# Face index for single-faced cards. Multi-face cards use "1" and "2" for their
# respective faces, which is the face_idx the card rows carry.
DEFAULT_FACE = "1"
# Every face index the S3 layout accepts.
VALID_FACES = (DEFAULT_FACE, "2")

# S3 image key layout: img/{set_code}/{collector_number}/{face}/{size}.webp
IMAGE_PREFIX = "img/"
# Parts after the img/ prefix: set code, collector number, face, size.webp
IMAGE_KEY_PART_COUNT = 4

# S3 caps ListObjectsV2 at 1000 keys per response regardless of MaxKeys, so a
# full image listing is hundreds of round trips whose responses botocore has to
# parse. That parsing is CPU-bound and holds the GIL, so the listing fans out
# across processes; threads measured no better than listing sequentially. Kept
# only modestly above core count because the sync host also serves API traffic.
S3_LIST_WORKERS = 16
# Prefixes handed to a listing worker per task.
S3_LIST_CHUNKSIZE = 2


class CardImage:
    """A card image."""

    def __init__(self, set_code: str, collector_number: str, face_idx: str, size: str, png_url: str | None = None) -> None:
        """Initialize a card image."""
        self.set_code = set_code
        self.collector_number = collector_number
        self.face_idx = face_idx
        self.size = size
        self.png_url = png_url
        if size not in [SMALL_KEY, MEDIUM_KEY, LARGE_KEY, XLARGE_KEY, ORIGINAL_KEY]:
            msg = f"Invalid size: {size}"
            raise ValueError(msg)
        if face_idx not in VALID_FACES:
            msg = f"Invalid face index: {face_idx}"
            raise ValueError(msg)

    def get_s3_key(self) -> str:
        """Get the S3 key for the card image."""
        return f"img/{self.set_code}/{self.collector_number}/{self.face_idx}/{self.size}.webp"

    def __hash__(self) -> int:
        """Hash the card image."""
        return hash((self.set_code, self.collector_number, self.face_idx, self.size))

    def __eq__(self, other: object) -> bool:
        """Check if the card image is equal to another object."""
        if not isinstance(other, CardImage):
            return False
        return (
            self.set_code == other.set_code
            and self.collector_number == other.collector_number
            and self.face_idx == other.face_idx
            and self.size == other.size
        )


def setup_logging(verbose: bool = False) -> None:
    """Set up logging configuration."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )


def get_database_connection() -> psycopg.Connection:
    """Get a connection to the PostgreSQL database."""
    creds = get_pg_creds()
    conninfo = " ".join(f"{k}={v}" for k, v in creds.items())
    conn = psycopg.connect(conninfo)
    configure_connection(conn)
    return conn


def fetch_cards_from_db(
    conn: psycopg.Connection,
    limit: int | None = None,
    set_code: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch card data from the database.

    Args:
        conn: Database connection
        limit: Maximum number of cards to fetch (None for all)
        set_code: Filter by specific set code (None for all sets)

    Returns:
        List of dictionaries containing
             card_set_code,
             collector_number,
             png_url,
             face_idx
    """
    with conn.cursor() as cursor:
        where_clause = ""
        params = []

        conditions = []

        if set_code:
            conditions.append("card_set_code = %s")
            params.append(set_code)

        if not conditions:
            conditions.append("TRUE")

        where_clause = " AND ".join(conditions)

        query = f"""
            SELECT
                card_set_code,
                collector_number,
                raw_card_blob->'image_uris'->>'png' as png_url,
                coalesce((raw_card_blob->>'face_idx')::int, 1) as face_idx
            FROM
                magic.cards
            WHERE
                {where_clause}
            ORDER BY
                card_set_code,
                collector_number_int NULLS LAST,
                collector_number
            LIMIT
                {json.dumps(limit)}
        """

        cursor.execute(query, params)
        cards = cursor.fetchall()

    logger.info("Fetched %d cards from database", len(cards))
    return cards


def download_image(url: str, output_path: Path) -> bool:
    """Download an image from a URL.

    Args:
        url: URL to download from
        output_path: Path to save the image

    Returns:
        True if successful, False otherwise
    """
    try:
        # Scryfall rejects HTTP-library default User-Agents with 400 generic_user_agent, and
        # rejects a request that sends no User-Agent at all.
        response = requests.get(url, timeout=30, stream=True, headers={"User-Agent": make_user_agent()})
        response.raise_for_status()

        with output_path.open("wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        logger.debug("Downloaded image to %s", output_path)
        return True
    except requests.RequestException as e:
        logger.error("Failed to download image from %s: %s", url, e)
        return False


def convert_to_webp(
    input_path: Path,
    output_path: Path,
    width: int,
    quality: int = WEBP_QUALITY,
) -> bool:
    """Convert an image to WebP format with resizing.

    Args:
        input_path: Path to input image (PNG or JPG)
        output_path: Path to save WebP output
        width: Target width in pixels (height auto-calculated)
        quality: WebP quality (0-100)

    Returns:
        True if successful, False otherwise
    """
    try:
        cmd = [
            "cwebp",
            str(input_path),
            "-resize",
            str(width),
            "0",
            "-m",
            "6",
            "-noalpha",
            "-q",
            str(quality),
            "-sharp_yuv",
            "-o",
            str(output_path),
        ]

        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )

        logger.debug("Converted image to WebP: %s (width=%dpx)", output_path, width)
        return True
    except subprocess.CalledProcessError as e:
        logger.error("Failed to convert image to WebP: %s", e.stderr)
        return False
    except FileNotFoundError:
        logger.error("cwebp command not found. Please install webp tools: apt-get install webp")
        return False
    except subprocess.TimeoutExpired:
        logger.error("Timeout converting image %s", input_path)
        return False


def upload_to_s3(
    s3_client: Any,  # noqa: ANN401
    local_path: Path,
    bucket: str,
    key: str,
) -> bool:
    """Upload a file to S3.

    Args:
        s3_client: Boto3 S3 client
        local_path: Path to local file
        bucket: S3 bucket name
        key: S3 object key

    Returns:
        True if successful or skipped, False otherwise
    """
    cache_duration = datetime.timedelta(days=105)
    duration_seconds = int(cache_duration.total_seconds())
    try:
        s3_client.upload_file(
            str(local_path),
            bucket,
            key,
            ExtraArgs={
                "CacheControl": f"public, max-age={duration_seconds}, immutable",
                "ContentType": "image/webp",
            },
        )
        logger.debug("Uploaded to S3: s3://%s/%s", bucket, key)
        return True
    except ClientError as e:
        logger.error("Failed to upload to S3 %s/%s: %s", bucket, key, e)
        return False


def process_card(
    card: dict[str, Any],
    s3_client: Any,  # noqa: ANN401
    bucket: str,
    dry_run: bool = False,
) -> dict[str, bool]:
    """Process a single card: download, convert, and upload.

    Args:
        card: Card data dict with card_set_code, collector_number, face_idx, png_url
        s3_client: Boto3 S3 client
        bucket: S3 bucket name
        dry_run: If True, skip actual downloads and uploads

    Returns:
        Dict with success status for each size (280, 388, 538, 745)
    """
    set_code = card["card_set_code"]
    collector_number = card["collector_number"]
    face_idx = card["face_idx"]
    png_url = card["png_url"]

    if not set_code or not collector_number or not png_url:
        logger.warning("Skipping card with missing data: %s", card)
        return {SMALL_KEY: False, MEDIUM_KEY: False, LARGE_KEY: False, XLARGE_KEY: False}

    if face_idx not in VALID_FACES:
        # Writing an unknown face would land at a key get_s3_cards cannot parse back, so the
        # image would be re-uploaded on every subsequent run.
        logger.warning("Skipping card with unexpected face index %r: %s", face_idx, card)
        return {SMALL_KEY: False, MEDIUM_KEY: False, LARGE_KEY: False, XLARGE_KEY: False}

    logger.info("Processing %s/%s face %s", set_code, collector_number, face_idx)

    if dry_run:
        logger.info("[DRY RUN] Would process %s/%s face %s", set_code, collector_number, face_idx)
        return {SMALL_KEY: True, MEDIUM_KEY: True, LARGE_KEY: True, XLARGE_KEY: True}

    results = {SMALL_KEY: False, MEDIUM_KEY: False, LARGE_KEY: False, XLARGE_KEY: False}

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        # Download PNG image from Scryfall
        png_path = temp_path / "original.png"

        if not download_image(png_url, png_path):
            return results

        sizes = {
            SMALL_KEY: SMALL_WIDTH,
            MEDIUM_KEY: MEDIUM_WIDTH,
            LARGE_KEY: LARGE_WIDTH,
            XLARGE_KEY: XLARGE_WIDTH,
        }

        # Convert and upload each size
        for size_name, width in sizes.items():
            webp_path = temp_path / f"{size_name}.webp"

            if not convert_to_webp(png_path, webp_path, width):
                continue

            # Face-aware key structure: img/{set_code}/{collector_number}/{face}/{size}.webp
            s3_key = f"img/{set_code}/{collector_number}/{face_idx}/{size_name}.webp"

            if upload_to_s3(s3_client, webp_path, bucket, s3_key):
                results[size_name] = True

    success_count = sum(results.values())
    logger.info("Completed %s/%s face %s: %d/4 sizes uploaded", set_code, collector_number, face_idx, success_count)

    return results


class CardProcessorPool:
    """Multiprocessing worker pool for processing cards.

    Each worker process gets its own S3 client initialized once via init_worker.
    This avoids creating a new boto3 client for every card while keeping
    the S3 client namespaced within a class instead of as a global variable.
    """

    s3_client = None

    @classmethod
    def init_worker(cls) -> None:
        """Initialize worker process with S3 client.

        This runs once per worker process when the pool is created.
        Sets cls.s3_client which is separate per worker process.
        """
        cls.s3_client = boto3.client("s3")

    @classmethod
    def process_card_worker(cls, job_task: dict[str, Any]) -> dict[str, bool]:
        """Worker function for parallel processing of cards.

        Uses the class-level S3 client initialized once per worker process.

        Args:
            job_task: Dict of job task

        Returns:
            Dict with success status for each size (280, 388, 538, 745)
        """
        bucket = job_task.pop("bucket")
        dry_run = job_task.pop("dry_run")
        return process_card(job_task, cls.s3_client, bucket, dry_run)


@dataclass
class Args:
    """Command-line arguments for the image copy script.

    Attributes:
        bucket: S3 bucket name to upload images to
        set_code: Optional set code to filter cards by
        limit: Optional limit on number of cards to process
        skip_existing: Whether to skip cards that already have images in S3
        dry_run: If True, simulate the process without actual downloads/uploads
        verbose: Enable verbose logging output
        workers: Number of parallel worker processes for image processing
    """

    bucket: str = "biblioplex"
    set_code: str | None = None
    limit: int | None = None
    skip_existing: bool = True
    dry_run: bool = False
    verbose: bool = False
    workers: int = 8


def get_args() -> Args:
    """Parse command-line arguments and return Args dataclass.

    Returns:
        Args object containing parsed command-line arguments
    """

    def lowerstr(s: str) -> str:
        return s.lower()

    parser = argparse.ArgumentParser(
        description="Copy card images to S3 with WebP conversion",
    )
    parser.add_argument(
        "--bucket",
        default="biblioplex",
        help="S3 bucket name (default: biblioplex)",
    )
    parser.add_argument(
        "--set",
        dest="set_code",
        help="Process only cards from a specific set (e.g., 'iko')",
        type=lowerstr,
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limit number of cards to process",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        default=True,
        help="Skip cards that already have all images in S3 (default: True)",
    )
    parser.add_argument(
        "--no-skip-existing",
        action="store_false",
        dest="skip_existing",
        help="Re-process cards even if images exist in S3",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry run mode - don't actually download or upload",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Number of parallel worker processes (default: 8)",
    )
    return Args(**vars(parser.parse_args()))


def configure_env() -> None:
    """Load environment variables from env.json file."""
    with Path("env.json").open("r") as f:
        env = json.load(f)
    os.environ.update(env)


def check_cwebp() -> None:
    """Check if cwebp command is available and exit if not found."""
    try:
        subprocess.run(["cwebp", "-version"], capture_output=True, check=True, timeout=5)
    except (subprocess.CalledProcessError, FileNotFoundError):
        logger.error(
            "cwebp not found. Please install webp tools:\n  Ubuntu/Debian: sudo apt-get install webp\n  macOS: brew install webp",
        )
        sys.exit(1)


def get_db_cards(args: Args) -> set[tuple[str, str, str, str]]:
    """Get all cards in the database."""
    logger.info("Connecting to database...")
    conn = get_database_connection()

    # Fetch cards
    logger.info("Fetching cards from database (set=%s, limit=%s)...", args.set_code, args.limit)
    db_cards = fetch_cards_from_db(conn, limit=args.limit, set_code=args.set_code)
    conn.close()

    if not db_cards:
        logger.warning("No cards found to process")
        return None

    sizes = [SMALL_KEY, MEDIUM_KEY, LARGE_KEY, XLARGE_KEY]
    logger.info("Found %d cards in database, should create %d images", len(db_cards), len(db_cards) * len(sizes))
    return {
        CardImage(
            set_code=card["card_set_code"],
            collector_number=card["collector_number"],
            face_idx=str(card["face_idx"]),
            png_url=card["png_url"],
            size=size,
        )
        for card in db_cards
        for size in sizes
    }


def make_listing_session() -> botocore.session.Session:
    """Build a botocore session that leaves S3 timestamps unparsed.

    botocore runs every LastModified value through dateutil's generic text
    parser, which costs roughly 190 microseconds per key and accounts for about
    two thirds of the CPU spent listing a few hundred thousand images. Listing
    only needs object keys, so the timestamp is left as the string S3 sent.

    This is scoped to listing rather than applied globally so that upload
    clients keep parsing timestamps into datetime objects as usual.

    Returns:
        A botocore session whose response parsers skip timestamp parsing
    """
    session = botocore.session.get_session()
    session.get_component("response_parser_factory").set_parser_defaults(
        timestamp_parser=lambda value: value,
    )
    return session


def make_listing_client() -> Any:  # noqa: ANN401
    """Build an S3 client tuned for large listings.

    Returns:
        Boto3 S3 client that does not parse response timestamps
    """
    return boto3.Session(botocore_session=make_listing_session()).client("s3")


def parse_image_key(key: str) -> tuple[str, str, str, str] | None:
    """Parse an S3 image key into its component parts.

    Args:
        key: S3 object key, expected as img/{set}/{collector}/{face}/{size}.webp

    Returns:
        The (set_code, collector_number, face_idx, size) parts, or None if the
        key is not a WebP image in that layout
    """
    if not key.endswith(".webp"):
        return None

    # Discard the img/ prefix
    _img, _slash, obj_key = key.partition("/")
    parts = obj_key.split("/")
    if len(parts) != IMAGE_KEY_PART_COUNT:
        return None

    set_code, collector_number, face_idx, size_webp = parts
    return set_code, collector_number, face_idx, size_webp.partition(".")[0]


def list_prefix_keys(s3_client: Any, bucket: str, prefix: str) -> set[tuple[str, str, str, str]]:  # noqa: ANN401
    """List every image key under one prefix.

    Args:
        s3_client: Boto3 S3 client
        bucket: S3 bucket name
        prefix: Key prefix to page through

    Returns:
        Set of (set_code, collector_number, face_idx, size) tuples
    """
    found = set()
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            parsed = parse_image_key(obj["Key"])
            if parsed is not None:
                found.add(parsed)
    return found


def list_image_prefixes(s3_client: Any, bucket: str, set_code: str | None) -> list[str]:  # noqa: ANN401
    """Enumerate the per-set prefixes that the listing fans out over.

    Args:
        s3_client: Boto3 S3 client
        bucket: S3 bucket name
        set_code: Restrict to a single set, which skips prefix discovery

    Returns:
        List of key prefixes, one per set
    """
    if set_code:
        return [f"{IMAGE_PREFIX}{set_code}/"]

    prefixes = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=IMAGE_PREFIX, Delimiter="/"):
        prefixes.extend(common["Prefix"] for common in page.get("CommonPrefixes", []))
    return prefixes


class S3ImageLister:
    """Multiprocessing worker pool for listing images already in S3.

    Mirrors CardProcessorPool: each worker builds one client in the initializer
    rather than per task. Workers return plain tuples instead of CardImage
    instances, because pickling several hundred thousand objects back to the
    parent would cost more than the listing this parallelizes saves.
    """

    s3_client = None

    @classmethod
    def init_worker(cls) -> None:
        """Initialize worker process with a listing-tuned S3 client.

        This runs once per worker process when the pool is created.
        Sets cls.s3_client which is separate per worker process.
        """
        cls.s3_client = make_listing_client()

    @classmethod
    def list_prefix_worker(cls, job_task: tuple[str, str]) -> set[tuple[str, str, str, str]]:
        """Worker function for parallel listing of a single prefix.

        Args:
            job_task: The (bucket, prefix) pair to list

        Returns:
            Set of (set_code, collector_number, face_idx, size) tuples
        """
        bucket, prefix = job_task
        return list_prefix_keys(cls.s3_client, bucket, prefix)


def get_s3_cards(args: Args) -> set[CardImage]:
    """Get all cards in S3.

    The listing is fanned out one task per set prefix. A single sequential
    listing spends most of its time parsing responses rather than waiting on
    the network, so splitting it across processes is what actually helps.

    Args:
        args: Command-line arguments

    Returns:
        Set of CardImage objects already present in S3
    """
    s3_cards = set()
    if not args.skip_existing:
        # no point in populating if we're not going to use it
        return s3_cards

    s3_client = make_listing_client()
    prefixes = list_image_prefixes(s3_client, args.bucket, args.set_code)

    if len(prefixes) <= 1:
        # One prefix cannot be split across workers, so skip the pool entirely.
        key_parts = list_prefix_keys(s3_client, args.bucket, prefixes[0]) if prefixes else set()
    else:
        logger.info("Listing %d image prefixes using %d worker processes", len(prefixes), S3_LIST_WORKERS)
        key_parts = set()
        job_tasks = [(args.bucket, prefix) for prefix in prefixes]
        pool = multiprocessing.Pool(processes=S3_LIST_WORKERS, initializer=S3ImageLister.init_worker)
        try:
            for found in pool.imap_unordered(
                func=S3ImageLister.list_prefix_worker,
                iterable=job_tasks,
                chunksize=S3_LIST_CHUNKSIZE,
            ):
                key_parts |= found
        finally:
            # Properly clean up the pool to avoid weakref finalize errors
            pool.close()  # Prevent new tasks from being submitted
            pool.join()  # Wait for all worker processes to finish

    for set_code, collector_number, face_idx, size in key_parts:
        try:
            s3_cards.add(
                CardImage(
                    set_code=set_code,
                    collector_number=collector_number,
                    face_idx=face_idx,
                    size=size,
                )
            )
        except ValueError:
            continue

    distinct_s3_cards = {(c.set_code, c.collector_number) for c in s3_cards}
    logger.info("Found %d image objects in S3, belonging to %d distinct cards", len(s3_cards), len(distinct_s3_cards))
    return s3_cards


def main() -> None:
    """Main entry point for the script."""
    args = get_args()
    setup_logging(args.verbose)

    if args.dry_run:
        logger.info("Running in DRY RUN mode - no actual downloads or uploads")

    check_cwebp()
    configure_env()

    db_cards = get_db_cards(args)
    s3_cards = get_s3_cards(args)

    missing_cards = db_cards - s3_cards

    # group by set_code and collector_number
    by_set_code_and_collector_number = {}

    for card in missing_cards:
        set_code = card.set_code
        collector_number = card.collector_number
        face_idx = card.face_idx
        by_set_code_and_collector_number.setdefault((set_code, collector_number, face_idx), []).append(card)

    cards_with_missing_images = []
    for (set_code, collector_number, face_idx), cards in by_set_code_and_collector_number.items():
        missing_sizes = [c.size for c in cards]
        missing_info = {
            "bucket": args.bucket,
            "card_set_code": set_code,
            "collector_number": collector_number,
            "dry_run": args.dry_run,
            "face_idx": face_idx,
            "png_url": cards[0].png_url,
            "sizes": missing_sizes,
        }
        cards_with_missing_images.append(missing_info)
    logger.info("Found %d cards with missing images", len(cards_with_missing_images))

    # Process cards in parallel
    logger.info("Processing cards using %d worker processes", args.workers)

    successful_cards = failed_cards = 0
    start_time = time.monotonic()

    pool = multiprocessing.Pool(processes=args.workers, initializer=CardProcessorPool.init_worker)
    try:
        # Use imap_unordered for better progress tracking
        for idx, results in enumerate(
            pool.imap_unordered(
                func=CardProcessorPool.process_card_worker,
                iterable=cards_with_missing_images,
            ),
            1,
        ):
            if (idx and idx % 10 == 0) or idx == len(cards_with_missing_images):
                elapsed_time = time.monotonic() - start_time
                fraction_complete = idx / len(cards_with_missing_images)
                estimated_time_remaining = (elapsed_time / fraction_complete) - elapsed_time
                estimated_remaining_duration = datetime.timedelta(seconds=round(estimated_time_remaining, 1))
                logger.info(
                    "Progress: %d / %d cards processed (ETA: %s)",
                    idx,
                    len(cards_with_missing_images),
                    estimated_remaining_duration,
                )

            if all(results.values()):
                successful_cards += 1
            else:
                failed_cards += 1
    finally:
        # Properly clean up the pool to avoid weakref finalize errors
        pool.close()  # Prevent new tasks from being submitted
        pool.join()  # Wait for all worker processes to finish

    logger.info(
        "Processing complete: %d successful, %d failed out of %d total",
        successful_cards,
        failed_cards,
        len(cards_with_missing_images),
    )


if __name__ == "__main__":
    main()
