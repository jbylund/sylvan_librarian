"""Scryfall's ignore-and-continue query policy, term by term.

Every expectation here is a MEASUREMENT against api.scryfall.com on 2026-08-16, not a design: the
warning sentences, the 20-character expression echo, which characters fold, which keywords Scryfall
does not know and which of its own it refuses to negate were all read off live responses. See
`api/scryfall_compat/query_terms.py` for the request that produced each.
"""

from __future__ import annotations

import pytest

from api.parsing import parse_scryfall_query
from api.scryfall_compat.query_terms import (
    _postgres_syntax_reason,
    fold_smart_quotes,
    scryfall_regex_text_reason,
    scryfall_term_policy,
)


def ignored(echo: str, reason: str) -> str:
    """`Invalid expression \u201c<echo>\u201d was ignored. <reason>` -- the echo is Scryfall's, typed out."""
    return f"Invalid expression \u201c{echo}\u201d was ignored. {reason}"


class TestSmartQuotes:
    """The typographic characters Scryfall folds before lexing."""

    def test_the_four_scryfall_folds(self):
        assert fold_smart_quotes("o:\u201cdraw\u201d") == 'o:"draw"'
        assert fold_smart_quotes("o:\u2018draw\u2019") == "o:'draw'"
        # U+2018/U+2019 fold to the APOSTROPHE, not to the double quote. The discriminator is
        # measured: `name:<U+2018>Gaea"s Blessing<U+2019>` finds nothing on Scryfall, which only
        # holds if the result is `name:'Gaea"s Blessing'`; folding all four to `"` would have made
        # it find the card.
        assert fold_smart_quotes("name:\u201cGaea\u2019s Blessing\u201d") == 'name:"Gaea\'s Blessing"'

    @pytest.mark.parametrize(
        "literal",
        [
            "\u00ab",
            "\u00bb",
            "\u2039",
            "\u203a",
            "\u201e",
            "\u201a",
            "\u2032",
            "\u2033",
            "\uff02",
            "\u300c",
            "\u02bc",
            "`",
            "\u00b4",
        ],
    )
    def test_every_other_quotation_shaped_character_stays_literal(self, literal):
        assert fold_smart_quotes(f"o:{literal}draw{literal}") == f"o:{literal}draw{literal}"


class TestUntouched:
    """The property the whole policy rests on: it acts only where it has a measured reason to."""

    @pytest.mark.parametrize(
        "query",
        [
            't:creature c:r cmc<=2 o:"draw a card"',
            '!"Lightning Bolt"',
            "(t:creature or t:land) e:khm",
            "-t:creature e:lea",
            "name:/^Whenever/ e:khm",
            "cmc>=3 pow>tou",
            "otag:draw atag:forest",
            # `-cn:1` only. `-date:2021` and `-cmc!=3` used to sit on this line as untouched terms
            # and they are not -- see TestNegatedComparison below, which measured both.
            "-cn:1 -r>=rare -c>=2 -produces>=2",
            "m:{2}{R}",
            "r>=rare f:modern lang:ja oracleid:0d5f3b41-1b4d-4d8b-8d4c-3f1b2c9e8a70",
        ],
    )
    def test_a_query_with_nothing_to_ignore_comes_back_byte_identical(self, query):
        result = scryfall_term_policy(query)
        assert result.query == query
        assert result.warnings == []
        assert result.all_ignored is False


class TestUnknownKeywords:
    """Keywords Scryfall does not know -- ours, and nobody's."""

    def test_a_local_only_spelling_is_dropped_and_named(self):
        result = scryfall_term_policy("subtype:eldrazi e:khm")
        assert result.query == "e:khm"
        assert result.warnings == [
            "Invalid expression \u201csubtype:eldrazi\u201d was ignored. Unknown keyword \u201csubtype\u201d.",
        ]

    def test_the_minus_is_inside_the_quoted_keyword(self):
        assert scryfall_term_policy("-subtype:human t:cleric").warnings == [
            "Invalid expression \u201c-subtype:human\u201d was ignored. Unknown keyword \u201c-subtype\u201d.",
        ]

    def test_the_scryfall_spelling_of_the_same_predicate_survives(self):
        """`oracle_tags:` is ours and `otag:` is Scryfall's; both reach the same column here."""
        assert scryfall_term_policy("otag:draw e:khm").warnings == []
        assert scryfall_term_policy("oracle_tags:draw e:khm").query == "e:khm"

    def test_a_keyword_neither_side_knows_is_ignored_too(self):
        assert scryfall_term_policy("nonsense:value e:khm").warnings == [
            "Invalid expression \u201cnonsense:value\u201d was ignored. Unknown keyword \u201cnonsense\u201d.",
        ]

    @pytest.mark.parametrize("keyword", ["game", "in", "cube", "new", "stamp", "cheapest"])
    def test_a_keyword_scryfall_knows_and_we_do_not_is_left_alone(self, keyword):
        """Ignoring one would answer a WIDER result than Scryfall, silently, because it honors it."""
        assert scryfall_term_policy(f"{keyword}:x e:khm").warnings == []


class TestComparisonScryfallDoesNotImplement:
    """A comparison operator on a keyword Scryfall does not compare is honored and matches nothing.

    ONE rule, not two. An unknown keyword under `>` `>=` `<` `<=` `!=` and a TEXT column under the
    same five reach the same answer by the same route: the term is kept, it matches nothing, and
    there is no `warnings` key at all. Under `:`/`=` both run a validator and are ignored-and-warned
    instead, which is the pair that separates the two mechanisms::

        nonsense:1   151 + `Unknown keyword "nonsense".`   nonsense>=1   404, no warning
        t:creature   151                                   t>creature    404, no warning
        f:notaformat 151 + `Unknown game format`           f>notaformat  404, no warning
        lang:zz      151 + `Unknown language `zz``         lang>zz       404, no warning

    The boundary is a KEYWORD table, enumerated rather than guessed: every alias in DB_COLUMNS and
    every directive name was probed as `<alias>>=0 e:khm t:creature` against api.scryfall.com on
    2026-08-16. See _COMPARABLE_KEYWORDS for the three classes the 78 rows fell into.
    """

    @pytest.mark.parametrize("operator", [">", ">=", "<", "<=", "!="])
    def test_an_unknown_keyword_under_a_comparison_is_not_the_ignore_machinery(self, operator):
        result = scryfall_term_policy(f"nonsense{operator}1 e:khm t:creature")
        assert result.warnings == []
        assert result.query == "cmc<0 e:khm t:creature"

    @pytest.mark.parametrize("operator", [":", "="])
    def test_and_under_equality_it_still_is(self, operator):
        assert scryfall_term_policy(f"nonsense{operator}1 e:khm").warnings == [
            f"Invalid expression \u201cnonsense{operator}1\u201d was ignored. Unknown keyword \u201cnonsense\u201d.",
        ]

    @pytest.mark.parametrize("term", ["t>creature", "t!=creature", "o!=flying", "name!=a", "a>guay", "ft>zzz", "wm>zzz"])
    def test_a_text_column_under_a_comparison_matches_nothing(self, term):
        result = scryfall_term_policy(f"{term} e:khm")
        assert result.warnings == []
        assert result.query == "cmc<0 e:khm"

    @pytest.mark.parametrize("term", ["t:creature", "o:flying", "name:a", "t=creature"])
    def test_the_equality_twins_are_ordinary_searches(self, term):
        assert scryfall_term_policy(f"{term} e:khm").query == f"{term} e:khm"

    @pytest.mark.parametrize("term", ["f>notaformat", "lang>zz", "oracleid>abc", "is>foil", "layout>normal", "border>black"])
    def test_it_runs_before_every_value_validator(self, term):
        """Which is why those go quiet under a comparison.

        `f>notaformat`, `lang>zz`, `oracleid>abc` and `is>foil` are one 404 each with no warning,
        where `f:notaformat`, `lang:zz` and `oracleid:abc` are all ignored-and-warned.
        """
        result = scryfall_term_policy(f"{term} e:khm t:creature")
        assert result.warnings == []
        assert result.query == "cmc<0 e:khm t:creature"

    @pytest.mark.parametrize("keyword", ["unique", "sort", "order", "direction", "dir", "prefer"])
    def test_the_directive_names_take_it_too(self, keyword):
        assert scryfall_term_policy(f"{keyword}>=0 e:khm").query == "cmc<0 e:khm"

    @pytest.mark.parametrize(
        "term",
        [
            "c>=2",
            "ci>=2",
            "colour>=2",
            "commander>=2",
            "id>=2",
            "produces>=2",
            "m>=2",
            "cmc>=3",
            "mv>=3",
            "manavalue>=3",
            "pow>=1",
            "power>=1",
            "tou>=1",
            "toughness>=1",
            "loy>=3",
            "loyalty>=3",
            "usd>=1",
            "eur>=1",
            "tix>=1",
            "cn>=100",
            "number>=100",
            "year>=2022",
            "date>=2022",
            "r>=rare",
            "rarity>=rare",
        ],
    )
    def test_the_keywords_scryfall_does_compare_are_untouched(self, term):
        assert scryfall_term_policy(f"{term} e:khm").query == f"{term} e:khm"

    @pytest.mark.parametrize("term", ["-nonsense>=1", "-t>creature", "-lang>zz"])
    def test_the_negated_form_still_takes_the_tautology(self, term):
        """And this rule does not steal it.

        `-nonsense>=1` and `-t>creature` are 151 with `warnings` absent -- the always-true leaf the
        negation rule installs, NOT this rule's empty one. The negation block runs first, so a
        negated comparison never reaches here.
        """
        result = scryfall_term_policy(f"{term} e:khm t:creature")
        assert result.warnings == []
        assert result.query == "-cmc<0 e:khm t:creature"


