"""Import Scryfall rulings bulk data into `magic.rulings`.

Rulings back `/cards/:id/rulings` and its four sibling routes. They hang off `oracle_id`, not off a
printing, so the table is independent of `magic.cards` and is loaded the same way the tag
collections are: streamed from the cached bulk file and written in batches.

The load is a whole-table replace inside one transaction, rather than an upsert. The bulk file
carries no ruling id — a row's identity is the tuple itself — and rulings are occasionally
retracted, so "insert what is there" would accumulate rows that Scryfall has withdrawn. A replace
also cannot get the pruning wrong when one card's rulings straddle a batch boundary, which is the
failure an incremental prune invites. `DELETE` rather than `TRUNCATE` so readers keep seeing the
previous contents through MVCC instead of blocking on an ACCESS EXCLUSIVE lock for the load.
"""

from __future__ import annotations

import itertools
import logging
import time
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from api.scryfall_bulk_data_fetcher import BulkDataKey

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    import psycopg_pool

    from api.scryfall_bulk_data_fetcher import ScryfallBulkDataFetcher

logger = logging.getLogger(__name__)

# Rows per INSERT. The whole batch travels as one jsonb bind parameter, so this sets the statement's
# server-side peak; the rulings file is two orders of magnitude smaller than the cards file, so it
# can sit well above the card upsert's page size and still be a fraction of its parameter size.
_BATCH_SIZE = 5_000

_INSERT_SQL = """
INSERT INTO magic.rulings (oracle_id, source, published_at, comment)
SELECT
    (entry ->> 'oracle_id')::uuid,
    entry ->> 'source',
    (entry ->> 'published_at')::date,
    entry ->> 'comment'
FROM jsonb_array_elements(%(rows)s) AS entry
ON CONFLICT DO NOTHING
"""

_REQUIRED_FIELDS = ("oracle_id", "source", "published_at", "comment")

# Scryfall publishes `published_at` as a bare date; slicing rather than parsing keeps a future
# timestamp form from failing the ::date cast.
_DATE_LENGTH = 10


def _valid_rulings(rulings: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Yield the entries that carry every column the table requires.

    Args:
        rulings: Raw entries from the bulk file.

    Yields:
        One normalized row per usable entry.
    """
    for ruling in rulings:
        if all(ruling.get(field) for field in _REQUIRED_FIELDS):
            yield {
                "oracle_id": ruling["oracle_id"],
                "source": ruling["source"],
                "published_at": str(ruling["published_at"])[:_DATE_LENGTH],
                "comment": ruling["comment"],
            }


def import_rulings(conn_pool: psycopg_pool.ConnectionPool, fetcher: ScryfallBulkDataFetcher) -> int:
    """Replace `magic.rulings` with the current rulings bulk file.

    Args:
        conn_pool: Pool to run the load through.
        fetcher: Bulk data fetcher, which caches the download between runs.

    Returns:
        The number of rulings loaded.
    """
    before = time.monotonic()
    loaded = 0
    with conn_pool.connection() as conn:
        # One transaction: the DELETE is only visible to other sessions once the reload commits,
        # so no request can observe an empty rulings table.
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM magic.rulings")
            for batch in itertools.batched(_valid_rulings(fetcher.stream_data_for_key(BulkDataKey.RULINGS)), _BATCH_SIZE):
                cursor.execute(_INSERT_SQL, {"rows": Jsonb(list(batch))})
                # rowcount, not len(batch): the file repeats a tuple often enough to matter -- 37 of
                # 77,998 entries on 2026-08-11 -- and ON CONFLICT DO NOTHING drops those. Counting
                # what was sent would report a row total the table does not hold.
                loaded += cursor.rowcount
        conn.commit()

    logger.info("Imported %d rulings in %.2f seconds", loaded, time.monotonic() - before)
    return loaded
