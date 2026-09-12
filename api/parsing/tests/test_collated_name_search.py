"""`name:` is two searches, and the quotes decide which.

Measured against api.scryfall.com on 2026-08-16, every number below from a live `/cards/search`:

    name:ft        1,628      name:"ft"        362      name:'ft'        362      name:*ft*  1,628
    name:ofthe     1,109      name:"ofthe"       0      name:"of the"  1,109
    name:eowyn         3      name:"eowyn"       0      name:"éowyn"       3
    name:limdul        8      name:"limdul"      0      name:"lim-dûl"     8      name:lim-dul   8
    name:/lim-dul/     0      name:/Lim-D.l/     8

A BARE word is matched against the name with diacritics folded AND every non-alphanumeric
character removed — which is what lets `ft` reach "Sword **of the** Ages" through the vanished
space, and `limdul` reach "Lim-Dûl's Vault". A QUOTED value is matched literally,
case-insensitively and nothing else. A plain-literal regex lowers to the quoted reading, not the
bare one.

`!"…"` is collated on both sides for the same reason: `!"Lim-Dûl's Vault"`, `!"lim-dul's vault"`,
`!"limduls vault"` and `!"Lim-Dul's Vault"` all answer the same one card there, and
`!"eowyn, lady of rohan"` answers "Éowyn, Lady of Rohan".
"""

import pytest

from api.parsing.card_query_nodes import ExactNameNode, collate_name, fold_accents
from api.parsing.nodes import StringValueNode


def _name_rhs(parse_query, query: str) -> dict:
    """The wire-format rhs of a single `name:` clause."""
    return parse_query(query).root.to_json()["kwargs"]["rhs"]


@pytest.mark.parametrize(
    argnames=("query", "expected_value"),
    argvalues=[
        ("name:ft", "Ft"),
        ("ft", "Ft"),  # a bare word IS a name: search
        ("name:ofthe", "Ofthe"),
        ("name:of-the", "OfThe"),  # the hyphen is a separator, not part of the word
        ("name:limdul", "Limdul"),
        ("name:lim-dul", "LimDul"),
        ("name:eowyn", "Eowyn"),
        ("name:éowyn", "Eowyn"),  # ...and the accent folds to the same needle
        ("name:limdulsvault", "Limdulsvault"),
    ],
)
def test_bare_name_word_is_collated(parse_query, query: str, expected_value: str) -> None:
    """A bare word emits CollatedNameValueNode carrying the folded, separator-free needle."""
    rhs = _name_rhs(parse_query, query)
    assert rhs["node_type"] == "CollatedNameValueNode", rhs
    assert rhs["kwargs"]["value"] == expected_value
    assert collate_name(rhs["kwargs"]["value"]) == rhs["kwargs"]["value"], "already collated"


@pytest.mark.parametrize(
    argnames=("query", "expected_value"),
    argvalues=[
        ('name:"ft"', "Ft"),
        ("name:'ft'", "Ft"),
        ('"ft"', "Ft"),  # a bare QUOTED term is name:"…", still literal
        ('name:"of the"', "Of The"),  # the space survives — this is not the bare search
        ('name:"éowyn"', "Éowyn"),  # ...and so does the accent
        ('name:"lim-dul"', "Lim-Dul"),
        ("name:/lim-dul/", "Lim-Dul"),  # a plain-literal regex lowers to the LITERAL reading
    ],
)
def test_quoted_name_value_is_literal(parse_query, query: str, expected_value: str) -> None:
    """A quoted value (or a lowered plain-literal regex) stays a plain StringValueNode."""
    rhs = _name_rhs(parse_query, query)
    assert rhs["node_type"] == "StringValueNode", rhs
    assert rhs["kwargs"]["value"] == expected_value


@pytest.mark.parametrize(
    argnames="query",
    argvalues=[
        '!"Lim-Dûl\'s Vault"',
        '!"lim-dul\'s vault"',
        '!"limduls vault"',
        '!"Lim-Dul\'s Vault"',
        "!limdulsvault",
    ],
)
def test_exact_name_spellings_collapse_to_one_needle(parse_query, query: str) -> None:
    """Every spelling of a card's name Scryfall accepts for `!` reaches the same wire value."""
    node = parse_query(query).root
    assert isinstance(node, ExactNameNode)
    assert node.kwargs() == {"value": "limdulsvault"}


def test_exact_name_folds_accents_and_commas(parse_query) -> None:
    """`!"eowyn, lady of rohan"` is the same search as the fully accented spelling."""
    unaccented = parse_query('!"eowyn, lady of rohan"').root.kwargs()
    accented = parse_query('!"Éowyn, Lady of Rohan"').root.kwargs()
    assert unaccented == accented == {"value": "eowynladyofrohan"}


@pytest.mark.parametrize(
    argnames=("raw", "expected"),
    argvalues=[
        ("Lim-Dûl's Vault", "LimDulsVault"),
        ("of the", "ofthe"),
        ("50%", "50"),
        ("", ""),
        ("*ft*", "ft"),  # Scryfall accepts `name:*ft*` and answers it as the bare word
        ("東方", "東方"),  # non-ASCII alphanumerics are kept whole
    ],
)
def test_collate_name(raw: str, expected: str) -> None:
    """collate_name drops exactly the non-alphanumeric characters, keeping everything else."""
    assert collate_name(fold_accents(raw)) == expected


def test_literal_flag_is_not_serialized() -> None:
    """`literal` is parser metadata: every other field serializes a quoted value unchanged."""
    assert StringValueNode("x", literal=True).kwargs() == StringValueNode("x").kwargs() == {"value": "x"}
