"""Tests for _upsert_cards and streaming import wiring."""

from __future__ import annotations

import logging
import multiprocessing
import uuid
from unittest.mock import patch

import psycopg
import pytest

from api.admin_resource import (
    PRINT_COUNT_COLUMNS,
    AdminResource,
    _build_boolean_is_tags_sql,
    _build_cheapest_codes_sql,
    _build_new_rarity_sql,
    _build_print_counts_sql,
)
from api.api_resource import APIResource
from api.card_processing import preprocess_card
from api.db.bulk_upsert import bulk_upsert
from api.parsing import QueryContext
from api.parsing.card_query_nodes import CheapestNode, NewNode
from api.release_batches import RELEASE_BATCHES
from api.scryfall_bulk_data_fetcher import BulkDataKey
from api.tests.helpers import make_raw_card
from api.tests.support import mock_app_context
from api.utils.db_utils import get_migrations

# ---------------------------------------------------------------------------
# Status-code tests
# ---------------------------------------------------------------------------


class TestUpsertCardsStatus:
    """_upsert_cards returns the correct status string for each no-cards scenario."""

    def test_empty_list_returns_no_cards_before_preprocessing(self, api_resource: APIResource) -> None:
        result = api_resource.admin._upsert_cards([])
        assert result["status"] == "no_cards_before_preprocessing"
        assert result["cards_loaded"] == 0
        assert result["cards_sent"] == 0

    def test_empty_generator_returns_no_cards_before_preprocessing(self, api_resource: APIResource) -> None:
        result = api_resource.admin._upsert_cards(x for x in [])
        assert result["status"] == "no_cards_before_preprocessing"

    def test_preprocessing_filters_all_cards_returns_no_cards_after_preprocessing(self, api_resource: APIResource) -> None:
        """When preprocess_card returns [] for all inputs, status is no_cards_after_preprocessing."""
        with patch("api.admin_resource.preprocess_card", return_value=[]):
            result = api_resource.admin._upsert_cards([make_raw_card()])
        assert result["status"] == "no_cards_after_preprocessing"
        assert result["cards_loaded"] == 0

    def test_unchanged_card_on_reimport_loads_zero(self, api_resource: APIResource) -> None:
        """Re-submitting an identical card produces success with zero loads (unchanged, no write)."""
        card = make_raw_card(name="Already Present Card")
        api_resource.admin._upsert_cards([card])  # first insert

        result = api_resource.admin._upsert_cards([card])  # second attempt
        assert result["status"] == "success"
        assert result["cards_loaded"] == 0

    def test_success_result_includes_cards_sent(self, api_resource: APIResource) -> None:
        result = api_resource.admin._upsert_cards([make_raw_card(name="Cards Sent Test")])
        assert result["status"] == "success"
        assert result["cards_sent"] >= 1
        assert "cards_loaded" in result


# ---------------------------------------------------------------------------
# Boolean-backed is: tags (reserved / game_changer)
# ---------------------------------------------------------------------------


class TestBuildBooleanIsTagsSql:
    """_build_boolean_is_tags_sql always binds chunk scope via query parameters."""

    def test_sql_uses_bound_chunk_parameters(self) -> None:
        sql = _build_boolean_is_tags_sql({"reserved": "cards.raw_card_blob->'reserved' = 'true'::jsonb"})
        assert "jsonb_build_object" in sql
        assert "jsonb_strip_nulls" in sql
        assert "hashtext(cards.scryfall_id::text)" in sql
        assert "%(num_chunks)s" in sql
        assert "%(chunk_index)s" in sql
        assert "jsonb_object_agg" not in sql


def _is_tags_for(api_resource: APIResource, scryfall_id: str) -> dict:
    with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT card_is_tags FROM magic.cards WHERE scryfall_id = %(sid)s",
            {"sid": scryfall_id},
        )
        row = cursor.fetchone()
    return row["card_is_tags"] if row else {}


class TestBooleanIsTags:
    """reserved/game_changer booleans on bulk cards sync into card_is_tags both ways."""

    def test_reserved_boolean_lands_as_is_tag(self, api_resource: APIResource) -> None:
        card = make_raw_card(name="Reserved Import Test")
        card["reserved"] = True
        api_resource.admin._upsert_cards([card])
        assert _is_tags_for(api_resource, card["id"]).get("reserved") is True

    def test_game_changer_boolean_lands_as_gamechanger(self, api_resource: APIResource) -> None:
        card = make_raw_card(name="Bracket Import Test")
        card["game_changer"] = True
        api_resource.admin._upsert_cards([card])
        tags = _is_tags_for(api_resource, card["id"])
        assert tags.get("gamechanger") is True
        assert "reserved" not in tags

    def test_flag_removal_strips_the_tag(self, api_resource: APIResource) -> None:
        # A card leaving the game-changer roster must lose the tag on reimport.
        # Each import builds a FRESH dict, as the real bulk stream does --
        # preprocess_card embeds a raw_card_blob snapshot into the dict it is
        # given and short-circuits dicts that already carry one, so reusing
        # the first import's object would re-store the stale blob.
        card = make_raw_card(name="Debracketed Test")
        card["game_changer"] = True
        api_resource.admin._upsert_cards([card])
        reimport = make_raw_card(card_id=card["id"], name="Debracketed Test")
        reimport["oracle_text"] = "changed so the reimport writes"
        api_resource.admin._upsert_cards([reimport])
        assert "gamechanger" not in _is_tags_for(api_resource, card["id"])

    def test_sync_preserves_unrelated_is_tags(self, api_resource: APIResource) -> None:
        card = make_raw_card(name="Historic Bystander Test")
        card["reserved"] = True
        api_resource.admin._upsert_cards([card])
        with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                """UPDATE magic.cards SET card_is_tags = card_is_tags || '{"historic": true}'::jsonb
                   WHERE scryfall_id = %(sid)s""",
                {"sid": card["id"]},
            )
            conn.commit()
        reimport = make_raw_card(card_id=card["id"], name="Historic Bystander Test")
        reimport["reserved"] = True
        reimport["oracle_text"] = "changed so the reimport writes"
        api_resource.admin._upsert_cards([reimport])
        tags = _is_tags_for(api_resource, card["id"])
        assert tags.get("historic") is True
        assert tags.get("reserved") is True

    def test_plain_boolean_lands_as_is_tag(self, api_resource: APIResource) -> None:
        """story_spotlight -> spotlight, same top-level-boolean shape as reserved/gamechanger."""
        card = make_raw_card(name="Spotlight Import Test")
        card["story_spotlight"] = True
        api_resource.admin._upsert_cards([card])
        assert _is_tags_for(api_resource, card["id"]).get("spotlight") is True

    @pytest.mark.parametrize(
        "mana_cost",
        [
            "{R/G}",  # the ten two-colour symbols
            "{2/W}",  # the twobrid cycle -- 19 of Scryfall's is:hybrid cards have only these
            "{C/U}",  # colourless-hybrid -- 1 card
            "{G/W/P}",  # Phyrexian-hybrid -- 4 cards
        ],
    )
    def test_every_hybrid_family_lands_as_is_tag(self, api_resource: APIResource, mana_cost: str) -> None:
        """Hybrid reads the front face's cost, and counts all FOUR hybrid families.

        A regex, not a rewrite, per docs/issues/done/00713-is-tag-recovery.md (an open, growing
        symbol set makes an enumerated rewrite brittle) -- but the regex has to be as wide as the
        set it stands in for. Reading only `{W/U}`-style symbols answered 569 of Scryfall's 603.
        """
        card = make_raw_card(name=f"Hybrid Mana Import Test {mana_cost}")
        card["mana_cost"] = mana_cost
        api_resource.admin._upsert_cards([card])
        tags = _is_tags_for(api_resource, card["id"])
        assert tags.get("hybrid") is True

    def test_colourless_phyrexian_is_not_hybrid(self, api_resource: APIResource) -> None:
        """`{C/P}` is Phyrexian, not hybrid, and Scryfall agrees: `is:hybrid o:"{c/p}"` is empty."""
        card = make_raw_card(name="Colourless Phyrexian Import Test")
        card["mana_cost"] = "{C/P}"
        api_resource.admin._upsert_cards([card])
        tags = _is_tags_for(api_resource, card["id"])
        assert "hybrid" not in tags
        assert tags.get("phyrexian") is True

    def test_phyrexian_mana_symbol_lands_as_is_tag(self, api_resource: APIResource) -> None:
        card = make_raw_card(name="Phyrexian Mana Import Test")
        card["mana_cost"] = "{W/P}"
        api_resource.admin._upsert_cards([card])
        tags = _is_tags_for(api_resource, card["id"])
        assert tags.get("phyrexian") is True
        assert "hybrid" not in tags

    def test_phyrexian_is_anywhere_on_the_card_not_only_the_cost(self, api_resource: APIResource) -> None:
        """The cost is the SMALLER half: 36 of Scryfall's 73 carry the symbol in rules text only.

        Reading `mana_cost_text` alone answers 33 of the 73 -- Spellskite, the Souleaters and every
        `{2}{B/P}: transform` back face put the symbol in rules text and nowhere else.
        """
        card = make_raw_card(name="Phyrexian In Rules Text")
        card["mana_cost"] = "{2}{U}"
        card["oracle_text"] = "{W/P}: Draw a card."
        api_resource.admin._upsert_cards([card])
        assert _is_tags_for(api_resource, card["id"]).get("phyrexian") is True

    def test_promo_types_membership_lands_as_is_tag(self, api_resource: APIResource) -> None:
        card = make_raw_card(name="FNM Import Test")
        card["promo_types"] = ["fnm"]
        api_resource.admin._upsert_cards([card])
        assert _is_tags_for(api_resource, card["id"]).get("fnm") is True

    def test_promo_types_absent_does_not_set_unrelated_tags(self, api_resource: APIResource) -> None:
        card = make_raw_card(name="Instore Import Test")
        card["promo_types"] = ["instore"]
        api_resource.admin._upsert_cards([card])
        tags = _is_tags_for(api_resource, card["id"])
        assert tags.get("instore") is True
        assert "fnm" not in tags
        assert "buyabox" not in tags

    def test_partner_keyword_lands_as_is_tag(self, api_resource: APIResource) -> None:
        card = make_raw_card(name="Partner Import Test")
        card["keywords"] = ["Partner"]
        api_resource.admin._upsert_cards([card])
        assert _is_tags_for(api_resource, card["id"]).get("partner") is True

    def test_partner_with_keyword_alone_does_not_set_partner(self, api_resource: APIResource) -> None:
        """Verify the sync itself, not the corpus assumption it relies on.

        Real bulk data always pairs "Partner with" alongside a plain "Partner" keyword
        (verified against the corpus); a card carrying only "Partner with" is not tagged,
        so `is:partner` stays exact rather than papering over a blob that turns out not to
        follow the usual pairing.
        """
        card = make_raw_card(name="Partner With Alone Test")
        card["keywords"] = ["Partner with"]
        api_resource.admin._upsert_cards([card])
        assert "partner" not in _is_tags_for(api_resource, card["id"])

    def test_etched_finish_lands_as_is_tag(self, api_resource: APIResource) -> None:
        card = make_raw_card(name="Etched Import Test")
        card["finishes"] = ["nonfoil", "etched"]
        api_resource.admin._upsert_cards([card])
        assert _is_tags_for(api_resource, card["id"]).get("etched") is True

    def test_masterpiece_set_type_lands_as_is_tag(self, api_resource: APIResource) -> None:
        card = make_raw_card(name="Masterpiece Import Test")
        card["set_type"] = "masterpiece"
        api_resource.admin._upsert_cards([card])
        assert _is_tags_for(api_resource, card["id"]).get("masterpiece") is True

    def test_scryfallpreview_source_lands_as_is_tag(self, api_resource: APIResource) -> None:
        card = make_raw_card(name="Scryfall Preview Import Test")
        card["preview"] = {"source": "Scryfall"}
        api_resource.admin._upsert_cards([card])
        assert _is_tags_for(api_resource, card["id"]).get("scryfallpreview") is True

    def test_other_preview_source_does_not_set_scryfallpreview(self, api_resource: APIResource) -> None:
        card = make_raw_card(name="Other Preview Source Test")
        card["preview"] = {"source": "The Command Zone"}
        api_resource.admin._upsert_cards([card])
        assert "scryfallpreview" not in _is_tags_for(api_resource, card["id"])


