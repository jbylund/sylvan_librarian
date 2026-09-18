"""Import oracle and art tags from Scryfall bulk data."""

from __future__ import annotations

import itertools
import logging
import time
from typing import TYPE_CHECKING

from psycopg import sql
from psycopg.types.json import Jsonb

from api.scryfall_bulk_data_fetcher import BulkDataKey, ScryfallBulkDataFetcher

if TYPE_CHECKING:
    from collections.abc import Iterable

    import psycopg
    import psycopg_pool

logger = logging.getLogger(__name__)


def _build_uuid_to_slug(tags: list[dict]) -> dict[str, str]:
    return {tag["id"]: tag["slug"] for tag in tags}


def _build_all_ancestors(tags: list[dict], uuid_to_slug: dict[str, str]) -> dict[str, frozenset[str]]:
    """Return a map from each slug to the set of all its ancestor slugs (parents, grandparents, etc.).

    Scryfall tag hierarchies have parent = broader category, child = more specific. A search for
    a parent tag should match cards tagged with any descendant, which we achieve by storing all
    ancestor slugs on each card at import time (ancestor propagation / denormalization).
    """
    slug_to_parent_slugs: dict[str, set[str]] = {}
    for tag in tags:
        slug = uuid_to_slug.get(tag["id"])
        if not slug:
            continue
        slug_to_parent_slugs[slug] = {uuid_to_slug[pid] for pid in tag.get("parent_ids", []) if pid in uuid_to_slug}

    result: dict[str, frozenset[str]] = {}
    for slug in slug_to_parent_slugs:
        if slug in result:
            continue
        ancestors: set[str] = set()
        queue = list(slug_to_parent_slugs.get(slug, set()))
        visited: set[str] = {slug}
        while queue:
            current = queue.pop()
            if current in visited:
                continue
            visited.add(current)
            ancestors.add(current)
            queue.extend(slug_to_parent_slugs.get(current, set()) - visited)
        result[slug] = frozenset(ancestors)

    return result


def _sync_hierarchy(
    conn: psycopg.Connection,
    tags_table: str,
    relationships_table: str,
    tags: list[dict],
    uuid_to_slug: dict[str, str],
) -> None:
    tags_ident = sql.Identifier("magic", tags_table)
    rels_ident = sql.Identifier("magic", relationships_table)

    with conn.cursor() as cursor:
        cursor.execute(sql.SQL("DELETE FROM {}").format(rels_ident))
        cursor.execute(sql.SQL("DELETE FROM {}").format(tags_ident))

        cursor.executemany(
            sql.SQL("INSERT INTO {} (tag) VALUES (%(tag)s)").format(tags_ident),
            [{"tag": tag["slug"]} for tag in tags],
        )

        pairs = []
        for tag in tags:
            child_slug = tag["slug"]
            for parent_id in tag.get("parent_ids", []):
                parent_slug = uuid_to_slug.get(parent_id)
                if parent_slug and parent_slug != child_slug:
                    pairs.append({"child_tag": child_slug, "parent_tag": parent_slug})

        if pairs:
            cursor.executemany(
                sql.SQL(
                    "INSERT INTO {} (child_tag, parent_tag) VALUES (%(child_tag)s, %(parent_tag)s) ON CONFLICT DO NOTHING"
                ).format(rels_ident),
                pairs,
            )

        conn.commit()


