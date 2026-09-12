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

from api.enums import CardOrdering, SortDirection, UniqueOn, resolve_direction
from api.parsing import parse_scryfall_query
from api.scryfall_compat import routes as routes_module
from api.scryfall_compat.objects import MAX_COLLECTION_IDENTIFIERS, PAGE_SIZE
from api.scryfall_compat.routes import _csv_cell, _csv_mana_cost, _csv_price
from api.settings import settings
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
EXTRA_ID = "55555555-5555-4555-8555-555555555555"
# A SET OF ITS OWN, on purpose. `e:`/`s:` is the CONDITIONAL `include_extras` trigger -- a set term
# turns extras on iff that set holds one -- so putting the extra in SET_CODE would auto-enable
# every `s:sfc` query in this module and quietly change what the paging tests page over.
EXTRAS_SET_CODE = "sfe"

# The by-name key rule wants two more shapes -- a name with FIVE halves and a punctuated, accented
# one -- and they live in a set of their own so the `s:sfc` paging tests keep asserting three cards.
NAME_SET_CODE = "sfn"
WHO_ID = "77777777-7777-4777-8777-777777777777"
VAULT_ID = "66666666-6666-4666-8666-666666666666"
# 50 bytes, and deliberately short: the engine stores a card's folded name in an `InlineStr<61>`
# and TRUNCATES anything longer, so a five-part name spelled "Compat ..." five times (70 bytes) is
# unreachable through the engine while the SQL fallback still finds it. That is a pre-existing bug
# in its own right -- 36 names in the real corpus are over the limit -- and not the one under test
# here, so this name stays inside the bound rather than pinning the wrong divergence.
WHO_NAME = "Cw Who // Cw What // Cw When // Cw Where // Cw Why"
VAULT_NAME = "Compat Lim-Dûl's Vault"

# The language rule wants an address NO English printing carries and an address TWO languages
# share, in a set of their own so nothing else here counts them. Modelled on The Hobbit Eternal,
# which prints five of its 158 cards only in Dwarvish: on api.scryfall.com `/cards/hoc/95` is the
# `lang: dw` Arcane Signet, because there is no English hoc/95 (measured 2026-09-11).
LANG_SET_CODE = "sfl"
SIGNET_ID = "88888888-8888-4888-8888-888888888888"
AMBER_ORACLE_ID = "cccccccc-3333-4333-8333-cccccccccccc"
AMBER_EN_ID = "99999999-9999-4999-8999-999999999999"
AMBER_JA_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


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
        # Present like every real bulk row's: the collection route's {set, collector_number}
        # identifier and the segment-less /cards/:code/:number both put the English printing first,
        # and a lang-less row sorts behind every language (see _card_at_address).
        "lang": "en",
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


def _extra() -> dict:
    """A printing of the `is:extra` class, hidden by default and brought back by a trigger.

    A "Card" type line, which is how Scryfall ships the checklist and substitute-card family --
    `!"The Monarch"` (tmkc/31) is 404 bare and 200 with `include_extras=true`.
    """
    card = make_raw_card(card_id=EXTRA_ID, name="Compat Substitute")
    card |= {
        "object": "card",
        "set": EXTRAS_SET_CODE,
        "set_name": "Scryfall Compat Extras",
        "collector_number": "1",
        "type_line": "Card",
        "oracle_text": "",
        "lang": "en",
    }
    return card


def _who() -> dict:
    """A FIVE-part name, which has no face keys at all.

    `{"name":"Who // What // When // Where // Why"}` answers und/75 on api.scryfall.com while
    `{"name":"Who"}` and `exact=Who` are each not_found -- the whole name is the key and its parts
    are not. A rule built on `split_part(name, ' // ', 1)` reads this as a card with a front face
    named "Compat Who" and answers it.
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


def _signet() -> dict:
    """A printing that exists in ONE language, and that language is not English -- hoc/95's shape."""
    card = make_raw_card(card_id=SIGNET_ID, name="Compat Dwarvish Signet")
    card |= {
        "object": "card",
        "set": LANG_SET_CODE,
        "set_name": "Scryfall Compat Languages",
        "collector_number": "1",
        "type_line": "Artifact",
        "oracle_text": "",
        "lang": "dw",
    }
    return card


def _amber(card_id: str, lang: str, released_at: str) -> dict:
    """One of two printings sharing an address.

    The address lookup breaks ties on prefer_score and then on released_at DESC. The corpus puts
    the Japanese row FIRST on both -- the fixture pins its prefer_score above the English row's, and
    it released later -- on purpose: the English-first rule is only proven by an address where
    English would otherwise lose.
    """
    card = make_raw_card(card_id=card_id, name="Compat Shared Amber")
    card |= {
        "object": "card",
        "oracle_id": AMBER_ORACLE_ID,
        "set": LANG_SET_CODE,
        "set_name": "Scryfall Compat Languages",
        "collector_number": "2",
        "type_line": "Artifact",
        "oracle_text": "",
        "lang": lang,
        "released_at": released_at,
    }
    return card


@pytest.fixture(name="compat_corpus", scope="module")
def compat_corpus_fixture(api_resource: APIResource) -> APIResource:
    """Load this module's cards and their rulings once, then hand back the resource."""
    cards = (
        _bolt(),
        _bear(),
        _delver(),
        _extra(),
        _who(),
        _vault(),
        _signet(),
        _amber(AMBER_JA_ID, "ja", "2021-06-01"),
        _amber(AMBER_EN_ID, "en", "2020-01-01"),
    )
    api_resource.admin._upsert_cards([copy.deepcopy(card) for card in cards])
    with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
        # The ORDER of the pair sharing sfl/2 is the premise of the language tests, and prefer_score
        # is session state: a backfill run by any module before this one scores an English row about
        # forty higher than its foreign twin, which would put the English row first and make
        # "English is not displaced" true for the wrong reason. Pinned so the Japanese row leads
        # whatever ran before.
        cursor.execute(
            "UPDATE magic.cards SET prefer_score = %(score)s WHERE scryfall_id = %(id)s",
            {"score": 200, "id": AMBER_JA_ID},
        )
        cursor.execute(
            "UPDATE magic.cards SET prefer_score = %(score)s WHERE scryfall_id = %(id)s",
            {"score": 100, "id": AMBER_EN_ID},
        )
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


