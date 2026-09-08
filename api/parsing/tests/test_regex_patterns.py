"""Tests for regex pattern search functionality."""

import pytest

from api.parsing import (
    AndNode,
    AttributeNode,
    BinaryOperatorNode,
    RegexValueNode,
    generate_sql_query,
)
from api.parsing.db_info import FACE_JOINED_TEXT_COLUMNS, FACE_TEXT_SEPARATOR


class TestRegexPatternParsing:
    """Test regex pattern parsing with forward-slash delimiters."""

    @pytest.mark.parametrize(
        ("query", "expected_pattern"),
        [
            ("name:/izz.t/", "izz.t"),
            ("o:/^{T}:/", "^{T}:"),
            (r"name:/\bizzet\b/", r"\bizzet\b"),
            ("o:/exile|destroy/", "exile|destroy"),
            (r"o:/\spp/", r"\spp"),
            ("flavor:/.*flavor.*/", ".*flavor.*"),
            ("t:/creature|instant/", "creature|instant"),
        ],
    )
    def test_parse_regex_patterns(self, parse_query, query: str, expected_pattern: str) -> None:
        """Test that regex patterns are parsed correctly."""
        result = parse_query(query)
        assert isinstance(result.root, BinaryOperatorNode)
        assert isinstance(result.root.rhs, RegexValueNode)
        assert result.root.rhs.value == expected_pattern

    def test_parse_regex_with_escaped_forward_slash(self, parse_query) -> None:
        """Test that escaped forward slashes are handled correctly in regex patterns."""
        # Test pattern with escaped forward slash: /a\/b.*/  (the `.*` keeps it a genuine regex, so
        # the escaped-delimiter handling is what's under test, not the literal-lowering pass).
        query = "name:/a\\/b.*/"
        result = parse_query(query)
        assert isinstance(result.root, BinaryOperatorNode)
        assert isinstance(result.root.rhs, RegexValueNode)
        # The escaped forward slash should be preserved
        assert result.root.rhs.value == "a/b.*"

    @pytest.mark.parametrize(
        argnames=["query", "expected_pattern"],
        argvalues=[
            ("power/2>1 name:/a.*/", "a.*"),
            ("cmc/2>1 o:/fly.*/", "fly.*"),
            ("(power+1)/2>1 flavor:/gob.*/", "gob.*"),
        ],
        ids=["division_then_name_regex", "division_then_oracle_regex", "paren_division_then_flavor_regex"],
    )
    def test_division_before_a_regex(self, parse_query, query: str, expected_pattern: str) -> None:
        """Arithmetic division and a regex coexist: the '/' only opens a regex in value position.

        The scan used to run greedily from any '/', so the division slash swallowed everything up
        to the regex's opening slash and neither half parsed (#908).
        """
        sql, params = generate_sql_query(parse_query(query))

        assert " / " in sql, f"division did not survive in {sql}"
        assert "~*" in sql, f"regex did not survive as a regex in {sql}"
        assert expected_pattern in params.values()

    @pytest.mark.parametrize(
        argnames=["query"],
        argvalues=[("/bolt/",), ("/foo/ /bar/",), ("(t:elf) /foo/",)],
        ids=["bare_regex", "two_bare_regexes", "bare_regex_after_group"],
    )
    def test_bare_regex_is_not_a_supported_shape(self, parse_query, query: str) -> None:
        """A regex only opens in value position, so a bare one is rejected — as it was before #908.

        Scryfall does not treat these as regexes either: it strips the slashes and runs a plain
        name search, so /bolt/ == bolt == name:bolt (41 cards) while the real regex form
        name:/bolt/ gives 48, and /^Lightning/ matches nothing because there is no anchoring to
        apply. Giving the bare form regex semantics would be an extension beyond Scryfall, not
        parity with it, so it stays unsupported.
        """
        with pytest.raises(ValueError, match=r"(Failed to parse query|Unmatched)"):
            parse_query(query)

    def test_combined_regex_and_regular_search(self, parse_query) -> None:
        """Test combining regex searches with regular text searches."""
        query = "t:creature o:/^{T}:/"
        result = parse_query(query)
        assert isinstance(result.root, AndNode)
        assert len(result.root.operands) == 2

        # First operand should be type search (not regex)
        first = result.root.operands[0]
        assert isinstance(first, BinaryOperatorNode)
        assert isinstance(first.lhs, AttributeNode)

        # Second operand should be oracle text search with regex
        second = result.root.operands[1]
        assert isinstance(second, BinaryOperatorNode)
        assert isinstance(second.rhs, RegexValueNode)
        assert second.rhs.value == "^{T}:"


