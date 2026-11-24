"""Integration tests for legality search functionality."""

import pytest

from api.parsing import generate_sql_query, parse_scryfall_query


def legality_params(format_name: str, status: str) -> list[dict[str, str]]:
    """The parameters a legality term binds: `f:`/`format:`/`legal:` also match restricted, as on Scryfall."""
    if status == "legal":
        return [{format_name: "legal"}, {format_name: "restricted"}]
    return [{format_name: status}]


class TestLegalityIntegration:
    """Test legality search functionality with end-to-end integration."""

    def test_format_search_integration(self) -> None:
        """Test that format search generates correct SQL end-to-end."""
        query = "format:standard"
        parsed = parse_scryfall_query(query)
        sql, params = generate_sql_query(parsed)

        # Should generate JSONB containment query
        assert "card.card_legalities @>" in sql
        # Parameters should be the format's legal and restricted mappings
        assert list(params.values()) == legality_params("standard", "legal")

    def test_banned_search_integration(self) -> None:
        """Test that banned search generates correct SQL end-to-end."""
        query = "banned:modern"
        parsed = parse_scryfall_query(query)
        sql, params = generate_sql_query(parsed)

        # Should generate JSONB containment query
        assert "card.card_legalities @>" in sql
        # Parameter should contain format -> banned mapping
        assert list(params.values()) == legality_params("modern", "banned")

    def test_restricted_search_integration(self) -> None:
        """Test that restricted search generates correct SQL end-to-end."""
        query = "restricted:vintage"
        parsed = parse_scryfall_query(query)
        sql, params = generate_sql_query(parsed)

        # Should generate JSONB containment query
        assert "card.card_legalities @>" in sql
        # Parameter should contain format -> restricted mapping
        assert list(params.values()) == legality_params("vintage", "restricted")

    def test_format_alias_integration(self) -> None:
        """Test that format alias 'f:' works correctly."""
        query = "f:commander"
        parsed = parse_scryfall_query(query)
        sql, params = generate_sql_query(parsed)

        # Should generate JSONB containment query
        assert "card.card_legalities @>" in sql
        # Parameters should be the format's legal and restricted mappings
        assert list(params.values()) == legality_params("commander", "legal")

    def test_legal_explicit_integration(self) -> None:
        """Test that explicit 'legal:' search works correctly."""
        query = "legal:legacy"
        parsed = parse_scryfall_query(query)
        sql, params = generate_sql_query(parsed)

        # Should generate JSONB containment query
        assert "card.card_legalities @>" in sql
        # Parameters should be the format's legal and restricted mappings
        assert list(params.values()) == legality_params("legacy", "legal")

    def test_complex_legality_query_integration(self) -> None:
        """Test complex queries combining legality searches."""
        query = "format:standard and banned:modern"
        parsed = parse_scryfall_query(query)
        sql, params = generate_sql_query(parsed)

        # Should generate AND query with two JSONB containment clauses
        assert "AND" in sql
        sql_parts = sql.split(" AND ")
        assert len(sql_parts) == 2
        assert all("card.card_legalities @>" in part for part in sql_parts)

        # `format:standard` binds legal and restricted, `banned:modern` binds banned
        assert list(params.values()) == legality_params("standard", "legal") + legality_params("modern", "banned")

    def test_case_insensitive_format_integration(self) -> None:
        """Test that format names are case-insensitive."""
        queries = ["format:Standard", "format:MODERN", "banned:Legacy"]
        expected_formats = ["standard", "modern", "legacy"]
        expected_statuses = ["legal", "legal", "banned"]

        for query, expected_format, expected_status in zip(queries, expected_formats, expected_statuses, strict=False):
            parsed = parse_scryfall_query(query)
            sql, params = generate_sql_query(parsed)

            # Should generate JSONB containment query
            assert "card.card_legalities @>" in sql
            # Parameter should contain lowercase format name
            assert list(params.values()) == legality_params(expected_format, expected_status)

    def test_quoted_format_names_integration(self) -> None:
        """Test that quoted format names with spaces work correctly."""
        query = 'format:"Historic Brawl"'
        parsed = parse_scryfall_query(query)
        sql, params = generate_sql_query(parsed)

        # Should generate JSONB containment query
        assert "card.card_legalities @>" in sql
        # Parameter should contain the format with spaces
        assert list(params.values()) == legality_params("historic brawl", "legal")

    @pytest.mark.parametrize(
        ("query", "expected_format", "expected_status"),
        [
            ("format:pioneer", "pioneer", "legal"),
            ("f:modern", "modern", "legal"),
            ("legal:pauper", "pauper", "legal"),
            ("banned:extended", "extended", "banned"),
            ("restricted:vintage", "vintage", "restricted"),
        ],
    )
    def test_various_formats_integration(self, query: str, expected_format: str, expected_status: str) -> None:
        """Test various format names and search types."""
        parsed = parse_scryfall_query(query)
        sql, params = generate_sql_query(parsed)

        # Should generate JSONB containment query
        assert "card.card_legalities @>" in sql
        # Parameter should contain expected format and status
        assert list(params.values()) == legality_params(expected_format, expected_status)
