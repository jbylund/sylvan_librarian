"""Shared fixtures for parsing tests."""

import pytest

from api.parsing import parse_scryfall_query
from api.parsing.pyparsing_based import parse_search_query


@pytest.fixture(params=[parse_scryfall_query, parse_search_query], ids=["hand_rolled", "pyparsing"])
def parse_query(request):
    """Parametrized fixture that runs each test against both parser implementations."""
    return request.param