class TestNegatedNumericEquality:
    """Scryfall cannot express it, and says so in two different sentences."""

    def test_mana_value_gets_the_value_sentence(self):
        assert scryfall_term_policy("-cmc:3 e:lea").warnings == [
            "Invalid expression \u201c-cmc:3\u201d was ignored. The value must be a number, or \u201ceven\u201d/\u201codd\u201d",
        ]

    @pytest.mark.parametrize(("term", "keyword"), [("-tou:1", "-tou"), ("-usd:0", "-usd"), ("-loy:3", "-loy")])
    def test_the_other_numeric_columns_get_an_unknown_keyword_sentence(self, term, keyword):
        assert scryfall_term_policy(f"{term} e:lea").warnings == [
            f"Invalid expression \u201c{term}\u201d was ignored. Unknown keyword \u201c{keyword}\u201d.",
        ]

    @pytest.mark.parametrize("query", ["-t:creature e:lea", "-cn:1", "-o:flying e:khm", "-is:foil e:khm"])
    def test_negation_itself_is_fine(self, query):
        result = scryfall_term_policy(query)
        assert result.warnings == []
        assert result.query == query


# The general case of the class above, measured on api.scryfall.com 2026-08-16 with the anchor
# `e:khm t:creature` = 151. A row answering 151 is a term that did nothing; see
# _NEGATION_HONORING_COMPARISONS in query_terms.py for the full 65-row table.
_ALWAYS = "-cmc<0"


class TestNegatedComparison:
    """A negated comparison is not applied -- it is always-true, and silently so."""

    #   pow>=1 = 146 / -pow>=1 = 151     cmc!=3 = 106 / -cmc!=3 = 151
    #   tou!=1 = 133 / -tou!=1 = 151     usd>=1 =  28 / -usd>=1 = 151
    @pytest.mark.parametrize("keyword", ["pow", "power", "tou", "toughness", "cmc", "mv", "loy", "usd", "eur", "tix", "year", "cn"])
    @pytest.mark.parametrize("operator", [">", ">=", "<", "<=", "!="])
    def test_every_operator_on_every_column_scryfall_honors_positive(self, keyword, operator):
        result = scryfall_term_policy(f"-{keyword}{operator}1 e:khm t:creature")
        assert result.query == f"{_ALWAYS} e:khm t:creature"
        assert result.warnings == []

    # `name>zzz`, `t>creature` and `nonsense>=1` are all 404 positive -- so the negated form matching
    # everything is ordinary -- but `-nonsense>=1 e:khm t:creature` is 151 with NO `warnings` key,
    # where `nonsense:1` is ignored-and-warned. That silence is the assertion.
    @pytest.mark.parametrize("term", ["-name>zzz", "-o>draw", "-t>creature", "-layout>normal", "-nonsense>=1", "-subtype>=1"])
    def test_a_text_column_or_an_unknown_keyword_takes_it_too_and_stays_quiet(self, term):
        result = scryfall_term_policy(f"{term} e:khm")
        assert result.query == f"{_ALWAYS} e:khm"
        assert result.warnings == []

    # Each of these is ignored-and-warned unnegated and 151-with-no-warning negated.
    @pytest.mark.parametrize("term", ["-lang>zz", "-f>notaformat", "-oracleid>abc", "-cmc>=notanumber"])
    def test_it_runs_before_the_value_validators_because_scryfalls_does(self, term):
        result = scryfall_term_policy(f"{term} e:khm")
        assert result.query == f"{_ALWAYS} e:khm"
        assert result.warnings == []

    # `r>=rare` 52 / `-r>=rare` 99, `c>=2` 19 / `-c>=2` 132, `m>=2` 102 / `-m>=2` 49,
    # `produces>=2` 5 / `-produces>=2` 146, `devotion>={r}{r}` 7 / negated 144 -- all exact
    # complements of the 151 anchor, so the boundary is the KEYWORD and not the operator.
    @pytest.mark.parametrize(
        "term",
        [
            "-c>=2",
            "-color>=2",
            "-colors>=2",
            "-colour>=2",
            "-colours>=2",
            "-id>=2",
            "-identity>=2",
            "-ci>=2",
            "-commander>=2",
            "-r>=rare",
            "-rarity!=rare",
            "-m>=2",
            "-mana!=2",
            "-produces>=2",
            "-devotion>={R}{R}",
        ],
    )
    def test_the_set_comparison_columns_negate_correctly_and_must_be_left_alone(self, term):
        assert scryfall_term_policy(f"{term} e:khm").query == f"{term} e:khm"

    def test_a_parenthesised_group_is_honored_the_fault_is_how_minus_binds_to_a_leaf(self):
        # `-(cmc>=3) e:khm t:creature` = 39, the complement of `cmc>=3`'s 112, where the bare
        # `-cmc>=3` = 151.
        assert scryfall_term_policy("-(cmc>=3) e:khm").query == "-(cmc>=3) e:khm"
        assert scryfall_term_policy("-(pow>=1 t:god) e:khm").query == "-(pow>=1 t:god) e:khm"

    def test_it_is_a_tautology_not_a_dropped_term_which_only_an_or_can_tell_apart(self):
        # `(-pow>=1 or t:god) e:khm` = 323, all of Kaldheim; `(t:god) e:khm` = 13, which is what a
        # REMOVED arm answers. And `-pow>=1` alone is 200 with the whole 33,599-card corpus, where
        # `-pow:1` alone is the 400 "All of your terms were ignored." -- two different mechanisms.
        assert scryfall_term_policy("(-pow>=1 or t:god) e:khm").query == f"({_ALWAYS} or t:god) e:khm"
        alone = scryfall_term_policy("-pow>=1")
        assert alone.query == _ALWAYS
        assert alone.all_ignored is False
        assert alone.warnings == []
        assert scryfall_term_policy("-pow:1").all_ignored is True

    def test_the_two_mechanisms_coexist_without_borrowing_each_others_sentence(self):
        # `-pow>=1 -cmc:3 e:khm t:creature` is 151 warning ONLY about `-cmc:3`.
        result = scryfall_term_policy("-pow>=1 -cmc:3 e:khm")
        assert result.query == f"{_ALWAYS} e:khm"
        assert result.warnings == [
            "Invalid expression \u201c-cmc:3\u201d was ignored. The value must be a number, or \u201ceven\u201d/\u201codd\u201d",
        ]

    # Measured with values that separate all three readings: `date>=2022` = 11 and `-date>=2022` = 11
    # (honored would be 140, dropped would be the anchor's 151); `date<2022` = 141 = `-date<2022`.
    # `year`, the same column under another name, takes the tautology instead -- `year>=2022` = 11,
    # `-year>=2022` = 151 -- which the first test pins.
    @pytest.mark.parametrize("operator", [">", ">=", "<", "<=", "!=", ":", "="])
    def test_date_discards_the_minus_instead_every_operator_included(self, operator):
        result = scryfall_term_policy(f"-date{operator}2021 e:khm")
        assert result.query == f"date{operator}2021 e:khm"
        assert result.warnings == []


