"""Data-management handlers, mounted as a child resource rather than sharing the public namespace.

These import, backfill and re-tag the card corpus. They are not part of the API a visitor uses, and
nothing calls them over HTTP — `APIResource.__init__` calls two of them in-process at startup, and the
rest are operator actions.

They live here because registration used to have no way to say "not part of the public API": every
public method became a route, and the only lever was a leading underscore, which also lies about a
method's Python visibility. Mounting a separate resource replaces that lever with a boundary, and
keeps the honest names.

`AdminResource` no longer holds a reference to `APIResource` at all. What it used to reach a parent
for — `reload_engine`, `setup_complete`, `cache_generation`, `last_import_time`,
`invalidate_setup_complete`, the connection pool — all live on the `AppContext` both resources take a
reference to at construction (see `api/app_context.py`). Neither resource owns that state; both are
peers that reach into a neutral shared object instead of into each other.

`import_guard`/`schema_setup_event` are not part of `AppContext`: they're cross-worker-shared, but
only *within* `AdminResource`'s own copies across processes. `import_guard` serialises concurrent
*schema setup* only — the import flow itself serialises on `AppContext.last_import_time`'s own lock
instead (see `import_data`) — and nothing on the search side ever touches either primitive. They get
their own small bundle, `AdminContext`, defined here. `APIResource` and `api_worker` import the type
to construct and forward it, but nothing outside this module reads or writes its fields.
"""

from __future__ import annotations

import datetime
import itertools
import logging
import os
import re
import time
import uuid
from typing import TYPE_CHECKING, Any

# Imported at runtime, not under TYPE_CHECKING, because route handlers annotate falcon_response with
# it and build_route_table resolves those annotations to real types at mount. Under TYPE_CHECKING the
# name is absent at runtime and resolution raises UnresolvableAnnotationError — by design, rather than
# silently losing coercion. Same reason api_resource keeps Sequence at runtime.
import falcon  # noqa: TC002
import orjson
import psycopg
import requests
from cachebox import TTLCache

from api.card_processing import preprocess_card
from api.db.bulk_upsert import bulk_upsert as _bulk_upsert
from api.parsing.db_info import CHEAPEST_NEGATED_TERM, CHEAPEST_SHIFTS, CHEAPEST_TERM, CHEAPEST_UNKNOWN
from api.release_batches import RELEASE_BATCHES
from api.scryfall_bulk_data_fetcher import BulkDataKey, ScryfallBulkDataFetcher
from api.settings import settings
from api.tag_import import import_art_tags as _import_art_tags
from api.tag_import import import_oracle_tags as _import_oracle_tags
from api.utils import db_utils, multiprocessing_utils
from api.utils.caching import cached
from api.utils.http_utils import make_user_agent
from api.utils.page_rendering import serve_static_file
from api.utils.routing import route

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from multiprocessing.synchronize import Event as EventType
    from multiprocessing.synchronize import RLock as LockType

    from psycopg import Connection

    from api.app_context import AppContext

# Path prefix the child mounts under. Underscore-prefixed to match the convention API namespaces use
# for internal routes (Elasticsearch _search, CouchDB _all_docs), and to stay clear of /admin, which
# is among the most-scanned paths on the internet. The prefix is not a control — an unmounted path and
# an unknown one return the same 404 — it just keeps the probe noise down.
ADMIN_MOUNT_PREFIX = "_admin"

logger = logging.getLogger(__name__)


# pylint: disable=c-extension-no-member
NOT_FOUND = 404

MIN_IMPORT_INTERVAL = 300

IMPORT_LOCK_TIMEOUT = 2


# Cards per bulk_upsert call during an import. The whole batch becomes ONE bind parameter: a JSON
# array that Postgres must receive, cast to jsonb, expand with jsonb_array_elements, and join against
# magic.cards. So this value sets the server-side peak for the statement, and the corpus grows over
# time — a size that fit once does not stay fitting. Lowered from 6000 to 3000 after backends were
# lost mid-statement during import; the extra round trips are not measurable against the import's
# total, and it halves the logged parameter too (see log_parameter_max_length in the pg config).
_UPSERT_PAGE_SIZE = 3_000

# BOOLEAN_IS_TAGS sync runs once per import over the whole corpus, evaluating every
# managed expression per row. Chunk by scryfall_id hash so each statement stays within
# the import's statement_timeout as the tag list grows.
_BOOLEAN_IS_TAGS_SYNC_CHUNK_COUNT = 4

# is: values derivable from a single boolean SQL expression against a card's own row,
# synced in chunked set-based statements after each import (see _sync_boolean_is_tags) -- no
# per-tag API sweep, unlike CUSTOM_IS_TAGS below, and no accumulation in the import loop.
# Each expression must reference the row alias `cards` -- adding a tag here is the whole
# change. Most read
# `cards.raw_card_blob`; hybrid/phyrexian read `cards.mana_cost_text` instead, per
# docs/issues/done/00713-is-tag-recovery.md's own reasoning for putting them here rather
# than in the query-rewrite table: the DSL only does exact-symbol containment, so a
# rewrite would be a brittle ~15-term OR over an open, growing symbol set. Density-gated
# at ~2% of the corpus (see docs/issues/00985): reserved (1.1%) and gamechanger (0.4%)
# were the original two; the rest were added after a corpus-wide survey of every is: tag
# on Scryfall's syntax page found these sitting at or under masterpiece's 1.8%.
# foil/nonfoil/reprint/booster/hires/universesbeyond/promo/full/datestamped/prerelease
# were excluded here too ("higher cardinality, memory check first") but are now included:
# the Postgres row-growth cost is accepted, and #1003 made a dense value cost a bitmap
# instead of a posting list on the engine side, so density no longer argues against them.
BOOLEAN_IS_TAGS: dict[str, str] = {
    # Alphabetized by key. Expressions read either a plain top-level boolean (reserved,
    # gamechanger, spotlight), promo_types/keywords/finishes array membership, or a
    # single-field lookup (set_type, preview.source).
    "arena_league": "cards.raw_card_blob->'promo_types' @> '\"arenaleague\"'",
    "booster": "cards.raw_card_blob->'booster' = 'true'::jsonb",
    "buyabox": "cards.raw_card_blob->'promo_types' @> '\"buyabox\"'",
    "convention": "cards.raw_card_blob->'promo_types' @> '\"convention\"'",
    "datestamped": "cards.raw_card_blob->'promo_types' @> '\"datestamped\"'",
    "etched": "cards.raw_card_blob->'finishes' @> '\"etched\"'",
    "fnm": "cards.raw_card_blob->'promo_types' @> '\"fnm\"'",
    "foil": "cards.raw_card_blob->'foil' = 'true'::jsonb",
    "full": "cards.raw_card_blob->'full_art' = 'true'::jsonb",
    "gamechanger": "cards.raw_card_blob->'game_changer' = 'true'::jsonb",
    "gameday": "cards.raw_card_blob->'promo_types' @> '\"gameday\"'",
    "giftbox": "cards.raw_card_blob->'promo_types' @> '\"giftbox\"'",
    "glossy": "cards.raw_card_blob->'promo_types' @> '\"glossy\"'",
    "hires": "cards.raw_card_blob->'highres_image' = 'true'::jsonb",
    # Matches color/color, 2/color, colorless/color, and color/color/phyrexian.
    "hybrid": r"cards.mana_cost_text ~ '\{[2CWUBRG]/[WUBRG]'",
    "instore": "cards.raw_card_blob->'promo_types' @> '\"instore\"'",
    "intro_pack": "cards.raw_card_blob->'promo_types' @> '\"intropack\"'",
    "judge_gift": "cards.raw_card_blob->'promo_types' @> '\"judgegift\"'",
    "league": "cards.raw_card_blob->'promo_types' @> '\"league\"'",
    "masterpiece": "cards.raw_card_blob->>'set_type' = 'masterpiece'",
    "media_insert": "cards.raw_card_blob->'promo_types' @> '\"mediainsert\"'",
    "nonfoil": "cards.raw_card_blob->'nonfoil' = 'true'::jsonb",
    # "Partner with <name>" cards carry a plain "Partner" keyword alongside it (verified
    # against the corpus), so checking for "Partner" alone already covers both.
    "partner": "cards.raw_card_blob->'keywords' @> '\"Partner\"'",
    # Search for `/P}` in mana costs and oracle texts.
    "phyrexian": r"(cards.mana_cost_text ~ '/P\}' OR cards.oracle_text ~ '/P\}')",
    "planeswalker_deck": "cards.raw_card_blob->'promo_types' @> '\"planeswalkerdeck\"'",
    "player_rewards": "cards.raw_card_blob->'promo_types' @> '\"playerrewards\"'",
    "prerelease": "cards.raw_card_blob->'promo_types' @> '\"prerelease\"'",
    "promo": "cards.raw_card_blob->'promo' = 'true'::jsonb",
    "release": "cards.raw_card_blob->'promo_types' @> '\"release\"'",
    "reprint": "cards.raw_card_blob->'reprint' = 'true'::jsonb",
    "reserved": "cards.raw_card_blob->'reserved' = 'true'::jsonb",
    "scryfallpreview": "cards.raw_card_blob->'preview'->>'source' = 'Scryfall'",
    "set_promo": "cards.raw_card_blob->'promo_types' @> '\"setpromo\"'",
    "spotlight": "cards.raw_card_blob->'story_spotlight' = 'true'::jsonb",
    "universesbeyond": "cards.raw_card_blob->'promo_types' @> '\"universesbeyond\"'",
}