def _sync_card_tags(
    conn: psycopg.Connection,
    id_column: str,
    tag_column: str,
    id_to_tags: dict[str, dict[str, bool]],
) -> tuple[int, int]:
    """Update one card tag column. Returns (cards_updated, cards_cleared)."""
    id_col = sql.Identifier(id_column)
    tag_col = sql.Identifier(tag_column)

    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL("SELECT {id} FROM magic.cards WHERE {tag} != '{{}}' AND {id} IS NOT NULL").format(id=id_col, tag=tag_col)
        )
        tagged_in_db = {str(r[id_column]) for r in cursor.fetchall()}
        to_clear = list(tagged_in_db - set(id_to_tags))
        if to_clear:
            cursor.execute(
                sql.SQL("UPDATE magic.cards SET {tag} = '{{}}' WHERE {id} = ANY(%s::uuid[])").format(id=id_col, tag=tag_col),
                [to_clear],
            )
        conn.commit()

        cards_updated = 0
        records = [{"id": id_, "tags": tags} for id_, tags in id_to_tags.items()]
        for batch in itertools.batched(records, 5000):
            cursor.execute(
                sql.SQL(
                    """
                    WITH incoming AS (
                        SELECT * FROM jsonb_to_recordset(%(records)s) AS t(id uuid, tags jsonb)
                    )
                    UPDATE magic.cards
                    SET {tag} = incoming.tags
                    FROM incoming
                    WHERE magic.cards.{id} = incoming.id
                      AND magic.cards.{tag} IS DISTINCT FROM incoming.tags
                    """
                ).format(id=id_col, tag=tag_col),
                {"records": Jsonb(list(batch))},
            )
            cards_updated += cursor.rowcount
            conn.commit()

    return cards_updated, len(to_clear)


def _fetch_illustrations_shown(conn: psycopg.Connection) -> list[tuple[str, list[str]]]:
    """Return (scryfall_id, illustration_ids) for every card that shows any illustration.

    Narrow on purpose: `illustration_ids` is a small maintained column (api/card_processing.py), so
    this reads no `raw_card_blob` and detoasts nothing. Cards showing no illustration are excluded
    because they can never carry an art tag, which keeps the result at the size of the tagged
    corpus rather than the whole table.
    """
    with conn.cursor() as cursor:
        cursor.execute("SELECT scryfall_id, illustration_ids FROM magic.cards WHERE illustration_ids <> '[]'::jsonb")
        return [(str(row["scryfall_id"]), row["illustration_ids"]) for row in cursor.fetchall()]


def _union_art_tags(
    illustrations_shown: Iterable[tuple[str, list[str]]],
    illustration_id_to_tags: dict[str, dict[str, bool]],
) -> dict[str, dict[str, bool]]:
    """Map each card to the UNION of the art tags of every illustration it shows.

    A printing shows all of its art, so the tags on it are every illustration's, not the front
    face's alone. Measured against api.scryfall.com on 2026-08-16: `arttag:snow e:khm` is 75 there
    and was 73 with the front-only reading -- Birgi // Harnfel and Esika // The Prismatic Bridge
    carry their snow on the BACK face's art -- and `-art:human e:khm t:creature` is 135 there
    against 136, the surplus being Valki // Tibalt, whose human is Tibalt and whose Tibalt is the
    back.

    Cards with no tagged illustration are omitted rather than mapped to `{}`: _sync_card_tags reads
    absence as "clear this row", which is the same outcome and a much smaller payload.

    The overwhelming majority of rows show exactly ONE illustration, and those reference the
    existing tag dict rather than copying it -- only a genuinely multi-illustration row (9,368 of
    them on the 2026-08-16 bulk, of which 5,491 gain a tag from a non-front face) allocates.
    """
    card_id_to_tags: dict[str, dict[str, bool]] = {}
    for scryfall_id, illustration_ids in illustrations_shown:
        tagged = [tags for tags in (illustration_id_to_tags.get(iid) for iid in illustration_ids) if tags]
        if not tagged:
            continue
        if len(tagged) == 1:
            card_id_to_tags[scryfall_id] = tagged[0]
            continue
        union: dict[str, bool] = {}
        for tags in tagged:
            union.update(tags)
        card_id_to_tags[scryfall_id] = union
    return card_id_to_tags