class TestValues:
    """Values a known keyword cannot take."""

    def test_format(self):
        assert scryfall_term_policy("f:notaformat e:khm").warnings == [
            "Invalid expression \u201cf:notaformat\u201d was ignored. Unknown game format \u201cnotaformat\u201d",
        ]
        # Measured as honored despite not being a `legalities` key; `pauperedh` and `frontier` are
        # measured as ignored, so the list is a boundary rather than a superset.
        assert scryfall_term_policy("f:explorer").warnings == []

    def test_language_uses_backticks_not_quotes(self):
        assert scryfall_term_policy("lang:zz e:khm").warnings == [
            "Invalid expression \u201clang:zz\u201d was ignored. Unknown language `zz`",
        ]

    @pytest.mark.parametrize("term", ["lang:ja", "lang:any", "lang:pt-br", "lang:chinesesimplified", "language:english"])
    def test_the_language_spellings_scryfall_resolves(self, term):
        assert scryfall_term_policy(f"{term} e:khm").warnings == []

    def test_rarity_puts_the_full_stop_inside_the_quotes(self):
        """Scryfall's, not a typo here: the live body puts the full stop inside the quotes."""
        assert scryfall_term_policy("r:notarare e:khm").warnings == [
            "Invalid expression \u201cr:notarare\u201d was ignored. Unknown rarity \u201cnotarare.\u201d",
        ]
        assert scryfall_term_policy("r>=rare e:khm").warnings == []

    @pytest.mark.parametrize("operator", [":", "=", ">", ">=", "<", "<=", "!="])
    def test_rarity_checks_its_value_under_every_operator(self, operator):
        """Rarity is an ordered enum, so `r>rare` is a comparison Scryfall really performs.

        It therefore checks the value under a comparison exactly as it does under equality. Anchor
        `e:khm t:creature` = 151, one request each: all seven answer 151 carrying the same sentence.
        With an equality-only guard this surface answered `400 Failed to parse query` for the five
        comparisons, because nothing removed the term and the parser rejects a word that is not a
        rarity.
        """
        term = f"r{operator}notarare"
        assert scryfall_term_policy(f"{term} e:khm t:creature").warnings == [
            f"Invalid expression \u201c{term}\u201d was ignored. Unknown rarity \u201cnotarare.\u201d",
        ]

    def test_a_number_is_not_a_rarity_either(self):
        assert scryfall_term_policy("rarity>=0 e:khm").warnings == [
            "Invalid expression \u201crarity>=0\u201d was ignored. Unknown rarity \u201c0.\u201d",
        ]

    @pytest.mark.parametrize("term", ["r>rare", "r<=mythic", "r!=common", "-r>=rare"])
    def test_the_rarity_comparisons_scryfall_performs_are_untouched(self, term):
        assert scryfall_term_policy(f"{term} e:khm").warnings == []

    @pytest.mark.parametrize(
        ("value", "reason"),
        [
            ("2", "Devotion can only match single color or hybrid mana."),
            ("{c}", "Devotion can only match single color or hybrid mana."),
            ("{s}", "Devotion can only match single color or hybrid mana."),
            ("{x}", "Devotion can only match single color or hybrid mana."),
            ("{1}", "Devotion can only match single color or hybrid mana."),
            ("{2/r}", "Devotion can only match single color or hybrid mana."),
            ("{r/p}", "Devotion can only match single color or hybrid mana."),
            ("{w}{u}", "Devotion can only match single color or hybrid mana."),
            ("{r}{g}", "Devotion can only match single color or hybrid mana."),
            ("rg", "Devotion can only match single color or hybrid mana."),
            ("{r}{r/g}", "Devotion can only match single color or hybrid mana."),
            # Not a mana symbol at all -- a different sentence, and the echo is the value as
            # written with `upper()` applied.
            ("{p}", "Unknown mana symbols \u201c{P}\u201d."),
            ("{}", "Unknown mana symbols \u201c{}\u201d."),
            ("notmana", "Unknown mana symbols \u201cNOTMANA\u201d."),
        ],
    )
    def test_devotion_takes_one_colour_or_one_hybrid_pair(self, value, reason):
        assert scryfall_term_policy(f"devotion:{value} e:khm").warnings == [
            f"Invalid expression \u201cdevotion:{value}\u201d was ignored. {reason}",
        ]

    @pytest.mark.parametrize(
        "value",
        ["{r}", "{R}", "r", "{r}{r}", "rr", "{r}{r}{r}", "{r/g}", "{g/r}", "{r/g}{r/g}", "{r/g}{g/r}"],
    )
    def test_the_devotion_values_scryfall_honors(self, value):
        """One colour repeated, one hybrid pair repeated, either brace order, braced or not.

        Order-insensitivity is measured rather than assumed: `{g/r}` and `{r/g}` answer the same 62,
        and mixing the two spellings in one value answers the same 16 as either alone.
        """
        assert scryfall_term_policy(f"devotion:{value} e:khm").warnings == []

    @pytest.mark.parametrize("term", ["devotion:2", "devotion>2", "devotion>=2", "-devotion:2", "-devotion>2"])
    def test_devotion_checks_its_value_in_both_polarities(self, term):
        """A VALUE check, not a negation rule.

        `devotion` is in _NEGATION_HONORING_COMPARISONS, which is what lets a negated comparison
        reach the validator instead of being swallowed as an always-true leaf.
        """
        assert scryfall_term_policy(f"{term} e:khm t:creature").warnings == [
            f"Invalid expression \u201c{term}\u201d was ignored. Devotion can only match single color or hybrid mana.",
        ]

    def test_oracle_id_must_be_a_v4_uuid(self):
        assert scryfall_term_policy("oracleid:notauuid e:khm").warnings == [
            "Invalid expression \u201coracleid:notauuid\u201d was ignored. You must provide a valid v4 UUID.",
        ]

    @pytest.mark.parametrize(
        ("term", "reason"),
        [
            ("c:qq", "Unknown color \u201cq\u201d"),
            ("c:glint", "Unknown color \u201ci\u201d"),
            ("c:notacolor", "A card cannot be both colored and colorless."),
            ("c:witch", "A card cannot be both colored and colorless."),
            ("c:cm", "Using \u201cm\u201d with other colors is no longer supported. Use c>c instead."),
        ],
    )
    def test_color(self, term, reason):
        assert scryfall_term_policy(f"{term} e:khm").warnings == [
            f"Invalid expression \u201c{term}\u201d was ignored. {reason}",
        ]

    @pytest.mark.parametrize("term", ["c:rg", "c:azorius", "c:colorless", "c:2", "ci:wu", "c>=uw", "produces:r"])
    def test_the_color_values_scryfall_accepts(self, term):
        assert scryfall_term_policy(f"{term} e:khm").warnings == []

    def test_a_numeric_column_asked_for_a_word(self):
        """Two Scryfall answers, so two rules.

        `q=cmc:notanumber` is the 400 with a warning; `q=cmc>=notanumber` is the ordinary 404 --
        the term is HONORED and matches nothing. Dropping the second would have turned
        `cmc>=notanumber e:khm` into all of Kaldheim where Scryfall answers "no cards".
        """
        assert scryfall_term_policy("cmc:notanumber").all_ignored is True
        assert scryfall_term_policy("pow:notanumber").warnings == [
            "Invalid expression \u201cpow:notanumber\u201d was ignored. Unknown keyword \u201cpow\u201d.",
        ]
        comparison = scryfall_term_policy("cmc>=notanumber e:khm")
        assert comparison.warnings == []
        assert comparison.all_ignored is False
        assert comparison.query == "cmc<0 e:khm"


class TestRegexes:
    """PostgreSQL's sentences, read off live responses rather than translated."""

    @pytest.mark.parametrize(
        ("query", "reason"),
        [
            ("o:/[unclosed/", "brackets [] not balanced."),
            ("name:/[a-/", "brackets [] not balanced."),
            ("o:/(unclosed/", "parentheses () not balanced."),
            ("o:/a)/", "parentheses () not balanced."),
            ("o:/a{2,1}/", "invalid repetition count(s)."),
        ],
    )
    def test_a_pattern_that_will_not_compile(self, query, reason):
        assert scryfall_term_policy(query).warnings == [
            f"Invalid expression \u201c{query}\u201d was ignored. Invalid regular expression: {reason}",
        ]

    @pytest.mark.parametrize("query", [r"o:/\(this creature/", r"name:/\./ e:khm", r"cn:/\d/"])
    def test_a_pattern_that_compiles_is_left_alone_escapes_and_all(self, query):
        result = scryfall_term_policy(query)
        assert result.warnings == []
        assert result.query == query