class TestRegexSQLGeneration:
    """Test SQL generation for regex patterns."""

    @pytest.mark.parametrize(
        ("query", "expected_operator", "expected_pattern", "face_joined"),
        [
            ("name:/izz.t/", "~*", "izz.t", False),
            ("o:/^{T}:/", "~*", "^{T}:", True),
            (r"name:/\bizzet\b/", "~*", r"\bizzet\b", False),
            ("flavor:/.*flavor.*/", "~*", ".*flavor.*", True),
        ],
    )
    def test_regex_sql_generation(
        self, parse_query, query: str, expected_operator: str, expected_pattern: str, face_joined: bool
    ) -> None:
        """Test that regex patterns generate correct PostgreSQL regex SQL.

        A face-joined column carries a second parameter, the separator its stored value is split
        back apart on before the pattern runs; a column that is one face's text carries only the
        pattern.
        """
        result = parse_query(query)
        sql, params = generate_sql_query(result)

        # Should use PostgreSQL case-insensitive regex operator
        assert expected_operator in sql
        # Should not use ILIKE operator
        assert "ILIKE" not in sql

        assert expected_pattern in params.values()
        assert (FACE_TEXT_SEPARATOR in params.values()) is face_joined
        assert len(params) == (2 if face_joined else 1)

    def test_regex_on_name_attribute(self, parse_query) -> None:
        """Test regex on name attribute generates correct SQL."""
        result = parse_query(r"name:/\bizzet\b/")
        sql, params = generate_sql_query(result)

        assert "card.card_name ~*" in sql
        assert len(params) == 1
        assert r"\bizzet\b" in params.values()

    def test_regex_on_oracle_attribute(self, parse_query) -> None:
        r"""Test regex on oracle text attribute generates correct SQL.

        The pattern runs against each FACE, not against the joined column: oracle text is stored
        as a multi-face card's faces glued with a separator this project invented, and matching the
        join answers from characters no card carries (`o:/\/\//` was 849 against Scryfall's 1).
        """
        result = parse_query("o:/^{T}:/")
        sql, params = generate_sql_query(result)

        assert "string_to_array(card.oracle_text" in sql
        assert "face_text ~*" in sql
        assert "card.oracle_text ~*" not in sql, "the joined value must not be the haystack"
        assert set(params.values()) == {"^{T}:", FACE_TEXT_SEPARATOR}

    def test_regex_on_flavor_attribute(self, parse_query) -> None:
        """Test regex on flavor text attribute generates correct SQL -- joined, so split."""
        result = parse_query("flavor:/mag.c/")
        sql, params = generate_sql_query(result)

        assert "string_to_array(card.flavor_text" in sql
        assert "face_text ~*" in sql
        assert set(params.values()) == {"mag.c", FACE_TEXT_SEPARATOR}

    def test_regex_on_a_face_joined_column_stays_three_valued(self, parse_query) -> None:
        """A NULL column must stay NULL, not become False.

        EXISTS is two-valued, and `flavor_text` is nullable: without the CASE guard, `-flavor:/x/`
        would start matching every printing that has no flavor text at all -- rows the unsplit
        `card.flavor_text ~* p` left as NULL, and rows the engine still evaluates to Null.
        """
        sql, _ = generate_sql_query(parse_query("flavor:/mag.c/"))
        assert "CASE WHEN card.flavor_text IS NULL THEN NULL ELSE EXISTS" in sql

    def test_combined_regex_and_text_search_sql(self, parse_query) -> None:
        """Test SQL generation for combined regex and text searches."""
        result = parse_query("t:creature o:/^{T}:/")
        sql, params = generate_sql_query(result)

        # Should contain both the type search and regex oracle search
        assert "card.card_types" in sql
        assert "face_text ~*" in sql

        # The type value, the pattern, and the face separator
        assert len(params) == 3

    def test_regular_text_search_uses_lower_like(self, parse_query) -> None:
        """Test that regular text searches use lower() LIKE, not regex."""
        result = parse_query("name:lightning")
        sql, params = generate_sql_query(result)

        # Should use lower() LIKE pattern matching, not regex
        assert "lower(" in sql
        assert "LIKE" in sql
        assert "ILIKE" not in sql
        assert "~*" not in sql

        # Should have wildcards in the parameter
        param_value = next(iter(params.values()))
        assert "%" in param_value