def import_oracle_tags(
    conn_pool: psycopg_pool.ConnectionPool,
    bulk_data_fetcher: ScryfallBulkDataFetcher,
) -> dict:
    """Download oracle tag bulk data and sync oracle_tags, oracle_tag_relationships, and card_oracle_tags."""
    start = time.monotonic()
    logger.info("Downloading oracle tags bulk data")
    tags = list(bulk_data_fetcher.stream_data_for_key(BulkDataKey.ORACLE_TAGS))
    uuid_to_slug = _build_uuid_to_slug(tags)
    all_ancestors = _build_all_ancestors(tags, uuid_to_slug)

    oracle_id_to_tags: dict[str, dict[str, bool]] = {}
    for tag in tags:
        slug = tag["slug"]
        for tagging in tag.get("taggings", []):
            oid = tagging.get("oracle_id")
            if oid:
                card_tags = oracle_id_to_tags.setdefault(oid, {})
                card_tags[slug] = True
                for ancestor in all_ancestors.get(slug, frozenset()):
                    card_tags[ancestor] = True

    logger.info("Syncing %d oracle tags covering %d cards", len(tags), len(oracle_id_to_tags))
    with conn_pool.connection() as conn:
        _sync_hierarchy(conn, "oracle_tags", "oracle_tag_relationships", tags, uuid_to_slug)
        cards_updated, cards_cleared = _sync_card_tags(conn, "oracle_id", "card_oracle_tags", oracle_id_to_tags)

    result = {
        "duration_seconds": round(time.monotonic() - start, 2),
        "tags_imported": len(tags),
        "cards_with_tags": len(oracle_id_to_tags),
        "cards_updated": cards_updated,
        "cards_cleared": cards_cleared,
    }
    logger.info("Oracle tag import complete: %s", result)
    return result


def import_art_tags(
    conn_pool: psycopg_pool.ConnectionPool,
    bulk_data_fetcher: ScryfallBulkDataFetcher,
) -> dict:
    """Download art tag bulk data and sync art_tags, art_tag_relationships, and card_art_tags."""
    start = time.monotonic()
    logger.info("Downloading art tags bulk data")
    tags = list(bulk_data_fetcher.stream_data_for_key(BulkDataKey.ART_TAGS))
    uuid_to_slug = _build_uuid_to_slug(tags)
    all_ancestors = _build_all_ancestors(tags, uuid_to_slug)

    illustration_id_to_tags: dict[str, dict[str, bool]] = {}
    for tag in tags:
        slug = tag["slug"]
        for tagging in tag.get("taggings", []):
            iid = tagging.get("illustration_id")
            if iid:
                card_tags = illustration_id_to_tags.setdefault(iid, {})
                card_tags[slug] = True
                for ancestor in all_ancestors.get(slug, frozenset()):
                    card_tags[ancestor] = True

    logger.info("Syncing %d art tags covering %d illustrations", len(tags), len(illustration_id_to_tags))
    with conn_pool.connection() as conn:
        _sync_hierarchy(conn, "art_tags", "art_tag_relationships", tags, uuid_to_slug)
        # Keyed on scryfall_id, not illustration_id: a card's tags are the union over the
        # illustrations it shows (see _union_art_tags), so the row -- not the illustration -- is
        # the only thing a single incoming record can fully determine. Resolving the union here
        # rather than in SQL is also what keeps _sync_card_tags batchable: a batch of illustration
        # ids can split a card's illustrations across two statements, a batch of cards cannot.
        card_id_to_tags = _union_art_tags(_fetch_illustrations_shown(conn), illustration_id_to_tags)
        cards_updated, cards_cleared = _sync_card_tags(conn, "scryfall_id", "card_art_tags", card_id_to_tags)

    result = {
        "duration_seconds": round(time.monotonic() - start, 2),
        "tags_imported": len(tags),
        "illustrations_with_tags": len(illustration_id_to_tags),
        "cards_with_tags": len(card_id_to_tags),
        "cards_updated": cards_updated,
        "cards_cleared": cards_cleared,
    }
    logger.info("Art tag import complete: %s", result)
    return result
