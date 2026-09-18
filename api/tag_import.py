"""Import oracle and art tags from Scryfall bulk data."""

from __future__ import annotations

import collections
import itertools
import logging
import time
from typing import TYPE_CHECKING

from psycopg import sql
from psycopg.types.json import Jsonb

from api.parsing.card_query_nodes import slugify_tag
from api.scryfall_bulk_data_fetcher import BulkDataKey, ScryfallBulkDataFetcher

if TYPE_CHECKING:
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


def _build_slug_to_aliases(tags: list[dict]) -> dict[str, frozenset[str]]:
    """Return a map from each tag slug to the alias spellings that stand for it.

    Aliases are alternate spellings Scryfall's tagger resolves to the tag before matching, which is
    why `art:flames` finds the `fire` tag on Scryfall. They are slugified here so that both written
    forms reach the same stored key -- half the art aliases are spelled with spaces ("open mouth",
    an alias of `loose-lips`), and `slugify_tag` normalizes the search term the same way.

    An alias that lands on a real tag slug, or that two tags both claim, is ambiguous, so it is
    dropped rather than guessed at: the slug always wins. Neither dump contains one today (checked
    against both), so this is a guard against upstream data changing, not a live case.
    """
    slugs = {tag["slug"] for tag in tags}
    claimants: dict[str, set[str]] = collections.defaultdict(set)
    for tag in tags:
        for alias in tag.get("aliases") or []:
            slugified = slugify_tag(alias)
            if slugified:
                claimants[slugified].add(tag["slug"])

    slug_to_aliases: dict[str, set[str]] = collections.defaultdict(set)
    dropped = []
    for alias, claiming_slugs in claimants.items():
        if alias in slugs or len(claiming_slugs) > 1:
            dropped.append(alias)
            continue
        slug_to_aliases[next(iter(claiming_slugs))].add(alias)

    if dropped:
        logger.warning("Dropping %d ambiguous tag aliases: %s", len(dropped), sorted(dropped))

    return {slug: frozenset(aliases) for slug, aliases in slug_to_aliases.items()}


def _build_tag_keys(tags: list[dict], uuid_to_slug: dict[str, str]) -> dict[str, frozenset[str]]:
    """Return every key a card tagged with a given slug should carry in its tag column.

    That is the slug itself, all of its ancestors (see _build_all_ancestors), and the aliases of
    each of those. Aliases have to ride along on the ancestors too, because Scryfall resolves an
    alias to its tag *before* expanding the hierarchy: `art:flames` returns exactly what `art:fire`
    does, descendants included, not just the artworks tagged `fire` directly.
    """
    all_ancestors = _build_all_ancestors(tags, uuid_to_slug)
    slug_to_aliases = _build_slug_to_aliases(tags)

    tag_keys: dict[str, frozenset[str]] = {}
    for slug, ancestors in all_ancestors.items():
        keys = {slug, *ancestors}
        tag_keys[slug] = frozenset(keys.union(*(slug_to_aliases.get(key, frozenset()) for key in keys)))

    return tag_keys


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


def import_oracle_tags(
    conn_pool: psycopg_pool.ConnectionPool,
    bulk_data_fetcher: ScryfallBulkDataFetcher,
) -> dict:
    """Download oracle tag bulk data and sync oracle_tags, oracle_tag_relationships, and card_oracle_tags."""
    start = time.monotonic()
    logger.info("Downloading oracle tags bulk data")
    tags = list(bulk_data_fetcher.stream_data_for_key(BulkDataKey.ORACLE_TAGS))
    uuid_to_slug = _build_uuid_to_slug(tags)
    tag_keys = _build_tag_keys(tags, uuid_to_slug)

    oracle_id_to_tags: dict[str, dict[str, bool]] = {}
    for tag in tags:
        keys = tag_keys.get(tag["slug"], frozenset({tag["slug"]}))
        for tagging in tag.get("taggings", []):
            oid = tagging.get("oracle_id")
            if oid:
                card_tags = oracle_id_to_tags.setdefault(oid, {})
                for key in keys:
                    card_tags[key] = True

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
    tag_keys = _build_tag_keys(tags, uuid_to_slug)

    illustration_id_to_tags: dict[str, dict[str, bool]] = {}
    for tag in tags:
        keys = tag_keys.get(tag["slug"], frozenset({tag["slug"]}))
        for tagging in tag.get("taggings", []):
            iid = tagging.get("illustration_id")
            if iid:
                card_tags = illustration_id_to_tags.setdefault(iid, {})
                for key in keys:
                    card_tags[key] = True

    logger.info("Syncing %d art tags covering %d illustrations", len(tags), len(illustration_id_to_tags))
    with conn_pool.connection() as conn:
        _sync_hierarchy(conn, "art_tags", "art_tag_relationships", tags, uuid_to_slug)
        cards_updated, cards_cleared = _sync_card_tags(conn, "illustration_id", "card_art_tags", illustration_id_to_tags)

    result = {
        "duration_seconds": round(time.monotonic() - start, 2),
        "tags_imported": len(tags),
        "cards_with_tags": len(illustration_id_to_tags),
        "cards_updated": cards_updated,
        "cards_cleared": cards_cleared,
    }
    logger.info("Art tag import complete: %s", result)
    return result