def _build_boolean_is_tags_sql(tags: dict[str, str]) -> str:
    """Build a BOOLEAN_IS_TAGS sync statement from `tags`.

    Each `(tag, expr)` pair becomes one ``jsonb_build_object`` entry: ``expr`` reads the
    outer `cards` row (`cards.raw_card_blob`, `cards.mana_cost_text`, etc.), so it may be
    any boolean SQL expression -- not just "this top-level key is literally true" -- letting
    one mechanism cover plain booleans, promo_types/keywords/finishes membership, and nested
    lookups. ``jsonb_strip_nulls`` drops keys whose ``CASE WHEN`` did not fire. `tags` is a
    static, developer-authored module constant, never user input, so embedding its keys and
    expressions as literal SQL text here (rather than binding them as query parameters, which
    can't carry per-tag SQL syntax anyway) is safe.

    Callers pass ``num_chunks`` and ``chunk_index`` as query parameters. Use
    ``num_chunks=1, chunk_index=0`` to scan the whole corpus; otherwise only cards whose
    ``hashtext(scryfall_id)`` falls in that slice are touched.
    """
    managed = ", ".join(f"'{tag}'" for tag in tags)
    object_entries = ",\n            ".join(
        f"'{tag}', CASE WHEN ({expr}) THEN true END" for tag, expr in tags.items()
    )
    return f"""
WITH proposed AS (
    SELECT
        cards.scryfall_id,
        (cards.card_is_tags - ARRAY[{managed}]::text[])
            || jsonb_strip_nulls(
                jsonb_build_object(
            {object_entries}
                )
            ) AS proposed_is_tags
    FROM magic.cards cards
    WHERE (abs(hashtext(cards.scryfall_id::text)) %% %(num_chunks)s) = %(chunk_index)s
)
UPDATE magic.cards
SET card_is_tags = proposed.proposed_is_tags
FROM proposed
WHERE
    cards.scryfall_id = proposed.scryfall_id AND
    cards.card_is_tags IS DISTINCT FROM proposed.proposed_is_tags
"""

# Same chunking motive as _BOOLEAN_IS_TAGS_SYNC_CHUNK_COUNT, but by ORACLE id, not printing: a
# per-card count is only chunkable if every row of a card lands in the same chunk.
_PRINT_COUNTS_SYNC_CHUNK_COUNT = 4

# The six columns _sync_print_counts maintains, in the order the statement writes them.
PRINT_COUNT_COLUMNS = (
    "card_print_count",
    "card_set_count",
    "card_paper_print_count",
    "card_paper_set_count",
    "card_illustration_count",
    "artist_count",
)


def _build_print_counts_sql() -> str:
    """Build the count-keyword sync statement: five per-card counts written onto every row of the card.

    Scryfall's `prints`, `sets`, `paperprints`, `papersets` and `illustrations` each compare a
    count over ALL of a card's printings, and `artists` a count on the printing itself. A SQL row
    is one printing and the engine's `tri()` holds one card and one printing, so neither can count
    siblings at query time; the numbers are decided here, once per import.

    The rules, measured on api.scryfall.com 2026-10-03 and 2026-10-04 by reading every printing of
    a card and binary-searching the value Scryfall holds (`!"Lightning Bolt" prints>=K`):

    - `prints`: distinct (set, collector number) SLOTS -- not rows, so a slot printed in eleven
      languages counts once -- over every printing, extras, promos and memorabilia included.
    - A VARIATION is not a print. Embermage Goblin is ons/200 and the foil-only ons/200★, which
      Scryfall marks `variation: true`, and is `prints=1`, `paperprints=1`. The slot is what is
      skipped, when ANY row of it carries the flag: Monstrous Growth's por/173† is flagged in
      four languages and not in Japanese, and the card is `prints=9` of its ten slots. Its
      artwork and its set still count (`!"Embermage Goblin" illustrations=2`).
    - `sets`: distinct set codes over every row (`sets=1` is `is:unique`).
    - `paperprints` / `papersets`: the same two counts over the rows of PAPER SETS. The set
      decides, not the row's own `games`: "Name Sticker" Goblin's only printing, unf/107m, is
      `games: [mtgo]` in a paper set and is `paperprints=1`; Rakshasa Vizier's Arena-only
      ktk/193y makes it 4, not 3. Zero is a value: a card printed only in digital sets is
      `paperprints=0`.
    - `illustrations`: distinct artworks; a printing with no illustration id contributes none.
    - `artists`: how many artists the PRINTING credits, the length of its `artist_ids`.

    A PAPER SET is one with any row on paper. That is Scryfall's `digital` flag on the Set object
    read off the rows: over the 1,052 sets in the default_cards bulk file of 2026-10-03, "no card
    of the set has paper in `games`" and `/sets`' `digital` agree on every one. Derived rather
    than stored because this schema has no sets table.

    They are counts over the rows this table holds, so they are exact for a card whose every
    printing is imported and low by the printings `preprocess_card` drops.

    Callers pass ``num_chunks`` and ``chunk_index`` as query parameters. Chunking is by
    ``hashtext(oracle_id)`` rather than scryfall_id: the counts are per card, so every row of a
    card must fall in the same chunk or a count taken inside a chunk would be partial. `paper_sets`
    is the one CTE that is NOT chunked: whether a set is on paper is a fact about every card in
    it. Rows with a NULL oracle_id (no card to belong to) keep NULL counts.

    api/db/2026-10-04-01-print-counts.sql carries the same statement, unchunked, as its backfill.
    """
    columns = ", ".join(PRINT_COUNT_COLUMNS)
    return f"""
WITH paper_sets AS (
    SELECT DISTINCT lower(cards.card_set_code) AS set_code
    FROM magic.cards cards
    WHERE COALESCE(cards.raw_card_blob->'games', '[]'::jsonb) ? 'paper'
), card_rows AS (
    SELECT
        cards.oracle_id,
        lower(cards.card_set_code) AS set_code,
        cards.collector_number,
        cards.illustration_id,
        paper_sets.set_code IS NOT NULL AS in_paper_set,
        bool_or(COALESCE(cards.raw_card_blob->>'variation', 'false') = 'true') OVER (
            PARTITION BY cards.oracle_id, lower(cards.card_set_code), cards.collector_number
        ) AS slot_is_variation
    FROM magic.cards cards
    LEFT JOIN paper_sets ON paper_sets.set_code = lower(cards.card_set_code)
    WHERE cards.oracle_id IS NOT NULL
      AND (abs(hashtext(cards.oracle_id::text)) %% %(num_chunks)s) = %(chunk_index)s
), per_card AS (
    SELECT
        card_rows.oracle_id,
        count(DISTINCT (card_rows.set_code, card_rows.collector_number))
            FILTER (WHERE NOT card_rows.slot_is_variation) AS prints,
        count(DISTINCT card_rows.set_code) AS sets,
        count(DISTINCT (card_rows.set_code, card_rows.collector_number))
            FILTER (WHERE NOT card_rows.slot_is_variation AND card_rows.in_paper_set) AS paper_prints,
        count(DISTINCT card_rows.set_code) FILTER (WHERE card_rows.in_paper_set) AS paper_sets,
        count(DISTINCT card_rows.illustration_id) AS illustrations
    FROM card_rows
    GROUP BY card_rows.oracle_id
), proposed AS (
    SELECT
        cards.scryfall_id,
        LEAST(per_card.prints, 32767)::smallint AS card_print_count,
        LEAST(per_card.sets, 32767)::smallint AS card_set_count,
        LEAST(per_card.paper_prints, 32767)::smallint AS card_paper_print_count,
        LEAST(per_card.paper_sets, 32767)::smallint AS card_paper_set_count,
        LEAST(per_card.illustrations, 32767)::smallint AS card_illustration_count,
        LEAST(
            CASE WHEN jsonb_typeof(cards.raw_card_blob->'artist_ids') = 'array'
                 THEN jsonb_array_length(cards.raw_card_blob->'artist_ids') ELSE 0 END,
            32767
        )::smallint AS artist_count
    FROM magic.cards cards
    JOIN per_card ON per_card.oracle_id = cards.oracle_id
)
UPDATE magic.cards
SET ({columns}) = (
    proposed.card_print_count, proposed.card_set_count, proposed.card_paper_print_count,
    proposed.card_paper_set_count, proposed.card_illustration_count, proposed.artist_count
)
FROM proposed
WHERE
    cards.scryfall_id = proposed.scryfall_id AND
    ({", ".join(f"cards.{column}" for column in PRINT_COUNT_COLUMNS)})
    IS DISTINCT FROM
    ({", ".join(f"proposed.{column}" for column in PRINT_COUNT_COLUMNS)})
"""


