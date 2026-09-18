"""Integration tests for the Scryfall-compatible `/sets`, `/catalog` and `/symbology` routes.

Everything here dispatches through `_handle`, the same path a real request takes. The reference
tables are global rather than per-card, so unlike the `/cards/*` tests this module cannot scope its
fixtures to rows it owns: it seeds all three tables and asserts against exactly what it seeded.
That is safe because nothing else in the suite writes them, and it is stated here so a future test
that does write them knows what it would break.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import falcon
import falcon.testing
import orjson
import pytest
from psycopg.types.json import Jsonb

if TYPE_CHECKING:
    from api.api_resource import APIResource

BOLT_SET_ID = "aaaaaaaa-0000-4000-8000-aaaaaaaaaaaa"
OLD_SET_ID = "bbbbbbbb-0000-4000-8000-bbbbbbbbbbbb"
TCGPLAYER_ID = 90210


def _set_object(set_id: str, code: str, name: str, released: str, tcgplayer_id: int | None) -> dict[str, Any]:
    """A Set object shaped like Scryfall's, including the fields no card carries."""
    payload = {
        "object": "set",
        "id": set_id,
        "code": code,
        "mtgo_code": code,
        "arena_code": code,
        "name": name,
        "uri": f"https://api.scryfall.com/sets/{set_id}",
        "scryfall_uri": f"https://scryfall.com/sets/{code}",
        "search_uri": f"https://api.scryfall.com/cards/search?q=e%3A{code}",
        "released_at": released,
        "set_type": "expansion",
        "card_count": 135,
        "digital": False,
        "nonfoil_only": False,
        "foil_only": False,
        "icon_svg_uri": f"https://svgs.scryfall.io/sets/{code}.svg",
    }
    if tcgplayer_id is not None:
        payload["tcgplayer_id"] = tcgplayer_id
    return payload


SETS = [
    _set_object(BOLT_SET_ID, "zzt", "Compat Test Set", "2026-01-01", TCGPLAYER_ID),
    _set_object(OLD_SET_ID, "zzo", "Compat Older Set", "1999-01-01", None),
]

SYMBOLS = [
    {"object": "card_symbol", "symbol": "{ZT}", "svg_uri": "https://svgs.test/zt.svg", "english": "compat tap", "cmc": 0.0},
    {"object": "card_symbol", "symbol": "{ZW}", "svg_uri": "https://svgs.test/zw.svg", "english": "compat white", "cmc": 1.0},
]

CREATURE_TYPES = ["Compat Beast", "Compat Wizard"]


@pytest.fixture(name="reference_corpus", scope="module")
def reference_corpus_fixture(api_resource: APIResource) -> APIResource:
    """Seed the three reference tables once, then hand back the resource."""
    with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
        cursor.execute("DELETE FROM magic.sets")
        cursor.executemany(
            "INSERT INTO magic.sets (id, code, tcgplayer_id, position, set_object) VALUES (%s, %s, %s, %s, %s)",
            [
                (entry["id"], entry["code"], entry.get("tcgplayer_id"), position, Jsonb(entry))
                for position, entry in enumerate(SETS)
            ],
        )
        cursor.execute("DELETE FROM magic.card_symbols")
        cursor.executemany(
            "INSERT INTO magic.card_symbols (symbol, position, symbol_object) VALUES (%s, %s, %s)",
            [(entry["symbol"], position, Jsonb(entry)) for position, entry in enumerate(SYMBOLS)],
        )
        cursor.execute(
            "INSERT INTO magic.catalogs (name, entries) VALUES (%s, %s) "
            "ON CONFLICT (name) DO UPDATE SET entries = EXCLUDED.entries",
            ("creature-types", Jsonb(CREATURE_TYPES)),
        )
        conn.commit()
    api_resource.admin._clear_caches()
    return api_resource


def dispatch(api: APIResource, path: str, query_string: str = "", *, method: str = "GET") -> falcon.Response:
    """Run one request through `_handle` and return the Falcon response."""
    environ = falcon.testing.create_environ(path=path, query_string=query_string, method=method, body="")
    req = falcon.Request(environ)
    resp = falcon.Response()
    api._handle(req, resp)
    return resp


def payload(resp: falcon.Response) -> dict[str, Any]:
    """Decode a response body regardless of whether it was set as media or as text."""
    if resp.media is not None:
        return resp.media
    return orjson.loads(resp.render_body())