class TestWhatIsLeft:
    """What the query becomes when terms leave it."""

    def test_a_group_whose_every_arm_went_takes_its_parentheses_with_it(self):
        result = scryfall_term_policy("(subtype:elf or subtype:goblin) e:war")
        assert result.query == "e:war"
        assert len(result.warnings) == 2

    def test_a_group_that_keeps_an_arm_keeps_its_parentheses(self):
        assert scryfall_term_policy("(subtype:elf t:creature) e:war").query == "(t:creature) e:war"

    @pytest.mark.parametrize(
        ("query", "expected"),
        [("t:creature or subtype:elf", "t:creature"), ("subtype:elf or t:creature", "t:creature")],
    )
    def test_a_connector_orphaned_by_a_drop_goes_too(self, query, expected):
        assert scryfall_term_policy(query).query == expected

    def test_every_term_ignored_is_the_400_case(self):
        result = scryfall_term_policy("subtype:elf or subtype:goblin")
        assert result.all_ignored is True
        assert len(result.warnings) == 2

    def test_an_empty_group_is_all_ignored_with_nothing_to_warn_about(self):
        result = scryfall_term_policy("()")
        assert result.all_ignored is True
        assert result.warnings == []

    def test_a_dangling_operator_is_the_bare_keyword_searched_as_a_name(self):
        """Not a dropped term and not a vacuous one: `t:` is `t`, and a bare word is `name:t`.

        Sixteen live pairs pin it (see _dangling_operator_term); the ones asserted here are
        `t: e:khm` = `t e:khm` = 215, `-t: e:khm` = 108, and `t:` alone = `name:t` = 22,261
        rather than the 400 that "every term was ignored" would produce.
        """
        assert scryfall_term_policy("t: e:khm").query == "name:t e:khm"
        assert scryfall_term_policy("t: e:khm").warnings == []
        assert scryfall_term_policy("-t: e:khm").query == "-name:t e:khm"
        alone = scryfall_term_policy("t:")
        assert alone.all_ignored is False
        assert alone.warnings == []
        assert alone.query == "name:t"

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            # `t>` = `t<` = `t:` = 215 in Kaldheim; `t=`, `t>=`, `t<=` and `t!=` are all 404, the
            # same answer `name:"t="` gives. The split is measured, not tidied.
            ("t> e:khm", "name:t e:khm"),
            ("t< e:khm", "name:t e:khm"),
            ("t= e:khm", 'name:"t=" e:khm'),
            ("t>= e:khm", 'name:"t>=" e:khm'),
            ("t!= e:khm", 'name:"t!=" e:khm'),
            # A keyword neither side knows is still a bare word once its value is gone:
            # `nonsense:x` is "Unknown keyword" and `nonsense:` is the 404 `q=nonsense` gives.
            ("nonsense: e:khm", "name:nonsense e:khm"),
            ("cmc: e:khm", "name:cmc e:khm"),
            ("subtype: e:khm", "name:subtype e:khm"),
        ],
    )
    def test_the_operator_decides_how_much_of_the_token_becomes_the_word(self, query, expected):
        result = scryfall_term_policy(query)
        assert result.query == expected
        assert result.warnings == []

    @pytest.mark.parametrize("query", ["e:khm (t:god", "e:khm t:god)", "(", ")", "((t:god)"])
    def test_parentheses_that_do_not_balance(self, query):
        assert scryfall_term_policy(query).unclosed_parens is True

    @pytest.mark.parametrize("query", ['name:"(a"', r"o:/\(this creature/", "(t:creature or t:land) e:khm", "m:{2}{R}"])
    def test_a_parenthesis_inside_a_string_a_pattern_or_a_mana_symbol_is_not_a_parenthesis(self, query):
        assert scryfall_term_policy(query).unclosed_parens is False


def test_a_long_expression_is_echoed_at_twenty_characters_ellipsis_included():
    """Measured a character at a time.

    `f:abcdefghijklmnopqr` (20 characters) comes back whole and one more character comes back cut.
    Only the EXPRESSION is cut -- the reason still names the full value.
    """
    assert "\u201cf:abcdefghijklmnopqr\u201d" in scryfall_term_policy("f:abcdefghijklmnopqr").warnings[0]
    cut = scryfall_term_policy("f:abcdefghijklmnopqrs").warnings[0]
    assert "\u201cf:abcdefghijklmnopq\u2026\u201d" in cut
    assert "Unknown game format \u201cabcdefghijklmnopqrs\u201d" in cut


class TestRegexComplexity:
    """`Regular expression too complex.` -- a weighted count of six characters, and a length.

    Every boundary was found on api.scryfall.com 2026-10-03 by lengthening one pattern a character
    at a time under `t:instant` (3,909 with the warning is a dropped regex).
    """

    COMPLEX = "Regular expression too complex."

    @staticmethod
    def runs(body: str) -> None:
        # SCRYFALL'S rule, asked directly: the parser's own budget is narrower in places (64
        # constructs, 4 lookarounds) and refuses some of these rows with the same sentence.
        assert scryfall_regex_text_reason(body) is None

    def refused(self, body: str) -> None:
        result = scryfall_term_policy(f"t:instant o:/{body}/")
        assert result.query == "t:instant"
        assert len(result.warnings) == 1
        assert result.warnings[0].endswith(f"was ignored. {self.COMPLEX}")

    def test_the_score_a_dot_and_a_paren_cost_1_a_quantifier_2_a_pipe_4_and_90_is_refused(self):
        self.runs("." * 89)
        self.refused("." * 90)
        self.runs("destroy" + "." * 89 + "creature")
        self.refused("destroy" + "." * 90 + "creature")
        for quantified in ("a*", "a+", "a?"):
            self.runs(quantified * 44)
            self.refused(quantified * 45)
        self.runs("x" + "|" * 22)
        self.refused("x" + "|" * 23)
        self.runs("(a)" * 29 + "." * 60)
        self.refused("(a)" * 30 + "." * 60)
        self.runs("a{2}" * 14 + "." * 60)
        self.refused("a{2}" * 15 + "." * 60)

    def test_it_is_one_sum_not_a_cap_per_character(self):
        self.runs("." * 45 + "a*" * 22)
        self.refused("." * 46 + "a*" * 22)
        self.runs("|" * 20 + "." * 9)
        self.refused("|" * 20 + "." * 10)
        self.runs(".*" * 29)
        self.refused(".*" * 30)
        self.runs("a*?" * 7 + "." * 60)
        self.refused("a*?" * 8 + "." * 60)

    @pytest.mark.parametrize("group", ["(?=a)", "(?!a)", "(?<=a)", "(?<!a)", "(?:a)"])
    def test_a_lookaround_costs_its_parenthesis_and_its_question_mark(self, group):
        self.runs(group * 9 + "." * 60)
        self.refused(group * 10 + "." * 60)

    def test_an_escaped_or_bracketed_operator_costs_what_a_live_one_does(self):
        self.runs("\\." * 44 + "." * 45)
        self.refused("\\." * 45 + "." * 45)
        self.runs("[.]" * 29 + "." * 60)
        self.refused("[.]" * 30 + "." * 60)
        self.runs("[|]" * 22)
        self.refused("[|]" * 23)
        self.runs("\\|" * 22)
        self.refused("\\|" * 23)
        self.refused("[*]" * 45)
        self.refused("\\*" * 45)
        self.refused("\\{" * 15 + "." * 60)

    @pytest.mark.parametrize("free", ["\\s", "\\)", "[a]", "^", "$", "\\w", "\\d", "\\W", "\\n", "-x", "~", "#", ",", ":", "!"])
    def test_what_weighs_nothing(self, free):
        self.runs(free * 40 + "." * 89)

    def test_letters_weigh_nothing(self):
        self.runs("a" * 150 + "." * 89)

    @pytest.mark.parametrize("char", ["a", "1", "A", ",", "\u00e9"])
    def test_248_characters_run_and_249_do_not(self, char):
        self.runs(char * 248)
        self.refused(char * 249)

    @pytest.mark.parametrize("keyword", ["name", "o", "t", "ft"])
    def test_the_length_is_the_patterns_whatever_the_keyword(self, keyword):
        assert scryfall_term_policy(f"t:instant {keyword}:/{'a' * 248}/").warnings == []
        assert len(scryfall_term_policy(f"t:instant {keyword}:/{'a' * 249}/").warnings) == 1

    def test_a_backslash_and_a_double_quote_count_twice_a_hash_brace_three_times_two_hyphens_once(self):
        self.runs("\\." * 82)
        self.refused("\\." * 83)
        self.runs("\\w" * 82)
        self.refused("\\w" * 83)
        self.runs('"' * 10 + "a" * 228)
        self.refused('"' * 10 + "a" * 229)
        self.runs("#{" * 10 + "a" * 218)
        self.refused("#{" * 10 + "a" * 219)
        self.runs("-" * 10 + "a" * 243)
        self.refused("-" * 10 + "a" * 244)
        self.runs("-" * 249)
        self.runs("'" * 10 + "a" * 238)
        self.refused("'" * 10 + "a" * 239)

    @staticmethod
    def junk_alternation(count: int) -> str:
        """`destroy target (creature|qazxjkvw|qbzxjkvw|...)` with `count` junk alternatives."""
        letters = "abcdefghijklmnopqrstuvwxyz"
        return f"destroy target ({'|'.join(['creature', *(f'q{letters[i]}zxjkvw' for i in range(count))])})"

    def test_the_reported_shapes_a_long_alternation_and_a_run_of_dots(self):
        # 23 alternatives run (223 characters); 25 do not (241), nor does anything longer.
        self.runs(self.junk_alternation(22))
        self.refused(self.junk_alternation(24))
        self.refused(self.junk_alternation(26))
        self.refused("destroy" + "." * 135 + "creature")
        # Beside another term the query ANSWERS, with Scryfall's echo: 19 characters and U+2026.
        beside = scryfall_term_policy(f"t:instant o:/{self.junk_alternation(25)}/")
        assert beside.all_ignored is False
        assert beside.warnings == [ignored("o:/destroy target (\u2026", self.COMPLEX)]
        # Alone it is Scryfall's 400, carrying the warning.
        alone = scryfall_term_policy(f"o:/{self.junk_alternation(24)}/")
        assert alone.all_ignored is True
        assert alone.warnings == [ignored("o:/destroy target (\u2026", self.COMPLEX)]

    def test_it_is_decided_before_the_nesting_rule_and_before_the_compiler(self):
        def reason_of(body: str) -> str:
            return scryfall_term_policy(f"t:instant o:/{body}/").warnings[0]

        assert reason_of("(((a)))" + "." * 90).endswith(self.COMPLEX)
        assert reason_of("(((a)))" + "a" * 249).endswith(self.COMPLEX)
        assert reason_of("[" + "." * 90).endswith(self.COMPLEX)
        assert reason_of("(" + "." * 90).endswith(self.COMPLEX)
        # 88 dots and one group is 89, and runs; the 89th dot makes 90.
        self.runs("." * 88 + "(a)")
        self.refused("." * 89 + "(a)")


