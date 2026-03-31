#!/usr/bin/env python3
"""Client script to generate random queries and run them against the API.

This script continuously generates random card search queries and executes them
against the Scryfall OS API to help identify which database indexes are being used
and which queries perform well or poorly.

Modes:
  default    Generate a fixed synthetic query corpus.
  --realistic  Pull the most common/slowest real queries from magic.query_log
               (requires PG* env vars pointing at the database).
"""

import argparse
import logging
import os
import random
import time

import psycopg
import requests

logger = logging.getLogger(__name__)
# Constants
DEFAULT_API_URL = "http://apiservice:8080"
DEFAULT_QUERY_DELAY = 1.0  # Delay between queries in seconds
DEFAULT_BATCH_SIZE = 50  # Number of queries before reporting stats

_REALISTIC_SQL = """
SELECT q
FROM magic.query_log
WHERE had_error = false
  AND cache_hit = false
  AND q IS NOT NULL
GROUP BY q
ORDER BY AVG(execute_ms) DESC NULLS LAST, COUNT(*) DESC
LIMIT %(limit)s
"""


def setup_logging() -> None:
    """Set up logging configuration."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )


_ORDERBY_VALUES = ["edhrec", "cubecobra", "cmc", "power", "toughness", "rarity", "usd"]

# unique=card 75%, printing 20%, artwork 5%
_UNIQUE_VALUES = ["card"] * 75 + ["printing"] * 20 + ["artwork"] * 5

# Each dimension has a relative weight controlling how often it is chosen, and a list of
# query fragments.  Weights are approximate — they reflect realistic user search patterns.
# Higher weight = picked more often as one of the 1-4 dimensions in a random query.
_DIMENSIONS: list[tuple[int, str, list[str]]] = [
    # (weight, name, fragments)
    (30, "name", [
        "name:bolt", "name:angel", "name:dragon", "name:counter", "name:force",
        "name:fire", "name:dark", "name:ancient", "name:storm", "name:path",
        "name:bo", "name:an", "name:dr", "name:co", "name:fo",
        "name:fi", "name:da", "name:an", "name:st", "name:pa",
    ]),
    (20, "oracle_text", [
        "oracle:flying", "oracle:haste", "oracle:trample", "oracle:deathtouch",
        "oracle:lifelink", "oracle:vigilance", "oracle:draw", "oracle:counter",
        "oracle:destroy", "oracle:exile", "oracle:return", "oracle:token",
        "oracle:sacrifice", "oracle:search",
    ]),
    (18, "card_type", [
        "type:creature", "type:instant", "type:sorcery",
        "type:enchantment", "type:artifact", "type:planeswalker", "type:land",
    ]),
    (14, "color", [
        "color:w", "color:u", "color:b", "color:r", "color:g",
        "color:wu", "color:ub", "color:br", "color:rg", "color:gw",
        "color:wub", "color:ubr", "color:brg",
    ]),
    (15, "color_identity", [
        "id:w", "id:u", "id:b", "id:r", "id:g",
        "id:wu", "id:ub", "id:br", "id:rg", "id:gw",
        "id:wub", "id:ubr", "id:brg",
    ]),
    (12, "set_code", [
        "set:m21", "set:znr", "set:khm", "set:stx", "set:mid",
        "set:neo", "set:snc", "set:dmu", "set:bro", "set:mom",
        "set:ltr", "set:woe", "set:mkm", "set:otj", "set:blb",
    ]),
    (10, "card_subtype", [
        "type:dragon", "type:wizard", "type:goblin", "type:zombie",
        "type:elf", "type:angel", "type:vampire", "type:merfolk",
        "type:equipment", "type:aura",
    ]),
    (8, "legality", [
        "format:standard", "format:modern", "format:commander",
        "format:legacy", "format:vintage", "format:pioneer", "format:pauper",
    ]),
    (7, "year", [
        "year:2019", "year:2020", "year:2021", "year:2022", "year:2023", "year:2024",
    ]),
    (6, "power", [
        "pow=0", "pow=1", "pow=2", "pow=3", "pow=4", "pow=5", "pow=6",
        "pow<2", "pow<4", "pow>2", "pow>4", "pow>=5",
    ]),
    (6, "toughness", [
        "tou=0", "tou=1", "tou=2", "tou=3", "tou=4", "tou=5", "tou=6",
        "tou<2", "tou<4", "tou>2", "tou>4", "tou>=5",
    ]),
    (5, "price_usd", [
        "usd<0.25", "usd<1", "usd<5", "usd>1", "usd>5", "usd>20", "usd>50",
    ]),
    (3, "artist", [
        "artist:tedin", "artist:rahn", "artist:avon", "artist:burns",
        "artist:thomas", "artist:foglio",
    ]),
    (3, "is_tag", [
        "is:spell", "is:permanent", "is:historic", "is:modal",
        "is:token", "is:commander",
    ]),
    (2, "price_tix", [
        "tix<0.1", "tix<1", "tix>1", "tix>5",
    ]),
    (2, "price_eur", [
        "eur<1", "eur<5", "eur>5", "eur>20",
    ]),
    (2, "produced_mana", [
        "produces:w", "produces:u", "produces:b", "produces:r", "produces:g", "produces:c",
    ]),
    (1, "flavor_text", [
        "flavor:death", "flavor:fire", "flavor:light", "flavor:darkness",
        "flavor:power", "flavor:ancient",
    ]),
    (1, "border", [
        "border:black", "border:white", "border:silver", "border:borderless",
    ]),
    (1, "frame", [
        "frame:old", "frame:modern", "frame:future", "frame:showcase", "frame:extendedart",
    ]),
    (1, "watermark", [
        "watermark:set", "watermark:planeswalker", "watermark:guild",
    ]),
    (1, "collector_number", [
        "cn:1", "cn:100", "cn:200", "cn:300",
    ]),
    (1, "devotion", [
        "devotion:w", "devotion:u", "devotion:b", "devotion:r", "devotion:g",
        "devotion:www", "devotion:uuu", "devotion:bbb", "devotion:rrr", "devotion:ggg",
    ]),
]

# Pre-split for use with random.choices
_DIM_WEIGHTS = [w for w, _, _ in _DIMENSIONS]
_DIM_NAMES = [n for _, n, _ in _DIMENSIONS]
_DIM_VALUES = {n: v for _, n, v in _DIMENSIONS}


def random_query() -> str:
    """Generate a single random multi-dimensional query.

    Picks 1-4 dimensions weighted by search frequency, then picks one value
    from each, giving realistic coverage without enumerating the full cross-product.

    Returns:
        A query string combining fragments from the chosen dimensions.
    """
    r = random.randint(1, 4)  # noqa: S311
    chosen_dims = random.choices(_DIM_NAMES, weights=_DIM_WEIGHTS, k=r)  # noqa: S311
    fragments = set()
    for idx, cd in enumerate(chosen_dims, start=1):
        dimension_choices = _DIM_VALUES[cd]
        while len(fragments) < idx:
            fragments.add(random.choice(dimension_choices))
    return " ".join(sorted(fragments))


def fetch_realistic_queries(limit: int = 500) -> list[str]:
    """Pull real queries from magic.query_log, ordered by average DB execution time.

    Requires PG* environment variables (PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD)
    to be set so the script can connect directly to the database.

    Args:
        limit: Maximum number of distinct queries to return.

    Returns:
        List of query strings from actual user searches, slowest first.
    """
    mapping = {"database": "dbname"}
    creds = {mapping.get(k[2:].lower(), k[2:].lower()): v for k, v in os.environ.items() if k.startswith("PG")}
    if not creds:
        msg = "No PG* env vars found; cannot connect to DB for realistic queries"
        raise RuntimeError(msg)
    conninfo = " ".join(f"{k}={v}" for k, v in creds.items())
    with psycopg.connect(conninfo) as conn, conn.cursor() as cur:
        cur.execute(_REALISTIC_SQL, {"limit": limit})
        rows = cur.fetchall()
    queries = [row[0] for row in rows]
    logger.info("Fetched %d realistic queries from magic.query_log", len(queries))
    return queries


def run_query(api_url: str, query: str, session: requests.Session, orderby: str, unique: str) -> dict:
    """Run a single search query against the API.

    Args:
        api_url: Base URL for the API.
        query: The search query string.
        session: Requests session for API calls.
        orderby: The orderby parameter value.
        unique: The unique parameter value.

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
            params={"q": query, "limit": 100, "orderby": orderby, "unique": unique},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        result["success"] = True
        card_count = len(data.get("cards", []))
        result["card_count"] = card_count
        result["execute_ms"] = (data.get("inner_timings") or {}).get("execute_query")
    except requests.RequestException as oops:
        result["success"] = False
        result["error"] = str(oops)
    finally:
        elapsed = time.monotonic() - before
        elapsed_ms = 1000 * elapsed
        result["elapsed_ms"] = elapsed_ms

    if result["success"]:
        execute_ms = result.get("execute_ms")
        execute_str = f" | DB execute: {execute_ms:.1f}ms" if execute_ms is not None else ""
        logging.info(
            "Query: '%s' orderby=%s unique=%s | HTTP: %.1fms%s | Cards: %d",
            query,
            orderby,
            unique,
            elapsed_ms,
            execute_str,
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

        execute_times = [r["execute_ms"] for r in successful if r.get("execute_ms") is not None]
        execute_str = ""
        if execute_times:
            execute_str = f"\n  Avg DB execute: {sum(execute_times) / len(execute_times):.1f}ms | Max: {max(execute_times):.1f}ms"

        logger.info("=" * 60)
        logger.info("Statistics for %d queries:", total_queries)
        logger.info("  Success rate: %.1f%%", success_rate)
        logger.info("  Successful queries: %d", len(successful))
        logger.info("  Failed queries: %d", len(failed))
        logger.info("  Total cards returned: %d", total_cards)
        logger.info(
            "  Avg HTTP duration: %.1fms | Min: %.1fms | Max: %.1fms%s", avg_duration, min_duration, max_duration, execute_str
        )
        logger.info("=" * 60)


def main() -> None:
    """Main function to continuously run random queries."""
    setup_logging()
    while True:
        time.sleep(1)

    parser = argparse.ArgumentParser(description="Run search queries against the Arcane Tutor API.")
    parser.add_argument(
        "--realistic",
        action="store_true",
        help="Use real queries from magic.query_log (requires PG* env vars) instead of synthetic ones.",
    )
    args = parser.parse_args()

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

    # Build query pool (realistic mode only; synthetic generates on the fly)
    if args.realistic:
        logger.info("Mode: realistic (from magic.query_log)")
        query_pool = fetch_realistic_queries()

        def get_query() -> str:
            return random.choice(query_pool)
    else:
        get_query = random_query
        logger.info("Mode: synthetic")

    results = []
    query_count = 0

    try:
        while True:
            query = get_query()
            orderby = random.choice(_ORDERBY_VALUES)
            unique = random.choice(_UNIQUE_VALUES)

            # Run the query
            result = run_query(api_url, query, session, orderby=orderby, unique=unique)
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