# ---------------------------------------------------------------------------
# Count keywords (prints / sets / paperprints / papersets / illustrations / artists)
# ---------------------------------------------------------------------------


class TestBuildPrintCountsSql:
    """_build_print_counts_sql chunks by ORACLE id and writes all six columns."""

    def test_sql_chunks_by_oracle_id_with_bound_parameters(self) -> None:
        sql = _build_print_counts_sql()
        # Per card, so a card's rows must share a chunk: hashed on oracle_id, not scryfall_id.
        assert "hashtext(cards.oracle_id::text)" in sql
        assert "hashtext(cards.scryfall_id::text)" not in sql
        assert "%(num_chunks)s" in sql
        assert "%(chunk_index)s" in sql
        for column in PRINT_COUNT_COLUMNS:
            assert column in sql

    def test_migration_backfill_counts_the_same_way(self) -> None:
        """The migration's one-off backfill is the sync statement without the chunk predicate."""
        migration = next(m for m in get_migrations() if m["file_name"] == "2026-10-04-01-print-counts.sql")["file_contents"]

        def counting_ctes(sql: str) -> list[str]:
            # Everything that decides a number: from the first CTE to the UPDATE, whitespace folded.
            return sql[sql.index("WITH paper_sets AS (") : sql.index("UPDATE magic.cards")].split()

        chunk_predicate = "AND (abs(hashtext(cards.oracle_id::text)) %% %(num_chunks)s) = %(chunk_index)s"
        sync_sql = _build_print_counts_sql()
        assert chunk_predicate in sync_sql
        assert counting_ctes(migration) == counting_ctes(sync_sql.replace(chunk_predicate, ""))

    def test_a_paper_set_is_decided_over_the_whole_table_not_the_chunk(self) -> None:
        """Whether a set is on paper is a fact about every card in it, so `paper_sets` is unchunked."""
        sync_sql = _build_print_counts_sql()
        paper_sets = sync_sql[sync_sql.index("WITH paper_sets AS (") : sync_sql.index("), card_rows AS (")]
        assert "? 'paper'" in paper_sets
        assert "hashtext" not in paper_sets


def _printing(oracle_id: str, set_code: str, number: str, **extra: object) -> dict:
    """One raw printing of the card `oracle_id`, at the slot (set_code, number)."""
    card = make_raw_card(name=f"Count Test {oracle_id[:8]}")
    card |= {"oracle_id": oracle_id, "set": set_code, "collector_number": number, "lang": "en"} | extra
    return card


def _counts_for(api_resource: APIResource, scryfall_id: str) -> tuple:
    with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            f"SELECT {', '.join(PRINT_COUNT_COLUMNS)} FROM magic.cards WHERE scryfall_id = %(sid)s",
            {"sid": scryfall_id},
        )
        row = cursor.fetchone()
    return tuple(row[column] for column in PRINT_COUNT_COLUMNS)