class TestRegexPatternFeatures:
    """Test specific regex features mentioned in Scryfall docs."""

    def test_anchors_start_and_end(self, parse_query) -> None:
        """Test start (^) and end ($) anchors in regex patterns."""
        # Start anchor
        result = parse_query("o:/^{T}:/")
        assert isinstance(result.root.rhs, RegexValueNode)
        assert result.root.rhs.value == "^{T}:"

        # End anchor
        result = parse_query("o:/draw a card$/")
        assert isinstance(result.root.rhs, RegexValueNode)
        assert result.root.rhs.value == "draw a card$"

    def test_alternation_groups(self, parse_query) -> None:
        """Test alternation groups (a|b) in regex patterns."""
        result = parse_query("o:/exile|destroy/")
        assert isinstance(result.root.rhs, RegexValueNode)
        assert result.root.rhs.value == "exile|destroy"

    def test_character_classes(self, parse_query) -> None:
        r"""Test character classes like \d, \w, \s in regex patterns."""
        # Whitespace character class
        result = parse_query(r"o:/\spp/")
        assert isinstance(result.root.rhs, RegexValueNode)
        assert result.root.rhs.value == r"\spp"

        # Word character class
        result = parse_query(r"o:/\w+/")
        assert isinstance(result.root.rhs, RegexValueNode)
        assert result.root.rhs.value == r"\w+"

        # Digit character class
        result = parse_query(r"o:/\d+/")
        assert isinstance(result.root.rhs, RegexValueNode)
        assert result.root.rhs.value == r"\d+"

    def test_word_boundaries(self, parse_query) -> None:
        r"""Test word boundary anchors (\b) in regex patterns."""
        result = parse_query(r"name:/\bizzet\b/")
        assert isinstance(result.root.rhs, RegexValueNode)
        assert result.root.rhs.value == r"\bizzet\b"

    def test_brackets_and_quantifiers(self, parse_query) -> None:
        """Test brackets [ab] and quantifiers .*?, +, * in regex patterns."""
        # Brackets
        result = parse_query("o:/[Tt]ap/")
        assert isinstance(result.root.rhs, RegexValueNode)
        assert result.root.rhs.value == "[Tt]ap"

        # Quantifiers
        result = parse_query("o:/.*?draw.*?/")
        assert isinstance(result.root.rhs, RegexValueNode)
        assert result.root.rhs.value == ".*?draw.*?"

    def test_lookahead_assertions(self, parse_query) -> None:
        """Test lookahead assertions (?!) in regex patterns."""
        result = parse_query("o:/(?!non)/")
        assert isinstance(result.root.rhs, RegexValueNode)
        assert result.root.rhs.value == "(?!non)"


class TestRegexSupportedAttributes:
    """Test that regex patterns work with all supported attributes per Scryfall docs."""

    @pytest.mark.parametrize(
        ("query", "expected_attribute"),
        [
            ("name:/te.t/", "card_name"),
            ("oracle:/te.t/", "oracle_text"),
            ("o:/te.t/", "oracle_text"),
            ("flavor:/te.t/", "flavor_text"),
        ],
    )
    def test_regex_supported_on_text_attributes(self, parse_query, query: str, expected_attribute: str) -> None:
        """Test that regex patterns are supported on all documented text attributes.

        Note: type: and t: attributes are JSONB arrays and regex support for them
        would require array element matching, which is not yet implemented.
        The alias ft: is not currently defined (only 'flavor' is).
        """
        result = parse_query(query)
        sql, params = generate_sql_query(result)

        # Should contain the expected attribute in SQL
        assert expected_attribute in sql
        # Should use regex operator
        assert "~*" in sql
        # The pattern, plus the face separator on the two columns that join faces
        assert "te.t" in params.values()
        assert len(params) == (2 if expected_attribute in FACE_JOINED_TEXT_COLUMNS else 1)
