"""A `prefer:` in the query text chooses the printing, over the `prefer` request parameter."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from api.enums import PreferOrder

if TYPE_CHECKING:
    from api.api_resource import APIResource


@pytest.fixture(name="api")
def api_fixture(stub_api_resource: APIResource) -> APIResource:
    """An APIResource whose engine is empty, so every search takes the SQL path."""
    stub_api_resource.app_context.engine = MagicMock()
    stub_api_resource.app_context.engine.size.return_value = 0
    return stub_api_resource


@pytest.mark.parametrize(
    ("query", "param", "expected"),
    [
        ("t:elf prefer:usd-low", PreferOrder.DEFAULT, PreferOrder.USD_LOW),
        ("t:elf prefer:oldest", PreferOrder.NEWEST, PreferOrder.OLDEST),  # the query wins over the parameter
        ("game:paper prefer:usd-high", PreferOrder.DEFAULT, PreferOrder.USD_HIGH),
        ("t:elf", PreferOrder.NEWEST, PreferOrder.NEWEST),  # no prefer: in the query keeps the parameter
    ],
)
def test_query_prefer_sets_the_search_prefer(api: APIResource, query: str, param: PreferOrder, expected: PreferOrder) -> None:
    """search() hands the SQL path the query's prefer: when it has one, else the parameter."""
    with patch.object(api, "_search_sql", return_value={"cards": []}) as mock_sql:
        api.search(q=query, prefer=param)
    assert mock_sql.call_args.kwargs["prefer"] is expected