class TestSetsListing:
    """GET /sets."""

    def test_returns_a_list_object(self, reference_corpus: APIResource) -> None:
        body = payload(dispatch(reference_corpus, "/sets"))
        assert body["object"] == "list"
        assert body["has_more"] is False
        assert [entry["code"] for entry in body["data"]] == ["zzt", "zzo"]

    def test_preserves_scryfalls_own_ordering(self, reference_corpus: APIResource) -> None:
        """Ordered by the stored position, not recomputed — sets sharing a date have no derivable order."""
        body = payload(dispatch(reference_corpus, "/sets"))
        assert body["data"][0]["released_at"] > body["data"][1]["released_at"]

    def test_serves_the_fields_no_card_carries(self, reference_corpus: APIResource) -> None:
        """The whole reason sets are mirrored rather than derived."""
        first = payload(dispatch(reference_corpus, "/sets"))["data"][0]
        for field in ("icon_svg_uri", "mtgo_code", "arena_code", "card_count", "tcgplayer_id"):
            assert field in first, field


class TestSetLookup:
    """GET /sets/:code, /sets/:id and /sets/tcgplayer/:id."""

    def test_by_set_code(self, reference_corpus: APIResource) -> None:
        body = payload(dispatch(reference_corpus, "/sets/zzt"))
        assert body["object"] == "set"
        assert body["id"] == BOLT_SET_ID

    def test_set_codes_are_case_insensitive(self, reference_corpus: APIResource) -> None:
        assert payload(dispatch(reference_corpus, "/sets/ZZT"))["id"] == BOLT_SET_ID

    def test_by_scryfall_id(self, reference_corpus: APIResource) -> None:
        assert payload(dispatch(reference_corpus, f"/sets/{BOLT_SET_ID}"))["code"] == "zzt"

    def test_by_tcgplayer_id(self, reference_corpus: APIResource) -> None:
        assert payload(dispatch(reference_corpus, f"/sets/tcgplayer/{TCGPLAYER_ID}"))["code"] == "zzt"

    @pytest.mark.parametrize(
        "path",
        ["/sets/zzz", "/sets/tcgplayer/999999", "/sets/tcgplayer", "/sets/00000000-0000-4000-8000-000000000000"],
    )
    def test_a_set_miss_carries_scryfalls_own_wording(self, reference_corpus: APIResource, path: str) -> None:
        """Not the cards surface's generic body, and not a message about the id being absent.

        Captured from api.scryfall.com on 2026-08-12, including the `/sets/tcgplayer` shape: it
        answers the ordinary set miss there too.
        """
        resp = dispatch(reference_corpus, path)
        assert resp.status == falcon.HTTP_404
        assert payload(resp)["details"] == "No Magic set found for the given code or ID"

    def test_an_unknown_code_is_a_404(self, reference_corpus: APIResource) -> None:
        resp = dispatch(reference_corpus, "/sets/nope")
        assert resp.status == falcon.HTTP_404
        assert payload(resp)["object"] == "error"

    def test_an_unknown_tcgplayer_id_is_a_404(self, reference_corpus: APIResource) -> None:
        assert dispatch(reference_corpus, "/sets/tcgplayer/1").status == falcon.HTTP_404

    def test_a_non_numeric_tcgplayer_id_is_a_404_not_a_500(self, reference_corpus: APIResource) -> None:
        """The segment reaches the handler as text, so the int() has to be guarded."""
        assert dispatch(reference_corpus, "/sets/tcgplayer/abc").status == falcon.HTTP_404

    def test_a_set_with_no_tcgplayer_id_is_still_addressable_by_code(self, reference_corpus: APIResource) -> None:
        assert payload(dispatch(reference_corpus, "/sets/zzo"))["id"] == OLD_SET_ID


