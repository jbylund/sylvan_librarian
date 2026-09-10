"""Command-line interface for Gatherer import functionality."""

import argparse
import json
import logging
import sys
from pathlib import Path

from .fetch_gatherer_data import GathererFetcher

logger = logging.getLogger(__name__)


def fetch_all(fetcher: GathererFetcher, output_dir: str) -> int:
    """Fetch every set, one file each; keep going past a failed set and report them all at the end.

    Returns:
        0 if every set was written, 1 if any failed.
    """
    set_codes = fetcher.fetch_all_sets()
    failed = []
    for idx, set_code in enumerate(set_codes, 1):
        try:
            output_file = fetcher.save_set_to_json(set_code, output_dir)
        except Exception:
            logger.exception("[%d/%d] %s failed", idx, len(set_codes), set_code)
            failed.append(set_code)
            continue
        logger.info("[%d/%d] %s -> %s", idx, len(set_codes), set_code, output_file)

    logger.info("Fetched %d of %d sets", len(set_codes) - len(failed), len(set_codes))
    if failed:
        logger.error("Failed sets: %s", ", ".join(failed))
        return 1
    return 0


def main() -> int:
    """Run the Gatherer import command-line interface."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(
        description="Import Magic: The Gathering card data from Gatherer",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # list-sets command
    list_parser = subparsers.add_parser("list-sets", help="List all available sets")
    list_parser.add_argument("--output", "-o", help="Output JSON file path")

    # fetch-set command
    fetch_parser = subparsers.add_parser("fetch-set", help="Fetch a specific set")
    fetch_parser.add_argument("set_code", help="Set code (e.g., TDM)")
    fetch_parser.add_argument("--output", "-o", help="Output directory (default: gatherer_data)", default="gatherer_data")

    # fetch-all command
    fetch_all_parser = subparsers.add_parser("fetch-all", help="Fetch all sets")
    fetch_all_parser.add_argument("--output", "-o", help="Output directory (default: gatherer_data)", default="gatherer_data")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    fetcher = GathererFetcher()

    if args.command == "list-sets":
        sets = fetcher.fetch_all_sets()

        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with output_path.open("w", encoding="utf-8") as f:
                json.dump(sets, f, indent=2, ensure_ascii=False)
        else:
            pass

    elif args.command == "fetch-set":
        fetcher.save_set_to_json(args.set_code, args.output)

    elif args.command == "fetch-all":
        # fetch_all_sets() returns set-code strings; the previous loop looked for dicts and so
        # skipped every one of them, exiting 0 having fetched nothing.
        return fetch_all(fetcher, args.output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