class TestPrintCounts:
    """The six count columns are written at import, per card, onto every row of it.

    Tuples below are (prints, sets, paperprints, papersets, illustrations, artists) -- the first
    five are the card's and identical on each of its rows; the last is the row's own.
    """

    def test_counts_are_slots_sets_and_artworks_over_the_whole_card(self, api_resource: APIResource) -> None:
        oracle_id = str(uuid.uuid4())
        art_a, art_b = str(uuid.uuid4()), str(uuid.uuid4())
        artist_1, artist_2 = str(uuid.uuid4()), str(uuid.uuid4())
        first = _printing(oracle_id, "pca", "1", illustration_id=art_a, artist_ids=[artist_1])
        # The same SLOT in a second language: a row, not a print.
        first_ja = _printing(oracle_id, "pca", "1", illustration_id=art_a, artist_ids=[artist_1], lang="ja")
        # A second slot in the same set, new artwork, two artists.
        second = _printing(oracle_id, "pca", "2", illustration_id=art_b, artist_ids=[artist_1, artist_2])
        # A second set, reusing the first artwork, and crediting nobody.
        third = _printing(oracle_id, "pcb", "1", illustration_id=art_a)
        # Another card entirely: its rows must not leak into these counts.
        other = _printing(str(uuid.uuid4()), "pca", "3", illustration_id=str(uuid.uuid4()), artist_ids=[artist_2])

        api_resource.admin._upsert_cards([first, first_ja, second, third, other])

        # 4 rows, 3 slots, 2 sets, 2 artworks.
        assert _counts_for(api_resource, first["id"]) == (3, 2, 3, 2, 2, 1)
        assert _counts_for(api_resource, first_ja["id"]) == (3, 2, 3, 2, 2, 1)
        assert _counts_for(api_resource, second["id"]) == (3, 2, 3, 2, 2, 2)
        assert _counts_for(api_resource, third["id"]) == (3, 2, 3, 2, 2, 0)
        assert _counts_for(api_resource, other["id"]) == (1, 1, 1, 1, 1, 1)

    def test_a_printing_without_an_illustration_id_adds_no_artwork(self, api_resource: APIResource) -> None:
        """`illustrations=0` is four cards on Scryfall: zero is a value, not an absence."""
        oracle_id = str(uuid.uuid4())
        card = _printing(oracle_id, "pcc", "1")
        api_resource.admin._upsert_cards([card])

        assert _counts_for(api_resource, card["id"]) == (1, 1, 1, 1, 0, 0)

    def test_a_reprint_recounts_every_row_of_the_card(self, api_resource: APIResource) -> None:
        """A new printing changes the counts on the card's OLDER rows, which the import did not touch."""
        oracle_id = str(uuid.uuid4())
        art = str(uuid.uuid4())
        original = _printing(oracle_id, "pcd", "1", illustration_id=art)
        api_resource.admin._upsert_cards([original])
        assert _counts_for(api_resource, original["id"]) == (1, 1, 1, 1, 1, 0)

        reprint = _printing(oracle_id, "pce", "7", illustration_id=str(uuid.uuid4()))
        api_resource.admin._upsert_cards([reprint])

        assert _counts_for(api_resource, original["id"]) == (2, 2, 2, 2, 2, 0)
        assert _counts_for(api_resource, reprint["id"]) == (2, 2, 2, 2, 2, 0)

    def test_a_printing_is_a_paper_print_by_its_set_not_its_own_games(self, api_resource: APIResource) -> None:
        """`paperprints` / `papersets` count the rows of PAPER SETS -- sets with any row on paper.

        Measured on api.scryfall.com 2026-10-04: "Name Sticker" Goblin's only printing, unf/107m,
        is `games: [mtgo]` in Unfinity, a paper set, and the card is `paperprints=1`; Rakshasa
        Vizier's Arena-only ktk/193y makes it `paperprints=4`, not 3. `paperprints=0` is the 654
        cards printed only in digital sets.

        preprocess_card drops a printing without paper in `games`, so the digital rows are made
        here by editing the stored blob, and the sync is run as the import runs it.
        """
        oracle_id = str(uuid.uuid4())
        # pcf is a paper set: this row is on paper.
        paper = _printing(oracle_id, "pcf", "1", illustration_id=str(uuid.uuid4()))
        # pcg is a digital set: no row of it is on paper.
        arena = _printing(oracle_id, "pcg", "1", illustration_id=str(uuid.uuid4()))
        digital_only = _printing(str(uuid.uuid4()), "pcg", "2", illustration_id=str(uuid.uuid4()))
        # A card whose ONLY printing is digital, in the paper set: the "Name Sticker" Goblin shape.
        mtgo_in_paper_set = _printing(str(uuid.uuid4()), "pcf", "2m", illustration_id=str(uuid.uuid4()))
        api_resource.admin._upsert_cards([paper, arena, digital_only, mtgo_in_paper_set])

        with api_resource.app_context.writer_pool.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """UPDATE magic.cards SET raw_card_blob = raw_card_blob || '{"games": ["arena", "mtgo"]}'::jsonb
                       WHERE scryfall_id = ANY(%(ids)s::uuid[])""",
                    {"ids": [arena["id"], digital_only["id"], mtgo_in_paper_set["id"]]},
                )
            conn.commit()
            # The digital set's rows lose their paper counts; the digital row of the paper set keeps its own.
            assert api_resource.admin._sync_print_counts(conn) == 3

        assert _counts_for(api_resource, paper["id"]) == (2, 2, 1, 1, 2, 0)
        assert _counts_for(api_resource, arena["id"]) == (2, 2, 1, 1, 2, 0)
        assert _counts_for(api_resource, digital_only["id"]) == (1, 1, 0, 0, 1, 0)
        assert _counts_for(api_resource, mtgo_in_paper_set["id"]) == (1, 1, 1, 1, 1, 0)

    def test_a_variation_is_not_a_print(self, api_resource: APIResource) -> None:
        """A slot Scryfall marks `variation: true` adds no print, but its artwork and set still count.

        Measured on api.scryfall.com 2026-10-04: Embermage Goblin is ons/200 and the foil-only
        ons/200★, and `!"Embermage Goblin" prints=1`, `paperprints=1` and `illustrations=2` each
        find it where `prints=2` finds nothing.
        """
        oracle_id = str(uuid.uuid4())
        regular = _printing(oracle_id, "pci", "200", illustration_id=str(uuid.uuid4()))
        variation = _printing(oracle_id, "pci", "200★", illustration_id=str(uuid.uuid4()), variation=True)
        # A card whose only slot in a second set is a variation: the set still counts.
        other_oracle = str(uuid.uuid4())
        plain = _printing(other_oracle, "pci", "201", illustration_id=str(uuid.uuid4()))
        lone_variation = _printing(other_oracle, "pcj", "9†", illustration_id=plain["illustration_id"], variation=True)

        api_resource.admin._upsert_cards([regular, variation, plain, lone_variation])

        # Two slots, one print, two artworks -- and the variation row answers with the card's counts.
        assert _counts_for(api_resource, regular["id"]) == (1, 1, 1, 1, 2, 0)
        assert _counts_for(api_resource, variation["id"]) == (1, 1, 1, 1, 2, 0)
        assert _counts_for(api_resource, plain["id"]) == (1, 2, 1, 2, 1, 0)
        assert _counts_for(api_resource, lone_variation["id"]) == (1, 2, 1, 2, 1, 0)

    def test_a_variation_slot_is_out_whole_when_one_language_of_it_lacks_the_flag(self, api_resource: APIResource) -> None:
        """The SLOT is skipped, not the rows: Scryfall's flag is not the same on every language of one.

        Monstrous Growth's por/173† is `variation: true` in English, German, Spanish and French
        and `false` in Japanese on api.scryfall.com (2026-10-04), and the card is `prints=9` of
        its ten slots. Skipping rows would leave the slot standing on the Japanese one.
        """
        oracle_id = str(uuid.uuid4())
        art = str(uuid.uuid4())
        regular = _printing(oracle_id, "pck", "173", illustration_id=art)
        flagged = _printing(oracle_id, "pck", "173†", illustration_id=art, variation=True)
        unflagged_ja = _printing(oracle_id, "pck", "173†", illustration_id=art, variation=False, lang="ja")

        api_resource.admin._upsert_cards([regular, flagged, unflagged_ja])

        for row in (regular, flagged, unflagged_ja):
            assert _counts_for(api_resource, row["id"]) == (1, 1, 1, 1, 1, 0)

    def test_sync_converges_and_a_reimport_does_not_blank_the_counts(self, api_resource: APIResource) -> None:
        oracle_id = str(uuid.uuid4())
        card = _printing(oracle_id, "pch", "1", illustration_id=str(uuid.uuid4()), artist_ids=[str(uuid.uuid4())])
        api_resource.admin._upsert_cards([card])
        assert _counts_for(api_resource, card["id"]) == (1, 1, 1, 1, 1, 1)

        # Nothing changed, so a second sync rewrites no row at all.
        with api_resource.app_context.writer_pool.connection() as conn:
            assert api_resource.admin._sync_print_counts(conn) == 0

        # The bulk stream never carries the count columns; a re-import that rewrites the row
        # must leave them standing rather than reset them to NULL.
        reimport = _printing(oracle_id, "pch", "1", illustration_id=card["illustration_id"], artist_ids=card["artist_ids"])
        reimport["id"] = card["id"]
        reimport["oracle_text"] = "changed so the reimport writes"
        with patch.object(AdminResource, "_sync_print_counts", return_value=0):
            api_resource.admin._upsert_cards([reimport])
        assert _counts_for(api_resource, card["id"]) == (1, 1, 1, 1, 1, 1)


# ---------------------------------------------------------------------------
# cheapest:usd / cheapest:eur / cheapest:tix
# ---------------------------------------------------------------------------