class TestCatalog:
    """GET /catalog/:name."""

    def test_returns_a_catalog_object(self, reference_corpus: APIResource) -> None:
        body = payload(dispatch(reference_corpus, "/catalog/creature-types"))
        assert body["object"] == "catalog"
        assert body["total_values"] == len(CREATURE_TYPES)
        assert body["data"] == CREATURE_TYPES

    def test_a_catalog_carries_its_own_uri(self, reference_corpus: APIResource) -> None:
        """Measured: `/catalog/*` sends `uri`, and it sits between `object` and `total_values`.

        `/cards/autocomplete` answers the same object with no `uri` at all, which is why
        `catalog_object` takes one rather than always building one -- see the test below.
        """
        body = payload(dispatch(reference_corpus, "/catalog/creature-types"))
        assert body["uri"] == "https://api.scryfall.com/catalog/creature-types"
        assert list(body) == ["object", "uri", "total_values", "data"]

    def test_an_unknown_catalog_is_a_404(self, reference_corpus: APIResource) -> None:
        resp = dispatch(reference_corpus, "/catalog/not-a-catalog")
        assert resp.status == falcon.HTTP_404
        body = payload(resp)
        assert body["object"] == "error"
        # Scryfall's catalog miss has no "Please double-check your URI and try again." tail, where
        # the cards surface's generic body does. Measured, not assumed.
        assert body["details"] == "The requested object or REST method was not found."

    def test_a_known_but_unimported_catalog_is_empty_rather_than_missing(self, reference_corpus: APIResource) -> None:
        """The name is real, so 404 would tell a client the endpoint does not exist."""
        body = payload(dispatch(reference_corpus, "/catalog/watermarks"))
        assert body["object"] == "catalog"
        assert body["total_values"] == 0

    def test_the_name_is_CASE_SENSITIVE(self, reference_corpus: APIResource) -> None:  # noqa: N802
        """`/catalog/Creature-Types` is a 404 on api.scryfall.com (measured 2026-08-16).

        This asserted the opposite. Folding the case made the route answer a URL Scryfall does not
        serve, which is the same class of mistake as failing to answer one it does -- and it is the
        worse direction of the two, because a client cannot discover it from a 200.
        """
        resp = dispatch(reference_corpus, "/catalog/Creature-Types")
        assert resp.status == falcon.HTTP_404
        assert payload(resp)["object"] == "error"
        assert payload(dispatch(reference_corpus, "/catalog/creature-types"))["data"] == CREATURE_TYPES

    def test_a_path_miss_is_no_cache_where_a_data_miss_keeps_the_data_tier(self, reference_corpus: APIResource) -> None:
        """The tier splits by what the 404 is a statement ABOUT, and Scryfall really sends both.

        `/sets/zzzz` -- a well-formed set lookup that found nothing -- is `public`, the same tier the
        answer would have had. `/catalog/not-a-catalog` and `/sets/khm/extra` are `no-cache`. All
        three were `public` here, so a mistyped catalog name was held at every edge.
        """
        assert dispatch(reference_corpus, "/catalog/not-a-catalog").headers["cache-control"] == "no-cache"
        assert dispatch(reference_corpus, "/sets/zzzz").headers["cache-control"] == "public"

    def test_a_wrong_method_on_the_scryfall_surface_is_scryfalls_404(self, reference_corpus: APIResource) -> None:
        """404 with the ordinary `not_found` object and NO `Allow`, which is what Scryfall answers.

        Measured 2026-08-16 across eight requests -- POST, PUT, DELETE and PATCH against
        `/cards/search`, `/cards/named`, `/cards/collection`, `/cards/:id` and `/sets`. Not one
        carries `Allow`. 405 is the more correct HTTP answer in the abstract and would have needed an
        error code no measurement backs, since api.scryfall.com never emits a 405 to measure.
        """
        resp = dispatch(reference_corpus, "/sets", method="DELETE")
        assert resp.status == falcon.HTTP_404
        assert "allow" not in resp.headers
        assert payload(resp) == {
            "object": "error",
            "code": "not_found",
            "status": 404,
            "details": "The requested object or REST method was not found.",
        }

    def test_an_over_long_path_is_a_route_miss_not_a_set_miss(self, reference_corpus: APIResource) -> None:
        """`/sets/khm/extra` said "No Magic set found ..." -- about a set that was fine."""
        resp = dispatch(reference_corpus, "/sets/zzt/extra")
        assert resp.status == falcon.HTTP_404
        assert payload(resp)["details"] == "The requested object or REST method was not found."
        assert resp.headers["cache-control"] == "no-cache"


class TestSymbology:
    """GET /symbology."""

    def test_returns_every_symbol_in_order(self, reference_corpus: APIResource) -> None:
        body = payload(dispatch(reference_corpus, "/symbology"))
        assert body["object"] == "list"
        assert [entry["symbol"] for entry in body["data"]] == ["{ZT}", "{ZW}"]

    def test_serves_the_fields_no_card_carries(self, reference_corpus: APIResource) -> None:
        assert payload(dispatch(reference_corpus, "/symbology"))["data"][0]["svg_uri"].startswith("https://")


