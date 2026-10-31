"""Tests for _upsert_cards and streaming import wiring."""

from __future__ import annotations

import json
import logging
import multiprocessing
import re
import uuid
from unittest.mock import patch

import psycopg
import pytest

from api.admin_resource import BOOLEAN_IS_TAGS, AdminResource, _build_boolean_is_tags_sql
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

    def test_sql_never_passes_postgres_more_than_100_arguments_per_call(self) -> None:
        """Postgres refuses a function call with more than 100 arguments, and a pair is two.

        One `jsonb_build_object` over the whole table held until the table passed 50 rows
        (2026-09-03, the promo_types enumeration), when every import failed with "cannot pass
        more than 100 arguments to a function". The builder chunks the pairs; this pins the chunk
        size to the cap, on a synthetic table and on the real one.
        """
        tags = {f"tag{i}": "cards.raw_card_blob->'reserved' = 'true'::jsonb" for i in range(101)}
        calls = _build_boolean_is_tags_sql(tags).split("jsonb_build_object(")[1:]
        assert len(calls) == 3
        assert all(call.count("CASE WHEN") <= 50 for call in calls)
        real_calls = _build_boolean_is_tags_sql(BOOLEAN_IS_TAGS).split("jsonb_build_object(")[1:]
        assert len(real_calls) > 1
        assert all(call.count("CASE WHEN") <= 50 for call in real_calls)

    def test_sql_reads_the_blob_once_per_row(self) -> None:
        """The tag expressions read a subquery that has already produced the blob, not the table.

        `raw_card_blob` is TOASTed, and each `cards.raw_card_blob->...` against the table detoasts
        it again: with 122 tags a first sync of the 2026-08-16 bulk took 35 s per chunk, past the
        import's 30 s statement_timeout, and 10 s once the blob is produced once. `OFFSET 0` is
        what stops the planner flattening the subquery back onto the table.
        """
        sql = _build_boolean_is_tags_sql({"reserved": "cards.raw_card_blob->'reserved' = 'true'::jsonb"})
        subquery = sql[sql.index("FROM (") : sql.index(") cards")]
        assert "cards.raw_card_blob || '{}'::jsonb AS raw_card_blob" in subquery
        assert "FROM magic.cards cards" in subquery
        assert subquery.rstrip().endswith("OFFSET 0")
        assert sql.index("CASE WHEN") < sql.index("FROM (")

    def test_every_tag_expression_reads_only_columns_the_subquery_provides(self) -> None:
        """An expression naming a column the subquery does not select would fail every import."""
        sql = _build_boolean_is_tags_sql(BOOLEAN_IS_TAGS)
        subquery = sql[sql.index("FROM (") : sql.index(") cards")]
        provided = set(re.findall(r"cards\.(\w+)", subquery)) | {"raw_card_blob"}
        read = {column for expr in BOOLEAN_IS_TAGS.values() for column in re.findall(r"cards\.(\w+)", expr)}
        assert read <= provided


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

    def test_undocumented_promo_type_lands_as_is_tag(self, api_resource: APIResource) -> None:
        """A `promo_types` member Scryfall's syntax page never lists still becomes a tag.

        `is:serialized` is 292 cards on api.scryfall.com (2026-09-03) and was a silent zero here
        until the vocabulary was enumerated from the printings instead of read off the page.
        """
        card = make_raw_card(name="Serialized Import Test")
        card["promo_types"] = ["serialized"]
        api_resource.admin._upsert_cards([card])
        assert _is_tags_for(api_resource, card["id"]).get("serialized") is True

    @pytest.mark.parametrize("member", ["premiereshop", "schinesealtart", "setextension", "singularityfoil", "themepack"])
    def test_promo_type_the_first_enumeration_never_paged_lands_as_is_tag(self, api_resource: APIResource, member: str) -> None:
        """Five `promo_types` members found by probing `is:` values rather than paging printings.

        On api.scryfall.com (2026-10-04) `is:premiereshop` is 6 cards, `is:schinesealtart` 37,
        `is:setextension` 46, `is:singularityfoil` 1 and `is:themepack` 30; each was a silent zero
        here. The tag is the member's own name, and no other of the five rides along.
        """
        card = make_raw_card(name=f"Swept Promo Type {member}")
        card["promo_types"] = [member]
        api_resource.admin._upsert_cards([card])
        tags = _is_tags_for(api_resource, card["id"])
        assert tags.get(member) is True
        others = {"premiereshop", "schinesealtart", "setextension", "singularityfoil", "themepack"} - {member}
        assert not others & tags.keys()

    def test_content_warning_flag_lands_as_contentwarning(self, api_resource: APIResource) -> None:
        """`is:contentwarning` reads Scryfall's `content_warning` flag: 7 cards there (2026-10-04)."""
        card = make_raw_card(name="Content Warning Import Test")
        card["content_warning"] = True
        api_resource.admin._upsert_cards([card])
        assert _is_tags_for(api_resource, card["id"]).get("contentwarning") is True

    def test_content_warning_false_or_absent_sets_no_tag(self, api_resource: APIResource) -> None:
        """Exactly `true`, like every boolean row: a false flag and a missing one write nothing."""
        unwarned = make_raw_card(name="Content Warning False")
        unwarned["content_warning"] = False
        plain = make_raw_card(name="Content Warning Absent")
        api_resource.admin._upsert_cards([unwarned, plain])
        assert "contentwarning" not in _is_tags_for(api_resource, unwarned["id"])
        assert "contentwarning" not in _is_tags_for(api_resource, plain["id"])

    def test_content_warning_removal_strips_the_tag(self, api_resource: APIResource) -> None:
        """The sync converges both ways: a re-import without the flag removes the tag."""
        card = make_raw_card(name="Content Warning Withdrawn")
        card["content_warning"] = True
        api_resource.admin._upsert_cards([card])
        assert _is_tags_for(api_resource, card["id"]).get("contentwarning") is True
        reimport = make_raw_card(card_id=card["id"], name="Content Warning Withdrawn")
        reimport["oracle_text"] = "changed so the reimport writes"
        api_resource.admin._upsert_cards([reimport])
        assert "contentwarning" not in _is_tags_for(api_resource, card["id"])

    @staticmethod
    def _meld_all_parts(own_id: str, own_role: str) -> list[dict]:
        """A meld card's `all_parts`: this card in `own_role`, plus the other two members."""
        other_roles = ["meld_part", "meld_part", "meld_result"]
        other_roles.remove(own_role)
        parts = [{"object": "related_card", "id": own_id, "component": own_role, "name": "Own Half"}]
        parts.extend(
            {"object": "related_card", "id": str(uuid.uuid4()), "component": role, "name": f"Other {i}"}
            for i, role in enumerate(other_roles)
        )
        return parts

    def test_meld_part_reads_the_cards_own_all_parts_entry(self, api_resource: APIResource) -> None:
        """Every meld card lists all three members, so the role must come from ITS OWN entry."""
        card = make_raw_card(name="Meld Part Import Test")
        card["layout"] = "meld"
        card["all_parts"] = self._meld_all_parts(card["id"], "meld_part")
        api_resource.admin._upsert_cards([card])
        tags = _is_tags_for(api_resource, card["id"])
        assert tags.get("meldpart") is True
        assert "meldresult" not in tags

    def test_meld_result_reads_the_cards_own_all_parts_entry(self, api_resource: APIResource) -> None:
        card = make_raw_card(name="Meld Result Import Test")
        card["layout"] = "meld"
        card["all_parts"] = self._meld_all_parts(card["id"], "meld_result")
        api_resource.admin._upsert_cards([card])
        tags = _is_tags_for(api_resource, card["id"])
        assert tags.get("meldresult") is True
        assert "meldpart" not in tags

    def test_meld_role_falls_back_to_the_cards_name_when_all_parts_names_a_sibling_printing(
        self, api_resource: APIResource
    ) -> None:
        """A reprint whose `all_parts` carries another printing's ids still gets its role.

        Ragnarok, Divine Deliverance fin/99b and Vanille, Cheerful l'Cie fin/211 are this shape in
        the 2026-08-16 bulk: all three entries are there, none under the printing's own id.
        """
        result = make_raw_card(name="Sibling Ids Melded")
        part = make_raw_card(name="Sibling Ids Half")
        for card in (result, part):
            card["layout"] = "meld"
            card["all_parts"] = [
                {"object": "related_card", "id": str(uuid.uuid4()), "component": "meld_part", "name": "Sibling Ids Half"},
                {"object": "related_card", "id": str(uuid.uuid4()), "component": "meld_part", "name": "Sibling Ids Other Half"},
                {"object": "related_card", "id": str(uuid.uuid4()), "component": "meld_result", "name": "Sibling Ids Melded"},
            ]
        api_resource.admin._upsert_cards([result, part])
        result_tags = _is_tags_for(api_resource, result["id"])
        assert result_tags.get("meldresult") is True
        assert "meldpart" not in result_tags
        part_tags = _is_tags_for(api_resource, part["id"])
        assert part_tags.get("meldpart") is True
        assert "meldresult" not in part_tags

    def test_meld_name_fallback_does_not_override_the_cards_own_entry(self, api_resource: APIResource) -> None:
        """An entry under the card's own id is the answer, even beside a meld entry of its name."""
        card = make_raw_card(name="Shared Name Import Test")
        card["all_parts"] = [
            {"object": "related_card", "id": card["id"], "component": "combo_piece", "name": "Shared Name Import Test"},
            {"object": "related_card", "id": str(uuid.uuid4()), "component": "meld_result", "name": "Shared Name Import Test"},
        ]
        api_resource.admin._upsert_cards([card])
        tags = _is_tags_for(api_resource, card["id"])
        assert "meldpart" not in tags
        assert "meldresult" not in tags

    def test_a_token_sharing_the_cards_name_gives_it_no_meld_role(self, api_resource: APIResource) -> None:
        """The name fallback reads meld components only: a same-named token entry decides nothing."""
        card = make_raw_card(name="Token Namesake Import Test")
        card["all_parts"] = [
            {"object": "related_card", "id": str(uuid.uuid4()), "component": "token", "name": "Token Namesake Import Test"},
            {"object": "related_card", "id": str(uuid.uuid4()), "component": "meld_part", "name": "Someone Else"},
        ]
        api_resource.admin._upsert_cards([card])
        tags = _is_tags_for(api_resource, card["id"])
        assert "meldpart" not in tags
        assert "meldresult" not in tags

    def test_other_cards_meld_roles_do_not_tag_this_card(self, api_resource: APIResource) -> None:
        """`all_parts` naming OTHER cards' roles (a token maker, a combo piece) is not a meld role."""
        card = make_raw_card(name="Meld Bystander Import Test")
        card["all_parts"] = [
            {"object": "related_card", "id": str(uuid.uuid4()), "component": "meld_part", "name": "Someone Else"},
            {"object": "related_card", "id": str(uuid.uuid4()), "component": "meld_result", "name": "Someone Else Melded"},
            {"object": "related_card", "id": card["id"], "component": "combo_piece", "name": "Meld Bystander Import Test"},
        ]
        api_resource.admin._upsert_cards([card])
        tags = _is_tags_for(api_resource, card["id"])
        assert "meldpart" not in tags
        assert "meldresult" not in tags
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
        card["preview"] = {"source": "Scryfall", "source_uri": "https://scryfall.com/card/war/176/snarespinner"}
        api_resource.admin._upsert_cards([card])
        assert _is_tags_for(api_resource, card["id"]).get("scryfallpreview") is True

    def test_scryfall_source_without_a_card_page_does_not_set_scryfallpreview(self, api_resource: APIResource) -> None:
        """The 2026 `slz` shape: source Scryfall, URI the set page -- 321 printings Scryfall's list lacks."""
        card = make_raw_card(name="Scryfall Set Page Preview Test")
        card["preview"] = {"source": "Scryfall", "source_uri": "https://scryfall.com/sets/slz?order=spoiled"}
        api_resource.admin._upsert_cards([card])
        assert "scryfallpreview" not in _is_tags_for(api_resource, card["id"])

    @pytest.mark.parametrize(("set_code", "number"), [("uma", "50"), ("grn", "103"), ("plst", "GRN-103")])
    def test_scryfallpreview_names_the_three_printings_with_no_preview_object(
        self, api_resource: APIResource, set_code: str, number: str
    ) -> None:
        card = make_raw_card(name=f"Previewed Without a Preview {set_code}")
        card["set"], card["collector_number"] = set_code, number
        api_resource.admin._upsert_cards([card])
        assert _is_tags_for(api_resource, card["id"]).get("scryfallpreview") is True

    def test_scryfallpreview_does_not_name_the_neighbouring_printings(self, api_resource: APIResource) -> None:
        card = make_raw_card(name="Previewed Neighbour")
        card["set"], card["collector_number"] = "uma", "51"
        api_resource.admin._upsert_cards([card])
        assert "scryfallpreview" not in _is_tags_for(api_resource, card["id"])

    def test_other_preview_source_does_not_set_scryfallpreview(self, api_resource: APIResource) -> None:
        card = make_raw_card(name="Other Preview Source Test")
        card["preview"] = {"source": "The Command Zone"}
        api_resource.admin._upsert_cards([card])
        assert "scryfallpreview" not in _is_tags_for(api_resource, card["id"])


