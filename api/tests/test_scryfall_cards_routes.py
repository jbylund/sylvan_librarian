"""Integration tests for the Scryfall-compatible /cards/* routes.

Everything here dispatches through `_handle`, the same path a real request takes: route resolution,
parameter binding, and the response object the client would receive. The corpus is a handful of
cards this module inserts under names no other test uses, because the postgres container is shared
across the session.
"""

from __future__ import annotations

import copy
import json
import uuid
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import falcon
import falcon.testing
import orjson
import pytest
from cachebox import LRUCache

from api.enums import SortDirection, resolve_direction
from api.scryfall_compat.objects import PAGE_SIZE
from api.tests.helpers import make_raw_card
from api.utils.generation_cache import GenerationCache

if TYPE_CHECKING:
    from api.api_resource import APIResource

# Set code and names owned by this module alone, so assertions can be exact against a shared
# database. "sfc" is not a real Scryfall set code.
SET_CODE = "sfc"
BOLT_ID = "11111111-1111-4111-8111-111111111111"
BEAR_ID = "22222222-2222-4222-8222-222222222222"
DELVER_ID = "33333333-3333-4333-8333-333333333333"
BOLT_ORACLE_ID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"

# The by-name key rule wants two more shapes -- a name with FIVE halves and a punctuated, accented
# one -- and they live in a set of their own so the `s:sfc` paging tests keep asserting three cards.
NAME_SET_CODE = "sfn"
WHO_ID = "55555555-5555-4555-8555-555555555555"
VAULT_ID = "66666666-6666-4666-8666-666666666666"
# 50 bytes, and deliberately short. Sibling PR #912 serves these routes from the engine, where a
# card's folded name is an `InlineStr<61>` that TRUNCATES anything longer -- so a five-part name
# spelled "Compat ..." five times (70 bytes) resolves through SQL and not through the store. This
# branch has no engine by-name lookup and cannot hit that bound, but the fixture keeps #912's
# spelling so the two test files stay mergeable and neither pins the wrong divergence.
WHO_NAME = "Cw Who // Cw What // Cw When // Cw Where // Cw Why"
VAULT_NAME = "Compat Lim-Dûl's Vault"


def _bolt() -> dict:
    card = make_raw_card(card_id=BOLT_ID, name="Compat Bolt")
    card |= {
        "object": "card",
        "oracle_id": BOLT_ORACLE_ID,
        "set": SET_CODE,
        "set_name": "Scryfall Compat",
        "collector_number": "1",
        "mana_cost": "{R}",
        "type_line": "Instant",
        "oracle_text": "Compat Bolt deals 3 damage to any target.",
        "lang": "en",
        "multiverse_ids": [900001],
        "mtgo_id": 900002,
        "arena_id": 900003,
        "tcgplayer_id": 900004,
        "cardmarket_id": 900005,
        "illustration_id": "44444444-4444-4444-8444-444444444444",
    }
    return card


def _bear() -> dict:
    card = make_raw_card(card_id=BEAR_ID, name="Compat Bears")
    card |= {
        "object": "card",
        "set": SET_CODE,
        "set_name": "Scryfall Compat",
        "collector_number": "2",
        "mana_cost": "{1}{G}",
        "type_line": "Creature — Bear",
        "oracle_text": "",
        "power": "2",
        "toughness": "2",
        "colors": ["G"],
        "color_identity": ["G"],
        "lang": "en",
    }
    return card


def _delver() -> dict:
    """A transform card, so the multi-face path is exercised end to end."""
    card = make_raw_card(card_id=DELVER_ID, name="Compat Delver // Compat Aberration")
    card |= {
        "object": "card",
        "set": SET_CODE,
        "set_name": "Scryfall Compat",
        "collector_number": "3",
        "layout": "transform",
        "type_line": "Creature — Human Wizard // Creature — Human Insect",
        "colors": ["U"],
        "color_identity": ["U"],
        "card_faces": [
            {
                "object": "card_face",
                "name": "Compat Delver",
                "mana_cost": "{U}",
                "type_line": "Creature — Human Wizard",
                "oracle_text": "Look at the top card of your library.",
                "power": "1",
                "toughness": "1",
                "colors": ["U"],
                "image_uris": {"large": "https://cards.test/front.jpg", "png": "https://cards.test/front.png"},
            },
            {
                "object": "card_face",
                "name": "Compat Aberration",
                "mana_cost": "",
                "type_line": "Creature — Human Insect",
                "oracle_text": "Flying",
                "power": "3",
                "toughness": "2",
                "colors": ["U"],
                "image_uris": {"large": "https://cards.test/back.jpg", "png": "https://cards.test/back.png"},
            },
        ],
    }
    card.pop("image_uris", None)
    return card


def _who() -> dict:
    """A FIVE-part name, which has no face keys at all.

    `{"name":"Who // What // When // Where // Why"}` answers und/75 on api.scryfall.com while
    `{"name":"Who"}` and `exact=Who` are each not_found -- the whole name is the key and its parts
    are not. A rule built on `split_part(name, ' // ', 1)` reads this as a card with a front face
    named "Cw Who" and answers it.
    """
    card = make_raw_card(card_id=WHO_ID, name=WHO_NAME)
    card |= {
        "object": "card",
        "set": NAME_SET_CODE,
        "set_name": "Scryfall Compat Names",
        "collector_number": "1",
        "layout": "split",
        "type_line": " // ".join(["Sorcery"] * 5),
        "colors": ["W"],
        "color_identity": ["W"],
        "card_faces": [
            {
                "object": "card_face",
                "name": part,
                "mana_cost": "{W}",
                "type_line": "Sorcery",
                "oracle_text": f"{part} does nothing.",
            }
            for part in WHO_NAME.split(" // ")
        ],
    }
    card.pop("image_uris", None)
    return card