class TestRegexRepetition:
    """`Too much repetition.` -- the upper bounds of a pattern's `{...}` quantifiers may not pass 50."""

    REPETITION = "Too much repetition."

    @staticmethod
    def warnings_of(body: str) -> list[str]:
        return scryfall_term_policy(f"t:instant o:/{body}/").warnings

    def refused(self, body: str) -> None:
        term = f"o:/{body}/"
        echo = term if len(term) <= 20 else term[:19] + "\u2026"
        assert self.warnings_of(body) == [ignored(echo, self.REPETITION)]

    @pytest.mark.parametrize("body", ["a{50}", "a{0,50}", ".{50}", "a{25}b{25}", "x{3,4}y{46}", "a{1}" * 26, "(a{10}){10}"])
    def test_fifty_runs_as_one_bound_or_as_several(self, body):
        assert scryfall_regex_text_reason(body) is None

    @pytest.mark.parametrize("body", ["a{51}", "a{0,51}", ".{51}", "a{25}b{26}", "a{0,25}b{0,26}", "x{3,4}y{47}", "a{2}" * 26])
    def test_fifty_one_does_not(self, body):
        self.refused(body)

    def test_the_upper_bound_counts_and_an_open_one_counts_nothing(self):
        assert scryfall_regex_text_reason("a{25,26}") is None
        self.refused("a{51,60}")
        self.refused("a{60,51}")
        assert scryfall_regex_text_reason("a{51,}") is None
        assert scryfall_regex_text_reason("x{3,}y{50}") is None
        assert scryfall_regex_text_reason("a{255,}") is None

    def test_it_reads_characters_a_bracketed_brace_counts_an_escaped_or_spaced_one_does_not(self):
        self.refused("[{51}]")
        self.refused("{r}{51}")
        self.refused("a{051}")
        assert scryfall_regex_text_reason("\\{51\\}") is None
        assert scryfall_regex_text_reason("a{ 51}") is None

    def test_the_reported_pattern_and_where_the_rule_sits(self):
        assert self.warnings_of("destroy.{135}creature") == [ignored("o:/destroy.{135}cre\u2026", self.REPETITION)]
        assert self.warnings_of("destroy[^.]{0,100}creature") == [ignored("o:/destroy[^.]{0,10\u2026", self.REPETITION)]
        # The bound real queries use.
        assert self.warnings_of("destroy[^.]{0,35}creature") == []
        # After the other two text rules, before the compiler.
        assert self.warnings_of("(((a{60})))")[0].endswith("Too many nested groups.")
        assert self.warnings_of("a{60}" + "." * 90)[0].endswith("Regular expression too complex.")
        self.refused("a{60}[")
        alone = scryfall_term_policy("o:/a{1000}/")
        assert alone.all_ignored is True
        assert alone.warnings == [ignored("o:/a{1000}/", self.REPETITION)]


class TestRegexNesting:
    """`Too many nested groups.` -- parentheses three deep, counted as characters."""

    NESTED = "Too many nested groups."

    @pytest.mark.parametrize(
        "query",
        [
            "o:/destroy ((target|another) (nonblack|nonwhite)|that) creature/",
            "t:instant o:/(destroy) (target) (creature)/",
            "t:instant o:/(a)(b)(c)(d)(e)(f)(g)(h)(i)(j)(k)/",
            "t:instant o:/destroy ((target)|(another)) (creature)/",
            "t:instant o:/(?<!x(y))destroy target creature/",
        ],
    )
    def test_depth_two_runs_and_so_do_siblings_however_many(self, query):
        result = scryfall_term_policy(query)
        assert result.warnings == []
        assert result.query == query

    def test_depth_three_alone_is_the_400_that_carries_the_warning(self):
        result = scryfall_term_policy("o:/destroy ((target (nonblack|nonwhite))|that) creature/")
        assert result.all_ignored is True
        assert result.warnings == [ignored("o:/destroy ((target\u2026", self.NESTED)]

    def test_depth_three_beside_another_term_is_dropped_and_the_rest_answers(self):
        result = scryfall_term_policy("t:instant o:/destroy (((target))) creature/")
        assert result.query == "t:instant"
        assert result.warnings == [ignored("o:/destroy (((targe\u2026", self.NESTED)]

    @pytest.mark.parametrize(
        ("term", "echo"),
        [
            ("o:/destroy (?:(?:(?:target))) creature/", "o:/destroy (?:(?:(?\u2026"),
            ("o:/destroy (((?=target))) creature/", "o:/destroy (((?=tar\u2026"),
            ("o:/destroy ((?!(x))target) creature/", "o:/destroy ((?!(x))\u2026"),
            ("o:/(?<!x(y(z)))destroy target creature/", "o:/(?<!x(y(z)))dest\u2026"),
            ("o:/((destroy)(( target))) creature/", "o:/((destroy)(( tar\u2026"),
            ("name:/(((a)))/", "name:/(((a)))/"),
            ("t:/(((instant)))/", "t:/(((instant)))/"),
            ("ft:/(((the)))/", "ft:/(((the)))/"),
            ("mana:/((({r})))/", "mana:/((({r})))/"),
            ("-o:/(((target)))/", "-o:/(((target)))/"),
        ],
    )
    def test_every_kind_of_group_and_every_regex_keyword(self, term, echo):
        result = scryfall_term_policy(f"t:instant {term}")
        assert result.query == "t:instant"
        assert result.warnings == [ignored(echo, self.NESTED)]

    @pytest.mark.parametrize(
        ("term", "echo"),
        [
            ("o:/destroy (?:(?:[(]?target)) creature/", "o:/destroy (?:(?:[(\u2026"),
            ("o:/destroy (?:(?:\\(?target)) creature/", "o:/destroy (?:(?:\\(\u2026"),
            ("o:/\\(\\(\\(/", "o:/\\(\\(\\(/"),
            ("o:/[(][(][(]/", "o:/[(][(][(]/"),
            ("o:/destroy (?#(((x)target creature/", "o:/destroy (?#(((x)\u2026"),
        ],
    )
    def test_it_counts_the_two_characters_escaped_or_bracketed_and_not_the_groups(self, term, echo):
        assert scryfall_term_policy(f"t:instant {term}").warnings == [ignored(echo, self.NESTED)]

    @pytest.mark.parametrize("body", ["(\\)(\\)(a)))", "([)]([)](a)))"])
    def test_a_real_depth_of_three_the_count_reads_as_one_is_not_this_rule(self, body):
        r"""Each `\)` and `[)]` closed a level: both run on Scryfall (404 under `t:instant`, no warning)."""
        assert scryfall_regex_text_reason(body) is None

    def test_it_is_decided_before_the_compiler_speaks(self):
        assert scryfall_term_policy("t:instant o:/(((a/").warnings == [ignored("o:/(((a/", self.NESTED)]
        assert scryfall_term_policy("t:instant o:/(((a)))[/").warnings == [ignored("o:/(((a)))[/", self.NESTED)]
        # The counter is not clamped at zero: it reaches -1, comes back to 2, and the compiler's
        # sentence is the one Scryfall sends.
        assert scryfall_term_policy("t:instant o:/())(((a)/").warnings == [
            ignored("o:/())(((a)/", "Invalid regular expression: parentheses () not balanced."),
        ]

    def test_two_of_them_are_two_warnings_and_a_group_of_nothing_else_goes_with_them(self):
        result = scryfall_term_policy("t:instant (o:/(((a)))/ or o:/(((b)))/)")
        assert result.query == "t:instant"
        assert result.warnings == [ignored("o:/(((a)))/", self.NESTED), ignored("o:/(((b)))/", self.NESTED)]

    def test_a_shipped_query_that_carried_one(self):
        """All 18,760 creatures on Scryfall, where the regex ran here and answered 86."""
        result = scryfall_term_policy(
            "o:/((sacrifice (this creature|it)[^.]*end step)|(end step[^.]*sacrifice (this creature|it)))/ t:creature",
        )
        assert result.warnings == [ignored("o:/((sacrifice (thi\u2026", self.NESTED)]
        assert result.query == "t:creature"


