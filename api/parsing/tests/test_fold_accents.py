"""Tests for fold_accents(), the diacritic-folding helper behind #649."""

import pytest

from api.parsing.card_query_nodes import fold_accents


@pytest.mark.parametrize(
    argnames=("value", "expected"),
    argvalues=[
        ("eowyn", "eowyn"),
        ("Lightning Bolt", "Lightning Bolt"),
        # Accented characters observed in real Scryfall card names (#649).
        ("Éowyn, Fearless Knight", "Eowyn, Fearless Knight"),
        ("Círdan the Shipwright", "Cirdan the Shipwright"),
        ("Andúril, Flame of the West", "Anduril, Flame of the West"),
        ("Arna Kennerüd, Skycaptain", "Arna Kennerud, Skycaptain"),
        ("Barad-dûr", "Barad-dur"),
        ("Bespoke Bō", "Bespoke Bo"),
        ("Bösium Strip", "Bosium Strip"),
        ("Dandân", "Dandan"),
        ("Ghazbán Ogre", "Ghazban Ogre"),
        ("Altaïr Ibn-La'Ahad", "Altair Ibn-La'Ahad"),
        ("Araña, Heart of the Spider", "Arana, Heart of the Spider"),
        ("Arwen Undómiel", "Arwen Undomiel"),
        ("Song of Eärendil", "Song of Earendil"),
        ("Déjà Vu", "Deja Vu"),
    ],
)
def test_fold_accents(value: str, expected: str) -> None:
    """fold_accents() strips diacritics but leaves everything else (including case) untouched."""
    assert fold_accents(value) == expected
