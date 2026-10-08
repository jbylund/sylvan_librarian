"""`g:` / `group:` end to end: the mirrored set list decides which sets a group is.

Four sets of this module's own -- a root, its child, that child's child, and a set with no
relatives -- with one card in each, so every assertion is exact against the shared database. The
set rows are inserted beside whatever `magic.sets` already holds and removed again afterwards.

The rule itself (one step each way over `parent_set_code`) is pinned without a database in
api/parsing/tests/test_release_groups.py; this module pins that `/search`, `/cards/search`,
`/cards/random` and a collection scope read it from `magic.sets`, on the engine and on SQL.
"""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import falcon
import falcon.testing
import orjson
import pytest
from psycopg.types.json import Jsonb

from api.parsing.set_groups import release_group, replace_set_groups
from api.settings import settings
from api.tests.helpers import make_raw_card

if TYPE_CHECKING:
    from collections.abc import Iterator

    from api.api_resource import APIResource

ROOT, CHILD, GRANDCHILD, LONER = "rga", "rgb", "rgc", "rgd"
SETS = [
    ("aaaaaaaa-1000-4000-8000-000000000001", ROOT, None, "Release Group Root"),
    ("aaaaaaaa-1000-4000-8000-000000000002", CHILD, ROOT, "Release Group Child"),
    ("aaaaaaaa-1000-4000-8000-000000000003", GRANDCHILD, CHILD, "Release Group Grandchild"),
    ("aaaaaaaa-1000-4000-8000-000000000004", LONER, None, "Release Group Loner"),
]
CARD_IDS = {
    ROOT: "bbbbbbbb-1000-4000-8000-000000000001",
    CHILD: "bbbbbbbb-1000-4000-8000-000000000002",
    GRANDCHILD: "bbbbbbbb-1000-4000-8000-000000000003",
    LONER: "bbbbbbbb-1000-4000-8000-000000000004",
}
# Every card of this module, so a negated group can be asserted exactly.
OURS = 'name:"Release Group Card"'


def _card(set_code: str) -> dict:
    card = make_raw_card(card_id=CARD_IDS[set_code], name=f"Release Group Card {set_code.upper()}")
    card |= {"object": "card", "set": set_code, "set_name": f"Release Group {set_code}", "lang": "en"}
    return card


def _write_sets(api: APIResource, rows: list[tuple[str, str, str | None, str]]) -> None:
    codes = [code for _, code, _, _ in SETS]
    with api.app_context.writer_pool.connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM magic.sets WHERE code = ANY(%s)", (codes,))
            cursor.executemany(
                "INSERT INTO magic.sets (id, code, tcgplayer_id, position, set_object) VALUES (%s, %s, NULL, %s, %s)",
                [
                    (
                        set_id,
                        code,
                        9_000 + position,
                        Jsonb(
                            {"object": "set", "id": set_id, "code": code, "name": name}
                            | ({"parent_set_code": parent} if parent else {})
                        ),
                    )
                    for position, (set_id, code, parent, name) in enumerate(rows)
                ],
            )
        conn.commit()


@pytest.fixture(name="group_corpus", scope="module")
def group_corpus_fixture(api_resource: APIResource) -> Iterator[APIResource]:
    """Four sets, one card in each, the engine reloaded and the registry read from the table."""
    api_resource.admin._upsert_cards([copy.deepcopy(_card(code)) for code in CARD_IDS])
    _write_sets(api_resource, SETS)
    api_resource.app_context.reload_engine(force=True)
    api_resource.admin._clear_caches()
    api_resource.app_context.ensure_set_groups(force=True)
    yield api_resource
    _write_sets(api_resource, [])
    replace_set_groups([])


@pytest.fixture(name="lanes", params=["engine", "sql"])
def lanes_fixture(request: pytest.FixtureRequest, group_corpus: APIResource) -> Iterator[APIResource]:
    """The corpus with the engine serving, and again with it gated off so SQL answers."""
    saved = settings.enable_engine
    settings.enable_engine = request.param == "engine"
    group_corpus.admin._clear_caches()
    yield group_corpus
    settings.enable_engine = saved


def dispatch(api: APIResource, path: str, params: dict[str, str] | None = None, *, body: dict | None = None) -> falcon.Response:
    """Run one request through `_handle` and return the Falcon response."""
    environ = falcon.testing.create_environ(
        path=path,
        query_string=falcon.to_query_str(params or {}, prefix=False),
        method="POST" if body is not None else "GET",
        body=json.dumps(body) if body is not None else "",
        headers={"Content-Type": "application/json"} if body is not None else None,
    )
    resp = falcon.Response()
    api._handle(falcon.Request(environ), resp)
    return resp


def payload(resp: falcon.Response) -> dict:
    """Decode a response body regardless of whether it was set as media or as text."""
    if resp.media is not None:
        return resp.media
    return orjson.loads(resp.render_body())


def searched(api: APIResource, query: str) -> set[str]:
    """The set codes `/search` answers *query* with, among this module's cards."""
    body = payload(dispatch(api, "/search", {"q": f"{query} {OURS}", "unique": "printing", "limit": "50"}))
    return {card["set_code"] for card in body["cards"]}