@pytest.fixture(name="by_name_paths", params=["engine", "sql"])
def by_name_paths_fixture(request, compat_corpus: APIResource):
    """The corpus with the engine serving, and again with it gated off so SQL answers.

    The two are not peers -- every by-name route asks the engine first and reaches Postgres only
    when no store is loaded -- but they must agree, or the same request answers differently
    depending on which worker takes it. Running each case twice is what says so.
    """
    saved = settings.enable_engine
    settings.enable_engine = request.param == "engine"
    yield compat_corpus
    settings.enable_engine = saved


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
        body = payload(resp)
        # The character after `didn` is U+2018, and a `warnings: null` sits beside it: Scryfall
        # writes both, and this is the body a client sees most often after a typo. Key ORDER is
        # Scryfall's too.
        assert body["details"] == "You didn\u2018t enter anything to search for."
        assert list(body) == ["object", "code", "status", "warnings", "details"]
        assert body["warnings"] is None

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

    def test_page_beyond_the_results_is_a_422(self, compat_corpus: APIResource):
        """Scryfall separates "matched nothing" from "matched, but not this far in".

        A query with no results is `404 not_found` at every page; a page past the end of a result
        that DID match is `422 validation_error` (measured 2026-08-16). Answering 404 to both told
        a paginating client its query had stopped matching.
        """
        resp = dispatch(compat_corpus, "/cards/search", "q=%21%22Compat+Bolt%22&page=2")
        assert resp.status == falcon.HTTP_422
        body = payload(resp)
        assert body["code"] == "validation_error"
        assert body["details"].startswith("You have paginated beyond the end of these results")
        assert "warnings" not in body

    def test_a_query_that_matched_nothing_keeps_the_404_at_every_page(self, compat_corpus: APIResource):
        for page in ("1", "5"):
            resp = dispatch(compat_corpus, "/cards/search", f"q=%21%22No+Such+Card+At+All%22&page={page}")
            assert resp.status == falcon.HTTP_404
            assert payload(resp)["code"] == "not_found"

    @pytest.mark.parametrize("page", ["0", "-3", "abc", "", "0x2", "1e2", "-0"])
    def test_page_is_scryfalls_to_i_and_clamp_never_a_rejection(self, compat_corpus: APIResource, page):
        """Measured on api.scryfall.com 2026-08-16.

        page=0, page=-3, page=abc and page= all serve page 1, and page=2.5 / page=+2 / page=2abc
        truncate at the first non-digit. This surface answered `400 "The page parameter must be a
        positive integer."` -- a sentence Scryfall does not own and a rejection it does not make.
        """
        resp = dispatch(compat_corpus, "/cards/search", f"q=%21%22Compat+Bolt%22&page={page}")
        assert resp.status == falcon.HTTP_200

    def test_next_page_is_absent_when_the_page_is_the_last(self, compat_corpus: APIResource):
        assert "next_page" not in payload(dispatch(compat_corpus, "/cards/search", "q=%21%22Compat+Bolt%22"))

    def test_next_page_lowercases_q_and_collapses_its_whitespace(self, compat_corpus: APIResource, monkeypatch):
        """Scryfall's own echo, measured 2026-08-16.

        `E:KHM T:Creature OR T:Land` comes back as `e:khm t:creature or t:land`,
        `a:"Rebecca Guay"` as `a:"rebecca guay"` (inside the quotes), `o:/^Whenever/` as
        `o:/^whenever/`, `name:Éowyn` as `name:éowyn`, and edge or doubled whitespace is trimmed
        and collapsed. All are the same query to this parser, so the echo changes only spelling.
        """
        monkeypatch.setattr("api.scryfall_compat.routes.PAGE_SIZE", 1)
        body = payload(dispatch(compat_corpus, "/cards/search", "q=++T%3ACreature++OR+T%3ALand+"))
        assert body["has_more"] is True
        assert "q=t%3Acreature+or+t%3Aland&" in body["next_page"]

    def test_an_order_scryfall_serves_and_this_server_cannot_keeps_its_spelling(self, compat_corpus: APIResource, monkeypatch):
        """`penny` and `review` fall back to `name` here and Scryfall sorts by them.

        Scryfall's echo therefore says `order=penny`. Echoing `name` would round-trip fine (page 2
        falls back the same way) but differ from Scryfall for nothing.
        """
        monkeypatch.setattr("api.scryfall_compat.routes.PAGE_SIZE", 1)
        body = payload(dispatch(compat_corpus, "/cards/search", "q=t%3Acreature&order=penny"))
        assert "order=penny" in body["next_page"]

    def test_next_page_echoes_the_resolved_order_and_unique(self, compat_corpus: APIResource, monkeypatch):
        """Scryfall echoes what it DECIDED, not what it was sent.

        Measured against api.scryfall.com 2026-08-16: `?order=cubecobra` -- an ordering it does not
        recognize -- comes back as `order=name` in `next_page`. A client follows that URL verbatim,
        so echoing the raw parameter hands it a link whose ordering the server already declined,
        and page 2 then pages a different result set than page 1.

        Page size shrunk to 1 so a second page exists over the session's corpus.
        """
        monkeypatch.setattr("api.scryfall_compat.routes.PAGE_SIZE", 1)
        body = payload(dispatch(compat_corpus, "/cards/search", "q=t%3Acreature&order=nosuchorder&unique=nosuchmode"))
        assert body["has_more"] is True
        assert "order=name" in body["next_page"]
        assert "unique=cards" in body["next_page"]

    # ── include_extras ───────────────────────────────────────────────────────

    def test_the_extras_class_is_hidden_by_default(self, compat_corpus: APIResource):
        """Scryfall's default, which is a QUERY-TIME gate and not an import one.

        The printing is stored (#927 stopped dropping the class) and excluded here, which is the
        only arrangement where `include_extras=true` has anything to include.
        """
        resp = dispatch(compat_corpus, "/cards/search", "q=%21%22Compat+Substitute%22")
        assert resp.status == falcon.HTTP_404

    def test_include_extras_true_returns_them(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/search", "q=%21%22Compat+Substitute%22&include_extras=true"))
        assert [card["id"] for card in body["data"]] == [EXTRA_ID]

    def test_a_trigger_term_overrides_an_explicit_false(self, compat_corpus: APIResource):
        """`is:extra` is one of the unconditional triggers, so it can never answer nothing.

        Scryfall FORCES the flag rather than defaulting it -- an explicit `include_extras=false`
        alongside a trigger term is overridden, in the rows and in the echo alike.
        """
        body = payload(
            dispatch(compat_corpus, "/cards/search", "q=is%3Aextra&include_extras=false"),
        )
        assert [card["id"] for card in body["data"]] == [EXTRA_ID]

    @pytest.mark.parametrize(
        "query",
        ['a:"Test Artist"', "wm:setsymbol", "layout:normal", "name:/^comp/", "t:token", "is:extra"],
        ids=["artist", "watermark", "layout", "name-regex", "type-token", "is-extra"],
    )
    def test_every_unconditional_trigger_forces_the_flag(self, compat_corpus: APIResource, monkeypatch, query):
        """Each fires on the TERM, whatever it matches -- see `_extras_triggers` for the probes.

        Asserted on what reaches `_search` rather than on the page, because these terms select
        different cards and only the flag is under test.
        """
        seen = {}
        original = compat_corpus._search
        monkeypatch.setattr(compat_corpus, "_search", lambda **kw: (seen.update(kw), original(**kw))[1])
        dispatch(compat_corpus, "/cards/search", urlencode({"q": query, "include_extras": "false"}))
        assert seen["include_extras"] is True

    @pytest.mark.parametrize(
        "query",
        ["t:creature", "o:damage", 'name:"compat"', "cn:1", "border:black", "is:funny", '!"Compat Bolt"'],
        ids=["type", "oracle", "name-literal", "collector-number", "border", "is-other", "exact-name"],
    )
    def test_a_non_trigger_term_leaves_the_flag_alone(self, compat_corpus: APIResource, monkeypatch, query):
        """A non-trigger term leaves the flag exactly as the caller sent it.

        The rule is SYNTACTIC and narrow: `t:creature` matches 1,742 extras on Scryfall and still
        echoes `include_extras=false`. A rule read off the RESULT SET would fire on every one of
        these.
        """
        seen = {}
        original = compat_corpus._search
        monkeypatch.setattr(compat_corpus, "_search", lambda **kw: (seen.update(kw), original(**kw))[1])
        dispatch(compat_corpus, "/cards/search", urlencode({"q": query, "include_extras": "false"}))
        assert seen["include_extras"] is False

    def test_a_set_term_triggers_only_for_a_set_that_holds_an_extra(self, compat_corpus: APIResource, monkeypatch):
        """The one CONDITIONAL trigger, and the reason the engine folds `sets_with_extras`.

        Measured over 18 sets on 2026-08-16 and the split is perfect: lea/leb/2ed/3ed/sum hold 1,
        4ed/5ed/6ed 2, leg 4, j21 16, hbg 122, unk 506 and every one enables; ust, ice, war, unf,
        por and 7ed hold none and none of them does.
        """
        seen = {}
        original = compat_corpus._search
        monkeypatch.setattr(compat_corpus, "_search", lambda **kw: (seen.update(kw), original(**kw))[1])
        monkeypatch.setattr(compat_corpus, "_sets_with_extras", lambda: frozenset({EXTRAS_SET_CODE}))

        dispatch(compat_corpus, "/cards/search", urlencode({"q": f"e:{EXTRAS_SET_CODE}", "include_extras": "false"}))
        assert seen["include_extras"] is True
        dispatch(compat_corpus, "/cards/search", urlencode({"q": f"e:{SET_CODE}", "include_extras": "false"}))
        assert seen["include_extras"] is False

    def test_the_set_table_is_only_consulted_when_the_query_names_a_set(self, compat_corpus: APIResource, monkeypatch):
        """An ordinary page must not pay for the table -- the guard is the trigger walk, not a cache."""
        calls = []
        monkeypatch.setattr(compat_corpus, "_sets_with_extras", lambda: (calls.append(1), frozenset())[1])
        dispatch(compat_corpus, "/cards/search", "q=t%3Acreature")
        assert calls == []
        dispatch(compat_corpus, "/cards/search", urlencode({"q": f"e:{SET_CODE}"}))
        assert calls == [1]

    def test_an_engine_that_cannot_answer_leaves_the_auto_enable_off(self, compat_corpus: APIResource, monkeypatch):
        """A missing or wrong-build store is an empty table, never a 500 on the search."""

        class _Broken:
            def sets_with_extras(self) -> frozenset[str]:
                msg = "no store"
                raise RuntimeError(msg)

        broken = _Broken()
        monkeypatch.setattr(compat_corpus, "_engine_for_lookup", lambda: broken)
        assert compat_corpus._sets_with_extras() == frozenset()

    def test_next_page_echoes_the_resolved_include_extras(self, compat_corpus: APIResource, monkeypatch):
        """The echo is the value that was SERVED, not the parameter as sent.

        `q=e:lea&include_extras=false` echoes `include_extras=true` on api.scryfall.com AND
        returns the extras; echoing `false` while serving with them on gives a client a link that
        contradicts the page it came from. Measured 2026-08-16 over 57 set probes plus the
        unconditional families -- the echo agreed with what was served in every one.
        """
        monkeypatch.setattr("api.scryfall_compat.routes.PAGE_SIZE", 1)
        body = payload(dispatch(compat_corpus, "/cards/search", urlencode({"q": "name:/^compat/", "include_extras": "false"})))
        assert body["has_more"] is True
        assert "include_extras=true" in body["next_page"]

    def test_next_page_echoes_a_false_it_actually_served(self, compat_corpus: APIResource, monkeypatch):
        monkeypatch.setattr("api.scryfall_compat.routes.PAGE_SIZE", 1)
        body = payload(dispatch(compat_corpus, "/cards/search", "q=t%3Acreature&include_extras=false"))
        assert "include_extras=false" in body["next_page"]

    # ── in-query directives ──────────────────────────────────────────────────

    def test_a_directive_reaches_the_search(self, compat_corpus: APIResource, monkeypatch):
        """#893's fold runs inside `_search`, so this surface gets it by calling the same method."""
        seen = {}
        original = compat_corpus._search
        monkeypatch.setattr(compat_corpus, "_search", lambda **kw: (seen.update(kw), original(**kw))[1])
        dispatch(compat_corpus, "/cards/search", urlencode({"q": "t:creature unique:prints order:cmc dir:desc"}))
        assert seen["unique"] is UniqueOn.PRINTING
        assert seen["orderby"] is CardOrdering.CMC
        assert seen["direction"] is SortDirection.DESC

    def test_a_directive_beats_the_query_parameter(self, compat_corpus: APIResource, monkeypatch):
        """The directive wins, which is measured rather than assumed.

        api.scryfall.com 2026-08-16, in both directions: `unique:prints&unique=cards` answers 387
        and `unique:cards&unique=prints` answers 285.
        """
        seen = {}
        original = compat_corpus._search
        monkeypatch.setattr(compat_corpus, "_search", lambda **kw: (seen.update(kw), original(**kw))[1])
        dispatch(compat_corpus, "/cards/search", urlencode({"q": "t:creature unique:prints", "unique": "cards"}))
        assert seen["unique"] is UniqueOn.PRINTING

    def test_next_page_echoes_the_values_the_directives_decided(self, compat_corpus: APIResource, monkeypatch):
        """`q` echoes VERBATIM, directive and all, so the parameters beside it must agree with it.

        A link carrying `order=name` next to a `q` saying `order:cmc` pages a different result set
        on page 2 than the one page 1 came from.
        """
        monkeypatch.setattr("api.scryfall_compat.routes.PAGE_SIZE", 1)
        body = payload(
            dispatch(
                compat_corpus,
                "/cards/search",
                urlencode({"q": "t:creature order:cmc unique:prints dir:desc"}),
            ),
        )
        assert body["has_more"] is True
        assert "order=cmc" in body["next_page"]
        assert "unique=prints" in body["next_page"]
        assert "dir=desc" in body["next_page"]

    def test_a_directive_warning_is_reported_once(self, compat_corpus: APIResource):
        """The route folds for the echo and `_search` folds for the search; only one may speak."""
        body = payload(dispatch(compat_corpus, "/cards/search", urlencode({"q": "t:creature unique:bogus"})))
        assert body["warnings"] == ['Unknown unique mode "bogus" was ignored']

    def test_page_size_matches_scryfall(self):
        assert PAGE_SIZE == 175

    def test_pretty_emits_indented_json(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/search", "q=%21%22Compat+Bolt%22&pretty=true")
        assert resp.text.startswith('{\n  "object"')

    def test_csv_format_is_scryfalls_eighteen_column_export(self, compat_corpus: APIResource):
        """The header row's bytes are the contract, so they are asserted whole.

        This used to assert `header.startswith("object,id,oracle_id,")` -- the flattened card object
        this route invented. api.scryfall.com exports a SUMMARY instead: eighteen columns, several
        named differently from the JSON keys behind them (`scryfall_id` not `id`, `usd_price` not
        `prices.usd`, one `multiverse_id` rather than the array). Measured 2026-08-16.
        """
        resp = dispatch(compat_corpus, "/cards/search", "q=%21%22Compat+Bolt%22&format=csv")
        # NO charset, unlike every JSON response here -- Scryfall's exactly.
        assert resp.content_type == "text/csv"
        assert resp.headers["content-disposition"] == 'attachment; filename="search.csv"'
        # `has_more` has no envelope in a CSV body, so it rides a header.
        assert resp.headers["x-scryfall-has-more"] == "false"
        header, row = resp.text.splitlines()[:2]
        assert header == (
            "multiverse_id,mtgo_id,set,collector_number,lang,rarity,name,mana_cost,cmc,type_line,"
            "artist,usd_price,usd_foil_price,eur_price,tix_price,image_uri,scryfall_uri,scryfall_id"
        )
        assert row.endswith(BOLT_ID)
        assert resp.text.endswith("\n")

    def test_csv_is_case_sensitive_and_an_unknown_format_is_json(self, compat_corpus: APIResource):
        """`format=CSV` and `format=bogus` both serve JSON on api.scryfall.com -- never an error."""
        for spelling in ("CSV", "bogus", "text", "image"):
            resp = dispatch(compat_corpus, "/cards/search", f"q=%21%22Compat+Bolt%22&format={spelling}")
            assert resp.content_type.startswith("application/json"), spelling
            assert payload(resp)["object"] == "list", spelling

    def test_csv_quotes_only_what_rfc_4180_requires(self):
        """Minimal quoting, and an EMPTY STRING is not the same cell as an absent value.

        Every basic land is the proof: Scryfall's JSON gives it `"mana_cost": ""` and the CSV row
        reads `...,"",0.0,Land,...` -- two bytes where the null price columns beside it have none.
        """
        assert _csv_cell("Lightning Bolt") == "Lightning Bolt"
        assert _csv_cell("Legendary Creature — God // Legendary Creature — Bird") == (
            "Legendary Creature — God // Legendary Creature — Bird"
        )
        assert _csv_cell("Alrund, God of the Cosmos") == '"Alrund, God of the Cosmos"'
        assert _csv_cell('Henzie "Toolbox" Torre') == '"Henzie ""Toolbox"" Torre"'
        assert _csv_cell("") == '""'
        assert _csv_cell(None) == ""

    def test_csv_prices_round_trip_through_float(self):
        """The JSON carries two decimals always; the CSV does not, and a null price is empty."""
        assert _csv_price("60.00") == "60.0"
        assert _csv_price("0.10") == "0.1"
        assert _csv_price("2.57") == "2.57"
        assert _csv_price(None) is None
        assert _csv_cell(_csv_price(None)) == ""

    def test_csv_mana_cost_drops_the_empty_half_of_a_two_image_card(self):
        """Measured: Delver of Secrets is `{U}`, not `{U} // `, and a free MDFC land is `""`."""
        assert _csv_mana_cost({"mana_cost": "{1}{R} // {1}{U}"}) == "{1}{R} // {1}{U}"
        assert _csv_mana_cost({"card_faces": [{"mana_cost": "{U}"}, {"mana_cost": ""}]}) == "{U}"
        assert _csv_mana_cost({"card_faces": [{"mana_cost": ""}, {"mana_cost": ""}]}) == ""
        assert _csv_mana_cost({}) is None

    def test_unparseable_query_is_a_scryfall_400(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/search", "q=%28cmc%3E1")
        assert resp.status == falcon.HTTP_400
        assert payload(resp)["object"] == "error"

    def test_unbalanced_parentheses_get_scryfalls_own_sentence(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/search", "q=t%3Acreature+%28t%3Aland")
        assert resp.status == falcon.HTTP_400
        body = payload(resp)
        assert body["details"] == "Your search contains unclosed parentheses."
        assert body["warnings"] is None

    def test_a_query_whose_every_term_was_ignored_is_a_400_carrying_the_warnings(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/search", "q=subtype%3Aeldrazi")
        assert resp.status == falcon.HTTP_400
        body = payload(resp)
        assert list(body) == ["object", "code", "status", "warnings", "details"]
        assert body["details"] == "All of your terms were ignored."
        assert body["warnings"] == [
            "Invalid expression \u201csubtype:eldrazi\u201d was ignored. Unknown keyword \u201csubtype\u201d.",
        ]

    def test_a_surviving_term_makes_an_ignored_one_a_warning_on_a_200(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/search", "q=f%3Anotaformat+%21%22Compat+Bolt%22"))
        assert body["object"] == "list"
        assert body["total_cards"] == 1
        assert body["warnings"] == [
            "Invalid expression \u201cf:notaformat\u201d was ignored. Unknown game format \u201cnotaformat\u201d",
        ]

    def test_typographic_quotes_reach_the_parser_as_the_quotes_they_stand_for(self, compat_corpus: APIResource):
        """Users paste curly quotes constantly; this surface answered 400 to every one of them."""
        curly = payload(dispatch(compat_corpus, "/cards/search", "q=%21%E2%80%9CCompat+Bolt%E2%80%9D"))
        assert curly["total_cards"] == 1

    def test_a_malformed_regex_is_a_400_carrying_scryfalls_reason(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/search", "q=o%3A%2F%5Bunclosed%2F")
        assert resp.status == falcon.HTTP_400
        assert payload(resp)["warnings"] == [
            "Invalid expression \u201co:/[unclosed/\u201d was ignored. Invalid regular expression: brackets [] not balanced.",
        ]

    @pytest.mark.parametrize("unique", ["card", "printing", "printings", "artwork", "bogus"])
    def test_an_unrecognized_unique_mode_is_silent_as_scryfalls_is(self, compat_corpus: APIResource, unique):
        body = payload(dispatch(compat_corpus, "/cards/search", f"q=%21%22Compat+Bolt%22&unique={unique}"))
        assert "warnings" not in body


class TestVariationsGate:
    """`include_variations`, its auto-enable, and its independence from the extras gate."""

    def test_only_is_variation_forces_the_gate(self):
        """The whole of this gate's auto-enable rule, and it is a FORCE like the extras ones.

        `t:creature or is:variation` sent with `include_variations=false` answers 51,566 on
        api.scryfall.com and echoes true. Nothing that enables EXTRAS enables this: `a:`, `wm:`,
        `layout:`, `name:/^z/`, `t:token`, `is:extra`, `is:oversized`, `is:reserved`,
        `is:rebalanced` and a set term all echo `include_variations=false` (measured 2026-08-16,
        and `e:hho` is 21 bare against 23 with the parameter though hho auto-enables extras).
        """
        forced = routes_module._mentions_is_tag
        assert forced(parse_scryfall_query("is:variation"), "variation") is True
        assert forced(parse_scryfall_query("t:creature or is:variation"), "variation") is True
        assert forced(parse_scryfall_query("-is:variation t:land"), "variation") is True
        for query in ("a:guay", "wm:mirran", "layout:normal", "t:token", "is:extra", "is:oversized", "e:hho"):
            assert forced(parse_scryfall_query(query), "variation") is False, query

    def test_the_two_gates_are_independent(self, compat_corpus: APIResource):
        """Both conjuncts can be spliced at once, and neither subsumes the other.

        `t:creature` is 51,473 bare, 55,454 with extras alone, 51,523 with variations alone and
        55,506 with both; `is:variation` is 93 with variations on and 97 once extras are on too,
        so the classes overlap by 4 printings out of 97.
        """
        for params, expect_variation in (("", True), ("include_variations=true", False)):
            body = payload(dispatch(compat_corpus, "/cards/search", f"q=t%3Acreature&{params}"))
            echoed = body.get("next_page") or ""
            assert ("include_variations=true" in echoed) is not expect_variation or not echoed


class TestExtrasTriggers:
    """`_extras_triggers`, the syntactic rule behind `include_extras`'s auto-enable.

    Pure parse-tree tests: no corpus, because the whole point of the measurement is that the rule
    does NOT depend on what the query matches.
    """

    @pytest.mark.parametrize(
        "query",
        [
            'a:"Wesley Burt"',
            "wm:llorwyn",
            "layout:normal",
            "layout:art_series",
            "name:/^z/",
            "t:token",
            "is:extra",
        ],
    )
    def test_unconditional_triggers(self, query):
        """Each fires on the TERM, not on what it selects.

        `a:"Wesley Burt"` triggers although `a:"Wesley Burt" is:extra` is 0, `name:/zzzqq/` matches
        nothing and still triggers, and `layout:normal` -- the most ordinary value there is --
        triggers. Measured on api.scryfall.com 2026-08-16.
        """
        assert routes_module._extras_triggers(parse_scryfall_query(query)).forced is True

    @pytest.mark.parametrize(
        "query",
        [
            "t:creature",
            "o:draw",
            "o:/^whenever/",
            "ft:death",
            "cn:100",
            "year:1993",
            "border:black",
            "frame:2003",
            "is:funny",
            'name:"lightning"',
            "name:lightning",
            '!"Lightning Bolt"',
            "",
        ],
    )
    def test_probed_non_triggers(self, query):
        """Every one of these was probed and does NOT auto-enable.

        `t:creature`, `o:draw` and `ft:death` match 1,742 / 358 / 26 extras respectively, so a rule
        read off the RESULT SET would fire on all three. This one does not, which is the whole
        finding.
        """
        assert routes_module._extras_triggers(parse_scryfall_query(query)).forced is False

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("e:lea", ("lea",)),
            ("s:khm", ("khm",)),
            ("set:WAR", ("war",)),
            ("e:war or e:lea", ("war", "lea")),
            ("-e:lea t:land", ("lea",)),
        ],
    )
    def test_set_terms_are_collected_lowercased(self, query, expected):
        triggers = routes_module._extras_triggers(parse_scryfall_query(query))
        assert triggers.forced is False
        assert triggers.sets == expected

    def test_a_trigger_propagates_out_of_an_or_and_a_negation(self):
        """Both kinds of trigger propagate through `or` and through negation.

        `(e:lea t:creature) or t:land` enables on api.scryfall.com even though LEA's only extra is
        an enchantment that cannot be in that result set, and `-e:lea t:land` enables while
        `-e:war t:land` does not. Negation does not cancel a trigger, which is what makes the rule
        syntactic rather than semantic.
        """
        assert routes_module._extras_triggers(parse_scryfall_query("t:land or wm:x")).forced is True
        assert routes_module._extras_triggers(parse_scryfall_query("-wm:x t:land")).forced is True
        assert routes_module._extras_triggers(parse_scryfall_query("(e:lea t:creature) or t:land")).sets == ("lea",)

    def test_a_lowered_literal_regex_is_still_a_regex_here(self):
        """The rewrite lowers a metacharacter-free regex to a literal before this walk sees it.

        That USED to be a residual in both directions: `name:/zzzqq/` read as `name:"zzzqq"` and
        missed a trigger Scryfall fires, and `t:/token/` read as `t:token` and fired one Scryfall
        does not. `StringValueNode.regex_derived` records the spelling the rewrite erased, so both
        now answer as Scryfall does. Measured 2026-08-16::

            name:/bolt/  175 = its extras-on count    name:"bolt"  157
            t:token cmc=3  6 (extras auto-enabled)    t:/token/ cmc=3  0
            is:/extra/ cmc=3 and border:/silver/ cmc=3 both answer plain cmc=3 (22,832)

        Every pattern with a metacharacter keeps its RegexValueNode and behaved all along.
        """
        assert routes_module._extras_triggers(parse_scryfall_query("name:/zzzqq/")).forced is True
        assert routes_module._extras_triggers(parse_scryfall_query("name:/^z/")).forced is True
        assert routes_module._extras_triggers(parse_scryfall_query('name:"zzzqq"')).forced is False
        assert routes_module._extras_triggers(parse_scryfall_query("t:/token/")).forced is False
        assert routes_module._extras_triggers(parse_scryfall_query("t:token")).forced is True
        assert routes_module._extras_triggers(parse_scryfall_query("t:/^token$/")).forced is False
        assert routes_module._extras_triggers(parse_scryfall_query("is:/extra/")).forced is False
        assert routes_module._extras_triggers(parse_scryfall_query("border:/silver/")).forced is False

    def test_the_is_and_border_triggers_are_value_specific(self):
        """Five `is:` values and one `border:` value force extras on; their neighbours do not.

        All 32 supported STORED `is:` values were probed for the `include_extras` echo on
        2026-08-16, and `border:gold` is the control that makes `border:silver` a trigger rather
        than a coincidence: every gold border is a World Championship card, so the whole population
        is memorabilia, and it still answers 0 bare against 1,373 with the flag.

        `glossy` is the same point from the other side. It holds NO extras, so the flag cannot move
        its count and a count-based probe calls it unfalsifiable -- and the echo says true anyway,
        because the rule is syntactic.
        """
        for value in ("extra", "oversized", "reserved", "rebalanced", "glossy"):
            assert routes_module._extras_triggers(parse_scryfall_query(f"is:{value}")).forced is True, value
        for value in ("variation", "convention", "judge", "league", "promo", "foil"):
            assert routes_module._extras_triggers(parse_scryfall_query(f"is:{value}")).forced is False, value
        assert routes_module._extras_triggers(parse_scryfall_query("border:silver")).forced is True
        for value in ("gold", "black", "white", "borderless"):
            assert routes_module._extras_triggers(parse_scryfall_query(f"border:{value}")).forced is False, value
        for value in ("1993", "2015", "future"):
            assert routes_module._extras_triggers(parse_scryfall_query(f"frame:{value}")).forced is False, value

    def test_a_derived_is_fires_on_the_term_written_not_on_its_expansion(self):
        """`is:split` is not `layout:split` for this rule, though the rewrite makes them one tree.

        Measured on api.scryfall.com 2026-08-16: `is:split` echoes `include_extras=false` and
        answers 327 where `layout:split` echoes true and answers 347. All 90 derived values were
        probed one at a time; twelve fire and 78 do not, and the split follows no structural rule --
        `is:mdfc` fires with zero extras in its population, `is:stamped` does not fire with 696.
        """
        # Derived, expands to something that DOES trigger, and does not trigger there. The first
        # six expand to `layout:`, an unconditional trigger; `is:commander` expands to a subtree
        # ending in `-banned:commander`, which is one too.
        for query in (
            "is:split",
            "is:flip",
            "is:transform",
            "is:meld",
            "is:leveler",
            "is:adventure",
            "is:commander",
        ):
            assert routes_module._extras_triggers(parse_scryfall_query(query)).forced is False, query
        # Derived AND measured triggers. `is:dfc` is the one that makes this a list rather than a
        # rule: it is exactly the union of transform / modal_dfc / meld, and it fires where two of
        # its three parts do not.
        for query in ("is:mdfc", "is:dfc"):
            assert routes_module._extras_triggers(parse_scryfall_query(query)).forced is True, query
        # The spelling the caller DID write still fires beside the one they did not.
        assert routes_module._extras_triggers(parse_scryfall_query("is:split layout:normal")).forced is True
        # And a set term inside an expansion is not a set the caller named: nothing widens by
        # accident through the conditional arm either.
        assert routes_module._extras_triggers(parse_scryfall_query("is:split")).sets == ()

    def test_banned_triggers_wholesale_and_f_only_at_premodern(self):
        """Every legality alias binds to `card_legalities`, so the alias separates them.

        `banned:` fires at every value probed while `restricted:` does not, and of the 21 format
        values `premodern` is the only one that fires -- `legal:premodern` fires too, so it is the
        value rather than the alias. Measured 2026-08-16.
        """
        for value in ("legacy", "vintage", "modern", "pauper"):
            assert routes_module._extras_triggers(parse_scryfall_query(f"banned:{value}")).forced is True, value
        for query in ("f:premodern", "format:premodern", "legal:premodern", "-f:premodern t:land"):
            assert routes_module._extras_triggers(parse_scryfall_query(query)).forced is True, query
        for query in ("restricted:vintage", "f:pauper", "f:legacy", "f:vintage", "legal:standard", "f:oldschool"):
            assert routes_module._extras_triggers(parse_scryfall_query(query)).forced is False, query


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

    Every case runs TWICE, through `by_name_paths`: once with the engine serving and once with it
    gated off so the SQL fallback answers. The fallback is what a worker with no store loaded uses,
    so a key rule expressed in only one of the two is a rule this API applies only sometimes.
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
    def test_either_face_names_the_card_on_both_surfaces(self, by_name_paths: APIResource, name):
        """The BACK face is the half the predicate this replaced never looked at.

        It compared `split_part(card_name, ' // ', 1)` and the whole name, so
        `{"name":"Compat Aberration"}` missed a card api.scryfall.com resolves.
        """
        assert self._resolves(by_name_paths, name) == DELVER_ID
        assert self._exact(by_name_paths, name) == DELVER_ID

    def test_the_joined_name_is_exacts_key_and_not_a_collection_identifiers(self, by_name_paths: APIResource):
        """The one key the two surfaces disagree about."""
        joined = "Compat Delver // Compat Aberration"
        assert self._exact(by_name_paths, joined) == DELVER_ID
        assert self._resolves(by_name_paths, joined) is None

    def test_a_five_part_name_is_a_key_and_none_of_its_parts_is(self, by_name_paths: APIResource):
        """EXACTLY two halves is what makes a face key, which `split_part` cannot say on its own."""
        assert self._resolves(by_name_paths, WHO_NAME) == WHO_ID
        assert self._exact(by_name_paths, WHO_NAME) == WHO_ID
        for part in WHO_NAME.split(" // "):
            assert self._resolves(by_name_paths, part) is None, part
            assert self._exact(by_name_paths, part) is None, part

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
    def test_names_compare_collated(self, by_name_paths: APIResource, spelling):
        """Accent, apostrophe, hyphen and spacing all drop out of the comparison."""
        assert self._resolves(by_name_paths, spelling) == VAULT_ID
        assert self._exact(by_name_paths, spelling) == VAULT_ID

    def test_a_collated_face_does_not_straddle_the_join(self, by_name_paths: APIResource):
        """The halves are split BEFORE either side is collated, so `" // "` is not just punctuation.

        Collating first would delete the join and let `compatdelvercompataberration` -- and worse,
        a needle spanning it -- read as a face.
        """
        assert self._resolves(by_name_paths, "compatdelver") == DELVER_ID
        assert self._resolves(by_name_paths, "delvercompataberration") is None
        assert self._resolves(by_name_paths, "compatdelvercompataberration") is None
        assert self._exact(by_name_paths, "compatdelvercompataberration") == DELVER_ID

    def test_leading_and_trailing_whitespace_is_not_part_of_the_name(self, by_name_paths: APIResource):
        """`{"name":"  Lightning Bolt  "}` resolves on api.scryfall.com; this compared it as posted."""
        assert self._resolves(by_name_paths, "  Compat Bolt  ") == BOLT_ID

    @pytest.mark.parametrize("needle", ["Compat", "Bolt", "Vault", "", "   "])
    def test_a_substring_of_a_name_is_not_a_key(self, by_name_paths: APIResource, needle):
        """A name lookup, not a search.

        `{"name":"Elves"}` answers *Elves* (ffdn/9) on api.scryfall.com -- the card actually named
        that, not one of the hundreds whose names contain the word, which is what a containment
        lookup cut to one row would have answered.
        """
        assert self._resolves(by_name_paths, needle) is None

    def test_set_filters_the_lookup(self, by_name_paths: APIResource):
        """`{"name":"Delver of Secrets","set":"mid"}` answers mid/47 -- the card, in the set asked for.

        A card with no printing in that set drops out of the lookup rather than answering another
        printing, which is the same thing `exact=&set=` does.
        """
        assert self._resolves(by_name_paths, "Compat Bolt", SET_CODE) == BOLT_ID
        assert self._resolves(by_name_paths, "Compat Bolt", NAME_SET_CODE) is None
        assert self._resolves(by_name_paths, VAULT_NAME, NAME_SET_CODE) == VAULT_ID

    def test_an_unresolvable_name_comes_back_in_not_found(self, by_name_paths: APIResource):
        """Reported, not dropped: the client has to be able to tell which identifier failed."""
        body = self._collection(by_name_paths, "Compat Delver // Compat Aberration")
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

    # ── the extras gate, which this route ran without ────────────────────────
    #
    # A DRAW, so the assertions are built to be deterministic rather than sampled: the query
    # matches the extras printing and nothing else, so the gate turns the whole match set empty
    # and the answer is the 404 every time. Measured on api.scryfall.com 2026-08-17 with the same
    # shape — `/cards/random?q=t:goblin cmc=0` (no trigger, all extras) is 404 there, and
    # `&include_extras=true` returns q07/T12.

    def test_the_extras_class_is_hidden_from_the_draw(self, compat_corpus: APIResource):
        """`!"…"` fires no trigger, so this is the default lane, and the class is excluded in it."""
        resp = dispatch(compat_corpus, "/cards/random", "q=%21%22Compat+Substitute%22")
        assert resp.status == falcon.HTTP_404

    def test_include_extras_true_draws_the_extra(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/random", "q=%21%22Compat+Substitute%22&include_extras=true"))
        assert body["id"] == EXTRA_ID

    def test_a_trigger_term_draws_it_without_the_flag(self, compat_corpus: APIResource):
        """`is:extra` is an unconditional trigger, so the gate opens on the term alone.

        The same rule `/cards/search` runs, read with the same helper rather than a second copy of
        it — a query that names the class can never answer nothing.
        """
        body = payload(dispatch(compat_corpus, "/cards/random", "q=is%3Aextra&include_extras=false"))
        assert body["id"] == EXTRA_ID

    def test_a_set_term_on_an_extras_set_is_the_conditional_trigger(self, compat_corpus: APIResource, monkeypatch):
        """The one trigger that asks the store: a set term enables extras iff that set holds one.

        The table is stubbed for the same reason the `/cards/search` twin stubs it — it is folded
        into the archive at build, so a fixture engine is not the thing under test here.
        """
        monkeypatch.setattr(compat_corpus, "_sets_with_extras", lambda: frozenset({EXTRAS_SET_CODE}))
        body = payload(dispatch(compat_corpus, "/cards/random", f"q=e%3A{EXTRAS_SET_CODE}"))
        assert body["id"] == EXTRA_ID

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

    def test_an_address_no_english_printing_carries_falls_through_to_the_printing_that_does(self, compat_corpus: APIResource):
        """`/cards/hoc/95` is the Dwarvish Arcane Signet on api.scryfall.com, measured 2026-09-11.

        There is no English hoc/95. The segment-less form answers the printing that exists; the
        hardcoded `en` it replaced answered a 404 that `/cards/hoc/95/dw` contradicted.
        """
        card = payload(dispatch(compat_corpus, f"/cards/{LANG_SET_CODE}/1"))
        assert (card["id"], card["lang"]) == (SIGNET_ID, "dw")

    def test_english_answers_the_segment_less_form_even_when_a_foreign_row_outranks_it(self, compat_corpus: APIResource):
        """The fall-through is for an address with NO English row; it never displaces one that has.

        The premise comes first: under the lookup's own ordering the Japanese row is FIRST at this
        address, so a plain address lookup answers it and the assertion after cannot pass by an
        accident of ordering.
        """
        plain = compat_corpus._fetch_one_card(
            "lower(card_set_code) = %(set_code)s AND collector_number = %(number)s",
            {"set_code": LANG_SET_CODE, "number": "2"},
        )
        assert plain["id"] == AMBER_JA_ID
        assert payload(dispatch(compat_corpus, f"/cards/{LANG_SET_CODE}/2"))["id"] == AMBER_EN_ID

    def test_a_named_language_selects_that_row_where_two_share_the_address(self, compat_corpus: APIResource):
        assert payload(dispatch(compat_corpus, f"/cards/{LANG_SET_CODE}/2/ja"))["id"] == AMBER_JA_ID

    @pytest.mark.parametrize(
        ("number", "lang"),
        [("1", "en"), ("2", "de")],
        ids=["english-of-a-foreign-only-address", "a-language-nobody-printed"],
    )
    def test_a_named_language_no_printing_carries_is_still_a_404(self, compat_corpus: APIResource, number, lang):
        """`/cards/m15/18/de` is a 404 on api.scryfall.com: only the ABSENT segment falls through."""
        assert dispatch(compat_corpus, f"/cards/{LANG_SET_CODE}/{number}/{lang}").status == falcon.HTTP_404

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

    def test_set_and_number_falls_through_to_a_foreign_only_row_beside_an_english_answer(self, compat_corpus: APIResource):
        """`{"set":"hoc","collector_number":"95"}` finds the Dwarvish row with an empty not_found.

        Measured on api.scryfall.com 2026-09-11. In the same batch, an address that has an English
        printing answers the English one, whatever a foreign row sharing the number would score.
        """
        body = payload(
            dispatch(
                compat_corpus,
                "/cards/collection",
                method="POST",
                body={
                    "identifiers": [
                        {"set": LANG_SET_CODE, "collector_number": "1"},
                        {"set": LANG_SET_CODE, "collector_number": "2"},
                    ],
                },
            ),
        )
        assert {card["id"] for card in body["data"]} == {SIGNET_ID, AMBER_EN_ID}
        assert body["not_found"] == []

    def test_over_the_limit_is_scryfalls_400_not_a_422(self, compat_corpus: APIResource):
        """`400 bad_request` with Scryfall's own sentence, measured 2026-08-16.

        This asserted a `422 validation_error` with wording of its own, so a client string-matching
        Scryfall's message saw neither the status nor the text it was matching on.
        """
        resp = dispatch(
            compat_corpus,
            "/cards/collection",
            method="POST",
            body={"identifiers": [{"id": BOLT_ID}] * 76},
        )
        assert resp.status == falcon.HTTP_400
        body = payload(resp)
        assert body["code"] == "bad_request"
        assert body["details"] == "The `identifiers` list must have at least 1 and no more than 75 references."

    def test_a_body_without_identifiers_is_the_count_sentence(self, compat_corpus: APIResource):
        """An ABSENT list reads as an empty one, so it earns the count sentence rather than its own."""
        resp = dispatch(compat_corpus, "/cards/collection", method="POST", body={"cards": []})
        assert resp.status == falcon.HTTP_400
        assert payload(resp)["details"] == "The `identifiers` list must have at least 1 and no more than 75 references."

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

    def test_a_page_past_the_end_is_a_422(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards", "page=100000")
        assert resp.status == falcon.HTTP_422
        assert payload(resp)["code"] == "validation_error"

    def test_a_non_positive_page_serves_page_one(self, compat_corpus: APIResource):
        """The same never-rejecting `page` as /cards/search -- one rule, because it is one parameter."""
        resp = dispatch(compat_corpus, "/cards", "page=0")
        assert resp.status == falcon.HTTP_200
        assert payload(resp)["object"] == "list"

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

    def test_collection_returns_one_entry_per_identifier_including_duplicates(self, compat_corpus: APIResource):
        """`data` answers the LIST that was sent, not the set of cards it names.

        Deduplicating by card id looked like a courtesy and broke the route's contract: a client
        posting a deck list with four copies of a card got one object back. Measured 2026-08-16 --
        three identical `{id}` identifiers return three card objects.
        """
        result = self._client(compat_corpus).simulate_post(
            "/cards/collection",
            json={"identifiers": [{"id": BOLT_ID}, {"id": BOLT_ID}, {"id": BOLT_ID}]},
        )
        assert result.status_code == 200
        assert [card["id"] for card in orjson.loads(result.content)["data"]] == [BOLT_ID] * 3

    @pytest.mark.parametrize(
        ("identifier", "tail"),
        [
            # `arena_id` is the case worth having: a real key on a card object, and simply not a
            # collection identifier, so a client reaching for it used to be told the card is missing.
            ({"arena_id": 67330}, ""),
            ({}, ""),
            ({"nonsense": "x"}, ""),
            ({"set": "khm"}, "set"),
            ({"set": "khm", "lang": "ja"}, "set"),
            ({"set": "khm", "zzz": 1}, "set"),
            ({"collector_number": "1"}, "collector_number"),
            ({"collector_number": "1", "lang": "en"}, "collector_number"),
        ],
    )
    def test_collection_rejects_an_identifier_whose_keys_name_no_lookup(
        self, compat_corpus: APIResource, identifier: dict, tail: str
    ):
        """Every string here is measured, the tail included: it lists the RECOGNIZED keys present."""
        result = self._client(compat_corpus).simulate_post("/cards/collection", json={"identifiers": [identifier]})
        assert result.status_code == 400
        body = orjson.loads(result.content)
        assert body["code"] == "bad_request"
        assert body["details"] == f"Invalid identifier schema: {tail}"

    @pytest.mark.parametrize("key", ["mtgo_id", "multiverse_id"])
    def test_collection_rejects_a_non_integer_id(self, compat_corpus: APIResource, key: str):
        """`A` rather than `An` -- Scryfall picks the article per field, not per sentence."""
        result = self._client(compat_corpus).simulate_post("/cards/collection", json={"identifiers": [{key: "abc"}]})
        assert result.status_code == 400
        assert orjson.loads(result.content)["details"] == f"A `{key}` identifier must be an integer: abc"

    @pytest.mark.parametrize(
        "body",
        [
            {"identifiers": []},
            {},
            {"nope": True},
            {"identifiers": [None]},
            {"identifiers": ["Lightning Bolt"]},
            {"identifiers": [{"id": "x"}] * (MAX_COLLECTION_IDENTIFIERS + 1)},
        ],
    )
    def test_collection_answers_one_sentence_to_every_list_shaped_mistake(self, compat_corpus: APIResource, body: dict):
        """An empty, absent, over-long or non-object-holding list is all the same 400.

        `{"identifiers": []}` used to answer `200 {"data": []}`, telling the client its (empty)
        question had an (empty) answer, and the over-long one used to be a `422 validation_error`
        with wording of its own. The cap check also runs BEFORE identifier validation -- the 76
        entries here carry `{"id": "x"}`, which is not a valid UUID, and the count sentence still wins.
        """
        result = self._client(compat_corpus).simulate_post("/cards/collection", json=body)
        assert result.status_code == 400
        payload_body = orjson.loads(result.content)
        assert payload_body["code"] == "bad_request"
        assert payload_body["details"] == "The `identifiers` list must have at least 1 and no more than 75 references."
        assert result.headers["cache-control"] == "no-cache"

    def test_collection_distinguishes_a_missing_list_from_a_non_array_one(self, compat_corpus: APIResource):
        """An absent list reads as an empty one; a present-but-not-a-list one gets its own sentence."""
        result = self._client(compat_corpus).simulate_post("/cards/collection", json={"identifiers": {}})
        assert result.status_code == 400
        assert orjson.loads(result.content)["details"] == "The `identifiers` list must be a JSON array."

    def test_collection_envelope_has_no_has_more(self, compat_corpus: APIResource):
        """Scryfall's collection List is `{object, not_found, data}` -- it does not paginate."""
        result = self._client(compat_corpus).simulate_post("/cards/collection", json={"identifiers": [{"id": BOLT_ID}]})
        assert list(orjson.loads(result.content)) == ["object", "not_found", "data"]

    @pytest.mark.parametrize(
        ("identifier", "why"),
        [
            ("00000000-0000-0000-0000-000000000000", "nil uuid: version 0"),
            ("00000000-0000-0000-0000-000000000001", "not the zero VALUE: version 0"),
            ("3f2c8e5d-91b7-1a6e-bd12-4f5a9c7e8b01", "version 1"),
            ("3f2c8e5d-91b7-7a6e-bd12-4f5a9c7e8b01", "version 7"),
            ("3f2c8e5d-91b7-4a6e-cd12-4f5a9c7e8b01", "variant c"),
            ("3f2c8e5d-91b7-4a6e-0d12-4f5a9c7e8b01", "variant 0"),
            ("not-a-uuid", "not a uuid at all"),
            ("", "empty string"),
            ("7673784edb4b43a18d551bb9fc1e284f", "no dashes"),
        ],
    )
    def test_collection_rejects_a_non_v4_identifier(self, compat_corpus: APIResource, identifier: str, why: str):
        """Measured against api.scryfall.com 2026-08-16: a malformed identifier UUID 400s the request."""
        result = self._client(compat_corpus).simulate_post("/cards/collection", json={"identifiers": [{"id": identifier}]})
        assert result.status_code == 400, why
        assert orjson.loads(result.content)["code"] == "bad_request"

    @pytest.mark.parametrize(
        ("identifier", "why"),
        [
            ("00000000-0000-4000-8000-000000000000", "the ZERO value wearing v4's nibbles"),
            ("3f2c8e5d-91b7-4a6e-9d12-4f5a9c7e8b01", "valid, unknown: variant 9"),
            ("3f2c8e5d-91b7-4a6e-bd12-4f5a9c7e8b01", "valid, unknown: variant b"),
        ],
    )
    def test_collection_reports_a_valid_unknown_uuid_as_not_found(self, compat_corpus: APIResource, identifier: str, why: str):
        """The other half of the boundary: the rule is the SHAPE, not the value and not existence."""
        result = self._client(compat_corpus).simulate_post("/cards/collection", json={"identifiers": [{"id": identifier}]})
        assert result.status_code == 200, why
        body = orjson.loads(result.content)
        assert body["not_found"] == [{"id": identifier}]
        assert body["data"] == []

    def test_collection_echoes_the_offending_key_and_value(self, compat_corpus: APIResource):
        """30 characters then U+2026, and a short value whole -- both measured."""
        client = self._client(compat_corpus)
        long_value = client.simulate_post(
            "/cards/collection", json={"identifiers": [{"id": "00000000-0000-0000-0000-000000000000"}]}
        )
        assert (
            orjson.loads(long_value.content)["details"]
            == "An `id` identifier must be a valid UUID: 00000000-0000-0000-0000-000000\u2026"
        )
        short = client.simulate_post("/cards/collection", json={"identifiers": [{"oracle_id": "not-a-uuid"}]})
        assert orjson.loads(short.content)["details"] == "An `oracle_id` identifier must be a valid UUID: not-a-uuid"

    def test_one_malformed_identifier_400s_the_whole_batch(self, compat_corpus: APIResource):
        """Wherever it sits, and the FIRST malformed one is the one reported."""
        client = self._client(compat_corpus)
        bad = {"id": "00000000-0000-0000-0000-000000000000"}
        for identifiers in ([{"id": BOLT_ID}, bad], [bad, {"id": BOLT_ID}]):
            result = client.simulate_post("/cards/collection", json={"identifiers": identifiers})
            assert result.status_code == 400
            assert "00000000-0000-0000-0000-000000" in orjson.loads(result.content)["details"]

    def test_non_uuid_identifier_kinds_are_untouched_by_the_uuid_rule(self, compat_corpus: APIResource):
        """`{set, collector_number}` nonsense is a MISS, not a bad request -- only the three UUID keys are checked."""
        result = self._client(compat_corpus).simulate_post(
            "/cards/collection",
            json={"identifiers": [{"set": SET_CODE, "collector_number": "zzz"}, {"name": "Nothing At All Compat"}]},
        )
        assert result.status_code == 200
        assert len(orjson.loads(result.content)["not_found"]) == 2

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