class TestParseMana:
    """GET /symbology/parse-mana."""

    def test_parses_a_cost(self, reference_corpus: APIResource) -> None:
        body = payload(dispatch(reference_corpus, "/symbology/parse-mana", "cost=RUW"))
        assert body["object"] == "mana_cost"
        assert body["cost"] == "{U}{R}{W}"
        assert body["cmc"] == 3.0

    def test_a_missing_cost_is_the_same_200_an_empty_cost_is(self, reference_corpus: APIResource) -> None:
        """api.scryfall.com does not reject this (measured 2026-08-16).

        A missing `cost` is the same request as an empty one and both answer
        `200 {"object": "mana_cost", "cost": null, ...}`. This asserted the 400 the route used to
        send, whose sentence -- "You must provide a cost parameter to parse." -- Scryfall does not own.
        """
        resp = dispatch(reference_corpus, "/symbology/parse-mana")
        assert resp.status == falcon.HTTP_200
        body = payload(resp)
        assert body["object"] == "mana_cost"
        assert body["cost"] is None
        assert payload(dispatch(reference_corpus, "/symbology/parse-mana", "cost=")) == body

    def test_an_unreadable_cost_is_no_cache(self, reference_corpus: APIResource) -> None:
        """An unreadable cost is a fact about the REQUEST, and Scryfall declines to cache those."""
        resp = dispatch(reference_corpus, "/symbology/parse-mana", "cost=%7BQ%7D")
        assert resp.headers["cache-control"] == "no-cache"

    def test_an_unparseable_cost_is_a_422(self, reference_corpus: APIResource) -> None:
        """Scryfall answers 422 here, not 400."""
        resp = dispatch(reference_corpus, "/symbology/parse-mana", "cost=%7BQ%7D")
        assert resp.status == falcon.HTTP_422
        body = payload(resp)
        assert body["object"] == "error"
        # `validation_error`, which is what Scryfall sends with this 422 -- not `bad_request`.
        assert body["code"] == "validation_error"

    def test_it_answers_without_any_imported_data(self, reference_corpus: APIResource) -> None:
        """A pure function of the parameter, so it works before the first import."""
        assert payload(dispatch(reference_corpus, "/symbology/parse-mana", "cost=0"))["cost"] == "{0}"


class TestSharedBehaviour:
    """Conventions the reference routes inherit from the cards surface."""

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            # Measured from api.scryfall.com on 2026-08-11. Deliberately NOT the card routes'
            # `public, max-age=57600`: the mirrored routes send a bare `public` upstream, and
            # parse-mana is marked private and must-revalidate despite being the one deterministic
            # route here. A client that swaps its base URL should get the caching behaviour it
            # tuned against Scryfall, so these mirror upstream rather than being chosen.
            ("/sets", "public"),
            ("/sets/zzt", "public"),
            ("/sets/tcgplayer/90210", "public"),
            ("/catalog/creature-types", "public"),
            ("/symbology", "public"),
            ("/symbology/parse-mana?cost=R", "max-age=0, private, must-revalidate"),
        ],
    )
    def test_each_route_sends_the_tier_scryfall_sends(
        self,
        reference_corpus: APIResource,
        path: str,
        expected: str,
    ) -> None:
        route, _, query = path.partition("?")
        resp = dispatch(reference_corpus, route, query)
        assert resp.headers["cache-control"] == expected

    def test_an_error_still_carries_the_cache_tier(self, reference_corpus: APIResource) -> None:
        """The header is set before the handler body, so a 404 is cacheable like Scryfall's."""
        assert dispatch(reference_corpus, "/sets/nope").headers["cache-control"] == "public"

    @pytest.mark.parametrize("path", ["/sets", "/catalog/creature-types", "/symbology"])
    def test_pretty_indents_the_body(self, reference_corpus: APIResource, path: str) -> None:
        resp = dispatch(reference_corpus, path, "pretty=true")
        assert b"\n  " in resp.render_body()

    def test_errors_carry_the_scryfall_error_shape(self, reference_corpus: APIResource) -> None:
        body = payload(dispatch(reference_corpus, "/sets/nope"))
        assert body["object"] == "error"
        assert body["status"] == 404
        assert isinstance(body["details"], str)


class TestThroughTheFullApp:
    """The same routes through a real falcon.App, for wire-level concerns."""

    def _client(self, api: APIResource) -> falcon.testing.TestClient:
        app = falcon.App()
        app.add_sink(api._handle, prefix="/")
        return falcon.testing.TestClient(app)

    def test_content_type_is_scryfalls(self, reference_corpus: APIResource) -> None:
        result = self._client(reference_corpus).simulate_get("/sets")
        assert result.headers["content-type"] == "application/json; charset=utf-8"

    def test_a_set_lookup_round_trips(self, reference_corpus: APIResource) -> None:
        result = self._client(reference_corpus).simulate_get("/sets/zzt")
        assert json.loads(result.text)["code"] == "zzt"