class TestBackreferences:
    r"""A backreference is accepted and matches nothing: `\1` is not a backreference on Scryfall."""

    @pytest.mark.parametrize(
        ("query", "kept"),
        [
            ("t:creature name:/^(.)\\1\\1/", "t:creature name:/^(.)\\x01\\x01/"),
            ("name:/(o)\\1/ t:elf", "name:/(o)\\x01/ t:elf"),
            ("o:/(e)\\1/ t:elf", "o:/(e)\\x01/ t:elf"),
            # No group to refer to, a group that does not exist, `\0` and two digits: the same 404.
            ("name:/o\\1/ t:elf", "name:/o\\x01/ t:elf"),
            ("name:/^(.)\\2/ t:elf", "name:/^(.)\\x01/ t:elf"),
            ("name:/(o)\\0/ t:elf", "name:/(o)\\x01/ t:elf"),
            ("name:/(o)\\10/ t:elf", "name:/(o)\\x01/ t:elf"),
            # The `-` and the `or` compose on an empty leaf: 730 and 730 on Scryfall.
            ("-name:/(.)\\1/ t:elf", "-name:/(.)\\x01/ t:elf"),
            ("name:/a\\1b/ or t:elf", "name:/a\\x01b/ or t:elf"),
            ("Name:/(O)\\1/ t:elf", "Name:/(O)\\x01/ t:elf"),
        ],
    )
    def test_each_backreference_becomes_a_character_no_card_carries_and_the_term_stays_a_regex(self, query, kept):
        result = scryfall_term_policy(query)
        assert result.warnings == []
        assert result.all_ignored is False
        assert result.query == kept
        # ...and what is left is a query the parser takes, where the original was refused whole.
        parse_scryfall_query(result.query)

    def test_the_reported_query_was_a_refusal_of_the_whole_query(self):
        with pytest.raises(ValueError, match="unsupported regular expression"):
            parse_scryfall_query("t:creature name:/^(.)\\1\\1/")

    def test_an_escaped_backslash_before_a_digit_is_not_one(self):
        query = "o:/a\\\\1b/ t:elf"
        assert scryfall_term_policy(query).query == query

    def test_a_backreference_inside_a_nested_pattern_takes_that_rules_sentence(self):
        assert scryfall_term_policy("t:instant o:/(a)\\1\\1(((b)))/").warnings == [
            ignored("o:/(a)\\1\\1(((b)))/", "Too many nested groups."),
        ]


class TestTheParsersOwnRegexBudget:
    """A pattern Scryfall would run and the parser's budget will not is dropped, not fatal."""

    COMPLEX = "Regular expression too complex."

    def test_it_is_dropped_with_scryfalls_sentence_and_the_rest_of_the_query_answers(self):
        # Five lookarounds: 15 of Scryfall's 90 (152 instants there), one over the budget's four.
        query = "t:instant o:/(?=d)(?=de)(?=des)(?=dest)(?=destr)destroy target creature/"
        assert scryfall_regex_text_reason(query[len("t:instant o:/") : -1]) is None
        with pytest.raises(ValueError, match="unsupported regular expression"):
            parse_scryfall_query(query)
        result = scryfall_term_policy(query)
        assert result.query == "t:instant"
        assert result.warnings == [ignored("o:/(?=d)(?=de)(?=de\u2026", self.COMPLEX)]

    def test_four_lookarounds_run(self):
        query = "t:instant o:/(?=d)(?=de)(?=des)(?=dest)destroy target creature/"
        assert scryfall_term_policy(query).query == query

    @pytest.mark.parametrize("body", ["\\w" * 70, "a{2,}", "^" + "\u00e9" * 200])
    def test_the_other_three_places_the_budget_is_narrower(self, body):
        """More than 64 constructs, an open `{m,}`, and more than 256 UTF-8 bytes."""
        assert scryfall_regex_text_reason(body) is None
        result = scryfall_term_policy(f"t:instant o:/{body}/")
        assert result.query == "t:instant"
        assert result.warnings[0].endswith(self.COMPLEX)

    def test_a_metacharacter_free_pattern_never_reaches_the_budget(self):
        """It is lowered to a substring: 200 accented letters are 400 bytes and still run."""
        literal = f"t:instant o:/{'\u00e9' * 200}/"
        assert scryfall_term_policy(literal).query == literal
        parse_scryfall_query(literal)

    @pytest.mark.parametrize("body", ["\\w" * 70, "a{2,}", "(?=a)(?=b)(?=c)(?=d)(?=e)x", "^" + "\u00e9" * 200, "(a)\\1"])
    def test_no_regex_the_policy_keeps_is_one_the_parser_refuses(self, body):
        parse_scryfall_query(scryfall_term_policy(f"t:instant o:/{body}/").query)


class TestPostgresDialect:
    """Scryfall's regex dialect is PostgreSQL's: what its compiler refuses is ignored in its words.

    Measured on api.scryfall.com 2026-10-03; the requests are beside `_postgres_syntax_reason`.
    """

    QUANTIFIER = "Invalid regular expression: quantifier operand invalid."
    ESCAPE = "Invalid regular expression: invalid escape \\ sequence."

    @staticmethod
    def reasons_of(body: str) -> list[str]:
        marker = "was ignored. "
        warnings = scryfall_term_policy(f"o:/{body}/ t:instant").warnings
        return [warning[warning.index(marker) + len(marker) :] for warning in warnings]

    @pytest.mark.parametrize(
        "body",
        [
            "(?i)destroy target creature",
            "destroy(?i) target creature",
            "(?-i)destroy target creature",
            "(?i:destroy) target creature",
            "(?s:destroy) target creature",
            "(?<a>destroy) target creature",
            "(?P<a>destroy) target creature",
            "(?'n'destroy) target creature",
            "(?>destroy) target creature",
            "(?|destroy) target creature",
            "destroy++ target creature",
            "destroy*+ target creature",
            "destroy?+ target creature",
            "destroy+* target creature",
            "destroy{1}+ target creature",
            "destroy{1}{2} target creature",
            "destroy??? target creature",
            "^*destroy",
            "destroy$* target",
            "destroy\\y+ target creature",
            "destroy target creature\\b{2}",
            "(?=d)*destroy target creature",
            "destroy(*) target",
            "destroy |* target",
            "{2}a",
            "a|{2}",
            "(?#x)*a",
            "(?<x",
            "(a++",
            "a++[",
        ],
    )
    def test_quantifier_operand_invalid(self, body):
        assert self.reasons_of(body) == [self.QUANTIFIER]

    @pytest.mark.parametrize(
        "body",
        [
            "\\p{L}estroy target creature",
            "destroy\\htarget creature",
            "destroy target creature\\z",
            "destroy target creature\\Z",
            "destroy target creature\\k",
            "(o)\\g1",
            "destroy \\Qtarget\\E creature",
            "destro[\\p{L}] target creature",
            "\\x",
            "\\xg",
            "\\u12",
            "\\c",
            "\\p[",
        ],
    )
    def test_invalid_escape(self, body):
        assert self.reasons_of(body) == [self.ESCAPE]

    @pytest.mark.parametrize("letter", "abdefmnrstvwyABDEFMNRSTVWY")
    def test_every_letter_that_runs_after_a_backslash(self, letter):
        assert _postgres_syntax_reason(f"destroy target creature\\{letter}") is None

    @pytest.mark.parametrize("letter", "cghijklopquxzCGHIJKLOPQUXZ")
    def test_every_letter_that_does_not(self, letter):
        assert self.reasons_of(f"destroy target creature\\{letter}") == [self.ESCAPE]

    @pytest.mark.parametrize(
        "body",
        [
            "(?:destroy) target creature",
            "destroy target creature(?=\\.)",
            "(?<=destroy )target creature",
            "(?<!x(y))destroy target creature",
            "destroy target (?#comment)creature",
            "a(?#x)*",
            "destroy*? target creature",
            "destroy+? target creature",
            "destroy?? target creature",
            "destroy{1}? target creature",
            "destroy target creature{1,}?",
            "destroy target creature{1}",
            "destroy target creature{,2}",
            "{r}",
            "[[:alpha:]]estroy target creature",
            "destroy target [[:word:]]+",
            "destroy target [\\w]+",
            "destroy \\ytarget\\y creature",
            "destroy \\mtarget\\M creature",
            "destroy\\x20target creature",
            "destroy\\u0020target creature",
            "destroy \\cA?target creature",
            "destroy(|x) target creature",
            "(?:)*a",
            "()*a",
            "[+]+",
            "\\(this creature",
            "\\+1\\/\\+1",
        ],
    )
    def test_what_postgresql_runs(self, body):
        assert _postgres_syntax_reason(body) is None

    @pytest.mark.parametrize(
        "body",
        [
            "(?:destroy) target creature",
            "destroy target creature(?=\\.)",
            "(?<=destroy )target creature",
            "destroy target (?#comment)creature",
            "destroy*? target creature",
            "destroy target creature{1}",
            "destroy\\x20target creature",
            "destroy(|x) target creature",
            "[+]+",
            "\\(this creature",
        ],
    )
    def test_what_runs_is_kept_whole(self, body):
        query = f"o:/{body}/ t:instant"
        result = scryfall_term_policy(query)
        assert result.warnings == []
        assert result.query == query

    def test_the_first_error_left_to_right_is_the_one_reported(self):
        assert self.reasons_of("a)++") == ["Invalid regular expression: parentheses () not balanced."]
        assert self.reasons_of("[a++") == ["Invalid regular expression: brackets [] not balanced."]
        assert self.reasons_of("a{2,1}++") == ["Invalid regular expression: invalid repetition count(s)."]
        assert self.reasons_of("[\\p]") == [self.ESCAPE]
        assert self.reasons_of("a{51") == ["Invalid regular expression: braces {} not balanced."]
        assert self.reasons_of("a{256,}") == ["Invalid regular expression: invalid repetition count(s)."]
        # 255 is PostgreSQL's ceiling and runs there; the open bound is the parser's own budget.
        assert _postgres_syntax_reason("a{255,}") is None
        assert self.reasons_of("a{255,}") == ["Regular expression too complex."]

    def test_the_whole_warning_for_an_inline_flag(self):
        assert scryfall_term_policy("o:/(?i)destroy target creature/ t:instant").warnings == [
            ignored("o:/(?i)destroy targ\u2026", self.QUANTIFIER),
        ]


