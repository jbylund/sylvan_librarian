"""Mirror Scryfall's reference data — sets, catalogs and card symbols — into `magic`.

These three are not bulk data. Scryfall publishes them as ordinary API endpoints, small enough to
fetch whole: 1,047 sets, twenty catalogs totalling around 60,000 strings, and 84 card symbols. So
this module talks to `api.scryfall.com` directly through the bulk fetcher's retrying session rather
than through `stream_data_for_key`.

Every load is a whole-table replace inside one transaction, for the reason the rulings load is: the
upstream response is the entire truth each time, a set can be renamed or withdrawn before release,
and a replace cannot get the pruning wrong. `DELETE` rather than `TRUNCATE` so readers keep seeing
the previous contents through MVCC instead of blocking on an ACCESS EXCLUSIVE lock.

Why mirrored and not derived: the corpus cannot answer these. A Set object carries eight fields no
card carries, `card_count` counts printings this instance deliberately never imported, and a card
symbol's `svg_uri` exists nowhere in the card data. The full argument is in the migration header,
`api/db/2026-08-11-02-scryfall-sets-catalogs-symbology.sql`.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from api.scryfall_compat.reference_routes import CATALOG_NAMES

if TYPE_CHECKING:
    import psycopg_pool

    from api.scryfall_bulk_data_fetcher import ScryfallBulkDataFetcher

logger = logging.getLogger(__name__)


def import_sets(conn_pool: psycopg_pool.ConnectionPool, fetcher: ScryfallBulkDataFetcher) -> dict[str, Any]:
    """Replace `magic.sets` with Scryfall's current set list.

    Args:
        conn_pool: Pool to run the load through.
        fetcher: Fetcher whose session the request goes through.

    Returns:
        A summary of the load.
    """
    start = time.monotonic()
    payload = fetcher.fetch_api_json("sets")
    sets = [entry for entry in payload.get("data", []) if entry.get("id") and entry.get("code")]

    rows = [
        {
            "id": entry["id"],
            "code": entry["code"],
            "tcgplayer_id": entry.get("tcgplayer_id"),
            "position": position,
            "set_object": Jsonb(entry),
        }
        for position, entry in enumerate(sets)
    ]

    with conn_pool.connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM magic.sets")
            cursor.executemany(
                "INSERT INTO magic.sets (id, code, tcgplayer_id, position, set_object) "
                "VALUES (%(id)s, %(code)s, %(tcgplayer_id)s, %(position)s, %(set_object)s) "
                "ON CONFLICT (id) DO NOTHING",
                rows,
            )
        conn.commit()

    result = {
        "duration_seconds": round(time.monotonic() - start, 2),
        "sets_imported": len(rows),
        "sets_with_tcgplayer_id": sum(1 for row in rows if row["tcgplayer_id"] is not None),
    }
    logger.info("Set import complete: %s", result)
    return result


def import_catalogs(conn_pool: psycopg_pool.ConnectionPool, fetcher: ScryfallBulkDataFetcher) -> dict[str, Any]:
    """Replace `magic.catalogs` with the current contents of every documented catalog.

    A catalog that fails to fetch is skipped rather than fatal, and its previous row is left in
    place: nineteen fresh catalogs and one stale one is a better answer than aborting the refresh.

    Args:
        conn_pool: Pool to run the load through.
        fetcher: Fetcher whose session the requests go through.

    Returns:
        A summary of the load.
    """
    start = time.monotonic()
    fetched: dict[str, list[str]] = {}
    failed: list[str] = []
    for name in CATALOG_NAMES:
        try:
            payload = fetcher.fetch_api_json(f"catalog/{name}")
        except Exception:
            logger.exception("Catalog %s could not be fetched; keeping the previous contents", name)
            failed.append(name)
            continue
        values = payload.get("data")
        if isinstance(values, list):
            fetched[name] = [value for value in values if isinstance(value, str)]
        else:
            failed.append(name)

    with conn_pool.connection() as conn:
        with conn.cursor() as cursor:
            # Only the catalogs that came back are replaced, so a failed fetch keeps its old row.
            cursor.executemany(
                "INSERT INTO magic.catalogs (name, entries) VALUES (%(name)s, %(entries)s) "
                "ON CONFLICT (name) DO UPDATE SET entries = EXCLUDED.entries",
                [{"name": name, "entries": Jsonb(values)} for name, values in fetched.items()],
            )
        conn.commit()

    result = {
        "duration_seconds": round(time.monotonic() - start, 2),
        "catalogs_imported": len(fetched),
        "catalogs_failed": len(failed),
        "values_imported": sum(len(values) for values in fetched.values()),
    }
    logger.info("Catalog import complete: %s", result)
    return result


def import_symbology(conn_pool: psycopg_pool.ConnectionPool, fetcher: ScryfallBulkDataFetcher) -> dict[str, Any]:
    """Replace `magic.card_symbols` with Scryfall's current symbol list.

    Args:
        conn_pool: Pool to run the load through.
        fetcher: Fetcher whose session the request goes through.

    Returns:
        A summary of the load.
    """
    start = time.monotonic()
    payload = fetcher.fetch_api_json("symbology")
    symbols = [entry for entry in payload.get("data", []) if entry.get("symbol")]

    rows = [
        {"symbol": entry["symbol"], "position": position, "symbol_object": Jsonb(entry)} for position, entry in enumerate(symbols)
    ]

    with conn_pool.connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM magic.card_symbols")
            cursor.executemany(
                "INSERT INTO magic.card_symbols (symbol, position, symbol_object) "
                "VALUES (%(symbol)s, %(position)s, %(symbol_object)s) "
                "ON CONFLICT (symbol) DO NOTHING",
                rows,
            )
        conn.commit()

    result = {
        "duration_seconds": round(time.monotonic() - start, 2),
        "symbols_imported": len(rows),
    }
    logger.info("Symbology import complete: %s", result)
    return result
