"""Tests for date and year search functionality."""

import itertools

import pytest

from api.parsing import AttributeNode, BinaryOperatorNode, QueryContext, StringValueNode


@pytest.mark.parametrize(
    argnames=("searchattr", "searchoperator", "searchvalue"),
    argvalues=list(
        itertools.product(
            ["date"],
            [":", "=", ">", "<", ">=", "<="],
            ["2025-02-02", "2025"],
        ),
    )
    + list(
        itertools.product(
            ["year"],
            [":", "=", ">", "<", ">=", "<="],
            ["2025"],  # Year only accepts 4-digit years
        ),
    ),
)
def test_date_year_search_parsing(parse_query, searchattr: str, searchoperator: str, searchvalue: str) -> None:
    """Test that date and year searches parse correctly with all operators."""
    query_str = f"{searchattr}{searchoperator}{searchvalue}"
    parsed = parse_query(query_str)

    # Should parse to a BinaryOperatorNode
    assert isinstance(parsed.root, BinaryOperatorNode)
    assert isinstance(parsed.root.lhs, AttributeNode)
    assert parsed.root.operator == searchoperator

    # RHS should be a StringValueNode with the search value
    assert isinstance(parsed.root.rhs, StringValueNode)
    assert parsed.root.rhs.value == searchvalue


@pytest.mark.parametrize(
    argnames=("query", "expected_sql_fragment"),
    argvalues=[
        # Date searches should use the full date
        ("date:2025-02-02", "card.released_at = "),
        ("date=2025-02-02", "card.released_at = "),
        ("date>2025-02-02", "card.released_at > "),
        ("date<2025-02-02", "card.released_at < "),
        ("date>=2025-02-02", "card.released_at >= "),
        ("date<=2025-02-02", "card.released_at <= "),
        # Year searches should use date ranges for index usage
        ("year:2025", "<= card.released_at AND card.released_at <"),
        ("year=2025", "<= card.released_at AND card.released_at <"),
        ("year>2025", "card.released_at >="),
        ("year<2025", "card.released_at <"),
        ("year>=2025", "card.released_at >="),
        ("year<=2025", "card.released_at <"),
    ],
)
def test_date_year_sql_generation(parse_query, query: str, expected_sql_fragment: str) -> None:
    """Test that date and year searches generate correct SQL."""
    parsed = parse_query(query)
    context = QueryContext()
    sql = parsed.to_sql(context)

    # Check that the SQL contains the expected fragment
    assert expected_sql_fragment in sql
    # Check that parameters were added to context (1 for date, 1 or 2 for year)
    assert len(context) >= 1


def test_date_search_full_date(parse_query) -> None:
    """Test date search with full date format."""
    parsed = parse_query("date:2025-02-02")
    context = QueryContext()
    sql = parsed.to_sql(context)

    assert "card.released_at = " in sql
    # Should have a parameter with the date string
    assert "2025-02-02" in context.values()


def test_year_search_numeric(parse_query) -> None:
    """Test year search with numeric year."""
    parsed = parse_query("year:2025")
    context = QueryContext()
    sql = parsed.to_sql(context)

    # Year search should convert to date range: 2025-01-01 <= released_at < 2026-01-01
    assert "card.released_at" in sql
    assert "<= card.released_at AND card.released_at <" in sql
    # Should have parameters with date strings
    assert "2025-01-01" in context.values()
    assert "2026-01-01" in context.values()


def test_year_search_rejects_date_format(parse_query) -> None:
    """Test year search rejects date format (YYYY-MM-DD)."""
    # Year search should only accept 4-digit years
    # Parsing with date format should fail
    with pytest.raises(ValueError, match="Failed to parse query"):
        parse_query("year:2025-02-02")


def test_date_year_combined_query(parse_query) -> None:
    """Test combining date/year searches with other conditions."""
    parsed = parse_query("year:2025 AND cmc=3")
    context = QueryContext()
    sql = parsed.to_sql(context)

    assert "card.released_at" in sql
    assert "card.cmc = " in sql
    assert "2025-01-01" in context.values()
    assert 3 in context.values()


# ── Value validation ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    argnames=("query", "expected_value"),
    argvalues=[
        ("year<1991", "1991"),
        ("year<=1991", "1991"),
        ("date<1993", "1993"),
        ("date>=1990-01-01", "1990-01-01"),
        ("year>2100", "2100"),
        ("year>=1000", "1000"),
        ("year<9999", "9999"),
    ],
)
def test_comparisons_accept_any_four_digit_year(parse_query, query: str, expected_value: str) -> None:
    """`year<1991` is "before Magic existed" -- meaningful, and it used to be rejected outright."""
    parsed = parse_query(query)
    assert isinstance(parsed.root.rhs, StringValueNode)
    assert parsed.root.rhs.value == expected_value
    assert parsed.to_sql(QueryContext())


@pytest.mark.parametrize(
    argnames="query",
    argvalues=["year:1500", "year=1991", "date:2099", "date=2099-01-01", "year!2050", "date:1000-01-01"],
)
def test_equality_keeps_the_magic_era_gate(parse_query, query: str) -> None:
    """`year:1500` cannot match a printing, so `=` and `:` still get the sanity check."""
    with pytest.raises(ValueError, match=r"Failed to parse query|Year must be between"):
        parse_query(query)


@pytest.mark.parametrize(argnames="query", argvalues=["year>99999", "year<999", "date<10000", "year>=2020.5"])
def test_absurd_years_are_rejected_under_any_operator(parse_query, query: str) -> None:
    """A comparison lets any *year* through, and five digits or a float is not a year."""
    with pytest.raises(ValueError, match=r"Failed to parse query|four-digit year|integer year"):
        parse_query(query)


@pytest.mark.parametrize(
    argnames="query",
    argvalues=["date:2020-01", "date>=2020-01", "date:2020-1.5-01", "date:2020-01-1.5", "date:2020-02-30", "date:2020-13-01"],
    ids=["month_no_day", "month_no_day_comparison", "float_month", "float_day", "feb_30", "month_13"],
)
def test_partial_or_impossible_dates_are_errors(parse_query, query: str) -> None:
    """`date:2020-01` used to consume the month and then search for the bare year, silently."""
    with pytest.raises(ValueError, match=r"Failed to parse query|full date|integer month|Invalid date"):
        parse_query(query)