class TestDisplayOptions:
    """`unique:`, `order:`, `prefer:` and the rest are display options, not terms.

    Measured on api.scryfall.com 2026-10-03, `t:goblin` = 561.
    """

    @pytest.mark.parametrize(
        ("term", "warning"),
        [
            ("unique:nonsense", "Unknown unique mode \u201cnonsense\u201d was ignored"),
            ("order:nonsense", "Unknown order choice \u201cnonsense\u201d was ignored"),
            ("sort:nonsense", "Unknown order choice \u201cnonsense\u201d was ignored"),
            ("direction:nonsense", "Unknown direction choice \u201cnonsense\u201d was ignored"),
            ("dir:nonsense", "Unknown direction choice \u201cnonsense\u201d was ignored"),
            ("prefer:nonsense", "Unknown preference mode \u201cnonsense\u201d was ignored"),
            ("display:nonsense", "Unknown display mode \u201cnonsense\u201d was ignored"),
            ("as:nonsense", "Unknown display mode \u201cnonsense\u201d was ignored"),
            # Lower-cased, and cut to ten characters with the three ASCII dots included.
            ("unique:NONSENSE", "Unknown unique mode \u201cnonsense\u201d was ignored"),
            ("unique:abcdefghijklmnop", "Unknown unique mode \u201cabcdefg...\u201d was ignored"),
            ("prefer:abcdefghij", "Unknown preference mode \u201cabcdefghij\u201d was ignored"),
            # A quoted value is unknown, quotes echoed.
            ('unique:"prints"', 'Unknown unique mode \u201c"prints"\u201d was ignored'),
            ('order:"cmc"', 'Unknown order choice \u201c"cmc"\u201d was ignored'),
            # A `-` changes nothing.
            ("-unique:nonsense", "Unknown unique mode \u201cnonsense\u201d was ignored"),
        ],
    )
    def test_an_unknown_value_is_warned_about_in_scryfalls_sentence(self, term, warning):
        result = scryfall_term_policy(f"{term} t:goblin")
        assert result.query == "t:goblin"
        assert result.warnings == [warning]
        assert result.directives == []

    @pytest.mark.parametrize(
        ("term", "directive"),
        [
            ("unique:prints", ("unique", "prints", False)),
            ("unique:art", ("unique", "art", False)),
            ("UNIQUE:Prints", ("unique", "prints", False)),
            ("-unique:prints", ("unique", "prints", False)),
            ("order:cmc", ("order", "cmc", False)),
            ("sort:usd", ("sort", "usd", False)),
            ("direction:desc", ("direction", "desc", False)),
            ("dir:asc", ("dir", "asc", False)),
            ("prefer:oldest", ("prefer", "oldest", False)),
            ("prefer:usd-low", ("prefer", "usd-low", False)),
            # This project's own values stay honored where Scryfall warns.
            ("unique:artwork", ("unique", "artwork", False)),
            ("order:cubecobra", ("order", "cubecobra", False)),
        ],
    )
    def test_a_known_value_leaves_the_query_and_reaches_the_route(self, term, directive):
        result = scryfall_term_policy(f"{term} t:goblin")
        assert result.query == "t:goblin"
        assert result.warnings == []
        assert result.directives == [directive]

    def test_several_options_arrive_in_source_order(self):
        result = scryfall_term_policy("dir:desc t:goblin order:cmc unique:prints")
        assert result.query == "t:goblin"
        assert result.directives == [("dir", "desc", False), ("order", "cmc", False), ("unique", "prints", False)]

    @pytest.mark.parametrize("query", ["unique:prints", "order:cmc", "prefer:oldest", "display:grid", "unique:prints order:cmc"])
    def test_a_query_of_nothing_but_options_is_all_terms_ignored_with_no_warnings(self, query):
        result = scryfall_term_policy(query)
        assert result.all_ignored is True
        assert result.warnings == []

    def test_an_unknown_option_alone_carries_its_warning_into_the_400(self):
        result = scryfall_term_policy("unique:nonsense")
        assert result.all_ignored is True
        assert result.warnings == ["Unknown unique mode \u201cnonsense\u201d was ignored"]

    @pytest.mark.parametrize("keyword", ["unique", "order", "sort", "direction", "dir", "prefer", "display", "as", "include"])
    def test_only_a_colon_makes_one_an_equals_sign_is_an_unknown_keyword(self, keyword):
        """`unique=prints t:goblin` is 561 carrying the unknown-keyword sentence."""
        result = scryfall_term_policy(f"{keyword}=prints t:goblin")
        assert result.query == "t:goblin"
        assert result.directives == []
        assert result.warnings == [ignored(f"{keyword}=prints", f"Unknown keyword \u201c{keyword}\u201d.")]

    @pytest.mark.parametrize("mode", ["grid", "checklist", "full", "text", "images"])
    @pytest.mark.parametrize("keyword", ["display", "as"])
    def test_a_display_mode_is_accepted_silently(self, keyword, mode):
        result = scryfall_term_policy(f"{keyword}:{mode} t:goblin")
        assert result.query == "t:goblin"
        assert result.warnings == []

    @pytest.mark.parametrize("order", ["penny", "review"])
    def test_an_order_scryfall_sorts_by_and_this_server_cannot_keeps_the_parameters_sentence(self, order):
        result = scryfall_term_policy(f"order:{order} t:goblin")
        assert result.query == "t:goblin"
        assert result.directives == []
        assert result.warnings == [f"This server cannot sort by '{order}' yet; sorted by name instead."]

    @pytest.mark.parametrize(
        "query",
        [
            "(unique:prints t:goblin) cmc=0",
            "t:goblin cmc=0 (order:cmc or t:elf)",
            "(unique:nonsense t:goblin)",
            "-(prefer:oldest t:goblin)",
            "(-sort:cmc t:goblin)",
            "(display:grid t:goblin)",
        ],
    )
    def test_inside_parentheses_it_is_scryfalls_display_option_400(self, query):
        assert scryfall_term_policy(query).nested_display_option is True

    @pytest.mark.parametrize(
        "query", ['(o:"unique:prints")', "(sort: t:goblin)", "(sort=cmc t:goblin)", "unique:prints (t:goblin)"]
    )
    def test_what_is_not_a_nested_option(self, query):
        assert scryfall_term_policy(query).nested_display_option is False

    def test_a_dangling_option_is_a_bare_word(self):
        assert scryfall_term_policy("sort: t:goblin").query == "name:sort t:goblin"