# ---------------------------------------------------------------------------
# is: values that are a field, a list of sets or names, or a set type (2026-10-04 sweep)
# ---------------------------------------------------------------------------

_PRESENCE_TAGS = ("arenaid", "cardmarket", "illustration", "image", "mtgoid", "multiverse", "placeholderimage", "tcgplayer")


def _tag_expression_over(api_resource: APIResource, tag: str, blob: dict) -> bool:
    """Evaluate one BOOLEAN_IS_TAGS expression over a literal blob, as the sync's subquery presents it."""
    with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            f"SELECT COALESCE(({BOOLEAN_IS_TAGS[tag]}), false) AS answer FROM (SELECT %(blob)s::jsonb AS raw_card_blob) cards",
            {"blob": json.dumps(blob)},
        )
        return cursor.fetchone()["answer"]


def _tags_after_import(api_resource: APIResource, name: str, **fields: object) -> dict:
    """Import one otherwise-plain card carrying `fields` and return the tags the sync wrote."""
    card = make_raw_card(name=name)
    card.update(fields)
    api_resource.admin._upsert_cards([card])
    return _is_tags_for(api_resource, card["id"])


class TestPresenceIsTags:
    """`is:mtgoid`, `is:tcgplayer`, ...: the printing CARRIES the field. Two-valued, read off the blob."""

    @pytest.mark.parametrize(
        ("tag", "field", "value"),
        [
            ("mtgoid", "mtgo_id", 12345),
            ("arenaid", "arena_id", 67890),
            ("tcgplayer", "tcgplayer_id", 111),
            ("cardmarket", "cardmarket_id", 222),
            ("multiverse", "multiverse_ids", [333]),
            ("multiverse", "multiverse_ids", [333, 334]),
            ("illustration", "illustration_id", "0aeebaf5-8c7d-4636-9e82-8c27447861f7"),
            ("image", "image_status", "highres_scan"),
            ("image", "image_status", "placeholder"),
            ("placeholderimage", "image_status", "placeholder"),
        ],
    )
    def test_the_field_sets_its_own_tag(self, api_resource: APIResource, tag: str, field: str, value: object) -> None:
        tags = _tags_after_import(api_resource, f"Presence {tag} {field}", **{field: value})
        assert tags.get(tag) is True

    @pytest.mark.parametrize(
        ("tag", "field", "value"),
        [
            # the FOIL / ETCHED id is a different field: one printing carries the foil id alone
            # (63,188 against 63,187) and 892 carry the etched TCGplayer id alone
            ("mtgoid", "mtgo_foil_id", 12345),
            ("tcgplayer", "tcgplayer_etched_id", 111),
            ("multiverse", "multiverse_ids", []),
            ("image", "image_status", "missing"),
            ("placeholderimage", "image_status", "highres_scan"),
            ("placeholderimage", "image_status", "missing"),
            # Scryfall omits the key; a JSON null is not an id either
            ("arenaid", "arena_id", None),
            ("cardmarket", "cardmarket_id", None),
            ("illustration", "illustration_id", None),
        ],
    )
    def test_the_lookalike_or_the_empty_field_sets_no_tag(
        self, api_resource: APIResource, tag: str, field: str, value: object
    ) -> None:
        tags = _tags_after_import(api_resource, f"Absence {tag} {field}", **{field: value})
        assert tag not in tags

    def test_a_printing_with_no_image_status_has_no_image(self, api_resource: APIResource) -> None:
        """Every card object in the bulk files carries an `image_status`; one without has nothing to show."""
        tags = _tags_after_import(api_resource, "No Image Status")
        assert "image" not in tags
        assert "placeholderimage" not in tags

    def test_ids_do_not_ride_along(self, api_resource: APIResource) -> None:
        """Each tag reads only its own field: an Arena id sets `arenaid` and none of the other seven."""
        tags = _tags_after_import(api_resource, "Arena Id Alone", arena_id=1, image_status="missing")
        assert tags.get("arenaid") is True
        assert not {t for t in _PRESENCE_TAGS if t != "arenaid"} & tags.keys()

    def test_a_face_s_artwork_counts_when_the_printing_has_none_of_its_own(self, api_resource: APIResource) -> None:
        """The blob of a card kept with its faces (#894) holds the artwork on the face, not the printing."""
        assert _tag_expression_over(api_resource, "illustration", {"card_faces": [{"name": "A", "illustration_id": "x"}]})
        assert _tag_expression_over(api_resource, "illustration", {"illustration_id": "x"})
        assert not _tag_expression_over(api_resource, "illustration", {"card_faces": [{"name": "A"}, {"name": "B"}]})