# By ORACLE id for the same reason as _PRINT_COUNTS_SYNC_CHUNK_COUNT: a card's lowest price is only
# its lowest if every row of the card is in the chunk that takes the minimum.
_CHEAPEST_CODES_SYNC_CHUNK_COUNT = 4

# A Scryfall price is a decimal string ("0.38") or null. Anything else is treated as no price
# rather than cast, so one malformed blob cannot fail the import's sync.
_PRICE_TEXT_PATTERN = r"^[0-9]+(\.[0-9]+)?$"


def _blob_price_sql(key: str) -> str:
    """SQL for `raw_card_blob.prices.<key>` as an exact numeric, NULL when absent."""
    text = f"cards.raw_card_blob->'prices'->>'{key}'"
    return f"CASE WHEN {text} ~ '{_PRICE_TEXT_PATTERN}' THEN ({text})::numeric END"


def _cheapest_code_sql(plain: str, foil: str, minimum: str) -> str:
    """SQL for one currency's three bits of `cheapest_codes` (see CHEAPEST_TERM in api/parsing/db_info.py)."""
    return f"""CASE
            WHEN {minimum} IS NULL THEN
                CASE WHEN {plain} IS NOT NULL OR {foil} IS NOT NULL THEN {CHEAPEST_UNKNOWN} ELSE {CHEAPEST_NEGATED_TERM} END
            ELSE
                CASE WHEN {plain} = {minimum} OR {foil} = {minimum} THEN {CHEAPEST_TERM} ELSE 0 END
                + CASE WHEN ({plain} IS NULL OR {plain} <> {minimum}) AND ({foil} IS NULL OR {foil} = {minimum})
                       THEN {CHEAPEST_NEGATED_TERM} ELSE 0 END
        END"""


def _build_cheapest_codes_sql() -> str:
    """Build the `cheapest:` sync statement: each printing's answers, against its card's lowest price.

    Scryfall's `cheapest:usd`, `cheapest:eur` and `cheapest:tix` find the printings carrying their
    card's lowest price. The lowest price is over the card's other printings, so neither a SQL
    row nor the engine's `tri()` can decide it at query time; it is decided here, once per import,
    and prices move on every import.

    The rule, measured on api.scryfall.com 2026-10-04 by reading every priced printing, the three
    `cheapest:` lists and the three `-cheapest:` lists, and simulating the rule on Scryfall's own
    prices (108,901 of 108,901 rows agree per currency, in both polarities):

    - M, the card's lowest price, is taken over its printings OUTSIDE MEMORABILIA SETS
      (`set_type = 'memorabilia'`: World Championship decks, Collectors' Edition, 30th
      Anniversary Edition), a printing's price being
        usd   `prices.usd`, or `prices.usd_foil` when it has no plain price -- never `usd_etched`
        eur   `prices.eur` alone: a foil-only printing does not enter M
        tix   `prices.tix`
    - `cheapest:usd` is `usd = M OR usd_foil = M`. Ties all match (eld/1 at 0.38 / 0.38). A foil
      price EQUAL to M matches on a printing whose plain price does not (m21/130 at 0.20 / 0.03
      beside roe/136's 0.03); a foil price BELOW the plain one does not lower M (khm/73 is
      2.94 / 1.59 and the cheapest is the foil-only khm/400 at 1.77). A memorabilia printing is
      outside M and still matches when it equals it (wc04/jn13sb at 0.17 beside ice/15's 0.17).
    - `-cheapest:usd`, the negated TERM, is NOT the complement. It is
      `(usd IS NULL OR usd <> M) AND (usd_foil IS NULL OR usd_foil = M)`: 5 printings of Kaldheim
      where the term is 222 of 407, and khm/400 is in both. Only the negated GROUP
      `-(cheapest:usd)` is the complement (185). So the two answers are stored separately.
    - `cheapest:eur` and its negation are the same two expressions over `eur` / `eur_foil`;
      `cheapest:tix` is `tix = M`, which has no foil price, so its negated term is the complement.
    - NO M (every priced printing of the card is memorabilia, or in euros foil-only): a PRICED
      printing is SQL NULL, in neither the term nor its complement
      (`cheapest:usd st:memorabilia` 4, `-(cheapest:usd) st:memorabilia` 5,662 of 5,847); an
      UNPRICED printing is plain false for the term and true for the negated term.

    Prices are read from `raw_card_blob->'prices'` as exact numerics -- the strings `price_usd`,
    `price_eur` and `price_tix` were parsed from, plus the two foil prices no column holds -- so
    "equal to the minimum" is a decimal comparison and not one between `real`s.

    M is over the rows this table holds, so it is Scryfall's M for a card whose every printing
    is imported, and can be higher for one whose cheapest printing `preprocess_card` dropped.

    Each currency's answers are three bits of one smallint (CHEAPEST_TERM, CHEAPEST_NEGATED_TERM,
    CHEAPEST_UNKNOWN at CHEAPEST_SHIFTS[currency]). Rows with a NULL oracle_id keep a NULL column.

    Callers pass ``num_chunks`` and ``chunk_index`` as query parameters; chunking is by
    ``hashtext(oracle_id)`` so a card's rows share a chunk. api/db/2026-10-04-02-cheapest-codes.sql
    carries the same statement, unchunked, as its backfill.
    """
    prices = ",\n        ".join(f"{_blob_price_sql(key)} AS {key}" for key in ("usd", "usd_foil", "eur", "eur_foil", "tix"))
    usd_code, eur_code, tix_code = (
        _cheapest_code_sql(f"priced.{currency}", f"priced.{currency}_foil", f"card_minimum.{currency}")
        for currency in CHEAPEST_SHIFTS
    )
    return f"""
WITH priced AS (
    SELECT
        cards.scryfall_id,
        cards.oracle_id,
        COALESCE(cards.raw_card_blob->>'set_type', '') = 'memorabilia' AS is_memorabilia,
        {prices},
        NULL::numeric AS tix_foil
    FROM magic.cards cards
    WHERE cards.oracle_id IS NOT NULL
      AND (abs(hashtext(cards.oracle_id::text)) %% %(num_chunks)s) = %(chunk_index)s
), card_minimum AS (
    SELECT
        priced.oracle_id,
        min(COALESCE(priced.usd, priced.usd_foil)) FILTER (WHERE NOT priced.is_memorabilia) AS usd,
        min(priced.eur) FILTER (WHERE NOT priced.is_memorabilia) AS eur,
        min(priced.tix) FILTER (WHERE NOT priced.is_memorabilia) AS tix
    FROM priced
    GROUP BY priced.oracle_id
), proposed AS (
    SELECT
        priced.scryfall_id,
        (
        ({usd_code}) * {1 << CHEAPEST_SHIFTS["usd"]}
        + ({eur_code}) * {1 << CHEAPEST_SHIFTS["eur"]}
        + ({tix_code}) * {1 << CHEAPEST_SHIFTS["tix"]}
        )::smallint AS cheapest_codes
    FROM priced
    JOIN card_minimum ON card_minimum.oracle_id = priced.oracle_id
)
UPDATE magic.cards
SET cheapest_codes = proposed.cheapest_codes
FROM proposed
WHERE
    cards.scryfall_id = proposed.scryfall_id AND
    cards.cheapest_codes IS DISTINCT FROM proposed.cheapest_codes
"""


# By ORACLE id, as the two syncs above: a card's first printing at a rarity is only its first if
# every row of the card is in the chunk that ranks them.
_NEW_RARITY_SYNC_CHUNK_COUNT = 4

# Set types whose printings are never `new:rarity` on Scryfall, and the one masterpiece set that is.
# See _build_new_rarity_sql.
_NEW_RARITY_EXCLUDED_SET_TYPES = ("promo", "memorabilia", "from_the_vault", "treasure_chest")
_NEW_RARITY_MASTERPIECE_EXCEPTION = "wot"


