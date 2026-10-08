"""`g:` / `group:` -- a set's release group, rewritten into the `e:` terms it means.

The rule (api.parsing.set_groups) was measured on api.scryfall.com 2026-10-08: `g:<code>` is the
set, its children, its parent and its parent's other children -- one step each way over
`parent_set_code`, never the whole family. The catalog below is 26 real rows of that day's `/sets`:
Lorwyn Eclipsed (a root, five children, one grandchild), Outlaws of Thunder Junction (a root with
three branches that have children of their own), The Hobbit, Reality Fracture and Alpha.

No database: the registry is filled directly, which is all `AppContext.ensure_set_groups` does with
the rows it reads.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

import pytest

from api.parsing import generate_sql_query, parse_query, parse_scryfall_query
from api.parsing.pyparsing_based import parse_str_to_query as pyparsing_parse_str_to_query
from api.parsing.set_groups import release_group, replace_set_groups

if TYPE_CHECKING:
    from collections.abc import Iterator

parse_with_pyparsing = partial(parse_query, parser_fn=pyparsing_parse_str_to_query)

CATALOG = [
    ("ecl", None, "Lorwyn Eclipsed"),
    ("aecl", "ecl", "Lorwyn Eclipsed Art Series"),
    ("ecc", "ecl", "Lorwyn Eclipsed Commander"),
    ("pecl", "ecl", "Lorwyn Eclipsed Promos"),
    ("tecl", "ecl", "Lorwyn Eclipsed Tokens"),
    ("yecl", "ecl", "Alchemy: Lorwyn Eclipsed"),
    ("tecc", "ecc", "Lorwyn Eclipsed Commander Tokens"),
    ("otj", None, "Outlaws of Thunder Junction"),
    ("aotj", "otj", "Outlaws of Thunder Junction Art Series"),
    ("big", "otj", "The Big Score"),
    ("otc", "otj", "Outlaws of Thunder Junction Commander"),
    ("otp", "otj", "Breaking News"),
    ("potj", "otj", "Outlaws of Thunder Junction Promos"),
    ("totj", "otj", "Outlaws of Thunder Junction Tokens"),
    ("yotj", "otj", "Alchemy: Outlaws of Thunder Junction"),
    ("pbig", "big", "The Big Score Promos"),
    ("tbig", "big", "The Big Score Tokens"),
    ("totc", "otc", "Outlaws of Thunder Junction Commander Tokens"),
    ("totp", "otp", "Breaking News Tokens"),
    ("hob", None, "The Hobbit"),
    ("hoc", "hob", "The Hobbit Eternal"),
    ("thob", "hob", "The Hobbit Tokens"),
    ("fra", None, "Reality Fracture"),
    ("pfra", "fra", "Reality Fracture Promos"),
    ("yfra", "fra", "Alchemy: Reality Fracture"),
    ("lea", None, "Limited Edition Alpha"),
]

ECL_CHILDREN = ["aecl", "ecc", "pecl", "tecl", "yecl"]
OTJ_CHILDREN = ["aotj", "big", "otc", "otp", "potj", "totj", "yotj"]


@pytest.fixture(autouse=True)
def _catalog() -> Iterator[None]:
    """Fill the process-wide registry for one test, and leave it empty for every other module."""
    replace_set_groups({"code": code, "parent_set_code": parent, "name": name} for code, parent, name in CATALOG)
    yield
    replace_set_groups([])


def sets_of(code: str) -> list[str]:
    """The whole group *code* names, sorted."""
    group = release_group(code)
    assert group is not None
    return sorted((group[0], *group[1]))


def spelled_out(codes: list[str]) -> str:
    """The query a group is rewritten into."""
    terms = " or ".join(f"e:{code}" for code in sorted(codes))
    return f"({terms})" if len(codes) > 1 else terms


def sql(query: str) -> tuple:
    """The SQL both parsers must agree on for *query*."""
    hand = generate_sql_query(parse_scryfall_query(query))
    assert generate_sql_query(parse_with_pyparsing(query)) == hand, f"the two parsers disagree on {query!r}"
    return hand


class TestTheRule:
    """One step each way over `parent_set_code`."""

    @pytest.mark.parametrize(
        argnames=["code", "expected"],
        argvalues=[
            # A root: itself and its children. tecc, a child of ecc, is a grandchild and stays out.
            ("ecl", ["ecl", *ECL_CHILDREN]),
            # A child with no child of its own: its parent and its siblings -- the same six.
            ("tecl", ["ecl", *ECL_CHILDREN]),
            ("yecl", ["ecl", *ECL_CHILDREN]),
            # The one member whose group is the whole family: its own child joins.
            ("ecc", ["ecl", *ECL_CHILDREN, "tecc"]),
            # A grandchild: itself and its parent. Not the grandparent, not the parent's siblings.
            ("tecc", ["ecc", "tecc"]),
            ("totc", ["otc", "totc"]),
            # A root with three branches: none of the grandchildren.
            ("otj", ["otj", *OTJ_CHILDREN]),
            # A branch: its own children, its parent, its siblings -- and not its siblings' children.
            ("big", ["otj", *OTJ_CHILDREN, "pbig", "tbig"]),
            # A grandchild with a sibling: the two and their parent -- not Thunder Junction, and not
            # its cousin totc.
            ("pbig", ["big", "pbig", "tbig"]),
            ("hob", ["hob", "hoc", "thob"]),
            ("hoc", ["hob", "hoc", "thob"]),
            # No parent and no child: a group of one.
            ("lea", ["lea"]),
            # A set with no cards is still a member and still names its group (`g:yfra` is 672).
            ("yfra", ["fra", "pfra", "yfra"]),
        ],
    )
    def test_group_members(self, code: str, expected: list[str]) -> None:
        assert sets_of(code) == sorted(expected)

    def test_grandchildren_under_a_branch_are_two_steps_from_a_sibling(self) -> None:
        assert "pbig" in sets_of("big")
        assert "pbig" not in sets_of("otc")
        assert "pbig" not in sets_of("otj")

    def test_a_code_no_set_has_names_no_group(self) -> None:
        assert release_group("zzzz") is None
        assert release_group("ec") is None
        assert release_group("") is None

    def test_an_empty_catalog_names_no_group(self) -> None:
        replace_set_groups([])
        assert release_group("ecl") is None

    def test_rows_without_a_code_are_skipped(self) -> None:
        replace_set_groups(
            [{"code": None, "parent_set_code": "ecl", "name": "x"}, {"code": "ECL", "parent_set_code": None, "name": "y"}]
        )
        assert release_group("ecl") == ("ecl", ())


class TestSetNames:
    """A value that is no code is read as a set's whole name, case and separators ignored."""

    @pytest.mark.parametrize(
        argnames=["value", "code"],
        argvalues=[
            ("Lorwyn Eclipsed Commander", "ecc"),
            ("lorwyn eclipsed commander", "ecc"),
            ("lorwyn-eclipsed-commander", "ecc"),
            ("lorwyneclipsedcommander", "ecc"),
            ("Lorwyn Eclipsed Tokens", "tecl"),
            ("the hobbit", "hob"),
            ("ECC", "ecc"),
        ],
    )
    def test_a_name_resolves_to_its_set(self, value: str, code: str) -> None:
        group = release_group(value)
        assert group is not None
        assert group[0] == code

    def test_a_part_of_a_name_is_no_name(self) -> None:
        assert release_group("lorwyn") is None

    def test_a_name_with_a_character_scryfall_does_not_drop_is_no_name(self) -> None:
        # `e:"kamigawa: neon dynasty"` answers nothing on api.scryfall.com where the name without
        # its colon answers the set; a set whose own name carries one is reached by its code.
        assert release_group("alchemy: lorwyn eclipsed") is None
        assert release_group("alchemy lorwyn eclipsed") is None

    def test_a_code_wins_over_a_name(self) -> None:
        replace_set_groups(
            [{"code": "abc", "parent_set_code": None, "name": "xyz"}, {"code": "xyz", "parent_set_code": "abc", "name": "abc"}]
        )
        assert release_group("abc") == ("abc", ("xyz",))
        assert release_group("xyz") == ("xyz", ("abc",))

    def test_a_name_two_sets_share_names_neither(self) -> None:
        replace_set_groups(
            [{"code": "aaa", "parent_set_code": None, "name": "Twin"}, {"code": "bbb", "parent_set_code": None, "name": "twin"}]
        )
        assert release_group("twin") is None
        assert release_group("aaa") == ("aaa", ())