class TestBuildCheapestCodesSql:
    """_build_cheapest_codes_sql chunks by ORACLE id, and the migration backfills with the same statement."""

    def test_sql_chunks_by_oracle_id_with_bound_parameters(self) -> None:
        sql = _build_cheapest_codes_sql()
        # A card's lowest price is over all its rows, so they must share a chunk.
        assert "hashtext(cards.oracle_id::text)" in sql
        assert "hashtext(cards.scryfall_id::text)" not in sql
        assert "%(num_chunks)s" in sql
        assert "%(chunk_index)s" in sql
        # Only rows whose code differs are rewritten.
        assert "cards.cheapest_codes IS DISTINCT FROM proposed.cheapest_codes" in sql

    def test_migration_backfill_decides_the_same_way(self) -> None:
        """The migration's one-off backfill is the sync statement without the chunk predicate."""
        migration = next(m for m in get_migrations() if m["file_name"] == "2026-10-04-02-cheapest-codes.sql")["file_contents"]

        def statement(sql: str) -> list[str]:
            # The whole statement, from the first CTE to the end, whitespace folded.
            return sql[sql.index("WITH priced AS (") :].rstrip().rstrip(";").split()

        chunk_predicate = "AND (abs(hashtext(cards.oracle_id::text)) %% %(num_chunks)s) = %(chunk_index)s"
        sync_sql = _build_cheapest_codes_sql()
        assert chunk_predicate in sync_sql
        assert statement(migration) == statement(sync_sql.replace(chunk_predicate, ""))

    def test_prices_are_compared_as_exact_numerics_from_the_blob(self) -> None:
        """Both the plain and the foil price come from `raw_card_blob.prices`, never the `real` columns."""
        sql = _build_cheapest_codes_sql()
        for key in ("usd", "usd_foil", "eur", "eur_foil", "tix"):
            assert f"(cards.raw_card_blob->'prices'->>'{key}')::numeric END AS {key}" in sql
        assert "price_usd" not in sql
        assert "usd_etched" not in sql


def _priced(oracle_id: str, set_code: str, number: str, prices: dict[str, str | None], **extra: object) -> dict:
    """One raw printing of the card `oracle_id` carrying exactly `prices`."""
    card = make_raw_card(name=f"Cheapest Test {oracle_id[:8]}")
    card |= {"oracle_id": oracle_id, "set": set_code, "collector_number": number, "set_type": "expansion", "prices": prices} | extra
    return card


# The six questions a printing answers, in the order _answers returns them.
_CHEAPEST_QUESTIONS = [
    CheapestNode(currency, negated_term=negated) for currency in ("usd", "eur", "tix") for negated in (False, True)
]


def _answers(api_resource: APIResource, scryfall_id: str) -> tuple:
    """(usd, -usd, eur, -eur, tix, -tix) for one printing, through the SQL lane's own expressions.

    Each is True, False or None (SQL NULL) -- read with the SQL `CheapestNode.to_sql` generates,
    so these tests cover the sync and the query expression together.
    """
    context = QueryContext()
    select = ", ".join(f"{node.to_sql(context)} AS q{i}" for i, node in enumerate(_CHEAPEST_QUESTIONS))
    with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
        cursor.execute(f"SELECT {select} FROM magic.cards AS card WHERE card.scryfall_id = %(sid)s", {"sid": scryfall_id})
        row = cursor.fetchone()
    return tuple(row[f"q{i}"] for i in range(len(_CHEAPEST_QUESTIONS)))


def _usd(api_resource: APIResource, scryfall_id: str) -> tuple:
    """(cheapest:usd, -cheapest:usd) for one printing."""
    return _answers(api_resource, scryfall_id)[:2]


def _eur(api_resource: APIResource, scryfall_id: str) -> tuple:
    """(cheapest:eur, -cheapest:eur) for one printing."""
    return _answers(api_resource, scryfall_id)[2:4]


def _tix(api_resource: APIResource, scryfall_id: str) -> tuple:
    """(cheapest:tix, -cheapest:tix) for one printing."""
    return _answers(api_resource, scryfall_id)[4:]