class TestSearch:
    """`/search` on both lanes."""

    @pytest.mark.parametrize(
        argnames=["query", "expected"],
        argvalues=[
            (f"g:{ROOT}", {ROOT, CHILD}),
            (f"g:{CHILD}", {ROOT, CHILD, GRANDCHILD}),
            (f"g:{GRANDCHILD}", {CHILD, GRANDCHILD}),
            (f"g:{LONER}", {LONER}),
            (f"group:{CHILD.upper()}", {ROOT, CHILD, GRANDCHILD}),
            (f"g={GRANDCHILD}", {CHILD, GRANDCHILD}),
            ('g:"Release Group Grandchild"', {CHILD, GRANDCHILD}),
            # A minus on parentheses is the complement; a minus on the term keeps the named set.
            (f"-(g:{GRANDCHILD})", {ROOT, LONER}),
            (f"-g:{GRANDCHILD}", {ROOT, GRANDCHILD, LONER}),
            (f"-g:{CHILD}", {CHILD, LONER}),
            (f"-g:{CHILD} e:{CHILD}", {CHILD}),
            (f"-(g:{CHILD}) e:{CHILD}", set()),
            (f"-g:{LONER}", {ROOT, CHILD, GRANDCHILD, LONER}),
            ("-g:rgzz", {ROOT, CHILD, GRANDCHILD, LONER}),
            (f"(g:{GRANDCHILD} or g:{LONER})", {CHILD, GRANDCHILD, LONER}),
            (f"g:{ROOT} g:{GRANDCHILD}", {CHILD}),
            ("g:rgzz", set()),
            (f"g>={ROOT}", set()),
            (f"g:/{ROOT}/", set()),
            ('g:""', set()),
        ],
    )
    def test_the_group_is_the_sets_one_step_away(self, lanes: APIResource, query: str, expected: set[str]) -> None:
        assert searched(lanes, query) == expected

    def test_e_still_answers_one_set(self, lanes: APIResource) -> None:
        assert searched(lanes, f"e:{CHILD}") == {CHILD}


class TestScryfallRoutes:
    """The Scryfall-shaped routes parse with the same parser and read the same registry."""

    def test_cards_search(self, lanes: APIResource) -> None:
        body = payload(dispatch(lanes, "/cards/search", {"q": f"g:{GRANDCHILD} {OURS}", "unique": "prints"}))
        assert body["object"] == "list"
        assert {card["set"] for card in body["data"]} == {CHILD, GRANDCHILD}
        assert body["total_cards"] == 2

    def test_cards_search_with_a_code_no_set_has_is_a_404(self, lanes: APIResource) -> None:
        resp = dispatch(lanes, "/cards/search", {"q": "g:rgzz"})
        assert resp.status == falcon.HTTP_404
        assert "warnings" not in payload(resp)

    def test_cards_random_draws_from_the_group(self, group_corpus: APIResource) -> None:
        for _ in range(5):
            body = payload(dispatch(group_corpus, "/cards/random", {"q": f"g:{GRANDCHILD} {OURS}"}))
            assert body["set"] in {CHILD, GRANDCHILD}

    def test_a_collection_scope_is_a_group(self, group_corpus: APIResource) -> None:
        identifiers = [{"name": f"Release Group Card {code.upper()}"} for code in (CHILD, LONER)]
        resp = dispatch(group_corpus, "/cards/collection", {"q": f"g:{GRANDCHILD}"}, body={"identifiers": identifiers})
        body = payload(resp)
        assert [card["set"] for card in body["data"]] == [CHILD]
        assert body["not_found"] == [identifiers[1]]


class TestTheRegistryFollowsTheTable:
    """Which sets a group is changes when `magic.sets` does, without a restart."""

    def test_a_reparented_set_moves_groups_after_the_next_import(self, group_corpus: APIResource) -> None:
        assert release_group(LONER) == (LONER, ())
        try:
            _write_sets(group_corpus, [*SETS[:3], (SETS[3][0], LONER, ROOT, SETS[3][3])])
            # Still the old answer: the registry is cached per process until an import completes.
            group_corpus.app_context.ensure_set_groups()
            assert release_group(LONER) == (LONER, ())
            group_corpus.app_context.last_import_time.value += 1.0
            group_corpus.app_context.ensure_set_groups()
            assert release_group(LONER) == (LONER, (ROOT, CHILD))
            assert release_group(ROOT) == (ROOT, (CHILD, LONER))
        finally:
            _write_sets(group_corpus, SETS)
            group_corpus.app_context.ensure_set_groups(force=True)

    def test_the_reference_import_reloads_it_in_the_importing_worker(self, group_corpus: APIResource) -> None:
        replace_set_groups([])
        assert release_group(ROOT) is None
        # All three steps stubbed: left to run they would fetch api.scryfall.com and rewrite the tables.
        with (
            patch("api.admin_resource._import_sets"),
            patch("api.admin_resource._import_catalogs"),
            patch("api.admin_resource._import_symbology"),
        ):
            group_corpus.admin._import_reference_quietly()
        assert release_group(ROOT) == (ROOT, (CHILD,))