class TestClassIsTags:
    """`is:back`, `is:indicator`, `is:fbb`, `is:tron`, ...: a field, a list of sets, a list of names, a set type."""

    def test_a_back_of_its_own_sets_back(self, api_resource: APIResource) -> None:
        tags = _tags_after_import(api_resource, "Own Back", card_back_id="11111111-2222-3333-4444-555555555555")
        assert tags.get("back") is True

    def test_the_shared_magic_back_is_not_back(self, api_resource: APIResource) -> None:
        tags = _tags_after_import(api_resource, "Magic Back", card_back_id="0aeebaf5-8c7d-4636-9e82-8c27447861f7")
        assert "back" not in tags

    def test_no_card_back_id_is_not_back(self, api_resource: APIResource) -> None:
        assert "back" not in _tags_after_import(api_resource, "No Back Id")

    def test_a_two_faced_card_is_not_back_for_having_a_second_face(self, api_resource: APIResource) -> None:
        """`is:back` is not "has a back face": no two-sided card is in Scryfall's 3,330."""
        tags = _tags_after_import(
            api_resource,
            "Two Faces // Magic Back",
            layout="transform",
            card_back_id="0aeebaf5-8c7d-4636-9e82-8c27447861f7",
            card_faces=[
                {"name": "Two Faces", "type_line": "Creature — Human"},
                {"name": "Magic Back", "type_line": "Creature — Werewolf"},
            ],
        )
        assert "back" not in tags

    def test_a_colour_indicator_sets_indicator(self, api_resource: APIResource) -> None:
        assert _tags_after_import(api_resource, "Indicated", color_indicator=["U"]).get("indicator") is True

    def test_a_colour_indicator_on_a_face_sets_indicator(self, api_resource: APIResource) -> None:
        """Read over a blob that keeps its faces (#894): the indicator is on the back, not the printing."""
        faces = [{"name": "Plain"}, {"name": "Indicated", "color_indicator": ["R"]}]
        assert _tag_expression_over(api_resource, "indicator", {"card_faces": faces})
        assert not _tag_expression_over(api_resource, "indicator", {"card_faces": [{"name": "Plain"}, {"name": "Also"}]})
        assert not _tag_expression_over(api_resource, "indicator", {"card_faces": [{"color_indicator": []}]})

    def test_an_empty_colour_indicator_is_not_indicator(self, api_resource: APIResource) -> None:
        assert "indicator" not in _tags_after_import(api_resource, "Not Indicated", color_indicator=[])

    @pytest.mark.parametrize(
        ("set_code", "expected"),
        [
            ("fbb", True),
            ("bchr", True),
            ("ren", True),
            ("rin", True),
            ("4bb", True),
            ("3ed", False),
            ("tst", False),
            ("fbbx", False),
        ],
    )
    def test_fbb_is_five_sets(self, api_resource: APIResource, set_code: str, expected: bool) -> None:
        tags = _tags_after_import(api_resource, f"FBB {set_code}", set=set_code)
        assert tags.get("fbb", False) is expected

    @pytest.mark.parametrize("name", ["Urza's Mine", "Urza's Power Plant", "Urza's Tower"])
    def test_the_three_urza_lands_set_tron(self, api_resource: APIResource, name: str) -> None:
        assert _tags_after_import(api_resource, name).get("tron") is True

    def test_another_urza_card_is_not_tron(self, api_resource: APIResource) -> None:
        assert "tron" not in _tags_after_import(api_resource, "Urza's Saga")

    @pytest.mark.parametrize(
        "name",
        [
            "Blazemire Verge",
            "Bleachbone Verge",
            "Floodfarm Verge",
            "Gloomlake Verge",
            "Hushwood Verge",
            "Riverpyre Verge",
            "Sunbillow Verge",
            "Thornspire Verge",
            "Wastewood Verge",
            "Willowrush Verge",
        ],
    )
    def test_each_verge_sets_vergeland(self, api_resource: APIResource, name: str) -> None:
        assert _tags_after_import(api_resource, name).get("vergeland") is True

    def test_a_card_named_verge_is_not_vergeland(self, api_resource: APIResource) -> None:
        assert "vergeland" not in _tags_after_import(api_resource, "Verge of Collapse")

    @pytest.mark.parametrize(
        ("set_code", "frame", "rarity", "expected"),
        [
            ("tsb", "1997", "special", True),
            ("tsr", "1997", "common", True),
            ("tsr", "2015", "common", False),
            ("plst", "1997", "special", True),
            ("plst", "1997", "common", False),
            ("plst", "2015", "special", False),
            ("tsp", "2003", "common", False),
        ],
    )
    def test_timeshifted_is_the_old_frame_of_two_sets_and_the_lists_special_reprints(
        self, api_resource: APIResource, set_code: str, frame: str, rarity: str, expected: bool
    ) -> None:
        tags = _tags_after_import(api_resource, f"Timeshift {set_code} {frame} {rarity}", set=set_code, frame=frame, rarity=rarity)
        assert tags.get("timeshifted", False) is expected

    def test_the_moonlit_basics_set_moonlitland(self, api_resource: APIResource) -> None:
        assert _tags_after_import(api_resource, "Moonlit", promo_types=["moonlitland"]).get("moonlitland") is True
        assert "moonlitland" not in _tags_after_import(api_resource, "Not Moonlit", promo_types=["event"])

    @pytest.mark.parametrize(
        ("tag", "set_type"),
        [
            ("dueldeck", "duel_deck"),
            ("fromthevault", "from_the_vault"),
        ],
    )
    def test_a_set_type_word_is_that_set_type(self, api_resource: APIResource, tag: str, set_type: str) -> None:
        tags = _tags_after_import(api_resource, f"Set Type {set_type}", set_type=set_type)
        assert tags.get(tag) is True
        others = {"dueldeck", "fromthevault"} - {tag}
        assert not others & tags.keys()

    def test_a_set_type_word_is_not_another_set_type(self, api_resource: APIResource) -> None:
        tags = _tags_after_import(api_resource, "Core Set Card", set_type="core")
        assert not {"dueldeck", "fromthevault"} & tags.keys()


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