class TestCheapestCodes:
    """`cheapest_codes` is written at import: each printing's answers against its card's lowest price.

    Pairs below are (the term, the negated TERM). On api.scryfall.com (2026-10-04) the second is
    not the complement of the first: it is `(price IS NULL OR price <> M) AND (foil IS NULL OR
    foil = M)`, so a printing can be in both lists or in neither.
    """

    def test_ties_are_all_cheapest(self, api_resource: APIResource) -> None:
        """Every printing at the low price matches (eld/1 at 0.38 / 0.38 and each one sharing it)."""
        oracle_id = str(uuid.uuid4())
        first = _priced(oracle_id, "cha", "1", {"usd": "0.38"})
        second = _priced(oracle_id, "chb", "1", {"usd": "0.38", "usd_foil": "0.38"})
        dearer = _priced(oracle_id, "chc", "1", {"usd": "0.40"})
        # Another card's cheaper printing must not become this card's minimum.
        other = _priced(str(uuid.uuid4()), "cha", "2", {"usd": "0.01"})
        api_resource.admin._upsert_cards([first, second, dearer, other])

        assert _usd(api_resource, first["id"]) == (True, False)
        assert _usd(api_resource, second["id"]) == (True, False)
        assert _usd(api_resource, dearer["id"]) == (False, True)
        assert _usd(api_resource, other["id"]) == (True, False)

    def test_a_foil_price_equal_to_the_minimum_matches_when_the_plain_price_does_not(self, api_resource: APIResource) -> None:
        """m21/130 is 0.20 / 0.03 beside roe/136's 0.03: its foil is the cheapest, so it is in BOTH lists."""
        oracle_id = str(uuid.uuid4())
        foil_matches = _priced(oracle_id, "chd", "130", {"usd": "0.20", "usd_foil": "0.03"})
        plain_matches = _priced(oracle_id, "che", "136", {"usd": "0.03"})
        api_resource.admin._upsert_cards([foil_matches, plain_matches])

        assert _usd(api_resource, foil_matches["id"]) == (True, True)
        assert _usd(api_resource, plain_matches["id"]) == (True, False)

    def test_a_foil_price_below_the_plain_one_does_not_lower_the_minimum(self, api_resource: APIResource) -> None:
        """Reflections of Littjara: khm/73 is 2.94 / 1.59 and the cheapest is the foil-only khm/400 at 1.77.

        A foil price enters M only on a printing with no plain price. khm/73 is then in NEITHER
        list -- priced both ways, neither price the minimum -- and khm/400 in both.
        """
        oracle_id = str(uuid.uuid4())
        both_prices = _priced(oracle_id, "chf", "73", {"usd": "2.94", "usd_foil": "1.59"})
        foil_only = _priced(oracle_id, "chf", "400", {"usd": None, "usd_foil": "1.77"})
        api_resource.admin._upsert_cards([both_prices, foil_only])

        assert _usd(api_resource, both_prices["id"]) == (False, False)
        assert _usd(api_resource, foil_only["id"]) == (True, True)

    def test_a_foil_only_printing_enters_the_dollar_minimum_but_not_the_euro_one(self, api_resource: APIResource) -> None:
        """In euros M is `prices.eur` alone: ddu/35 at -- / 17.54 is not cheapest beside c14/177's 18.53."""
        oracle_id = str(uuid.uuid4())
        foil_only = _priced(oracle_id, "chg", "35", {"usd_foil": "17.54", "eur_foil": "17.54"})
        plain = _priced(oracle_id, "chh", "177", {"usd": "18.53", "eur": "18.53"})
        api_resource.admin._upsert_cards([foil_only, plain])

        # Dollars: the foil-only 17.54 IS the minimum.
        assert _usd(api_resource, foil_only["id"]) == (True, True)
        assert _usd(api_resource, plain["id"]) == (False, True)
        # Euros: the minimum is 18.53, and the foil-only printing is in neither list.
        assert _eur(api_resource, foil_only["id"]) == (False, False)
        assert _eur(api_resource, plain["id"]) == (True, False)

    def test_an_etched_price_never_counts(self, api_resource: APIResource) -> None:
        oracle_id = str(uuid.uuid4())
        etched_only = _priced(oracle_id, "chi", "1", {"usd": None, "usd_foil": None, "usd_etched": "0.01"})
        plain = _priced(oracle_id, "chj", "1", {"usd": "1.00"})
        api_resource.admin._upsert_cards([etched_only, plain])

        # The etched-only printing is unpriced as far as the rule goes: false, and its negation true.
        assert _usd(api_resource, etched_only["id"]) == (False, True)
        assert _usd(api_resource, plain["id"]) == (True, False)

    def test_memorabilia_is_outside_the_minimum_and_still_matches_when_equal(self, api_resource: APIResource) -> None:
        """Goblin Piledriver's wc03/we205 at 2.60 is not cheapest beside ori/151's 2.65; wc04/jn13sb at 0.17 is, because ice/15 is 0.17."""
        piledriver = str(uuid.uuid4())
        gold_bordered = _priced(piledriver, "chk", "we205", {"usd": "2.60", "eur": "2.60"}, set_type="memorabilia")
        regular = _priced(piledriver, "chl", "151", {"usd": "2.65", "eur": "2.65"})
        other_card = str(uuid.uuid4())
        equal_memorabilia = _priced(other_card, "chk", "jn13sb", {"usd": "0.17"}, set_type="memorabilia")
        equal_regular = _priced(other_card, "chm", "15", {"usd": "0.17"})
        api_resource.admin._upsert_cards([gold_bordered, regular, equal_memorabilia, equal_regular])

        # Priced BELOW the card's minimum, and not the cheapest.
        assert _usd(api_resource, gold_bordered["id"]) == (False, True)
        assert _eur(api_resource, gold_bordered["id"]) == (False, True)
        assert _usd(api_resource, regular["id"]) == (True, False)
        assert _usd(api_resource, equal_memorabilia["id"]) == (True, False)
        assert _usd(api_resource, equal_regular["id"]) == (True, False)

    def test_a_card_with_no_minimum_is_null_on_its_priced_printings(self, api_resource: APIResource) -> None:
        """`cheapest:usd st:memorabilia` is 4 and `-(cheapest:usd) st:memorabilia` 5,662 of 5,847 on Scryfall.

        The 181 priced printings of cards whose every priced printing is memorabilia are in
        neither: SQL NULL, which a NOT leaves NULL. An UNPRICED printing of such a card is not
        NULL -- it is plain false, and its negated term true.
        """
        oracle_id = str(uuid.uuid4())
        only_memorabilia = _priced(oracle_id, "chn", "1", {"usd": "5.00", "eur_foil": "4.00", "tix": None}, set_type="memorabilia")
        unpriced = _priced(oracle_id, "cho", "1", {"usd": None, "eur": None, "tix": None})
        api_resource.admin._upsert_cards([only_memorabilia, unpriced])

        assert _usd(api_resource, only_memorabilia["id"]) == (None, None)
        # Euros: priced in foil only, and the card has no euro minimum either.
        assert _eur(api_resource, only_memorabilia["id"]) == (None, None)
        assert _answers(api_resource, unpriced["id"]) == (False, True, False, True, False, True)
        # The negated GROUP keeps the NULL: neither `-(cheapest:usd)` nor `-(-cheapest:usd)` finds it.
        context = QueryContext()
        complement = f"NOT ({CheapestNode('usd').to_sql(context)})"
        complement_of_negated = f"NOT ({CheapestNode('usd', negated_term=True).to_sql(context)})"
        with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                f"SELECT {complement} AS a, {complement_of_negated} AS b FROM magic.cards AS card WHERE card.scryfall_id = %(sid)s",
                {"sid": only_memorabilia["id"]},
            )
            assert cursor.fetchone() == {"a": None, "b": None}

    def test_an_unpriced_printing_of_a_priced_card_is_false_and_its_negation_true(self, api_resource: APIResource) -> None:
        """`-cheapest:usd e:ymkm` is all 30 of an unpriced digital set."""
        oracle_id = str(uuid.uuid4())
        priced = _priced(oracle_id, "chp", "1", {"usd": "1.00", "eur": "1.00", "tix": "1.00"})
        unpriced = _priced(oracle_id, "chq", "1", {})
        api_resource.admin._upsert_cards([priced, unpriced])

        assert _answers(api_resource, priced["id"]) == (True, False, True, False, True, False)
        assert _answers(api_resource, unpriced["id"]) == (False, True, False, True, False, True)

    def test_tix_is_equality_and_its_negated_term_the_complement(self, api_resource: APIResource) -> None:
        oracle_id = str(uuid.uuid4())
        low = _priced(oracle_id, "chr", "1", {"usd": "9.00", "tix": "0.02"})
        tied = _priced(oracle_id, "chs", "1", {"usd": "1.00", "tix": "0.02"})
        high = _priced(oracle_id, "cht", "1", {"usd": "5.00", "tix": "0.03"})
        api_resource.admin._upsert_cards([low, tied, high])

        assert _tix(api_resource, low["id"]) == (True, False)
        assert _tix(api_resource, tied["id"]) == (True, False)
        assert _tix(api_resource, high["id"]) == (False, True)
        # Each currency has its own minimum: the cheapest in tix is the dearest in dollars.
        assert _usd(api_resource, low["id"]) == (False, True)
        assert _usd(api_resource, tied["id"]) == (True, False)

    def test_prices_compare_as_decimals_not_as_reals(self, api_resource: APIResource) -> None:
        """`price_usd` is a `real`, where 1000000.01 and 1000000.02 are the same number; the blob's strings are not."""
        oracle_id = str(uuid.uuid4())
        lower = _priced(oracle_id, "chu", "1", {"usd": "1000000.01"})
        higher = _priced(oracle_id, "chv", "1", {"usd": "1000000.02"})
        api_resource.admin._upsert_cards([lower, higher])

        with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT count(DISTINCT price_usd) AS n FROM magic.cards WHERE scryfall_id = ANY(%(ids)s::uuid[])",
                {"ids": [lower["id"], higher["id"]]},
            )
            assert cursor.fetchone()["n"] == 1, "the two prices are expected to collide as reals"
        assert _usd(api_resource, lower["id"]) == (True, False)
        assert _usd(api_resource, higher["id"]) == (False, True)

    def test_a_price_change_recomputes_the_other_rows_of_the_card(self, api_resource: APIResource) -> None:
        """Prices move on every import, and one printing's new price changes the answer on its SIBLINGS."""
        oracle_id = str(uuid.uuid4())
        was_cheapest = _priced(oracle_id, "chw", "1", {"usd": "1.00"})
        was_dearer = _priced(oracle_id, "chx", "1", {"usd": "2.00"})
        api_resource.admin._upsert_cards([was_cheapest, was_dearer])
        assert _usd(api_resource, was_cheapest["id"]) == (True, False)
        assert _usd(api_resource, was_dearer["id"]) == (False, True)

        # Only the dearer printing is re-imported, at a price below the other's.
        repriced = _priced(oracle_id, "chx", "1", {"usd": "0.50"})
        repriced["id"] = was_dearer["id"]
        api_resource.admin._upsert_cards([repriced])

        assert _usd(api_resource, was_cheapest["id"]) == (False, True), "the row the import did not touch"
        assert _usd(api_resource, was_dearer["id"]) == (True, False)

    def test_sync_converges_and_a_reimport_does_not_blank_the_codes(self, api_resource: APIResource) -> None:
        oracle_id = str(uuid.uuid4())
        card = _priced(oracle_id, "chy", "1", {"usd": "1.00", "eur": "0.90", "tix": "0.02"})
        api_resource.admin._upsert_cards([card])
        assert _answers(api_resource, card["id"]) == (True, False, True, False, True, False)

        # No price moved, so a second sync rewrites no row at all.
        with api_resource.app_context.writer_pool.connection() as conn:
            assert api_resource.admin._sync_cheapest_codes(conn) == 0

        # The bulk stream never carries the column; a re-import that rewrites the row must leave
        # it standing rather than reset it to NULL.
        reimport = _priced(oracle_id, "chy", "1", {"usd": "1.00", "eur": "0.90", "tix": "0.02"})
        reimport["id"] = card["id"]
        reimport["oracle_text"] = "changed so the reimport writes"
        with patch.object(AdminResource, "_sync_cheapest_codes", return_value=0):
            api_resource.admin._upsert_cards([reimport])
        assert _answers(api_resource, card["id"]) == (True, False, True, False, True, False)

    def test_a_malformed_price_is_no_price_rather_than_a_failed_sync(self, api_resource: APIResource) -> None:
        oracle_id = str(uuid.uuid4())
        malformed = _priced(oracle_id, "chz", "1", {"usd": "1.00"})
        sound = _priced(oracle_id, "chz", "2", {"usd": "2.00"})
        api_resource.admin._upsert_cards([malformed, sound])

        with api_resource.app_context.writer_pool.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """UPDATE magic.cards SET raw_card_blob = jsonb_set(raw_card_blob, '{prices,usd}', '"n/a"')
                       WHERE scryfall_id = %(sid)s""",
                    {"sid": malformed["id"]},
                )
            conn.commit()
            assert api_resource.admin._sync_cheapest_codes(conn) == 2

        assert _usd(api_resource, malformed["id"]) == (False, True)
        assert _usd(api_resource, sound["id"]) == (True, False)


# ---------------------------------------------------------------------------
# `new:rarity` tests
# ---------------------------------------------------------------------------


