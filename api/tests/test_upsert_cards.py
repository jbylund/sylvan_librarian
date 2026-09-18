"""Tests for _upsert_cards and streaming import wiring."""

from __future__ import annotations

import logging
import multiprocessing
import pathlib
import uuid
from unittest.mock import patch

import psycopg
import pytest

from api.admin_resource import AdminResource, _build_boolean_is_tags_sql
from api.api_resource import APIResource
from api.card_processing import preprocess_card
from api.db.bulk_upsert import bulk_upsert
from api.scryfall_bulk_data_fetcher import BulkDataKey
from api.tests.helpers import make_raw_card
from api.tests.support import mock_app_context

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


# ---------------------------------------------------------------------------
# illustration_ids: the column the art-tag join reads
# ---------------------------------------------------------------------------


def _dfc_raw_card(front: str, back: str) -> dict:
    card = make_raw_card(name=f"Illustration Front {uuid.uuid4()} // Illustration Back")
    card["layout"] = "transform"
    card["card_faces"] = [
        {"name": "Illustration Front", "type_line": "Creature — Human", "illustration_id": front},
        {"name": "Illustration Back", "type_line": "Creature — Insect", "illustration_id": back},
    ]
    return card


class TestIllustrationIds:
    """`illustration_ids` is every illustration the row shows, and it must survive an import.

    It is what `card_art_tags` is joined on (api/tag_import.py), so a row that loses it stops
    answering art-tag queries entirely -- and a merged double-faced row is the only place the
    back's illustration exists at all, `illustration_id` being the front's.
    """

    FRONT = "cccccccc-0000-4000-8000-000000000001"
    BACK = "cccccccc-0000-4000-8000-000000000002"

    @staticmethod
    def _stored(api_resource: APIResource, scryfall_id: str) -> list[str]:
        with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT illustration_ids FROM magic.cards WHERE scryfall_id = %s", (scryfall_id,))
            return cursor.fetchone()["illustration_ids"]

    def test_a_merged_row_stores_both_faces(self, api_resource: APIResource) -> None:
        card = _dfc_raw_card(self.FRONT, self.BACK)
        api_resource.admin._upsert_cards([card])
        assert self._stored(api_resource, card["id"]) == [self.FRONT, self.BACK]

    def test_a_single_faced_row_stores_its_one(self, api_resource: APIResource) -> None:
        card = make_raw_card()
        card["illustration_id"] = "cccccccc-0000-4000-8000-000000000003"
        api_resource.admin._upsert_cards([card])
        assert self._stored(api_resource, card["id"]) == ["cccccccc-0000-4000-8000-000000000003"]

    def test_the_migration_backfill_agrees_with_preprocessing(self, api_resource: APIResource) -> None:
        """The one-shot backfill must land where the next import would, or art tags go dark until it.

        It reads `raw_card_blob->'card_faces'`, which is the only record of the back's illustration
        on an already-imported row. Runs the migration's last statement -- the backfill -- against
        rows written by the real import path.
        """
        dfc = _dfc_raw_card(self.FRONT, self.BACK)
        solo = make_raw_card()
        solo["illustration_id"] = "cccccccc-0000-4000-8000-000000000004"
        api_resource.admin._upsert_cards([dfc, solo])

        migration = (pathlib.Path(__file__).parent.parent / "db" / "2026-08-16-01-illustration-ids.sql").read_text()
        backfill = migration[migration.index("WITH shown") :]

        with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute("UPDATE magic.cards SET illustration_ids = '[]'::jsonb")
            cursor.execute(backfill)
            conn.commit()

        assert self._stored(api_resource, dfc["id"]) == [self.FRONT, self.BACK]
        assert self._stored(api_resource, solo["id"]) == ["cccccccc-0000-4000-8000-000000000004"]
