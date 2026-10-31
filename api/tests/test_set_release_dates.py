"""Postgres-backed: `date:<set code>` resolves through a registry filled from magic.cards.

Uses the shared session database, so every assertion is about the `zzt` rows this file inserts
(unique set code and card names per the `api_resource` fixture's rules), never about global counts.
"""

from __future__ import annotations

import uuid

import falcon
import pytest

from api.parsing import set_dates
from api.parsing.set_dates import set_release_date
from api.settings import settings
from api.tests.helpers import make_raw_card


def _zzt_card(name: str, released_at: str) -> dict:
    # Fixed ids: every test re-upserts the same two rows, so a second insert is a no-op update, not
    # a second printing of the same name (which would double the search results below).
    card = make_raw_card(card_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"sylvan/tests/zzt/{name}")), name=name)
    card["oracle_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"sylvan/tests/zzt/oracle/{name}"))
    card["set"] = "zzt"
    card["released_at"] = released_at
    return card


@pytest.fixture
def zzt_imported(api_resource, monkeypatch) -> None:
    monkeypatch.setattr(set_dates, "_SET_RELEASE_DATES", {})
    monkeypatch.setattr(api_resource.app_context, "_set_release_dates_cache", None)
    # SQL serves the searches: the engine store is empty here and would otherwise kick off a
    # background reload of the whole shared database on the first search.
    monkeypatch.setattr(settings, "enable_engine", False)
    result = api_resource.admin._upsert_cards(
        [_zzt_card("Set Date Probe Later", "2020-03-01"), _zzt_card("Set Date Probe Earlier", "2020-01-01")],
    )
    assert result["status"] == "success"


def test_refresh_reads_the_earliest_printing_per_set(api_resource, zzt_imported) -> None:
    api_resource.app_context.refresh_set_release_dates()
    assert set_release_date("zzt") == "2020-01-01"
    assert set_release_date("ZZT") == "2020-01-01"


def test_search_resolves_a_set_code_against_the_database(api_resource, zzt_imported) -> None:
    """End to end: `_search` loads the registry itself, and the code compares as the set's full day."""
    at_or_after = api_resource._search(query='date>=zzt name:"Set Date Probe"')
    assert sorted(card["name"] for card in at_or_after["cards"]) == ["Set Date Probe Earlier", "Set Date Probe Later"]
    after = api_resource._search(query='date>zzt name:"Set Date Probe"')
    assert [card["name"] for card in after["cards"]] == ["Set Date Probe Later"]


def test_unknown_set_code_is_a_400_with_scryfalls_sentence(api_resource, zzt_imported) -> None:
    with pytest.raises(falcon.HTTPBadRequest) as exc_info:
        api_resource._search(query="date>=zzzz")
    assert exc_info.value.title == "Invalid Search Query"
    assert exc_info.value.description == "Invalid date or unknown set code “zzzz”"