class TestBuildNewRaritySql:
    """_build_new_rarity_sql chunks by ORACLE id, and the migration backfills with the same statement."""

    def test_sql_chunks_by_oracle_id_with_bound_parameters(self) -> None:
        sql = _build_new_rarity_sql()
        # A card's first printing at a rarity is over all its rows, so they must share a chunk.
        assert "hashtext(cards.oracle_id::text)" in sql
        assert "hashtext(cards.scryfall_id::text)" not in sql
        assert "%(num_chunks)s" in sql
        assert "%(chunk_index)s" in sql
        # Only rows whose flag differs are rewritten.
        assert "cards.new_rarity IS DISTINCT FROM proposed.new_rarity" in sql

    def test_the_unchunked_statement_has_no_parameters(self) -> None:
        sql = _build_new_rarity_sql(chunked=False)
        assert "%(" not in sql
        assert "hashtext" not in sql

    def test_migration_backfill_decides_the_same_way(self) -> None:
        """The migration's one-off backfill is the unchunked sync statement, release batches and all."""
        migration = next(m for m in get_migrations() if m["file_name"] == "2026-10-04-03-new-rarity.sql")["file_contents"]

        def statement(sql: str) -> list[str]:
            # The whole statement, from the first CTE to the end, whitespace folded.
            return sql[sql.index("WITH release_batches") :].rstrip().rstrip(";").split()

        assert statement(migration) == statement(_build_new_rarity_sql(chunked=False))

    def test_every_release_batch_is_in_the_statement(self) -> None:
        sql = _build_new_rarity_sql()
        assert sql.count("\n    (") == len(RELEASE_BATCHES)
        for date, set_code, batch in RELEASE_BATCHES:
            assert f"({date}, '{set_code}', {batch})" in sql

    def test_the_release_batch_table_is_well_formed(self) -> None:
        keys = [(date, set_code) for date, set_code, _ in RELEASE_BATCHES]
        assert keys == sorted(keys), "kept sorted so a diff of a refresh is readable"
        assert len(set(keys)) == len(keys), "one batch per (date, set)"
        for date, set_code, batch in RELEASE_BATCHES:
            assert 19930801 <= date <= 20991231
            assert set_code == set_code.lower()
            assert set_code.isalnum()
            assert batch >= 1, "batch 0 is what an unlisted (date, set) already is"


def _new_rarity_printing(
    oracle_id: str, set_code: str, number: str, released_at: str, rarity: str = "rare", **extra: object
) -> dict:
    """One raw printing of the card `oracle_id` for the `new:rarity` tests."""
    card = make_raw_card(name=f"New Rarity Test {oracle_id[:8]}", rarity=rarity)
    return (
        card
        | {
            "oracle_id": oracle_id,
            "set": set_code,
            "collector_number": number,
            "released_at": released_at,
            "set_type": "expansion",
        }
        | extra
    )


def _new_rarity(api_resource: APIResource, *cards: dict) -> list:
    """`new:rarity` for each card, through the SQL the parser generates (so the sync and the leaf are tested together)."""
    sql = NewNode("rarity").to_sql(QueryContext())
    with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            f"SELECT card.scryfall_id::text AS sid, ({sql}) AS answer FROM magic.cards AS card WHERE card.scryfall_id = ANY(%(ids)s::uuid[])",
            {"ids": [card["id"] for card in cards]},
        )
        answers = {row["sid"]: row["answer"] for row in cursor.fetchall()}
    return [answers[card["id"]] for card in cards]


def _new_rarity_complement(api_resource: APIResource, *cards: dict) -> list:
    """`-new:rarity` for each card."""
    sql = NewNode("rarity").to_sql(QueryContext())
    with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            f"SELECT card.scryfall_id::text AS sid, (NOT ({sql})) AS answer FROM magic.cards AS card WHERE card.scryfall_id = ANY(%(ids)s::uuid[])",
            {"ids": [card["id"] for card in cards]},
        )
        answers = {row["sid"]: row["answer"] for row in cursor.fetchall()}
    return [answers[card["id"]] for card in cards]


