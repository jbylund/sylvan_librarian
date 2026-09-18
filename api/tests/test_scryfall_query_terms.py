"""Scryfall's ignore-and-continue query policy, term by term.

Every expectation here is a MEASUREMENT against api.scryfall.com on 2026-08-16, not a design: the
warning sentences, the 20-character expression echo, which characters fold, which keywords Scryfall
does not know and which of its own it refuses to negate were all read off live responses. See
`api/scryfall_compat/query_terms.py` for the request that produced each.
"""

from __future__ import annotations

import pytest

from api.scryfall_compat.query_terms import fold_smart_quotes, scryfall_term_policy


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
    """Onigmo's sentences, read off live responses rather than translated."""

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