def _vault() -> dict:
    """A name carrying an accent AND an apostrophe AND a hyphen, so collation has something to do.

    `{"name":"limduls vault"}` and `{"name":"lim-duls vault"}` both answer Lim-Dûl's Vault on
    api.scryfall.com, and `exact=` of either does too. Every one of those spellings was a 404 here.
    """
    card = make_raw_card(card_id=VAULT_ID, name=VAULT_NAME)
    card |= {
        "object": "card",
        "set": NAME_SET_CODE,
        "set_name": "Scryfall Compat Names",
        "collector_number": "2",
        "mana_cost": "{1}{B}",
        "type_line": "Instant",
        "oracle_text": "Look at the top five cards of your library.",
        "colors": ["B"],
        "color_identity": ["B"],
        "lang": "en",
    }
    return card


@pytest.fixture(name="compat_corpus", scope="module")
def compat_corpus_fixture(api_resource: APIResource) -> APIResource:
    """Load this module's cards and their rulings once, then hand back the resource."""
    api_resource.admin._upsert_cards([copy.deepcopy(card) for card in (_bolt(), _bear(), _delver(), _who(), _vault())])
    with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
        cursor.execute("DELETE FROM magic.rulings WHERE oracle_id = %(oracle_id)s", {"oracle_id": BOLT_ORACLE_ID})
        # Three rulings across two dates, two of them same-day: a single ruling cannot tell one
        # ordering from another, which is how the ascending sort went unnoticed. Inserted oldest
        # first so the expected answer is not the insertion order either.
        cursor.executemany(
            "INSERT INTO magic.rulings (oracle_id, source, published_at, comment) VALUES (%s, %s, %s, %s)",
            [
                (BOLT_ORACLE_ID, "wotc", "2004-10-04", "Any target means any target."),
                (BOLT_ORACLE_ID, "wotc", "2021-02-05", "Zero damage is still damage."),
                (BOLT_ORACLE_ID, "wotc", "2021-02-05", "A later clarification."),
            ],
        )
        conn.commit()
    # /cards/search runs through _search, which prefers the in-process engine when its store is
    # loaded -- a store built before this insert would answer every query here with zero rows.
    api_resource.app_context.reload_engine(force=True)
    api_resource.admin._clear_caches()
    return api_resource


def dispatch(api: APIResource, path: str, query_string: str = "", *, method: str = "GET", body: dict | None = None):
    """Run one request through `_handle` and return the Falcon response."""
    environ = falcon.testing.create_environ(
        path=path,
        query_string=query_string,
        method=method,
        body=json.dumps(body) if body is not None else "",
        headers={"Content-Type": "application/json"} if body is not None else None,
    )
    req = falcon.Request(environ)
    resp = falcon.Response()
    api._handle(req, resp)
    return resp


def payload(resp) -> dict:
    """Decode a response body regardless of whether it was set as media or as text."""
    if resp.media is not None:
        return resp.media
    return orjson.loads(resp.render_body())


class TestRouteRegistration:
    """Every Scryfall /cards/* path resolves, and only the ones that exist."""

    @pytest.mark.parametrize(
        "path",
        ["cards", "cards/search", "cards/named", "cards/autocomplete", "cards/random", "cards/collection"],
    )
    def test_path_is_registered(self, stub_api_resource: APIResource, path):
        assert path in stub_api_resource.routes

    def test_catch_all_absorbs_three_segments(self, stub_api_resource: APIResource):
        """/cards/:code/:number/:lang is the longest shape, so capacity must be exactly three."""
        assert stub_api_resource.routes["cards"].positional_capacity == 3

    def test_a_fourth_segment_is_not_a_route(self, stub_api_resource: APIResource):
        entry, _ = stub_api_resource._resolve_action("cards/a/b/c/d")
        assert entry is None

    def test_collection_answers_post_not_get(self, stub_api_resource: APIResource):
        spec = stub_api_resource.routes["cards/collection"].spec
        assert spec.methods == frozenset({"POST"})

    def test_the_named_subroutes_win_over_the_catch_all(self, stub_api_resource: APIResource):
        entry, args = stub_api_resource._resolve_action("cards/search")
        assert args == []
        assert entry.action.__name__ == "scryfall_cards_search"