class TestNewRarity:
    """`new_rarity` is written at import: is this printing the first of its card at its rarity?

    Each rule below was measured on api.scryfall.com 2026-10-04 (38,943 of 38,943 printings); the
    docstring of `_build_new_rarity_sql` carries them.
    """

    def test_the_earliest_printing_at_each_rarity_is_new(self, api_resource: APIResource) -> None:
        oracle_id = str(uuid.uuid4())
        first = _new_rarity_printing(oracle_id, "nra", "1", "2001-01-01", "rare")
        reprint = _new_rarity_printing(oracle_id, "nrb", "1", "2005-01-01", "rare")
        other_rarity = _new_rarity_printing(oracle_id, "nrc", "1", "2010-01-01", "uncommon")
        later_other_rarity = _new_rarity_printing(oracle_id, "nrd", "1", "2012-01-01", "uncommon")
        api_resource.admin._upsert_cards([reprint, later_other_rarity, first, other_rarity])

        cards = [first, reprint, other_rarity, later_other_rarity]
        assert _new_rarity(api_resource, *cards) == [True, False, True, False]
        assert _new_rarity_complement(api_resource, *cards) == [False, True, False, True], "the plain complement"

    def test_two_cards_are_ranked_apart(self, api_resource: APIResource) -> None:
        one = _new_rarity_printing(str(uuid.uuid4()), "nre", "1", "2001-01-01")
        two = _new_rarity_printing(str(uuid.uuid4()), "nre", "2", "2001-01-01")
        api_resource.admin._upsert_cards([one, two])
        assert _new_rarity(api_resource, one, two) == [True, True]

    def test_a_card_with_a_single_printing_is_new_at_its_rarity(self, api_resource: APIResource) -> None:
        only = _new_rarity_printing(str(uuid.uuid4()), "nrf", "1", "2015-06-01", "mythic")
        api_resource.admin._upsert_cards([only])
        assert _new_rarity(api_resource, only) == [True]

    def test_the_collector_number_compares_as_its_first_integer(self, api_resource: APIResource) -> None:
        """9 before 10 (not the string order), `236s` is 236, `GRN-103` is 103, and a lone star is 0."""
        for numbers, first_number in [
            (("10", "9"), "9"),
            (("236s", "100"), "100"),
            (("GRN-103", "104"), "GRN-103"),
            (("\u2605", "1"), "\u2605"),
            (("A25-141", "26"), "A25-141"),
        ]:
            oracle_id = str(uuid.uuid4())
            printings = [_new_rarity_printing(oracle_id, f"n{i}x", number, "2001-01-01") for i, number in enumerate(numbers)]
            api_resource.admin._upsert_cards(printings)
            expected = [number == first_number for number in numbers]
            assert _new_rarity(api_resource, *printings) == expected, numbers

    def test_a_variation_sorts_after_its_plain_twin(self, api_resource: APIResource) -> None:
        """Same date, number and rarity: the variation loses even though its id sorts first."""
        oracle_id = str(uuid.uuid4())
        variation = _new_rarity_printing(oracle_id, "nrg", "5", "2001-01-01", variation=True)
        plain = _new_rarity_printing(oracle_id, "nrh", "5", "2001-01-01")
        variation["id"], plain["id"] = "00000000-0000-4000-8000-000000000001", "ffffffff-ffff-4fff-bfff-ffffffffffff"
        api_resource.admin._upsert_cards([variation, plain])
        assert _new_rarity(api_resource, variation, plain) == [False, True]

    def test_the_set_code_is_not_a_key_and_the_scryfall_id_breaks_the_tie(self, api_resource: APIResource) -> None:
        """Two sets, one date, one batch, one number: the smaller id wins whatever the codes are."""
        oracle_id = str(uuid.uuid4())
        by_code = _new_rarity_printing(oracle_id, "nri", "7", "2001-01-01")
        by_id = _new_rarity_printing(oracle_id, "nrz", "7", "2001-01-01")
        by_code["id"], by_id["id"] = "ffffffff-ffff-4fff-bfff-ffffffffffff", "00000000-0000-4000-8000-000000000002"
        api_resource.admin._upsert_cards([by_code, by_id])
        assert _new_rarity(api_resource, by_code, by_id) == [False, True]

    def test_the_release_batch_orders_two_sets_of_one_day(self, api_resource: APIResource) -> None:
        """`pal99` is in batch 1 of 1999-01-01 and an unlisted set in batch 0, so it comes first -- code order and id both against it."""
        assert (19990101, "pal99", 1) in RELEASE_BATCHES
        oracle_id = str(uuid.uuid4())
        listed = _new_rarity_printing(oracle_id, "pal99", "3", "1999-01-01")
        unlisted = _new_rarity_printing(oracle_id, "zzz99", "3", "1999-01-01")
        listed["id"], unlisted["id"] = "00000000-0000-4000-8000-000000000003", "ffffffff-ffff-4fff-bfff-ffffffffffff"
        api_resource.admin._upsert_cards([listed, unlisted])
        assert _new_rarity(api_resource, listed, unlisted) == [False, True]

    def test_the_date_comes_before_the_batch(self, api_resource: APIResource) -> None:
        """A batch-1 set released the day BEFORE a batch-0 set is still first."""
        oracle_id = str(uuid.uuid4())
        earlier_in_batch_one = _new_rarity_printing(oracle_id, "pal99", "3", "1999-01-01")
        later_in_batch_zero = _new_rarity_printing(oracle_id, "zzz99", "3", "1999-01-02")
        api_resource.admin._upsert_cards([earlier_in_batch_one, later_in_batch_zero])
        assert _new_rarity(api_resource, earlier_in_batch_one, later_in_batch_zero) == [True, False]

    def test_the_release_batch_applies_to_its_own_date_only(self, api_resource: APIResource) -> None:
        """The batch is per (date, set): `pal99` on another day is batch 0, and the id decides."""
        oracle_id = str(uuid.uuid4())
        listed = _new_rarity_printing(oracle_id, "pal99", "3", "1999-01-02")
        unlisted = _new_rarity_printing(oracle_id, "zzz99", "3", "1999-01-02")
        listed["id"], unlisted["id"] = "00000000-0000-4000-8000-000000000004", "ffffffff-ffff-4fff-bfff-ffffffffffff"
        api_resource.admin._upsert_cards([listed, unlisted])
        assert _new_rarity(api_resource, listed, unlisted) == [True, False]

    @pytest.mark.parametrize("set_type", ["promo", "memorabilia", "from_the_vault", "treasure_chest"])
    def test_an_excluded_set_type_is_never_new_and_does_not_hold_the_flag_back(
        self, api_resource: APIResource, set_type: str
    ) -> None:
        oracle_id = str(uuid.uuid4())
        excluded = _new_rarity_printing(oracle_id, "nrj", "1", "2001-01-01", set_type=set_type)
        expansion = _new_rarity_printing(oracle_id, "nrk", "1", "2005-01-01")
        api_resource.admin._upsert_cards([excluded, expansion])
        assert _new_rarity(api_resource, excluded, expansion) == [False, True]

    def test_a_masterpiece_printing_is_new_only_in_wot(self, api_resource: APIResource) -> None:
        oracle_id = str(uuid.uuid4())
        expedition = _new_rarity_printing(oracle_id, "exp", "1", "2001-01-01", "mythic", set_type="masterpiece")
        api_resource.admin._upsert_cards([expedition])
        assert _new_rarity(api_resource, expedition) == [False]

        other = str(uuid.uuid4())
        wot = _new_rarity_printing(other, "wot", "1", "2001-01-01", "mythic", set_type="masterpiece")
        later = _new_rarity_printing(other, "nrl", "1", "2005-01-01", "mythic")
        api_resource.admin._upsert_cards([wot, later])
        assert _new_rarity(api_resource, wot, later) == [True, False]

    def test_other_promo_types_and_serialized_printings_still_count(self, api_resource: APIResource) -> None:
        """Only the SET types exclude: a serialized printing (4 of Scryfall's 38,943) is new."""
        oracle_id = str(uuid.uuid4())
        serialized = _new_rarity_printing(oracle_id, "nrm", "1", "2001-01-01", promo_types=["serialized"])
        later = _new_rarity_printing(oracle_id, "nrn", "1", "2005-01-01")
        api_resource.admin._upsert_cards([serialized, later])
        assert _new_rarity(api_resource, serialized, later) == [True, False]

    def test_a_card_whose_every_printing_is_excluded_has_no_new_printing(self, api_resource: APIResource) -> None:
        """Not NULL: the sync has been over it, and the answer is no -- in the complement."""
        oracle_id = str(uuid.uuid4())
        promo = _new_rarity_printing(oracle_id, "nro", "1", "2001-01-01", set_type="promo")
        api_resource.admin._upsert_cards([promo])
        assert _new_rarity(api_resource, promo) == [False]
        assert _new_rarity_complement(api_resource, promo) == [True]

    def test_an_earlier_printing_takes_the_flag_from_the_one_that_held_it(self, api_resource: APIResource) -> None:
        """A new printing changes the answer on a SIBLING the import did not touch."""
        oracle_id = str(uuid.uuid4())
        held = _new_rarity_printing(oracle_id, "nrp", "1", "2005-01-01")
        api_resource.admin._upsert_cards([held])
        assert _new_rarity(api_resource, held) == [True]

        earlier = _new_rarity_printing(oracle_id, "nrq", "1", "2001-01-01")
        api_resource.admin._upsert_cards([earlier])
        assert _new_rarity(api_resource, held, earlier) == [False, True]

    def test_the_rarity_is_part_of_the_group(self, api_resource: APIResource) -> None:
        oracle_id = str(uuid.uuid4())
        common = _new_rarity_printing(oracle_id, "nrr", "1", "2001-01-01", "common")
        uncommon = _new_rarity_printing(oracle_id, "nrs", "1", "2002-01-01", "uncommon")
        special = _new_rarity_printing(oracle_id, "nrt", "1", "2003-01-01", "special")
        bonus = _new_rarity_printing(oracle_id, "nru", "1", "2004-01-01", "bonus")
        api_resource.admin._upsert_cards([common, uncommon, special, bonus])
        assert _new_rarity(api_resource, common, uncommon, special, bonus) == [True, True, True, True]

    def test_an_unreached_row_answers_neither_polarity(self, api_resource: APIResource) -> None:
        """NULL means not computed: in `new:rarity` and in `-new:rarity` alike."""
        card = _new_rarity_printing(str(uuid.uuid4()), "nrv", "1", "2001-01-01")
        api_resource.admin._upsert_cards([card])
        with api_resource.app_context.writer_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute("UPDATE magic.cards SET new_rarity = NULL WHERE scryfall_id = %(sid)s", {"sid": card["id"]})
            conn.commit()
        assert _new_rarity(api_resource, card) == [None]
        assert _new_rarity_complement(api_resource, card) == [None]

    def test_sync_converges_and_a_reimport_does_not_blank_the_flag(self, api_resource: APIResource) -> None:
        oracle_id = str(uuid.uuid4())
        card = _new_rarity_printing(oracle_id, "nrw", "1", "2001-01-01")
        api_resource.admin._upsert_cards([card])
        assert _new_rarity(api_resource, card) == [True]

        # Nothing moved, so a second sync rewrites no row at all.
        with api_resource.app_context.writer_pool.connection() as conn:
            assert api_resource.admin._sync_new_rarity(conn) == 0

        # The bulk stream never carries the column; a re-import that rewrites the row must leave
        # it standing rather than reset it to NULL.
        reimport = _new_rarity_printing(oracle_id, "nrw", "1", "2001-01-01")
        reimport["id"] = card["id"]
        reimport["oracle_text"] = "changed so the reimport writes"
        with patch.object(AdminResource, "_sync_new_rarity", return_value=0):
            api_resource.admin._upsert_cards([reimport])
        assert _new_rarity(api_resource, card) == [True]


# ---------------------------------------------------------------------------
# _CardStream counting tests
# ---------------------------------------------------------------------------


class TestCardStreamCounting:
    """_CardStream tallies stage counts that drive the status string selection."""

    def test_multiple_preprocessed_but_all_unchanged_loads_zero(self, api_resource: APIResource) -> None:
        """Raw > 0 and preprocessed > 0 but all unchanged → success with cards_loaded=0."""
        cards = [make_raw_card(name=f"Count Card {i}") for i in range(3)]
        api_resource.admin._upsert_cards(cards)  # seed the DB

        result = api_resource.admin._upsert_cards(cards)
        assert result["status"] == "success"
        assert result["cards_loaded"] == 0

    def test_preprocessing_filter_distinguished_from_empty_input(self, api_resource: APIResource) -> None:
        """no_cards_after_preprocessing is distinct from no_cards_before_preprocessing."""
        with patch("api.admin_resource.preprocess_card", return_value=[]):
            filtered = api_resource.admin._upsert_cards([make_raw_card(), make_raw_card()])
        empty = api_resource.admin._upsert_cards([])

        assert filtered["status"] == "no_cards_after_preprocessing"
        assert empty["status"] == "no_cards_before_preprocessing"


# ---------------------------------------------------------------------------
# Multi-batch tests
# ---------------------------------------------------------------------------


