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
        # THE LETTERS NFKD LEAVES WHOLE. A decomposition can only separate a base letter from its
        # marks, and "æ" is not "a" with a mark on it -- so every one of these survived untouched
        # and `name:ætherling` found nothing. Each pair was measured against api.scryfall.com on
        # 2026-08-16, needle against expansion, equal totals both ways: æ/ae 90, œ/oe 167,
        # ß/ss 2051, ø/o 22111, ł/l 18748, đ/d 14591, þ/th 5689, ð/d 14591, ħ/h 14176, ŋ/ng 4834,
        # ŧ/t 22261, U+0131/i 22954, ĸ/k 6616.
        ("Ætherling", "AEtherling"),
        ("ÆTHER VIAL", "AETHER VIAL"),
        ("Cœur", "Coeur"),
        ("Straße", "Strasse"),
        ("Ørjan Ruttenborg Svendsen", "Orjan Ruttenborg Svendsen"),
        ("Bartłomiej Gaweł", "Bartlomiej Gawel"),
        ("Đilo", "Dilo"),
        ("Þorbjörn", "Thorbjorn"),
        ("Ðagr", "Dagr"),
        ("Ħaġar", "Hagar"),
        ("Ŋombe", "NGombe"),
        # ...and the ones deliberately NOT in the table: U+00D7 and U+00F7 are symbols rather than
        # letters, both answer 404 on Scryfall, and collate_name() deletes them anyway.
        ("2\u00d72", "2\u00d72"),
    ],
)
def test_fold_accents(value: str, expected: str) -> None:
    """fold_accents() strips diacritics but leaves everything else (including case) untouched."""
    assert fold_accents(value) == expected