def _build_new_rarity_sql(*, chunked: bool = True) -> str:
    """Build the `new:rarity` sync statement: is each printing the first of its card at its rarity?

    Scryfall's `new:rarity` finds the printings that are the first of their card at their rarity.
    "First" is over the card's other printings, so neither a SQL row nor the engine's `tri()` can
    decide it at query time; it is decided here, once per import.

    The rule, measured on api.scryfall.com 2026-10-04 by reading the whole list (`new:rarity`,
    `unique=prints`, extras in: 38,943 printings) against the same day's `default_cards` bulk file,
    and exact there -- 38,943 of 38,943, nothing missing, nothing over:

        per card (oracle id) and RARITY, the one printing that is least by
          (release date, release batch, first integer of the collector number, variation last,
           Scryfall id)
        among the printings OUTSIDE the set types promo, memorabilia, from_the_vault and
        treasure_chest, and outside every masterpiece set but `wot`

    Each clause is evidence, not a reading of the name (groups are (card, rarity) pairs; the count
    is how many pick a different printing than Scryfall's without the clause):

    - the release BATCH is the order Scryfall gives sets that released the same day
      (api/release_batches.py): 18 groups wrong without it.
    - the collector number compares as its FIRST INTEGER (`236s` is 236, `GRN-103` is 103, a lone
      star is 0), not as the string: 2,230 groups wrong the other way. The set code is no key
      between it and the id: with one, 15 groups are wrong.
    - `variation: true` sorts after its plain twin: 33 groups wrong without that key.
    - the excluded set types: Scryfall's list holds no printing of a promo, memorabilia,
      from_the_vault or treasure_chest set (4,761 groups wrong without the exclusion). A
      masterpiece printing is on it only in `wot` (16 printings, every one this order picks); no
      other masterpiece set has one (`e:mps` is 0, though 54 of its printings would be the first
      of their card at their rarity). No field of a card or of `/sets` tells `wot` from the other
      masterpiece sets, so it is a measured exception, kept as one.
    - SERIALIZED printings count (4 of the 38,943), and promo TYPES other than the set types
      above exclude nothing.
    - the negated term is the plain complement (79,532: with 38,943 that is all 118,475 printings
      Scryfall holds, extras in), and `new:rarity` neither opens extras nor widens the search to
      other languages.

    The ranking is over the rows this table holds. Scryfall's is over every printing of the card,
    including the digital-only ones `preprocess_card` drops, so a card whose first printing at a
    rarity was dropped has its first PAPER printing flagged here instead, and a printing Scryfall
    flags that was dropped is simply not here.

    NOT `new:language`: the same order over every row of a (card, language) pair answers 285,528
    of Scryfall's 285,760 rows, 232 short by one shape (two sets released the same day, where
    Scryfall takes one set first and nothing published says which), so it is not stored.

    Rows with a NULL oracle_id keep a NULL column. Callers pass ``num_chunks`` and ``chunk_index``
    as query parameters unless ``chunked`` is false, which is the unchunked statement
    api/db/2026-10-04-03-new-rarity.sql backfills with; chunking is by ``hashtext(oracle_id)`` so
    a card's rows share a chunk.
    """
    excluded = ", ".join(f"'{set_type}'" for set_type in _NEW_RARITY_EXCLUDED_SET_TYPES)
    batches = ",\n    ".join(f"({date}, '{set_code}', {batch})" for date, set_code, batch in RELEASE_BATCHES)
    chunk = "\n      AND (abs(hashtext(cards.oracle_id::text)) %% %(num_chunks)s) = %(chunk_index)s" if chunked else ""
    return f"""
WITH release_batches (released_on, set_code, batch) AS (
    VALUES
    {batches}
), candidates AS (
    SELECT
        cards.scryfall_id,
        cards.oracle_id,
        cards.card_rarity_text AS rarity,
        cards.released_at,
        COALESCE(release_batches.batch, 0) AS batch,
        COALESCE(NULLIF(substring(cards.collector_number FROM '[0-9]+'), '')::numeric, 0) AS first_number,
        COALESCE(cards.raw_card_blob->>'variation', 'false') = 'true' AS is_variation,
        COALESCE(cards.raw_card_blob->>'set_type', '') NOT IN ({excluded})
            AND (COALESCE(cards.raw_card_blob->>'set_type', '') <> 'masterpiece' OR cards.card_set_code = '{_NEW_RARITY_MASTERPIECE_EXCEPTION}') AS is_eligible
    FROM magic.cards cards
    LEFT JOIN release_batches
        ON release_batches.released_on = to_char(cards.released_at, 'YYYYMMDD')::int
        AND release_batches.set_code = cards.card_set_code
    WHERE cards.oracle_id IS NOT NULL{chunk}
), ranked AS (
    SELECT
        candidates.scryfall_id,
        row_number() OVER (
            PARTITION BY candidates.oracle_id, candidates.rarity
            ORDER BY candidates.released_at, candidates.batch, candidates.first_number, candidates.is_variation, candidates.scryfall_id
        ) AS place
    FROM candidates
    WHERE candidates.is_eligible
), proposed AS (
    SELECT candidates.scryfall_id, COALESCE(ranked.place = 1, false) AS new_rarity
    FROM candidates
    LEFT JOIN ranked ON ranked.scryfall_id = candidates.scryfall_id
)
UPDATE magic.cards
SET new_rarity = proposed.new_rarity
FROM proposed
WHERE
    cards.scryfall_id = proposed.scryfall_id AND
    cards.new_rarity IS DISTINCT FROM proposed.new_rarity
"""


CUSTOM_IS_TAGS = [
    "historic",  # artifact, legendary, saga
    "permanent",  # ...
    "spell",  # ...
    "unique",  # has exactly one printing
    "old",  # 93/97 frame
    "new",  # newer frames
    "default",
]

LAND_IS_TAGS = [
    "bikeland",
    "bondland",
    "bounceland",
    "canopyland",
    "checkland",
    "creatureland",
    "fastland",
    "fetchland",
    "filterland",
    "gainland",
    "manland",
    "painland",
    "scryland",
    "shadowland",
    "shockland",
    "slowland",
    "storageland",
    "surveilland",
    "tangoland",
    "tricycleland",
    "triland",
]

CARD_IS_TAGS = LAND_IS_TAGS + [  # noqa: RUF005
    "bear",  # easy to make custom, but also small
    "commander",
    "outlaw",  # based on creature type
    "party",  # based on creature type
    "reserved",
    "vanilla",
]


class AdminContext:
    """Cross-worker primitives private to AdminResource's own use.

    The type itself is imported by `APIResource` and `api_worker` to construct and forward an
    instance, but nothing outside `AdminResource` reads or writes the primitives it holds.
    """

    def __init__(
        self,
        *,
        import_guard: LockType = multiprocessing_utils.DEFAULT_LOCK,
        schema_setup_event: EventType = multiprocessing_utils.DEFAULT_EVENT,
    ) -> None:
        """Build the primitives, or accept ones a caller already built.

        Args:
            import_guard: Cross-process lock serialising concurrent schema setup (see
                `setup_schema`) -- not the import flow itself, which serialises on
                `AppContext.last_import_time`'s own lock instead (see `import_data`).
            schema_setup_event: Set once the schema has been created.
        """
        self.import_guard = import_guard
        self.schema_setup_event = schema_setup_event