class TestSearch:
    """GET /cards/search."""

    def test_returns_a_list_object(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/search", "q=%21%22Compat+Bolt%22"))
        assert body["object"] == "list"
        assert body["total_cards"] == 1
        assert body["has_more"] is False
        assert body["data"][0]["name"] == "Compat Bolt"

    def test_cards_are_full_scryfall_objects(self, compat_corpus: APIResource):
        card = payload(dispatch(compat_corpus, "/cards/search", "q=%21%22Compat+Bolt%22"))["data"][0]
        assert card["object"] == "card"
        assert card["id"] == BOLT_ID
        assert card["oracle_text"] == "Compat Bolt deals 3 damage to any target."
        assert "card_name" not in card

    def test_no_match_is_a_scryfall_404(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/search", "q=%21%22No+Such+Compat+Card%22")
        body = payload(resp)
        assert resp.status == falcon.HTTP_404
        assert body["object"] == "error"
        assert body["code"] == "not_found"

    def test_missing_query_is_a_scryfall_400(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/search")
        assert resp.status == falcon.HTTP_400
        assert payload(resp)["details"] == "You didn't enter anything to search for."

    @pytest.mark.parametrize("order", ["penny", "review"])
    def test_the_two_unsupported_orders_warn_instead_of_failing(self, compat_corpus: APIResource, order):
        """Scryfall itself falls back silently; the cards still come back, with a note saying why."""
        body = payload(dispatch(compat_corpus, "/cards/search", f"q=%21%22Compat+Bolt%22&order={order}"))
        assert body["total_cards"] == 1
        assert any(order in warning for warning in body["warnings"])

    @pytest.mark.parametrize("order", ["released", "set", "artist", "color", "eur", "tix", "rarity", "cmc"])
    def test_supported_orders_do_not_warn(self, compat_corpus: APIResource, order):
        """The vocabulary is built from CardOrdering, so this fails if an ordering stops being wired."""
        body = payload(dispatch(compat_corpus, "/cards/search", f"q=%21%22Compat+Bolt%22&order={order}"))
        assert body["total_cards"] == 1
        assert "warnings" not in body

    def test_an_unrecognized_order_still_warns(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/search", "q=%21%22Compat+Bolt%22&order=nonsense"))
        assert any("nonsense" in warning for warning in body["warnings"])

    @pytest.mark.parametrize(
        ("order", "expected"),
        [("usd", SortDirection.DESC), ("rarity", SortDirection.DESC), ("name", SortDirection.ASC)],
        ids=["usd-desc", "rarity-desc", "name-asc"],
    )
    def test_dir_auto_resolves_per_order(self, compat_corpus: APIResource, monkeypatch, order, expected):
        """`auto` is Scryfall's default and is per-ordering; it must not flatten to ascending.

        Asserted on what reaches `_search` rather than on the returned page, because the fixture
        corpus is too small for two directions to differ on most orderings.
        """
        seen = {}
        original = compat_corpus._search

        def spy(**kwargs: object) -> dict:
            seen.update(kwargs)
            return original(**kwargs)

        monkeypatch.setattr(compat_corpus, "_search", spy)
        dispatch(compat_corpus, "/cards/search", f"q=%21%22Compat+Bolt%22&order={order}&dir=auto")
        assert resolve_direction(seen["direction"], seen["orderby"]) == expected

    def test_an_explicit_dir_is_not_turned_into_auto(self, compat_corpus: APIResource, monkeypatch):
        seen = {}
        original = compat_corpus._search
        monkeypatch.setattr(compat_corpus, "_search", lambda **kw: (seen.update(kw), original(**kw))[1])
        dispatch(compat_corpus, "/cards/search", "q=%21%22Compat+Bolt%22&order=usd&dir=asc")
        assert seen["direction"] == SortDirection.ASC

    def test_page_beyond_the_results_is_a_404(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/search", "q=%21%22Compat+Bolt%22&page=2")
        assert resp.status == falcon.HTTP_404

    def test_next_page_is_absent_when_the_page_is_the_last(self, compat_corpus: APIResource):
        assert "next_page" not in payload(dispatch(compat_corpus, "/cards/search", "q=%21%22Compat+Bolt%22"))

    def test_page_size_matches_scryfall(self):
        assert PAGE_SIZE == 175

    def test_pretty_emits_indented_json(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/search", "q=%21%22Compat+Bolt%22&pretty=true")
        assert resp.text.startswith('{\n  "object"')

    def test_csv_format_has_a_stable_header(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/search", "q=%21%22Compat+Bolt%22&format=csv")
        assert resp.content_type.startswith("text/csv")
        header, row = resp.text.splitlines()[:2]
        assert header.startswith("object,id,oracle_id,")
        assert BOLT_ID in row

    def test_unparseable_query_is_a_scryfall_400(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/search", "q=cmc%3E")
        assert resp.status == falcon.HTTP_400
        assert payload(resp)["object"] == "error"


class TestNamed:
    """GET /cards/named."""

    def test_exact_match_is_case_insensitive(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/named", "exact=compat+bolt"))
        assert body["name"] == "Compat Bolt"

    def test_exact_miss_is_a_404(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/named", "exact=Compat+Nothing")
        assert resp.status == falcon.HTTP_404
        assert payload(resp)["code"] == "not_found"

    def test_exact_matches_one_face_of_a_multi_face_card(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/named", "exact=Compat+Aberration"))
        assert body["id"] == DELVER_ID

    def test_set_filter_narrows_the_match(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/named", f"exact=Compat+Bolt&set={SET_CODE}"))
        assert body["set"] == SET_CODE

    def test_set_filter_that_excludes_the_card_is_a_404(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/named", "exact=Compat+Bolt&set=zzz")
        assert resp.status == falcon.HTTP_404

    def test_fuzzy_resolves_a_containment_match(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/named", "fuzzy=compat+bears"))
        assert body["name"] == "Compat Bears"

    def test_fuzzy_containment_ignores_the_names_separators(self, compat_corpus: APIResource):
        """Scryfall matches a word against the name with its separators gone (measured 2026-08-16).

        `fuzzy=redgoad` answers not_found there while `fuzzy=red goad` resolves, so this is one
        word matched against one unseparated name -- not the query being rejoined.
        """
        body = payload(dispatch(compat_corpus, "/cards/named", "fuzzy=compatbolt"))
        assert body["name"] == "Compat Bolt"

    def test_fuzzy_ambiguity_is_a_not_found_carrying_a_type(self, compat_corpus: APIResource):
        """api.scryfall.com sends code=not_found with type=ambiguous, not code=ambiguous."""
        resp = dispatch(compat_corpus, "/cards/named", "fuzzy=compat")
        assert resp.status == falcon.HTTP_404
        body = payload(resp)
        assert body["code"] == "not_found"
        assert body["type"] == "ambiguous"
        assert "Too many cards match ambiguous name" in body["details"]

    def test_fuzzy_tolerates_a_typo(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/named", "fuzzy=Compat+Bolzt"))
        assert body["name"] == "Compat Bolt"

    def test_fuzzy_miss_is_a_404(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/named", "fuzzy=qqqqzzzzxxxx")
        assert resp.status == falcon.HTTP_404

    def test_neither_parameter_is_a_400(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/named")
        assert resp.status == falcon.HTTP_400
        assert payload(resp)["code"] == "bad_request"

    def test_text_format_renders_plain_text(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/named", "exact=Compat+Bolt&format=text")
        assert resp.content_type.startswith("text/plain")
        assert resp.text.splitlines()[0] == "Compat Bolt {R}"

    def test_image_format_redirects_to_the_image(self, compat_corpus: APIResource):
        with pytest.raises(falcon.HTTPFound) as found:
            dispatch(compat_corpus, "/cards/named", "exact=Compat+Bolt&format=image&version=png")
        assert "/png/" in found.value.headers["location"]

    def test_image_format_honors_the_back_face(self, compat_corpus: APIResource):
        with pytest.raises(falcon.HTTPFound) as found:
            dispatch(compat_corpus, "/cards/named", "exact=Compat+Delver&format=image&face=back&version=large")
        # Derived from the card id, not read from the fixture: image URLs are a pure function of
        # the id, so the store carries none of them.
        assert (
            found.value.headers["location"] == "https://cards.scryfall.io/large/back/3/3/33333333-3333-4333-8333-333333333333.jpg"
        )


class TestNameKeyRule:
    """The keys a card answers to, on `named?exact=` and on a collection `{"name"}` identifier.

    THE TWO ARE NOT THE SAME LOOKUP. Measured against api.scryfall.com on 2026-08-31, ONE
    IDENTIFIER PER REQUEST -- a collection response's `data` is not in identifier order, and a
    batched probe attributes its answers to the wrong needles, which is how the first reading of
    this rule came out wrong:

      {"name":"Delver of Secrets"}                   -> Delver of Secrets // Insectile Aberration
      {"name":"Insectile Aberration"}                -> the same card (a BACK face names it)
      {"name":"Delver of Secrets // Insectile ..."}  -> not_found, where `exact=` is the card
      {"name":"Fire // Ice"} / {"Wear // Tear"} /
      {"name":"Bonecrusher Giant // Stomp"}          -> not_found, all three
      {"name":"Who // What // When // Where // Why"} -> und/75, a FIVE-part name IS a key
      {"name":"Who"}                                 -> not_found, and so is `exact=Who`
      {"name":"Elves"}                               -> Elves (ffdn/9), a KEY and not a substring
      {"name":"limduls vault"}                       -> Lim-Dûl's Vault, collated
      {"name":"  Lightning Bolt  "}                  -> Lightning Bolt, trimmed
      {"name":"Delver of Secrets","set":"mid"}       -> mid/47, set FILTERS the lookup

    ONE PATH HERE. Sibling PR #912 runs every one of these twice -- once with its engine by-name
    lookup serving and once with it gated off -- because there the SQL is a fallback that must agree
    with the store. This branch has no engine by-name lookup at all: `_EXACT_NAME_MATCH` and
    `_COLLECTION_NAME_MATCH` are the path that ships, so there is one answer to assert. The engine
    states the same rule for the `!"..."` SEARCH operator instead, and
    `exact_name_matches_either_face` in card_engine/src/tests.rs is where that copy is pinned.
    """

    @staticmethod
    def _collection(api: APIResource, name: str, set_code: str | None = None) -> dict:
        identifier: dict = {"name": name}
        if set_code:
            identifier["set"] = set_code
        return payload(dispatch(api, "/cards/collection", method="POST", body={"identifiers": [identifier]}))

    @staticmethod
    def _resolves(api: APIResource, name: str, set_code: str | None = None) -> str | None:
        """The id a collection identifier resolves to, or None for not_found."""
        body = TestNameKeyRule._collection(api, name, set_code)
        return body["data"][0]["id"] if body["data"] else None

    @staticmethod
    def _exact(api: APIResource, name: str) -> str | None:
        """The id `named?exact=name` resolves to, or None for a 404."""
        resp = dispatch(api, "/cards/named", urlencode({"exact": name}))
        return None if resp.status == falcon.HTTP_404 else payload(resp)["id"]

    @pytest.mark.parametrize("name", ["Compat Delver", "Compat Aberration"])
    def test_either_face_names_the_card_on_both_surfaces(self, compat_corpus: APIResource, name):
        """The BACK face is the half the collection predicate this replaced never looked at.

        It compared `split_part(card_name, ' // ', 1)` and the whole name, so
        `{"name":"Compat Aberration"}` missed a card api.scryfall.com resolves.
        """
        assert self._resolves(compat_corpus, name) == DELVER_ID
        assert self._exact(compat_corpus, name) == DELVER_ID

    def test_the_joined_name_is_exacts_key_and_not_a_collection_identifiers(self, compat_corpus: APIResource):
        """The one key the two surfaces disagree about."""
        joined = "Compat Delver // Compat Aberration"
        assert self._exact(compat_corpus, joined) == DELVER_ID
        assert self._resolves(compat_corpus, joined) is None

    def test_a_five_part_name_is_a_key_and_none_of_its_parts_is(self, compat_corpus: APIResource):
        """EXACTLY two halves is what makes a face key, which `split_part` cannot say on its own."""
        assert self._resolves(compat_corpus, WHO_NAME) == WHO_ID
        assert self._exact(compat_corpus, WHO_NAME) == WHO_ID
        for part in WHO_NAME.split(" // "):
            assert self._resolves(compat_corpus, part) is None, part
            assert self._exact(compat_corpus, part) is None, part

    @pytest.mark.parametrize(
        "spelling",
        [
            "Compat Lim-Dûl's Vault",
            "Compat Lim-Dul's Vault",
            "compat limduls vault",
            "compat lim-duls vault",
            "compatlimdulsvault",
            "COMPAT LIMDULS VAULT",
        ],
    )
    def test_names_compare_collated(self, compat_corpus: APIResource, spelling):
        """Accent, apostrophe, hyphen and spacing all drop out of the comparison."""
        assert self._resolves(compat_corpus, spelling) == VAULT_ID
        assert self._exact(compat_corpus, spelling) == VAULT_ID

    def test_a_collated_face_does_not_straddle_the_join(self, compat_corpus: APIResource):
        """The halves are split BEFORE either side is collated, so `" // "` is not just punctuation.

        Collating first would delete the join and let `compatdelvercompataberration` -- and worse,
        a needle spanning it -- read as a face.
        """
        assert self._resolves(compat_corpus, "compatdelver") == DELVER_ID
        assert self._resolves(compat_corpus, "delvercompataberration") is None
        assert self._resolves(compat_corpus, "compatdelvercompataberration") is None
        assert self._exact(compat_corpus, "compatdelvercompataberration") == DELVER_ID

    def test_leading_and_trailing_whitespace_is_not_part_of_the_name(self, compat_corpus: APIResource):
        """`{"name":"  Lightning Bolt  "}` resolves on api.scryfall.com; this compared it as posted."""
        assert self._resolves(compat_corpus, "  Compat Bolt  ") == BOLT_ID

    @pytest.mark.parametrize("needle", ["Compat", "Bolt", "Vault", "", "   "])
    def test_a_substring_of_a_name_is_not_a_key(self, compat_corpus: APIResource, needle):
        """A name lookup, not a search.

        `{"name":"Elves"}` answers *Elves* (ffdn/9) on api.scryfall.com -- the card actually named
        that, not one of the hundreds whose names contain the word, which is what a containment
        lookup cut to one row would have answered.
        """
        assert self._resolves(compat_corpus, needle) is None

    def test_set_filters_the_lookup(self, compat_corpus: APIResource):
        """`{"name":"Delver of Secrets","set":"mid"}` answers mid/47 -- the card, in the set asked for.

        A card with no printing in that set drops out of the lookup rather than answering another
        printing, which is the same thing `exact=&set=` does.
        """
        assert self._resolves(compat_corpus, "Compat Bolt", SET_CODE) == BOLT_ID
        assert self._resolves(compat_corpus, "Compat Bolt", NAME_SET_CODE) is None
        assert self._resolves(compat_corpus, VAULT_NAME, NAME_SET_CODE) == VAULT_ID

    def test_an_unresolvable_name_comes_back_in_not_found(self, compat_corpus: APIResource):
        """Reported, not dropped: the client has to be able to tell which identifier failed."""
        body = self._collection(compat_corpus, "Compat Delver // Compat Aberration")
        assert body["data"] == []
        assert body["not_found"] == [{"name": "Compat Delver // Compat Aberration"}]


class TestAutocomplete:
    """GET /cards/autocomplete."""

    def test_returns_a_catalog(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/autocomplete", "q=Compat+Bol"))
        assert body["object"] == "catalog"
        assert "Compat Bolt" in body["data"]
        assert body["total_values"] == len(body["data"])

    def test_short_queries_return_an_empty_catalog(self, compat_corpus: APIResource):
        assert payload(dispatch(compat_corpus, "/cards/autocomplete", "q=C"))["data"] == []

    def test_at_most_twenty_values(self, compat_corpus: APIResource):
        assert len(payload(dispatch(compat_corpus, "/cards/autocomplete", "q=co"))["data"]) <= 20


class TestRandom:
    """GET /cards/random."""

    def test_returns_a_card(self, compat_corpus: APIResource):
        """Unrestricted, so it can draw any row in the session-shared database -- assert on id only.

        The `object` key comes from whatever the drawn row's blob holds, and other modules insert
        fixtures that never had one; `test_query_restricts_the_draw` pins the full shape.
        """
        body = payload(dispatch(compat_corpus, "/cards/random"))
        assert uuid.UUID(body["id"])

    def test_query_restricts_the_draw(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/random", "q=%21%22Compat+Bolt%22"))
        assert body["object"] == "card"
        assert body["id"] == BOLT_ID

    def test_query_matching_nothing_is_a_404(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/random", "q=%21%22No+Such+Compat+Card%22")
        assert resp.status == falcon.HTTP_404

    def test_repeated_draws_are_not_all_the_same_card(self, compat_corpus: APIResource, monkeypatch):
        """The draw's SQL text and parameters never vary, so a memoized result would pin one card.

        The query cache holds a single entry when caching is off, which is how the suite runs — the
        count query and the draw evict each other and the bug hides. So this installs the
        multi-entry cache production has. Twenty draws over a corpus of at least three cards
        collide on one id with probability below (1/3)**19, so one distinct id here means the
        result was replayed rather than redrawn.
        """
        monkeypatch.setattr(
            compat_corpus,
            "_query_cache",
            GenerationCache(factory=lambda: LRUCache(maxsize=1_000), generation=compat_corpus.app_context.cache_generation),
        )
        drawn = {payload(dispatch(compat_corpus, "/cards/random"))["id"] for _ in range(20)}
        assert len(drawn) > 1

    def test_the_response_is_marked_uncacheable(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/random")
        assert "no-store" in resp.get_header("Cache-Control")


class TestByIdentifier:
    """GET /cards/:id and the external-id namespaces."""

    def test_scryfall_id(self, compat_corpus: APIResource):
        assert payload(dispatch(compat_corpus, f"/cards/{BOLT_ID}"))["id"] == BOLT_ID

    def test_unknown_scryfall_id_is_a_404(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, f"/cards/{uuid.uuid4()}")
        assert resp.status == falcon.HTTP_404
        assert payload(resp)["object"] == "error"

    def test_a_segment_that_is_not_a_uuid_is_a_404(self, compat_corpus: APIResource):
        assert dispatch(compat_corpus, "/cards/not-an-id").status == falcon.HTTP_404

    @pytest.mark.parametrize(
        ("namespace", "external_id"),
        [("multiverse", 900001), ("mtgo", 900002), ("arena", 900003), ("tcgplayer", 900004), ("cardmarket", 900005)],
    )
    def test_external_id_namespaces(self, compat_corpus: APIResource, namespace, external_id):
        assert payload(dispatch(compat_corpus, f"/cards/{namespace}/{external_id}"))["id"] == BOLT_ID

    def test_unknown_external_id_is_a_404(self, compat_corpus: APIResource):
        assert dispatch(compat_corpus, "/cards/mtgo/999999999").status == falcon.HTTP_404

    def test_non_numeric_external_id_is_a_404(self, compat_corpus: APIResource):
        assert dispatch(compat_corpus, "/cards/mtgo/abc").status == falcon.HTTP_404

    def test_set_code_and_collector_number(self, compat_corpus: APIResource):
        assert payload(dispatch(compat_corpus, f"/cards/{SET_CODE}/1"))["id"] == BOLT_ID

    def test_set_code_is_case_insensitive(self, compat_corpus: APIResource):
        assert payload(dispatch(compat_corpus, f"/cards/{SET_CODE.upper()}/1"))["id"] == BOLT_ID

    def test_language_segment_selects_english_by_default(self, compat_corpus: APIResource):
        assert payload(dispatch(compat_corpus, f"/cards/{SET_CODE}/1/en"))["id"] == BOLT_ID

    def test_a_language_the_printing_lacks_is_a_404(self, compat_corpus: APIResource):
        assert dispatch(compat_corpus, f"/cards/{SET_CODE}/1/ja").status == falcon.HTTP_404

    def test_text_format_on_a_path_lookup(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, f"/cards/{BOLT_ID}", "format=text")
        assert resp.text.splitlines()[0] == "Compat Bolt {R}"


class TestMultiFaceFidelity:
    """A multi-face printing comes back as the card Scryfall sent, not as one of its faces."""

    def test_the_card_object_carries_its_faces(self, compat_corpus: APIResource):
        card = payload(dispatch(compat_corpus, f"/cards/{DELVER_ID}"))
        assert card["object"] == "card"
        assert card["name"] == "Compat Delver // Compat Aberration"
        assert [face["name"] for face in card["card_faces"]] == ["Compat Delver", "Compat Aberration"]

    def test_no_importer_key_reaches_the_payload(self, compat_corpus: APIResource):
        """The blob is the card, but not byte-for-byte: three internal keys have to come back off."""
        card = payload(dispatch(compat_corpus, f"/cards/{DELVER_ID}"))
        assert not {"card_name", "face_name", "face_idx", "raw_card_blob"} & set(card)

    def test_text_format_renders_both_faces(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, f"/cards/{DELVER_ID}", "format=text")
        assert "Compat Delver {U}" in resp.text
        assert "Compat Aberration" in resp.text


class TestCollection:
    """POST /cards/collection."""

    def test_resolves_mixed_identifiers(self, compat_corpus: APIResource):
        body = payload(
            dispatch(
                compat_corpus,
                "/cards/collection",
                method="POST",
                body={"identifiers": [{"id": BOLT_ID}, {"name": "Compat Bears"}, {"set": SET_CODE, "collector_number": "3"}]},
            ),
        )
        assert body["object"] == "list"
        assert {card["id"] for card in body["data"]} == {BOLT_ID, BEAR_ID, DELVER_ID}
        assert body["not_found"] == []

    def test_unresolvable_identifiers_are_reported(self, compat_corpus: APIResource):
        body = payload(
            dispatch(compat_corpus, "/cards/collection", method="POST", body={"identifiers": [{"name": "Nothing At All Compat"}]}),
        )
        assert body["data"] == []
        assert body["not_found"] == [{"name": "Nothing At All Compat"}]

    def test_oracle_id_identifier(self, compat_corpus: APIResource):
        body = payload(
            dispatch(compat_corpus, "/cards/collection", method="POST", body={"identifiers": [{"oracle_id": BOLT_ORACLE_ID}]}),
        )
        assert body["data"][0]["id"] == BOLT_ID

    def test_over_the_limit_is_a_422(self, compat_corpus: APIResource):
        resp = dispatch(
            compat_corpus,
            "/cards/collection",
            method="POST",
            body={"identifiers": [{"id": BOLT_ID}] * 76},
        )
        assert resp.status == falcon.HTTP_422
        assert payload(resp)["code"] == "validation_error"

    def test_a_body_without_identifiers_is_a_422(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/collection", method="POST", body={"cards": []})
        assert resp.status == falcon.HTTP_422

    def test_get_is_not_allowed(self, compat_corpus: APIResource):
        with pytest.raises(falcon.HTTPMethodNotAllowed):
            dispatch(compat_corpus, "/cards/collection")


class TestRulings:
    """The five rulings routes."""

    def test_rulings_by_scryfall_id(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, f"/cards/{BOLT_ID}/rulings"))
        assert body["object"] == "list"
        assert body["data"][0]["object"] == "ruling"

    def test_rulings_are_newest_first_like_scryfalls(self, compat_corpus: APIResource):
        """The order api.scryfall.com serves: `published_at` descending.

        Measured against it on 2026-08-12 -- 16 of 16 cards whose rulings span more than one date
        came back newest-first, 0 oldest-first. The ascending sort this replaced inverted every one
        of them, which is the opposite of what a compatibility surface is for.

        The order WITHIN one date is `comment` only because something has to be deterministic:
        Scryfall breaks that tie with an internal ruling id the bulk file does not carry.
        """
        body = payload(dispatch(compat_corpus, f"/cards/{BOLT_ID}/rulings"))
        assert [(row["published_at"], row["comment"]) for row in body["data"]] == [
            ("2021-02-05", "A later clarification."),
            ("2021-02-05", "Zero damage is still damage."),
            ("2004-10-04", "Any target means any target."),
        ]

    def test_rulings_by_set_and_collector_number(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, f"/cards/{SET_CODE}/1/rulings"))
        assert len(body["data"]) == 3

    @pytest.mark.parametrize(
        ("namespace", "external_id"),
        [("multiverse", 900001), ("mtgo", 900002), ("arena", 900003)],
    )
    def test_rulings_by_external_id(self, compat_corpus: APIResource, namespace, external_id):
        body = payload(dispatch(compat_corpus, f"/cards/{namespace}/{external_id}/rulings"))
        assert len(body["data"]) == 3

    def test_a_card_with_no_rulings_returns_an_empty_list(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, f"/cards/{BEAR_ID}/rulings"))
        assert body["data"] == []
        assert body["has_more"] is False

    @pytest.mark.parametrize(
        ("path", "details"),
        [
            # The path addresses nothing at all.
            ("/cards/not-a-uuid", "The requested object or REST method was not found."),
            ("/cards/multiverse", "The requested object or REST method was not found."),
            # A well-formed address that resolves to no card.
            ("/cards/00000000-0000-4000-8000-000000000000", "No card found with the given ID or set code and collector number."),
            ("/cards/multiverse/99999999", "No card found with the given ID or set code and collector number."),
            # `/cards/<x>/rulings` where x is not an id reads as a set code and a collector number
            # called "rulings", so Scryfall answers the CARD miss rather than the rulings one.
            ("/cards/not-a-uuid/rulings", "No card found with the given ID or set code and collector number."),
            # The rulings shapes.
            (
                "/cards/00000000-0000-4000-8000-000000000000/rulings",
                "No card found with the given ID, multiverse ID, or set code & collector number.",
            ),
            (
                "/cards/multiverse/99999999/rulings",
                "No card found with the given ID, multiverse ID, or set code & collector number.",
            ),
            ("/cards/zzz/999/rulings", "No card found with the given ID, multiverse ID, or set code & collector number."),
        ],
    )
    def test_a_miss_carries_the_body_scryfall_words_for_that_shape(
        self, compat_corpus: APIResource, path: str, details: str
    ) -> None:
        """Three bodies, not one, and none of them the string this used to send.

        Captured from api.scryfall.com on 2026-08-12. Nothing pinned `details` before -- the route
        tests asserted the status and the code and left the field a client string-matches alone,
        which is exactly how a compatibility surface drifts on it.
        """
        response = dispatch(compat_corpus, path)
        assert response.status == falcon.HTTP_404
        assert payload(response)["details"] == details

    def test_rulings_for_an_unknown_card_is_a_404(self, compat_corpus: APIResource):
        assert dispatch(compat_corpus, f"/cards/{uuid.uuid4()}/rulings").status == falcon.HTTP_404


class TestAllCards:
    """GET /cards, the unfiltered listing."""

    def test_first_page_is_a_list_object(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards"))
        assert body["object"] == "list"
        assert body["total_cards"] >= 3
        assert len(body["data"]) <= PAGE_SIZE

    def test_a_page_past_the_end_is_a_404(self, compat_corpus: APIResource):
        assert dispatch(compat_corpus, "/cards", "page=100000").status == falcon.HTTP_404

    def test_a_non_positive_page_is_a_400(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards", "page=0")
        assert resp.status == falcon.HTTP_400
        assert payload(resp)["code"] == "bad_request"

    def test_next_page_addresses_this_host(self, compat_corpus: APIResource, monkeypatch):
        """The listing builds its own next_page, on a path the search route's tests never reach.

        Page size shrunk to 1 so a second page exists over the session's corpus rather than
        needing 176 rows.
        """
        monkeypatch.setattr("api.scryfall_compat.routes.PAGE_SIZE", 1)
        body = payload(dispatch(compat_corpus, "/cards"))
        assert body["has_more"] is True
        assert body["next_page"] == "http://falconframework.org/cards?page=2"


class TestThroughTheFullApp:
    """The same routes through a real Falcon app, which `_handle` alone does not exercise.

    `_handle` leaves `resp.media` for the framework to serialize and lets a redirect propagate as an
    exception. Only a full app resolves the media handler against the `application/json;
    charset=utf-8` content type these routes set, and only a full app turns `format=image` into an
    actual 302.
    """

    def _client(self, api: APIResource) -> falcon.testing.TestClient:
        app = falcon.App()
        app.add_sink(api._handle, prefix="/")
        return falcon.testing.TestClient(app)

    def test_a_card_serializes_over_the_wire(self, compat_corpus: APIResource):
        result = self._client(compat_corpus).simulate_get(f"/cards/{BOLT_ID}")
        assert result.status_code == 200
        assert result.headers["content-type"] == "application/json; charset=utf-8"
        assert orjson.loads(result.content)["id"] == BOLT_ID

    def test_a_search_serializes_over_the_wire(self, compat_corpus: APIResource):
        result = self._client(compat_corpus).simulate_get("/cards/search", params={"q": '!"Compat Bolt"'})
        assert result.status_code == 200
        assert orjson.loads(result.content)["object"] == "list"

    def test_an_error_carries_its_status_over_the_wire(self, compat_corpus: APIResource):
        result = self._client(compat_corpus).simulate_get("/cards/named", params={"exact": "Compat Nothing"})
        assert result.status_code == 404
        assert orjson.loads(result.content) == {
            "object": "error",
            "code": "not_found",
            "status": 404,
            "details": "No cards found matching “Compat Nothing”",
        }

    def test_image_format_is_a_302(self, compat_corpus: APIResource):
        result = self._client(compat_corpus).simulate_get(
            "/cards/named",
            params={"exact": "Compat Delver", "format": "image", "face": "back"},
        )
        assert result.status_code == 302
        assert result.headers["location"] == "https://cards.scryfall.io/large/back/3/3/33333333-3333-4333-8333-333333333333.jpg"

    def test_head_is_accepted_wherever_get_is(self, compat_corpus: APIResource):
        assert self._client(compat_corpus).simulate_head(f"/cards/{BOLT_ID}").status_code == 200

    def test_collection_round_trips_a_posted_body(self, compat_corpus: APIResource):
        result = self._client(compat_corpus).simulate_post(
            "/cards/collection",
            json={"identifiers": [{"id": BOLT_ID}, {"name": "Nothing At All Compat"}]},
        )
        assert result.status_code == 200
        body = orjson.loads(result.content)
        assert [card["id"] for card in body["data"]] == [BOLT_ID]
        assert body["not_found"] == [{"name": "Nothing At All Compat"}]

    def test_next_page_addresses_this_host(self, compat_corpus: APIResource, monkeypatch):
        """Every other URI in the payload stays Scryfall's; this one has to point back here.

        The page size is shrunk to 1 rather than inserting 176 cards: what is under test is the URL
        the handler builds when there is a further page, not the boundary arithmetic, which
        `test_page_beyond_the_results_is_a_404` already pins.
        """
        monkeypatch.setattr("api.scryfall_compat.routes.PAGE_SIZE", 1)
        result = self._client(compat_corpus).simulate_get("/cards/search", params={"q": f"s:{SET_CODE}"})
        body = orjson.loads(result.content)

        assert body["total_cards"] == 3
        assert len(body["data"]) == 1
        assert body["has_more"] is True
        assert body["next_page"].startswith("http://falconframework.org/cards/search?")
        assert "page=2" in body["next_page"]
        assert f"q=s%3A{SET_CODE}" in body["next_page"]

    def test_the_last_page_has_no_next_page(self, compat_corpus: APIResource, monkeypatch):
        monkeypatch.setattr("api.scryfall_compat.routes.PAGE_SIZE", 1)
        body = orjson.loads(
            self._client(compat_corpus).simulate_get("/cards/search", params={"q": f"s:{SET_CODE}", "page": "3"}).content,
        )
        assert body["has_more"] is False
        assert "next_page" not in body

    def test_following_next_page_walks_the_result_set(self, compat_corpus: APIResource, monkeypatch):
        """Paging by `next_page` must visit every card exactly once, which is what a client does."""
        monkeypatch.setattr("api.scryfall_compat.routes.PAGE_SIZE", 1)
        client = self._client(compat_corpus)
        seen, page = [], 1
        while True:
            body = orjson.loads(client.simulate_get("/cards/search", params={"q": f"s:{SET_CODE}", "page": str(page)}).content)
            seen.extend(card["id"] for card in body["data"])
            if not body["has_more"]:
                break
            page += 1
        assert sorted(seen) == sorted([BOLT_ID, BEAR_ID, DELVER_ID])


class TestSelfUrlScheme:
    """`next_page` has to be followable, which means getting the scheme right behind a proxy."""

    def _next_page(self, api: APIResource, monkeypatch, headers: dict[str, str] | None = None) -> str:
        monkeypatch.setattr("api.scryfall_compat.routes.PAGE_SIZE", 1)
        environ = falcon.testing.create_environ(path="/cards", headers=headers)
        req, resp = falcon.Request(environ), falcon.Response()
        api._handle(req, resp)
        return payload(resp)["next_page"]

    def test_plain_http_request_stays_http(self, compat_corpus: APIResource, monkeypatch):
        assert self._next_page(compat_corpus, monkeypatch).startswith("http://falconframework.org/")

    def test_a_forwarded_proto_wins_over_the_requests_own_scheme(self, compat_corpus: APIResource, monkeypatch):
        """Behind a TLS-terminating proxy the request itself is http; only the header knows better."""
        url = self._next_page(compat_corpus, monkeypatch, {"X-Forwarded-Proto": "https"})
        assert url.startswith("https://")

    def test_the_rfc_7239_forwarded_header_is_honored_too(self, compat_corpus: APIResource, monkeypatch):
        url = self._next_page(compat_corpus, monkeypatch, {"Forwarded": "proto=https"})
        assert url.startswith("https://")

    def test_the_proxy_host_header_sets_the_host(self, compat_corpus: APIResource, monkeypatch):
        url = self._next_page(
            compat_corpus,
            monkeypatch,
            {"X-Proxy-Host": "cards.example.test", "X-Forwarded-Proto": "https"},
        )
        assert url.startswith("https://cards.example.test/cards?")