class TestMultiBatchLoad:
    """Cards spanning multiple batches are fully inserted."""

    def test_all_cards_inserted_across_batches(self, api_resource: APIResource) -> None:
        cards = [make_raw_card(name=f"Batch Card {uuid.uuid4()}") for _ in range(7)]
        result = api_resource.admin._upsert_cards(cards, page_size=3)
        assert result["status"] == "success"
        assert result["cards_loaded"] == 7
        assert result["cards_sent"] == 7

    def test_batch_boundary_at_exact_multiple(self, api_resource: APIResource) -> None:
        """page_size=4, 8 cards → two full batches of 4."""
        cards = [make_raw_card(name=f"Exact Batch {uuid.uuid4()}") for _ in range(8)]
        result = api_resource.admin._upsert_cards(cards, page_size=4)
        assert result["status"] == "success"
        assert result["cards_loaded"] == 8

    def test_unchanged_cards_not_loaded_across_batch_boundary(self, api_resource: APIResource) -> None:
        existing = [make_raw_card(name=f"Existing {uuid.uuid4()}") for _ in range(3)]
        api_resource.admin._upsert_cards(existing, page_size=10)

        new_cards = [make_raw_card(name=f"New {uuid.uuid4()}") for _ in range(4)]
        result = api_resource.admin._upsert_cards(existing + new_cards, page_size=3)
        assert result["status"] == "success"
        assert result["cards_loaded"] == 4
        assert result["cards_sent"] == 7  # all cards are sent; existing ones just produce 0 loads


# ---------------------------------------------------------------------------
# Error-path cleanup tests
# ---------------------------------------------------------------------------


class TestErrorRecovery:
    """A mid-batch failure must not poison the pooled connection."""

    @staticmethod
    def _raise_data_error(*args: object, **kwargs: object) -> None:  # noqa: ARG004
        msg = "simulated failure mid-batch"
        raise psycopg.DataError(msg)

    def test_error_mid_batch_returns_database_error(self, api_resource: APIResource, caplog: pytest.LogCaptureFixture) -> None:
        with (
            patch("api.admin_resource._bulk_upsert", side_effect=self._raise_data_error),
            caplog.at_level(logging.ERROR, logger="api.admin_resource"),
        ):
            result = api_resource.admin._upsert_cards([make_raw_card(name="Doomed Card")])

        assert result["status"] == "database_error"
        assert result["cards_loaded"] == 0

        assert result["message"] == "Error loading cards: DataError: simulated failure mid-batch"
        error_records = [r for r in caplog.records if "Error loading cards" in r.message]
        assert error_records, "the failure should be logged"
        assert all(r.exc_info for r in error_records), "the log record should carry the traceback"

    def test_import_succeeds_after_earlier_failure(self, api_resource: APIResource) -> None:
        """The pool is reusable after a failed import: the next import on the same pool succeeds."""
        with patch("api.admin_resource._bulk_upsert", side_effect=self._raise_data_error):
            failed = api_resource.admin._upsert_cards([make_raw_card(name="First Try Fails")])
        assert failed["status"] == "database_error"

        recovered = api_resource.admin._upsert_cards([make_raw_card(name="Second Try Succeeds")])
        assert recovered["status"] == "success"
        assert recovered["cards_loaded"] == 1


# ---------------------------------------------------------------------------
# _run_import_under_lock streaming wiring (mocked — tests control flow only)
# ---------------------------------------------------------------------------


class TestRunImportUnderLockStreaming:
    """_run_import_under_lock must delegate to stream_data_for_key, not _get_cards_to_insert."""

    def _make_api(self) -> APIResource:
        # Patch out setup_schema and import_data during construction: __init__ calls both, and an
        # unpatched import_data with last_import_time=0.0 performs a real full Scryfall import.
        app_context = mock_app_context(last_import_time=multiprocessing.Value("d", 0.0, lock=True))
        with patch.object(AdminResource, "setup_schema"), patch.object(AdminResource, "import_data"):
            return APIResource(app_context=app_context)

    def test_calls_stream_data_for_key(self) -> None:
        api = self._make_api()
        with (
            patch.object(api.admin, "_import_recent", return_value=False),
            patch.object(api.admin, "setup_schema"),
            patch.object(
                api.admin,
                "_upsert_cards",
                return_value={"status": "no_cards_before_preprocessing", "cards_loaded": 0, "message": ""},
            ),
            patch.object(api.admin._bulk_data_fetcher, "stream_data_for_key") as mock_stream,
        ):
            mock_stream.return_value = iter([])
            api.admin._run_import_under_lock()
        mock_stream.assert_called_once_with(BulkDataKey.DEFAULT_CARDS)

    def test_stream_iterator_passed_directly_to_upsert_cards(self) -> None:
        """The exact iterator returned by stream_data_for_key is forwarded to _upsert_cards."""
        api = self._make_api()
        sentinel = iter([{"id": "sentinel"}])
        with (
            patch.object(api.admin, "_import_recent", return_value=False),
            patch.object(api.admin, "setup_schema"),
            patch.object(api.admin._bulk_data_fetcher, "stream_data_for_key", return_value=sentinel),
            patch.object(
                api.admin,
                "_upsert_cards",
                return_value={"status": "no_cards_before_preprocessing", "cards_loaded": 0, "message": ""},
            ) as mock_staging,
        ):
            api.admin._run_import_under_lock()
        args, _ = mock_staging.call_args
        assert args[0] is sentinel


# ---------------------------------------------------------------------------
# bulk_upsert deduplication tests
# ---------------------------------------------------------------------------


class TestBulkUpsertDedup:
    """Duplicate conflict keys in one batch must not reach ON CONFLICT."""

    def test_duplicate_scryfall_id_in_batch_last_wins(self, api_resource: APIResource) -> None:
        card_id = str(uuid.uuid4())
        (row_a,) = preprocess_card(make_raw_card(card_id=card_id, rarity="common"))
        (row_b,) = preprocess_card(make_raw_card(card_id=card_id, rarity="rare"))
        with api_resource.app_context.reader_pool.connection() as conn:
            result = bulk_upsert(
                conn,
                "cards",
                [row_a, row_b],
                schema="magic",
                conflict_target=["scryfall_id"],
            )
            conn.commit()
        assert result["inserted"] == 1
        with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT card_rarity_text FROM magic.cards WHERE scryfall_id = %s", (card_id,))
            row = cursor.fetchone()
        assert row["card_rarity_text"] == "rare"


# ---------------------------------------------------------------------------
# Upsert behavior tests
# ---------------------------------------------------------------------------


class TestUpsertBehavior:
    """_upsert_cards correctly partitions into new, unchanged, and changed cards."""

    def test_unchanged_card_skips_write(self, api_resource: APIResource) -> None:
        """Group 2: re-submitting identical data produces zero loads."""
        card_id = str(uuid.uuid4())
        card = make_raw_card(card_id=card_id)
        api_resource.admin._upsert_cards([card])

        result = api_resource.admin._upsert_cards([card])
        assert result["cards_inserted"] == 0
        assert result["cards_updated"] == 0

    def test_changed_card_is_updated(self, api_resource: APIResource) -> None:
        """Group 3: re-submitting a card with changed data updates the stored row."""
        card_id = str(uuid.uuid4())
        api_resource.admin._upsert_cards([make_raw_card(card_id=card_id)])

        result = api_resource.admin._upsert_cards([make_raw_card(card_id=card_id, rarity="rare")])
        assert result["cards_inserted"] == 0
        assert result["cards_updated"] == 1

        with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT card_rarity_text FROM magic.cards WHERE scryfall_id = %s", (card_id,))
            row = cursor.fetchone()
        assert row["card_rarity_text"] == "rare"

    def test_changed_card_preserves_backfilled_columns(self, api_resource: APIResource) -> None:
        """Group 3: updating a changed card leaves prefer_score and card_is_tags intact."""
        card_id = str(uuid.uuid4())
        api_resource.admin._upsert_cards([make_raw_card(card_id=card_id)])

        with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "UPDATE magic.cards SET prefer_score = 42.0, card_is_tags = '{\"is:instant\": true}'::jsonb WHERE scryfall_id = %s",
                (card_id,),
            )
            conn.commit()

        api_resource.admin._upsert_cards([make_raw_card(card_id=card_id, rarity="rare")])

        with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT prefer_score, card_is_tags FROM magic.cards WHERE scryfall_id = %s", (card_id,))
            row = cursor.fetchone()
        assert row["prefer_score"] == 42.0
        assert row["card_is_tags"] == {"is:instant": True}