class TestTheRewrite:
    """The term becomes the `e:` terms it means, identically in both parsers."""

    @pytest.mark.parametrize(
        argnames=["query", "same_as"],
        argvalues=[
            ("g:ecc", spelled_out(["ecl", *ECL_CHILDREN, "tecc"])),
            ("g:ecl", spelled_out(["ecl", *ECL_CHILDREN])),
            ("g:tecl", spelled_out(["ecl", *ECL_CHILDREN])),
            ("g:tecc", "(e:ecc or e:tecc)"),
            ("g:pbig", "(e:big or e:pbig or e:tbig)"),
            ("g:lea", "e:lea"),
            # Every spelling of the keyword, the operator and the value.
            ("group:ecc", "g:ecc"),
            ("g=ecc", "g:ecc"),
            ("group=ecc", "g:ecc"),
            ("G:ECC", "g:ecc"),
            ("GROUP:Ecc", "g:ecc"),
            ('g:"ecc"', "g:ecc"),
            ('g:"Lorwyn Eclipsed Commander"', "g:ecc"),
            ("g:lorwyn-eclipsed", "g:ecl"),
            # Inside a larger query the group is one parenthesised alternative.
            ("g:tecc t:elemental", "(e:ecc or e:tecc) t:elemental"),
            ("t:elemental g:tecc", "t:elemental (e:ecc or e:tecc)"),
            ("g:tecc or cmc=3", "e:ecc or e:tecc or cmc=3"),
            ("(g:tecc or g:pbig) r:m", "(e:ecc or e:tecc or e:big or e:pbig or e:tbig) r:m"),
            ("g:tecc g:ecc", "(e:ecc or e:tecc) " + spelled_out(["ecl", *ECL_CHILDREN, "tecc"])),
            # A minus on parentheses around the term is the complement of the group.
            ("-(g:tecc)", "-(e:ecc or e:tecc)"),
            ("-( g:tecc )", "-(e:ecc or e:tecc)"),
            ("-((g:tecc))", "-(e:ecc or e:tecc)"),
            ("-(g:lea)", "-e:lea"),
            ("-(g:tecc) e:ecl", "-(e:ecc or e:tecc) e:ecl"),
            ("-(g:tecc or t:elf)", "-(e:ecc or e:tecc or t:elf)"),
            ("-(g:tecc t:elf)", "-((e:ecc or e:tecc) t:elf)"),
            ("-(g:zzzz)", "-e:zzzz"),
            # A minus written ON the term drops the rest of the group and keeps the set it names.
            ("-g:tecc", "-e:ecc"),
            ("-g:ecc", "-(e:aecl or e:ecl or e:pecl or e:tecc or e:tecl or e:yecl)"),
            ("-group:ECC", "-g:ecc"),
            ("-g=ecc", "-g:ecc"),
            ('-g:"Lorwyn Eclipsed Commander"', "-g:ecc"),
            ("(-g:tecc)", "-e:ecc"),
            ("-g:tecc e:tecc", "-e:ecc e:tecc"),
            ("t:elf or -g:tecc", "t:elf or -e:ecc"),
            ("-(-g:tecc)", "-(-e:ecc)"),
            # ...so with no other set to drop it drops nothing: every card.
            ("-g:lea", '-e:""'),
            ("-g:zzzz", '-e:""'),
            # A comparison, a regex and an empty value are negated as the nothing they match.
            ("-g>=ecc", '-e:""'),
            ("-g:/ecc/", '-e:""'),
            ('-g:""', '-e:""'),
            # A value no listed set has is the `e:` term of that value, which matches nothing.
            ("g:zzzz", "e:zzzz"),
            ("g:ec", "e:ec"),
            ("g:zzzz or cmc=3", "e:zzzz or cmc=3"),
            # A comparison, a regex and an empty value name no group: the set no card is in.
            ("g>=ecc", 'e:""'),
            ("g<ecc", 'e:""'),
            ("g!=ecc", 'e:""'),
            ("g:/ecc/", 'e:""'),
            ('g:""', 'e:""'),
            ("g>=ecc or cmc=3", 'e:"" or cmc=3'),
        ],
    )
    def test_same_sql_as_the_sets_spelled_out(self, query: str, same_as: str) -> None:
        assert sql(query) == sql(same_as)

    def test_e_is_untouched(self) -> None:
        where, params = sql("e:ecc")
        assert where.count("card_set_code") == 1
        assert list(params.values()) == ["ecc"]

    def test_the_keyword_is_gone_from_the_engine_tree(self) -> None:
        tree = str(parse_scryfall_query("g:tecc t:elemental").to_json())
        assert "'original_attribute': 'g'" not in tree
        assert tree.count("'original_attribute': 'set'") == 2

    def test_g_and_group_without_an_operator_are_name_words(self) -> None:
        for word in ("g", "group"):
            assert sql(word) == sql(f"name:{word}")

    def test_an_unloaded_registry_degrades_to_the_named_set(self) -> None:
        replace_set_groups([])
        assert sql("g:ecc") == sql("e:ecc")
        assert sql("-(g:ecc)") == sql("-e:ecc")
        assert sql("-g:ecc") == sql('-e:""')

    def test_parentheses_around_any_other_term_still_change_nothing(self) -> None:
        for query in ("e:ecc", "t:elf", "cmc>=3", "o:flying", "is:split"):
            assert sql(f"-({query})") == sql(f"-{query}")
            assert sql(f"({query}) r:m") == sql(f"{query} r:m")
