"""Tests for _upsert_cards and streaming import wiring."""

from __future__ import annotations

import logging
import multiprocessing
import uuid
from unittest.mock import patch

import psycopg
import pytest

from api.admin_resource import PRINT_COUNT_COLUMNS, AdminResource, _build_boolean_is_tags_sql, _build_print_counts_sql
from api.api_resource import APIResource
from api.card_processing import preprocess_card
from api.db.bulk_upsert import bulk_upsert
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