class TestIncludeOption:
    """`include:` is Scryfall's in-query spelling of `include_extras` and its two siblings.

    Measured on api.scryfall.com 2026-10-03, base `cmc=3` = 8,089 (8,302 with `include_extras=true`),
    reading the three flags back out of `next_page`.
    """

    @staticmethod
    def flags(result) -> tuple[bool, bool, bool]:
        return result.include_extras, result.include_variations, result.include_multilingual

    def test_the_reported_query_parses_and_the_option_leaves_the_query(self):
        result = scryfall_term_policy("name:/^reset$/ include:extras")
        assert result.query == "name:/^reset$/"
        assert result.warnings == []
        assert self.flags(result) == (True, False, False)
        assert scryfall_term_policy("include:extras lightning").query == "lightning"

    @pytest.mark.parametrize(
        ("value", "flags"),
        [
            ("extras", (True, False, False)),
            ("extra", (True, False, False)),
            ("variations", (False, True, False)),
            ("variation", (False, True, False)),
            ("multilingual", (False, False, True)),
            ("all", (True, True, True)),
            ("everything", (True, True, True)),
            # Accepted, silently, and nothing observable moves.
            ("funny", (False, False, False)),
            ("digital", (False, False, False)),
            ("EXTRAS", (True, False, False)),
        ],
    )
    def test_each_value(self, value, flags):
        result = scryfall_term_policy(f"include:{value} cmc=3")
        assert result.query == "cmc=3"
        assert result.warnings == []
        assert self.flags(result) == flags

    @pytest.mark.parametrize(
        ("value", "echo"),
        [
            ("foo", "foo"),
            ("FOO", "foo"),
            ("foreign", "foreign"),
            ("tokens", "tokens"),
            ("any", "any"),
            ("1", "1"),
            # Quotes are part of the value: `include:"extras"` is not `include:extras`.
            ('"extras"', '"extras"'),
            # Ten characters, three ASCII dots included.
            ("extras,variations", "extras,..."),
            ("extrasx", "extrasx"),
        ],
    )
    def test_an_unknown_value_is_ignored_with_the_direction_sentence(self, value, echo):
        result = scryfall_term_policy(f"include:{value} cmc=3")
        assert result.query == "cmc=3"
        assert self.flags(result) == (False, False, False)
        assert result.warnings == [f"Unknown direction choice \u201c{echo}\u201d was ignored"]

    def test_a_minus_changes_nothing_and_several_options_add_up(self):
        assert scryfall_term_policy("-include:extras t:goblin cmc=0").include_extras is True
        assert scryfall_term_policy("-include:foo t:goblin").warnings == ["Unknown direction choice \u201cfoo\u201d was ignored"]
        both = scryfall_term_policy("include:extras include:variations cmc=3")
        assert both.query == "cmc=3"
        assert self.flags(both) == (True, True, False)
        mixed = scryfall_term_policy("t:goblin include:extras include:foo")
        assert mixed.include_extras is True
        assert len(mixed.warnings) == 1

    def test_it_is_removed_before_the_connectors_are_read(self):
        assert scryfall_term_policy("include:extras or t:goblin cmc=0").query == "t:goblin cmc=0"

    def test_only_under_a_colon(self):
        equals = scryfall_term_policy("include=extras t:goblin")
        assert equals.query == "t:goblin"
        assert self.flags(equals) == (False, False, False)
        assert equals.warnings == [ignored("include=extras", "Unknown keyword \u201cinclude\u201d.")]
        assert scryfall_term_policy("include>extras t:goblin").query == "cmc<0 t:goblin"

    def test_alone_it_is_the_400_with_no_warnings_an_option_is_not_a_term(self):
        alone = scryfall_term_policy("include:extras")
        assert alone.all_ignored is True
        assert alone.warnings == []
        with_ignored = scryfall_term_policy("include:extras f:notaformat")
        assert with_ignored.all_ignored is True
        assert with_ignored.warnings == [ignored("f:notaformat", "Unknown game format \u201cnotaformat\u201d")]

    @pytest.mark.parametrize("query", ["(include:extras t:goblin) cmc=0", "t:goblin cmc=0 (include:extras or t:elf)"])
    def test_inside_parentheses_it_is_scryfalls_display_option_400(self, query):
        assert scryfall_term_policy(query).nested_display_option is True

    def test_a_quoted_phrase_that_reads_like_one_is_text(self):
        result = scryfall_term_policy('(o:"include:extras")')
        assert result.nested_display_option is False
        assert result.include_extras is False


class TestKeywordsScryfallHonors:
    """A keyword Scryfall honors is never called unknown.

    Probed 2026-10-03 as `<keyword>:<value> e:khm t:god` (12 cards): each of these is HONORED on
    api.scryfall.com -- the count moves, or it is a plain 404 with no warnings -- and each was being
    dropped here with `Unknown keyword`, answering wider than Scryfall under a sentence that says
    Scryfall would have ignored it too. They are left for the parser to refuse instead.
    """

    @pytest.mark.parametrize(
        "term",
        [
            "block:khm",
            "b:khm",
            "edition:khm",
            "lore:x",
            "artists:1",
            "mtgoid:1",
            "multiverseid:1",
            "arenaid:1",
            "tcgplayerid:1",
            "prints:1",
            "sets:1",
            "paperprints:1",
            "papersets:1",
            "illustrations:1",
            "edhrec:1",
            "usdfoil:1",
            "collector:1",
            "collectornumber:1",
            # The ones that were already here.
            "cube:vintage",
            "new:art",
            "stamp:oval",
            "cheapest:usd",
        ],
    )
    def test_it_is_kept_unwarned_and_the_parser_refuses_the_query(self, term):
        query = f"{term} e:khm t:god"
        result = scryfall_term_policy(query)
        assert result.warnings == []
        assert result.query == query
        with pytest.raises(ValueError, match="Failed to"):
            parse_scryfall_query(result.query)

    @pytest.mark.parametrize(
        "term", ["fo:target", "fulloracle:target", "st:core", "settype:core", "set_type:core", "pt:6", "powtou:6"]
    )
    def test_a_keyword_whose_parser_column_is_on_another_branch_is_not_unknown_either(self, term):
        """Kept and unwarned; whether the parser answers it depends on which branches have landed."""
        query = f"{term} e:khm t:god"
        result = scryfall_term_policy(query)
        assert result.warnings == []
        assert result.query == query

    def test_under_a_comparison_they_are_honored_and_empty_as_before(self):
        assert scryfall_term_policy("edhrec>=5000 e:khm").query == "cmc<0 e:khm"

    def test_direct_is_the_opposite_case_scryfall_does_not_know_it(self):
        result = scryfall_term_policy("direct:x e:khm t:god")
        assert result.query == "e:khm t:god"
        assert result.warnings == [ignored("direct:x", "Unknown keyword \u201cdirect\u201d.")]

    @pytest.mark.parametrize("term", ["cardmarketid:1", "flavorname:x"])
    def test_two_that_really_are_unknown_there(self, term):
        assert len(scryfall_term_policy(f"{term} e:khm t:god").warnings) == 1


class TestPrintingIdKeywords:
    """`scryfallid:` and `illustrationid:` take the v4-UUID check `oracleid:` takes.

    Measured on api.scryfall.com 2026-10-03, anchor `e:khm t:god` = 12.
    """

    UUID_REASON = "You must provide a valid v4 UUID."
    SCRYFALL_ID = "860aa0fe-0337-458c-b864-5ef5733fbae6"
    ILLUSTRATION_ID = "9e42d409-161d-4e63-8982-71e313f27b2f"

    @pytest.mark.parametrize(
        ("term", "echo"),
        [
            ("scryfallid:abc", "scryfallid:abc"),
            ("scryfall_id:abc", "scryfall_id:abc"),
            ("illustrationid:abc", "illustrationid:abc"),
            ("illustration_id:abc", "illustration_id:abc"),
            ("-scryfallid:abc", "-scryfallid:abc"),
            # The nil UUID is not a v4 one, and neither is a v4 one without its hyphens.
            ("scryfallid:00000000-0000-0000-0000-000000000000", "scryfallid:00000000\u2026"),
            ("scryfallid:860aa0fe0337458cb8645ef5733fbae6", "scryfallid:860aa0fe\u2026"),
        ],
    )
    def test_a_value_that_is_not_a_v4_uuid_is_ignored_with_scryfalls_sentence(self, term, echo):
        result = scryfall_term_policy(f"{term} e:khm t:god")
        assert result.query == "e:khm t:god"
        assert result.warnings == [ignored(echo, self.UUID_REASON)]

    def test_alone_it_is_the_400_carrying_the_sentence(self):
        result = scryfall_term_policy("scryfallid:00000000-0000-0000-0000-000000000000")
        assert result.all_ignored is True
        assert result.warnings == [ignored("scryfallid:00000000\u2026", self.UUID_REASON)]

    @pytest.mark.parametrize(
        "term",
        [
            f"scryfallid:{SCRYFALL_ID}",
            f"scryfall_id:{SCRYFALL_ID}",
            f"scryfallid={SCRYFALL_ID}",
            f"SCRYFALLID:{SCRYFALL_ID.upper()}",
            f'scryfallid:"{SCRYFALL_ID}"',
            f"illustrationid:{ILLUSTRATION_ID}",
            f"illustration_id:{ILLUSTRATION_ID}",
            f"illustrationid={ILLUSTRATION_ID}",
            # Well-formed and names nothing: Scryfall's plain 404, not an ignored term.
            "scryfallid:11111111-1111-4111-8111-111111111111",
        ],
    )
    def test_a_well_formed_id_is_never_called_an_unknown_keyword(self, term):
        query = f"{term} e:khm"
        result = scryfall_term_policy(query)
        assert result.warnings == []
        assert result.query == query

    @pytest.mark.parametrize("operator", ["!=", ">", ">=", "<", "<="])
    def test_a_comparison_is_honored_and_empty(self, operator):
        result = scryfall_term_policy(f"scryfallid{operator}{self.SCRYFALL_ID} e:khm t:god")
        assert result.warnings == []
        assert result.query == "cmc<0 e:khm t:god"
