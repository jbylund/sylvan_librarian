"""The dropdowns in index.html must offer exactly what the enums accept.

An enum member with no `<option>` is unreachable from the UI; an `<option>` with no member is a
ParamCoercionError the moment someone picks it. Neither shows up in any other test — the API is
happy either way — so the drift is invisible until a user tries it. #913 added six orderings and
shipped without touching the markup, which is what prompted this.
"""

from __future__ import annotations

import pathlib
import re

from api.enums import CardOrdering, PreferOrder, UniqueOn

_INDEX_HTML = (pathlib.Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")


def _options(select_id: str) -> list[str]:
    """The option values of one <select>, in document order."""
    block = re.search(rf'<select[^>]*id="{select_id}".*?</select>', _INDEX_HTML, re.DOTALL)
    assert block is not None, f"no <select id={select_id!r}> in index.html"
    return re.findall(r'<option value="([^"]*)"', block.group(0))


def test_order_dropdown_offers_every_ordering() -> None:
    assert set(_options("orderDropdown")) == {str(member) for member in CardOrdering}


def test_unique_dropdown_offers_every_mode() -> None:
    assert set(_options("uniqueDropdown")) == {str(member) for member in UniqueOn}


def test_prefer_dropdown_offers_every_prefer() -> None:
    assert set(_options("preferDropdown")) == {str(member) for member in PreferOrder}


def test_no_dropdown_offers_a_value_the_api_would_reject() -> None:
    """The direction of the check that produces the worse failure: a 400 on a click."""
    for select_id, enum_cls in (
        ("orderDropdown", CardOrdering),
        ("uniqueDropdown", UniqueOn),
        ("preferDropdown", PreferOrder),
    ):
        accepted = {str(member) for member in enum_cls}
        for value in _options(select_id):
            assert value in accepted, f"{select_id} offers {value!r}, which {enum_cls.__name__} rejects"
