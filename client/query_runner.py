#!/usr/bin/env python3
"""Client script to generate random queries and run them against the API.

This script continuously generates random card search queries and executes them
against the Scryfall OS API to help identify which database indexes are being used
and which queries perform well or poorly.
"""

import logging
import os
import random
import time

import requests

logger = logging.getLogger(__name__)
# Constants
DEFAULT_API_URL = "http://apiservice:8080"
DEFAULT_QUERY_DELAY = 1.0  # Delay between queries in seconds
DEFAULT_BATCH_SIZE = 50  # Number of queries before reporting stats


def setup_logging() -> None:
    """Set up logging configuration."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )


def _generate_basic_queries() -> list[str]:
    """Generate basic search queries.

    Returns:
        List of basic query strings.
    """
    queries = []
    colors = ["w", "u", "b", "r", "g"]

    # Color queries
    for color in colors:
        queries.append(f"color:{color}")
        queries.append(f"c:{color}")
        queries.append(f"id:{color}")

    # Multicolor queries
    queries.extend(["color:wu", "color:ub", "color:br", "color:rg", "color:gw"])

    # CMC queries
    for cmc in range(10):
        queries.append(f"cmc={cmc}")
        queries.append(f"mv={cmc}")

    queries.extend(["cmc<3", "cmc>5", "cmc>=4", "cmc<=2"])

    return queries


def _generate_type_queries() -> list[str]:
    """Generate type and rarity queries.

    Returns:
        List of type query strings.
    """
    queries = []

    # Type queries
    types = ["creature", "instant", "sorcery", "enchantment", "artifact", "planeswalker", "land"]
    for card_type in types:
        queries.append(f"type:{card_type}")
        queries.append(f"t:{card_type}")

    # Rarity queries
    rarities = ["common", "uncommon", "rare", "mythic"]
    for rarity in rarities:
        queries.append(f"rarity:{rarity}")
        queries.append(f"r:{rarity}")

    # Power/Toughness queries
    for power in range(1, 6):
        queries.append(f"pow={power}")
        queries.append(f"tou={power}")

    return queries


def _generate_combined_queries() -> list[str]:
    """Generate combined queries.

    Returns:
        List of combined query strings.
    """
    queries = []
    colors = ["w", "u", "b", "r", "g"]

    # Combined queries (color + type)
    for color in colors:
        for card_type in ["creature", "instant", "sorcery"]:
            queries.append(f"color:{color} type:{card_type}")

    # Combined queries (color + cmc)
    for color in colors:
        for cmc in range(5):
            queries.append(f"color:{color} cmc={cmc}")

    return queries


def _generate_text_queries() -> list[str]:
    """Generate text and keyword queries.

    Returns:
        List of text query strings.
    """
    queries = []

    # Keyword queries
    keywords = ["flying", "haste", "trample", "deathtouch", "lifelink", "vigilance"]
    for keyword in keywords:
        queries.append(f"oracle:{keyword}")

    # Text search queries
    common_words = ["draw", "counter", "destroy", "exile", "return", "token"]
    for word in common_words:
        queries.append(f"oracle:{word}")

    # Set queries (using common set codes)
    sets = ["m21", "znr", "khm", "stx", "afr", "mid", "neo", "snc", "dmu", "bro"]
    for set_code in sets:
        queries.append(f"set:{set_code}")

    # Format queries
    formats = ["standard", "modern", "commander", "legacy", "vintage", "pioneer"]
    for format_name in formats:
        queries.append(f"format:{format_name}")

    return queries


def generate_random_queries() -> list[str]:
    """Generate a diverse set of random search queries.

    Returns:
        List of random search query strings.
    """
    queries = []
    queries.extend(_generate_basic_queries())
    queries.extend(_generate_type_queries())
    queries.extend(_generate_combined_queries())
    queries.extend(_generate_text_queries())
    return queries


def run_query(api_url: str, query: str, session: requests.Session) -> dict:
    """Run a single search query against the API.

    Args:
        api_url: Base URL for the API.
        query: The search query string.
        session: Requests session for API calls.

    Returns:
        Dictionary with query results and timing information.
    """
    before = time.monotonic()
    result = {
        "query": query,
    }

    try:
        response = session.get(
            f"{api_url}/search",
            params={"q": query, "limit": 100},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        result["success"] = True
        card_count = len(data.get("cards", []))
        result["card_count"] = card_count
    except requests.RequestException as oops:
        result["success"] = False
        result["error"] = str(oops)
    finally:
        elapsed = time.monotonic() - before
        elapsed_ms = 1000 * elapsed
        result["elapsed_ms"] = elapsed_ms

    if result["success"]:
        logging.info(
            "Query: '%s' | Duration: %.1fms | Cards: %d",
            query,
            elapsed_ms,
            card_count,
        )
    else:
        logging.error(
            "Query failed: '%s' | Duration: %.1fms | Error: %s",
            query,
            elapsed_ms,
            result["error"],
        )

    return result


def print_statistics(results: list[dict]) -> None:
    """Print statistics about the query results.

    Args:
        results: List of query result dictionaries.
    """
    if not results:
        return

    successful = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]

    total_queries = len(results)
    success_rate = (len(successful) / total_queries * 100) if total_queries > 0 else 0

    if successful:
        durations = [r["elapsed_ms"] for r in successful]
        avg_duration = sum(durations) / len(durations)
        min_duration = min(durations)
        max_duration = max(durations)

        total_cards = sum(r["card_count"] for r in successful)

        logger.info("=" * 60)
        logger.info("Statistics for %d queries:", total_queries)
        logger.info("  Success rate: %.1f%%", success_rate)
        logger.info("  Successful queries: %d", len(successful))
        logger.info("  Failed queries: %d", len(failed))
        logger.info("  Total cards returned: %d", total_cards)
        logger.info("  Average duration: %.3fms", avg_duration)
        logger.info("  Min duration: %.3fms", min_duration)
        logger.info("  Max duration: %.3fms", max_duration)
        logger.info("=" * 60)


def main() -> None:
    """Main function to continuously run random queries."""
    setup_logging()

    # Configuration from environment variables
    api_url = os.environ.get("API_URL", DEFAULT_API_URL)
    query_delay = float(os.environ.get("QUERY_DELAY", DEFAULT_QUERY_DELAY))
    batch_size = int(os.environ.get("BATCH_SIZE", DEFAULT_BATCH_SIZE))

    logger.info("Starting query runner against API: %s", api_url)
    logger.info("Query delay: %ss", query_delay)
    logger.info("Batch size: %d", batch_size)

    # Create a session for HTTP requests
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "ScryfallosQueryRunner/1.0",
        },
    )

    # Generate query pool
    query_pool = generate_random_queries()
    logger.info("Generated %d unique query patterns", len(query_pool))

    results = []
    query_count = 0

    try:
        while True:
            # Pick a random query from the pool
            query = random.choice(query_pool)

            # Run the query
            result = run_query(api_url, query, session)
            results.append(result)
            query_count += 1

            # Print statistics after each batch
            if query_count % batch_size == 0:
                print_statistics(results)
                results = []

            # Delay before next query
            time.sleep(query_delay)

    except KeyboardInterrupt:
        logger.info("Shutting down query runner...")
        if results:
            print_statistics(results)


if __name__ == "__main__":
    main()