class AdminResource:
    """Data-management routes, mounted behind a path prefix by APIResource."""

    def __init__(
        self,
        *,
        app_context: AppContext,
        admin_context: AdminContext | None = None,
    ) -> None:
        """Take ownership of the admin-only handles.

        Args:
            app_context: State and resources shared with the resource this is mounted on --
                connection pools, the query engine, the cross-worker cache/import signals.
            admin_context: Schema-setup-serialisation primitives private to this resource. Built
                fresh if not given, matching every other handle here.
        """
        self.app_context = app_context
        self.admin_context = admin_context or AdminContext()
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": make_user_agent()})
        self._bulk_data_fetcher = ScryfallBulkDataFetcher()

    @route()
    def setup_schema(self, *_: object, **__: object) -> None:
        """Set up the database schema and apply migrations as needed."""
        if self.admin_context.schema_setup_event.is_set():
            logger.info("Schema already setup (fastpath) in pid %d", os.getpid())
            return

        filesystem_migrations = db_utils.get_migrations()

        with self.admin_context.import_guard:
            if self.admin_context.schema_setup_event.is_set():
                logger.info("Schema already setup (slowpath) in pid %d", os.getpid())
                return
            logger.info("Setting up schema in pid %d", os.getpid())
            # read migrations from the db dir...
            # if any already applied migrations differ from what we want
            # to apply then drop everything
            with self.app_context.writer_pool.connection() as conn, conn.cursor() as cursor:
                cursor.execute(
                    """CREATE TABLE IF NOT EXISTS migrations (
                        file_name text not null,
                        file_sha256 text not null,
                        date_applied timestamp default now(),
                        file_contents text not null
                    )""",
                )
                cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_migrations_filename ON migrations (file_name)")
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_migrations_file_sha256 ON migrations USING HASH (file_sha256)",
                )

                cursor.execute("SELECT file_name, file_sha256 FROM migrations ORDER BY date_applied")
                applied_migrations = [dict(r) for r in cursor]

                already_applied = set()
                for applied_migration, fs_migration in zip(applied_migrations, filesystem_migrations, strict=False):
                    if applied_migration.items() <= fs_migration.items():
                        already_applied.add(applied_migration["file_sha256"])
                    else:
                        already_applied.clear()
                        cursor.execute("DELETE FROM migrations")
                        cursor.execute("DROP SCHEMA IF EXISTS magic CASCADE")
                        conn.commit()

                for imigration in filesystem_migrations:
                    file_sha256 = imigration["file_sha256"]
                    if file_sha256 in already_applied:
                        logger.info("%s was already applied...", imigration["file_name"])
                        continue
                    logger.info("Applying %s ...", imigration["file_name"])
                    cursor.execute(imigration["file_contents"])
                    cursor.execute(
                        """
                            INSERT INTO migrations
                                (  file_name  ,   file_sha256  ,   file_contents  ) VALUES
                                (%(file_name)s, %(file_sha256)s, %(file_contents)s)""",
                        imigration,
                    )
                    conn.commit()

            self.admin_context.schema_setup_event.set()
            logger.info("Schema setup complete in pid %d", os.getpid())

    def _import_recent(self) -> bool:
        """Return True if a bulk import completed in the last 5 minutes."""
        # Unlocked read: c_double is atomic on typical platforms; avoids lock contention on fast path
        t = self.app_context.last_import_time.get_obj().value
        if not t:
            logger.info("No import recorded...")
            return False
        time_since_import = time.time() - t
        retval = time_since_import < MIN_IMPORT_INTERVAL
        logger.info("Last import was %d seconds ago, %s", time_since_import, retval)
        return retval

    def _run_import_under_lock(self) -> None:
        """Run the import flow; caller must hold the import lock."""
        if self._import_recent():
            logger.info("Import recent slowpath...")
            return
        self.setup_schema()

        before = time.monotonic()

        result = self._upsert_cards(self._bulk_data_fetcher.stream_data_for_key(BulkDataKey.DEFAULT_CARDS))

        after_transfer = time.monotonic()

        if result["status"] == "success":
            self.app_context.last_import_time.value = time.time()
            total_time = after_transfer - before
            cards_sent = result.get("cards_sent", result["cards_loaded"])
            rate = cards_sent / total_time if total_time > 0 else 0
            logger.info(
                "Loaded %d cards (%d new, %d updated) in %.2f seconds, rate: %.2f cards/s...",
                result["cards_loaded"],
                result.get("cards_inserted", 0),
                result.get("cards_updated", 0),
                total_time,
                rate,
            )
            # Art tags first: the prefer score's art_style component reads card_art_tags, so
            # running the backfill ahead of the tag import scored every card as on-style on a
            # first boot, and nothing rescored until the next import. Oracle tags feed search
            # rather than scoring, so their position relative to the backfill does not matter.
            _import_art_tags(self.app_context.writer_pool, self._bulk_data_fetcher)
            self.backfill_prefer_scores()
            self.backfill_cubecobra_scores()
            _import_oracle_tags(self.app_context.writer_pool, self._bulk_data_fetcher)
            self.app_context.reload_engine(force=True)
            self._clear_caches()
            self.app_context.last_import_time.value = time.time()
            self.app_context.invalidate_setup_complete()
            # Every step above logs its own duration; this closes the sequence with the wall
            # clock the operator actually waited, upsert through engine reload.
            logger.info("Import complete in %.2f seconds", time.monotonic() - before)
            return
        logger.error("Failed to import data: %s", result["message"])
        return

    @cached(
        cache=TTLCache(maxsize=1, global_ttl=MIN_IMPORT_INTERVAL),
    )
    @route()
    def import_data(self, **_: object) -> None:
        """Import data from Scryfall and insert into the database."""
        before = time.monotonic()
        if self._import_recent():
            after = time.monotonic()
            total_time = after - before
            logger.info("Import recent fastpath took %.2f seconds in pid %d", total_time, os.getpid())
            # check without taking the lock so the majority of the time we never take the lock
            return None

        logger.info("Hitting slowpath in pid %d", os.getpid())

        import_lock = self.app_context.last_import_time.get_lock()

        acquired = import_lock.acquire(timeout=IMPORT_LOCK_TIMEOUT)
        if not acquired:
            if self.app_context.setup_complete():
                logger.info(
                    "Timed out waiting %.0fs for import lock; setup complete, skipping in pid %d",
                    IMPORT_LOCK_TIMEOUT,
                    os.getpid(),
                )
                return None
            # acquire with no timeout...
            import_lock.acquire()
        try:
            return self._run_import_under_lock()
        finally:
            import_lock.release()

    @route()
    def prefer_score_tuner(self, *, falcon_response: falcon.Response | None = None, **_: object) -> None:
        """Return the prefer score tuner page.

        Args:
        ----
            falcon_response (falcon.Response): The Falcon response to write to.

        """
        serve_static_file(filename="prefer_score_tuner.html", falcon_response=falcon_response)
        falcon_response.content_type = "text/html"

    @route()
    def backfill_prefer_scores(self, **_: object) -> dict[str, Any]:
        """Backfill prefer_score and prefer_score_components for all cards.

        This endpoint recalculates the prefer score for all existing cards based on:
        - Border color (black: 14, white: 0)
        - Frame version (2015: 42, 2003: 30)
        - Artwork popularity (logarithmic scaling: 23 * ln(count) / ln(40))
        - Rarity (common: 16, uncommon: 16, rare: 11, mythic: 0)
        - Extended art (12 points if present)
        - Highres scan (8 points if image_status='highres_scan')
        - Has paper (6 points if 'paper' in games array)
        - Language (English: 40 points)
        - Legendary frame (5 points if 'legendary' in frame_effects)
        - Non-showcase (10 points if 'showcase' not in frame_effects)
        - Finish (nonfoil: 10, foil: 5, etched: 0)
        - Artwork set (full-color: 20, black/white: 0)

        Returns:
            Dict with status and count of cards updated
        """
        start = time.monotonic()
        logger.info("Starting prefer score backfill")

        backfill_sql = db_utils.read_sql("backfill_prefer_scores")
        with self.app_context.writer_pool.connection() as conn, conn.cursor() as cursor:
            db_utils.set_statement_timeout(cursor, settings.prefer_score_backfill_timeout_ms)
            cursor.execute(backfill_sql)
            updated_count = cursor.rowcount

            # Get count of updated cards
            cursor.execute("SELECT COUNT(*) as count FROM magic.cards WHERE prefer_score IS NOT NULL")
            result = cursor.fetchone()
            total_cards = result["count"] if result else 0

            conn.commit()

        # cards_updated counts only rows whose score actually moved -- the backfill's UPDATE
        # skips rows already carrying the right value, so a steady-state re-run reports 0 of N
        # rather than N of N. Both numbers are worth having: the first says how much churned,
        # the second that the corpus is fully scored.
        stats = {
            "duration_seconds": round(time.monotonic() - start, 2),
            "cards_updated": updated_count,
            "cards_scored": total_cards,
        }
        logger.info("Prefer score backfill complete: %s", stats)

        return {
            "status": "success",
            "message": f"Successfully backfilled prefer scores for {updated_count} of {total_cards} cards",
            **stats,
        }

    def _fetch_cubecobra_data(self, db_oracle_ids: set[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
        """Paginate the CubeCobra top-cards API and return data keyed by oracle_id.

        Returns:
            Mapping of oracle_id -> {elo, cube_count, pick_count, popularity}.
        """
        cubecobra_url = "https://cubecobra.com/tool/api/topcards/"
        page = 0  # CubeCobra's `p` query param is 0-indexed; starting at 1 skips the top page.

        while True:
            time.sleep(0.5)
            logger.info("Fetching CubeCobra page %d", page)
            response = self._session.get(
                cubecobra_url,
                params={"p": page, "f": "", "s": "Elo", "d": "descending"},
                timeout=30,
            )
            response.raise_for_status()
            cards = response.json().get("data") or []

            if not cards:
                logger.info("Empty page %d - done paginating CubeCobra", page)
                break

            results: dict[uuid.UUID, dict[str, Any]] = {}
            for card in cards:
                oracle_id_str = card.get("oracle_id")
                if not oracle_id_str:
                    continue
                try:
                    oracle_id = uuid.UUID(oracle_id_str)
                except ValueError:
                    logger.warning("CubeCobra returned malformed oracle_id %r on page %d", oracle_id_str, page)
                    continue
                if oracle_id in db_oracle_ids:
                    results[oracle_id] = {
                        "elo": card.get("elo"),
                        "cube_count": card.get("cubeCount"),
                        "pick_count": card.get("pickCount"),
                    }

            logger.info("CubeCobra page %d: %d cards (total: %d)", page, len(cards), len(results))
            page += 1
            yield results

    def _insert_cubecobra_data(self, cubecobra_data: dict[uuid.UUID, dict[str, Any]]) -> int:
        """Write CubeCobra data into magic.cards, matching on oracle_id.

        Args:
            cubecobra_data: Mapping of oracle_id -> data dict from _fetch_cubecobra_data().

        Returns:
            Total number of card rows updated.
        """
        records = db_utils.maybe_json(
            [
                {
                    "elo": data["elo"],
                    "cube_count": data["cube_count"],
                    "pick_count": data["pick_count"],
                    "oracle_id": oracle_id,
                }
                for oracle_id, data in cubecobra_data.items()
            ]
        )

        with self.app_context.writer_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                WITH incoming AS (
                    SELECT * FROM jsonb_to_recordset(%(records)s) AS t(
                        elo real, cube_count integer, pick_count integer, oracle_id uuid
                    )
                )
                UPDATE magic.cards
                SET
                    cubecobra_elo        = incoming.elo,
                    cubecobra_cube_count = incoming.cube_count,
                    cubecobra_pick_count = incoming.pick_count
                FROM incoming
                WHERE magic.cards.oracle_id = incoming.oracle_id
                """,
                {"records": records},
            )
            total_updated = cursor.rowcount
            conn.commit()

        return total_updated

    @route()
    def backfill_cubecobra_scores(self, **_: object) -> dict[str, Any]:
        """Backfill cubecobra_score for all cards.

        Computes a weighted average of per-dimension PERCENT_RANK values (each in the 0-1
        range, where 0 is best and 1 is worst) and scales the result to a 0-100 score
        (0 = best, 100 = worst).

        The per-dimension weights are treated as relative and are internally normalized so
        that their sum is 100. Callers may supply any non-negative weights; they do not need
        to sum to 1.0.

        One score per distinct card_name is computed and then propagated to all printings.

        Returns:
            Dict with status and count of cards updated.
        """
        # pick_count is heavily discounted: it's largely redundant with cube_count (both are
        # volume/adoption proxies), and elo already captures in-draft preference in a
        # volume-normalized way, so pick_count mostly adds noise from cards that rack up big
        # totals just by living in one high-traffic cube.
        weights = {
            "w_cube_count": 1,
            "w_edhrec": 1,
            "w_elo": 1,
            "w_pick_count": 0.1,
        }
        start = time.monotonic()
        scale_factor = sum(weights.values()) / 100.0
        weights = {k: v / scale_factor for k, v in weights.items()}
        logger.info("Starting CubeCobra score backfill with weights: %s", weights)

        backfill_sql = db_utils.read_sql("backfill_cubecobra_scores")
        with self.app_context.writer_pool.connection() as conn, conn.cursor() as cursor:
            db_utils.set_statement_timeout(cursor, 600_000)
            cursor.execute(backfill_sql, weights)
            updated_count = cursor.rowcount

            # The percent ranks are computed over cubecobra_elo and friends, which the normal
            # import never populates -- they arrive via ingest_cubecobra. Reporting how many
            # cards actually carry that data distinguishes "ranked the whole corpus" from
            # "ranked a corpus of all-NULLs", which otherwise look identical in the log.
            cursor.execute("SELECT COUNT(*) as count FROM magic.cards WHERE cubecobra_elo IS NOT NULL")
            result = cursor.fetchone()
            cards_with_data = result["count"] if result else 0

            conn.commit()

        stats = {
            "duration_seconds": round(time.monotonic() - start, 2),
            "cards_updated": updated_count,
            "cards_with_cubecobra_data": cards_with_data,
        }
        logger.info("CubeCobra score backfill complete: %s", stats)
        return {
            "status": "success",
            "weights": weights,
            **stats,
        }

    @route()
    def ingest_cubecobra(self, **_: object) -> dict[str, Any]:
        """Fetch card data from CubeCobra and store it in magic.cards.

        Paginates the CubeCobra top-cards API, then updates all matching rows
        in magic.cards (matched on oracle_id). Cards not present in CubeCobra
        are left with NULL values for the cubecobra_* columns.

        Returns:
            Dict with status and count of rows updated.
        """
        logger.info("Starting CubeCobra ingest")
        # fetch the distinct, non-null oracle ids that are in the db
        with self.app_context.writer_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT oracle_id FROM magic.cards WHERE oracle_id IS NOT NULL",
            )
            db_oracle_ids = {r["oracle_id"] for r in cursor.fetchall()}

        cards_updated = 0
        for cubecobra_page in self._fetch_cubecobra_data(db_oracle_ids):
            num_fetched = len(cubecobra_page)
            updated_on_page = self._insert_cubecobra_data(cubecobra_page)
            logger.info("Fetched %d oracle_ids from CubeCobra, updated %d cards", num_fetched, updated_on_page)
            cards_updated += updated_on_page
        logger.info("CubeCobra ingest complete: %d card rows updated", cards_updated)

        backfill_result = self.backfill_cubecobra_scores()
        self._clear_caches()

        return {
            "status": "success",
            "cards_updated": cards_updated,
            "scores_backfilled": backfill_result["cards_updated"],
        }

    def _add_is_tag_to_cards_or_printings(self, *, is_tag: str) -> dict[str, Any]:
        """Add a specific is: tag to all cards or printings matching that tag using Scryfall search.

        Args:
        ----
            is_tag (str): The is: tag to fetch and apply to cards (e.g., 'creature', 'spell').

        Returns:
        -------
            Dict[str, Any]: Result summary with updated card count and tag info.

        """
        # TODO: is tags are not based on card name, but rather specific printing
        # meaning this needs to not use unique on cards, but instead do unique printing
        # which means it's gonna be hella slow

        if not is_tag:
            msg = "is_tag parameter is required"
            raise ValueError(msg)

        if is_tag in BOOLEAN_IS_TAGS:
            return {
                "cards_updated": 0,
                "is_tag": is_tag,
                "message": f"is:{is_tag} is synced automatically from BOOLEAN_IS_TAGS on every import, no manual action needed",
                "total_cards_found": 0,
            }
        if is_tag in CUSTOM_IS_TAGS:
            return self._add_is_tag_to_custom(is_tag=is_tag)
        if is_tag in CARD_IS_TAGS:
            return self._add_is_tag_to_cards(is_tag=is_tag)
        return self._add_is_tag_to_printings(is_tag=is_tag)

    def _add_is_tag_to_custom(self, *, is_tag: str) -> dict[str, Any]:
        """Add a specific is: tag to all custom cards matching that tag using Scryfall search."""
        # these are special cases where you can phrase the tag as a query over other properties
        logger.info("Adding is:%s to custom cards", is_tag)
        return {
            "cards_updated": 0,
            "is_tag": is_tag,
            "message": f"Custom is: tag {is_tag} is not supported",
            "total_cards_found": 0,
        }

    def _add_is_tag_to_cards(self, *, is_tag: str) -> dict[str, Any]:
        """Add a specific is: tag to all cards matching that tag using Scryfall search.

        Args:
        ----
            is_tag (str): The is: tag to fetch and apply to cards (e.g., 'creature', 'spell').

        Returns:
        -------
            Dict[str, Any]: Result summary with updated card count and tag info.

        """
        # Fetch cards with this is: tag from Scryfall API (handles pagination)
        cards = self._scryfall_search(query=f"is:{is_tag}", unique="cards")
        card_names = {c["name"] for c in cards}

        if not cards:
            logger.warning("No cards found with is:%s in Scryfall API", is_tag)
            return {
                "is_tag": is_tag,
                "cards_updated": 0,
                "total_cards_found": 0,
                "message": f"No cards found with is:{is_tag} in Scryfall API",
            }

        logger.info("Updating %d cards with is:%s", len(card_names), is_tag)
        # Update cards in database with the new is: tag
        updated_count = 0
        new_tag = orjson.dumps({is_tag: True}).decode("utf-8")
        with self.app_context.writer_pool.connection() as conn, conn.cursor() as cursor:
            # Use SQL update with jsonb concatenation to add the is: tag
            for card_name_batch in itertools.batched(sorted(card_names), 500):
                cursor.execute(
                    """
                    UPDATE
                        magic.cards
                    SET
                        card_is_tags = card_is_tags || %(new_tag)s::jsonb
                    WHERE
                        card_name = ANY(%(card_names)s) AND
                        not(card_is_tags @> %(new_tag)s::jsonb)
                    """,
                    {
                        "card_names": list(card_name_batch),
                        "new_tag": new_tag,
                    },
                )
                updated_count += cursor.rowcount
                conn.commit()

        return {
            "is_tag": is_tag,
            "cards_updated": updated_count,
            "total_cards_found": len(card_names),
            "message": f"Successfully updated {updated_count} cards with is:{is_tag}",
        }

    def _sync_boolean_is_tags(self, conn: Connection) -> int:
        """Sync the boolean-backed is: tags (BOOLEAN_IS_TAGS) from raw_card_blob.

        Rebuilds each card's managed keys as (existing minus managed) plus the keys whose
        blob-derived expression is true, touching only rows whose result actually differs
        -- so list churn (a card entering or leaving the game-changer roster) converges on
        every import, and unrelated card_is_tags entries are never disturbed.

        The sync is split into hash-scoped chunks so each statement stays within the
        import's statement_timeout even as the corpus grows.

        Args:
        ----
            conn (Connection): open connection; committed here once per chunk.

        Returns:
        -------
            int: rows whose card_is_tags changed.

        """
        updated_count = 0
        sync_sql = _build_boolean_is_tags_sql(BOOLEAN_IS_TAGS)
        with conn.cursor() as cursor:
            for chunk_index in range(_BOOLEAN_IS_TAGS_SYNC_CHUNK_COUNT):
                cursor.execute(
                    sync_sql,
                    {
                        "num_chunks": _BOOLEAN_IS_TAGS_SYNC_CHUNK_COUNT,
                        "chunk_index": chunk_index,
                    },
                )
                updated_count += cursor.rowcount
                conn.commit()
        if updated_count:
            logger.info("Synced boolean is: tags on %d printings", updated_count)
        return updated_count

    def _sync_print_counts(self, conn: Connection) -> int:
        """Sync the count-keyword columns (PRINT_COUNT_COLUMNS) -- per-card counts, on every row.

        Runs after every import because a new printing changes the counts of EVERY row of its
        card: a reprint makes all of that card's older rows `prints` + 1, and only a whole-card
        recount reaches them. Touches only rows whose numbers differ, so a re-import that adds
        nothing writes nothing. See _build_print_counts_sql for the rules.

        Args:
        ----
            conn (Connection): open connection; committed here once per chunk.

        Returns:
        -------
            int: rows whose counts changed.

        """
        updated_count = 0
        sync_sql = _build_print_counts_sql()
        with conn.cursor() as cursor:
            for chunk_index in range(_PRINT_COUNTS_SYNC_CHUNK_COUNT):
                cursor.execute(
                    sync_sql,
                    {
                        "num_chunks": _PRINT_COUNTS_SYNC_CHUNK_COUNT,
                        "chunk_index": chunk_index,
                    },
                )
                updated_count += cursor.rowcount
                conn.commit()
        if updated_count:
            logger.info("Synced print counts on %d printings", updated_count)
        return updated_count

    def _sync_cheapest_codes(self, conn: Connection) -> int:
        """Sync `cheapest_codes` -- each printing's answers to `cheapest:usd` / `:eur` / `:tix`.

        Runs after every import, and not only when a card gains a printing: prices change on
        every import, and one printing's new price changes the answer on the card's OTHER rows
        (yesterday's cheapest is no longer it). Only a whole-card recompute reaches them. Touches
        only rows whose code differs, so a re-import that moved no price writes nothing. See
        _build_cheapest_codes_sql for the rule.

        Args:
        ----
            conn (Connection): open connection; committed here once per chunk.

        Returns:
        -------
            int: rows whose code changed.

        """
        updated_count = 0
        sync_sql = _build_cheapest_codes_sql()
        with conn.cursor() as cursor:
            for chunk_index in range(_CHEAPEST_CODES_SYNC_CHUNK_COUNT):
                cursor.execute(
                    sync_sql,
                    {
                        "num_chunks": _CHEAPEST_CODES_SYNC_CHUNK_COUNT,
                        "chunk_index": chunk_index,
                    },
                )
                updated_count += cursor.rowcount
                conn.commit()
        if updated_count:
            logger.info("Synced cheapest codes on %d printings", updated_count)
        return updated_count

    def _sync_new_rarity(self, conn: Connection) -> int:
        """Sync `new_rarity` -- each printing's answer to Scryfall's `new:rarity`.

        Runs after every import, and not only when a card gains a printing: a new printing can be
        the card's first at its rarity and take the flag from a printing that held it, so only a
        whole-card recompute reaches the rows the import did not touch. Touches only rows whose
        flag differs, so a re-import that moved nothing writes nothing. See _build_new_rarity_sql
        for the rule.

        Args:
        ----
            conn (Connection): open connection; committed here once per chunk.

        Returns:
        -------
            int: rows whose flag changed.

        """
        updated_count = 0
        sync_sql = _build_new_rarity_sql()
        with conn.cursor() as cursor:
            for chunk_index in range(_NEW_RARITY_SYNC_CHUNK_COUNT):
                cursor.execute(
                    sync_sql,
                    {
                        "num_chunks": _NEW_RARITY_SYNC_CHUNK_COUNT,
                        "chunk_index": chunk_index,
                    },
                )
                updated_count += cursor.rowcount
                conn.commit()
        if updated_count:
            logger.info("Synced new:rarity on %d printings", updated_count)
        return updated_count

    def _add_is_tag_to_printings(self, *, is_tag: str) -> dict[str, Any]:
        """Add a specific is: tag to all printings matching that tag using Scryfall search.

        Args:
        ----
            is_tag (str): The is: tag to fetch and apply to printings (e.g., 'creature', 'spell').

        Returns:
        -------
            Dict[str, Any]: Result summary with updated card count and tag info.

        """
        # Fetch cards with this is: tag from Scryfall API (handles pagination)
        printings = self._scryfall_search(query=f"is:{is_tag}", unique="printings")

        if not printings:
            logger.warning("No printings found with is:%s in Scryfall API", is_tag)
            return {
                "is_tag": is_tag,
                "cards_updated": 0,
                "total_cards_found": 0,
                "message": f"No cards found with is:{is_tag} in Scryfall API",
            }

        logger.info("Updating %d printings with is:%s", len(printings), is_tag)
        # Update cards in database with the new is: tag
        updated_count = 0
        new_tag = orjson.dumps({is_tag: True}).decode("utf-8")
        scryfall_ids = {p["id"] for p in printings}
        with self.app_context.writer_pool.connection() as conn, conn.cursor() as cursor:
            # Use SQL update with jsonb concatenation to add the is: tag
            for scryfall_id_batch in itertools.batched(sorted(scryfall_ids), 500):
                cursor.execute(
                    """
                    UPDATE
                        magic.cards
                    SET
                        card_is_tags = card_is_tags || %(new_tag)s::jsonb
                    WHERE
                        scryfall_id = ANY(%(scryfall_ids)s) AND
                        not(card_is_tags @> %(new_tag)s::jsonb)
                    """,
                    {
                        "scryfall_ids": list(scryfall_id_batch),
                        "new_tag": new_tag,
                    },
                )
                updated_count += cursor.rowcount
                conn.commit()

        return {
            "is_tag": is_tag,
            "cards_updated": updated_count,
            "total_cards_found": len(scryfall_ids),
            "message": f"Successfully updated {updated_count} printings with is:{is_tag}",
        }

    @route()
    def discover_is_tags_from_syntax(self, **_: object) -> list[str]:
        """Discover all available is: tags from Scryfall syntax documentation.

        Returns:
        -------
            List[str]: List of all available is: tag names.

        Raises:
        ------
            ValueError: If API request fails or returns invalid data.

        """
        try:
            response = self._session.get("https://scryfall.com/docs/syntax", timeout=30)
            response.raise_for_status()
        except requests.RequestException as e:
            msg = f"Failed to fetch is: tags from Scryfall syntax: {e}"
            raise ValueError(msg) from e

        # Extract is: tag names from the documentation
        # Look for patterns like "is:permanent", "is:spell", etc.
        is_tag_pattern = r"is:([a-zA-Z_-]+)"
        matches = re.findall(is_tag_pattern, response.text)

        # Remove duplicates and sort
        unique_is_tags = sorted({match.lower() for match in matches})

        logger.info("Discovered %d unique is: tags from Scryfall syntax", len(unique_is_tags))
        return unique_is_tags

    @route()
    def import_oracle_tags(self, **_: object) -> dict[str, Any]:
        """Import oracle tags from Scryfall bulk data into oracle_tags, oracle_tag_relationships, and card_oracle_tags."""
        return _import_oracle_tags(self.app_context.writer_pool, self._bulk_data_fetcher)

    @route()
    def import_art_tags(self, **_: object) -> dict[str, Any]:
        """Import art tags from Scryfall bulk data into art_tags, art_tag_relationships, and card_art_tags."""
        return _import_art_tags(self.app_context.writer_pool, self._bulk_data_fetcher)

    @route()
    def import_all_is_tags(self, **_: object) -> dict[str, Any]:
        """Discover and import all is: tags from Scryfall syntax documentation.

        Returns:
        -------
            Dict[str, Any]: Summary of the bulk is: tag import operation.

        """
        result: dict[str, Any] = {
            "success": True,
        }
        logger.info("Starting bulk is: tag discovery and import")

        try:
            all_is_tags = self.discover_is_tags_from_syntax()
        except ValueError as e:
            result.update(
                {
                    "success": False,
                    "error": str(e),
                    "message": "Failed to discover is: tags from Scryfall syntax",
                },
            )
            return result

        if not all_is_tags:
            return {
                "success": False,
                "message": "No is: tags discovered from Scryfall syntax",
            }

        # Import card associations for each is: tag
        start_time = time.monotonic()
        imported_tags = []
        failed_tags = []
        total_cards_updated = 0

        for idx, is_tag in enumerate(all_is_tags):
            try:
                if idx > 0:
                    elapsed_time = time.monotonic() - start_time
                    fraction_complete = idx / len(all_is_tags)
                    estimated_time_remaining = (elapsed_time / fraction_complete) - elapsed_time
                    estimated_duration = datetime.timedelta(seconds=round(estimated_time_remaining, 1))
                    logger.info(
                        "Importing is: tag %d of %d: %20s (ETA: %s)",
                        idx + 1,
                        len(all_is_tags),
                        is_tag,
                        estimated_duration,
                    )

                tag_result = self._add_is_tag_to_cards_or_printings(is_tag=is_tag)
                imported_tags.append(
                    {
                        "is_tag": is_tag,
                        "cards_updated": tag_result["cards_updated"],
                        "total_cards_found": tag_result["total_cards_found"],
                    },
                )
                total_cards_updated += tag_result["cards_updated"]

            except ValueError as e:
                logger.warning("Failed to import is: tag '%s': %s", is_tag, e)
                failed_tags.append({"is_tag": is_tag, "error": str(e)})

        result.update(
            {
                "duration": time.monotonic() - start_time,
                "discovered_is_tags": len(all_is_tags),
                "imported_is_tags": len(imported_tags),
                "failed_is_tags": len(failed_tags),
                "total_cards_updated": total_cards_updated,
                "imported_tags": imported_tags,
                "failed_tags": failed_tags,
                "message": f"Successfully imported {len(imported_tags)} is: tags, {len(failed_tags)} failed",
            },
        )

        return result

    @route()
    def import_card_by_name(
        self,
        *,
        card_name: str,
        **_: object,
    ) -> dict[str, Any]:
        """Import a single card by name from Scryfall API.

        Args:
        ----
            card_name (str): The exact name of the card to import.

        Returns:
        -------
            Dict[str, Any]: Result summary with import status and card info.

        """
        if not card_name:
            msg = "card_name parameter is required"
            raise ValueError(msg)

        logger.info("Importing card by name: '%s'", card_name)

        # Check if card already exists in database for backward compatibility
        with self.app_context.writer_pool.connection() as conn, conn.cursor() as cursor:
            db_utils.set_statement_timeout(cursor, 10_000)
            cursor.execute(
                "SELECT card_name FROM magic.cards WHERE card_name = %(card_name)s",
                {"card_name": card_name},
            )
            card_already_exists = cursor.fetchone() is not None

        if card_already_exists:
            return {
                "card_name": card_name,
                "status": "already_exists",
                "message": f"Card '{card_name}' already exists in database",
            }

        # Use import_cards_by_search with exact name query
        return self.import_cards_by_search(search_query=f'!"{card_name}"')

    @route()
    def import_cards_by_search(
        self,
        *,
        search_query: str,
        **_: object,
    ) -> dict[str, Any]:
        """Import cards from Scryfall API using any search query.

        Args:
        ----
            search_query (str): The Scryfall search query to execute.

        Returns:
        -------
            Dict[str, Any]: Result summary with import status and card info.

        """
        if not search_query:
            msg = "search_query parameter is required"
            raise ValueError(msg)

        logger.info("Importing cards by search: '%s'", search_query)

        # Fetch card data from Scryfall API using the provided search query
        try:
            cards = self._scryfall_search(query=search_query)
            if not cards:
                return {
                    "search_query": search_query,
                    "status": "not_found",
                    "message": f"No cards found for search query '{search_query}' in Scryfall API",
                    "cards_loaded": 0,
                }

        except (requests.RequestException, ValueError, KeyError) as e:
            logger.error("Error fetching cards for search '%s' from Scryfall: %s", search_query, e)
            return {
                "search_query": search_query,
                "status": "error",
                "message": f"Error fetching cards from Scryfall: {e}",
                "cards_loaded": 0,
            }

        # Insert the cards into the database using the consolidated method
        load_result = self._upsert_cards(cards)

        if load_result["status"] == "success":
            self.app_context.reload_engine(force=True)

        # Add search_query to the result for consistency
        load_result["search_query"] = search_query

        return load_result

    def _scryfall_search(self, *, query: str, unique: str = "prints") -> list[dict[str, Any]]:
        """Search Scryfall API for cards matching the given query.

        This method handles pagination to get the complete list of cards and
        automatically applies filters for paper format and format legality.

        Args:
        ----
            query (str): The search query string for Scryfall.
            unique (str): The unique parameter to pass to the Scryfall API.

        Returns:
        -------
            List[Dict[str, Any]]: List of card data from Scryfall API.

        Raises:
        ------
            ValueError: If API request fails or returns invalid data.

        """
        # Add standard filters for paper format and format legality
        # Wrap original query in parentheses to ensure proper filter application
        filters = [
            "(f:m or f:l or f:c or f:v)",
            "game:paper",
            f"unique:{unique}",
        ]
        full_query = f"({query}) {' '.join(filters)}"

        base_url = "https://api.scryfall.com/cards/search"
        params = {"q": full_query, "format": "json"}
        all_cards = []

        total_cards = "?"
        try:
            while True:
                time.sleep(1 / 10)  # Rate limiting - 10 requests per second max
                logger.info(
                    "Making request to Scryfall API: %s %s (have %d of %s total cards)",
                    base_url,
                    params,
                    len(all_cards),
                    total_cards,
                )
                response = self._session.get(base_url, params=params, timeout=30)
                response.raise_for_status()
                data = orjson.loads(response.content)

                total_cards = data.get("total_cards", 1) or 1

                if "data" not in data:
                    break

                # Extract card data from current page
                page_cards = [card for card in data["data"] if card]
                all_cards.extend(page_cards)

                # Check if there are more pages
                if not data.get("has_more", False):
                    break

                # Get next page URL
                next_page = data.get("next_page")
                if not next_page:
                    break

                # Update base_url and clear params for next page
                base_url = next_page
                params = {}

        except requests.RequestException as oops:
            # Check if it's a 404 error - return empty list
            if (hasattr(oops, "response") and oops.response and oops.response.status_code == NOT_FOUND) or "404" in str(oops):
                return all_cards
            msg = f"Failed to fetch data from Scryfall API: {oops}"
            raise ValueError(msg) from oops

        return all_cards

    def _upsert_cards(
        self,
        cards: Iterable[dict[str, Any]],
        page_size: int = _UPSERT_PAGE_SIZE,
    ) -> dict[str, Any]:
        """Preprocess and upsert an iterable of raw card dicts into magic.cards.

        Preprocessing is applied lazily as cards flow through, so the full dataset
        is never held in memory. Each batch is upserted via bulk_upsert: new rows
        are inserted, changed rows are updated, and unchanged rows are skipped.

        Returns a dict with:
            - cards_inserted: new cards added
            - cards_updated: existing cards with changed data
            - cards_loaded: cards_inserted + cards_updated
            - cards_sent: rows attempted (after preprocessing)
            - status: "success", "no_cards_before_preprocessing", "no_cards_after_preprocessing", "database_error"
            - message: descriptive message
        """
        self.setup_schema()

        try:
            with self.app_context.writer_pool.connection() as conn:
                with conn.cursor() as cursor:
                    db_utils.set_statement_timeout(cursor, 30_000)

                class _CardStream:
                    """Preprocesses raw cards lazily, tracking stage counts."""

                    def __init__(self) -> None:
                        self.raw = 0
                        self.preprocessed = 0

                    def __iter__(self) -> Iterator[dict[str, Any]]:
                        for card in cards:
                            self.raw += 1
                            for processed in preprocess_card(card):
                                self.preprocessed += 1
                                yield processed

                stream = _CardStream()
                cards_inserted = cards_updated = cards_sent = 0

                for page in itertools.batched(stream, page_size):
                    batch = _bulk_upsert(
                        conn,
                        "cards",
                        list(page),
                        schema="magic",
                        conflict_target=["scryfall_id"],
                        skip_columns=["card_oracle_tags", "card_art_tags", "card_is_tags"],
                    )
                    cards_sent += len(page)
                    cards_inserted += batch["inserted"]
                    cards_updated += batch["updated"]
                    logger.info(
                        "%d inserted, %d updated, %d sent so far",
                        cards_inserted,
                        cards_updated,
                        cards_sent,
                    )

                conn.commit()

                if cards_sent:
                    self._sync_boolean_is_tags(conn)
                    self._sync_print_counts(conn)
                    self._sync_cheapest_codes(conn)
                    self._sync_new_rarity(conn)

                if cards_sent == 0:
                    if stream.raw == 0:
                        status, message = "no_cards_before_preprocessing", "No cards provided for loading"
                    else:
                        status, message = "no_cards_after_preprocessing", "No cards remaining after preprocessing"
                    logger.info("No cards imported: %s (raw=%d preprocessed=%d)", message, stream.raw, stream.preprocessed)
                    return {"status": status, "cards_loaded": 0, "cards_sent": 0, "message": message}

                cards_loaded = cards_inserted + cards_updated
                self._clear_caches()
                return {
                    "status": "success",
                    "cards_inserted": cards_inserted,
                    "cards_updated": cards_updated,
                    "cards_loaded": cards_loaded,
                    "cards_sent": cards_sent,
                    "message": f"Successfully loaded {cards_loaded} cards ({cards_inserted} new, {cards_updated} updated)",
                }

        except (psycopg.Error, ValueError, KeyError) as e:
            logger.exception("Error loading cards")
            return {
                "status": "database_error",
                "cards_loaded": 0,
                "cards_sent": 0,
                "message": f"Error loading cards: {type(e).__name__}: {e}",
            }

    def _clear_caches(self) -> None:
        self.app_context.bump_cache_generation()
