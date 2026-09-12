"""Tests for date and year search functionality."""

import itertools

import pytest

from api.parsing import AttributeNode, BinaryOperatorNode, QueryContext, StringValueNode, parse_scryfall_query


@pytest.mark.parametrize(
    argnames=("searchattr", "searchoperator", "searchvalue"),
    argvalues=list(
        itertools.product(
            ["date"],
            [":", "=", ">", "<", ">=", "<="],
            # All three precisions. `2025-02` was missing here, which is why the hand parser
            # could eat the month and hand back the bare year without a test moving.
            ["2025-02-02", "2025-02", "2025"],
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


# A YEAR-MONTH IS THAT MONTH, not the year around it. The hand parser consumed the MINUS and the
# month looking for a day, and when no day followed it returned the bare year -- dropping the two
# tokens it had already eaten, silently -- while the pyparsing grammar had no year-month shape at
# all and rejected the query outright. Measured on api.scryfall.com 2026-08-16: `date:2021-02` is
# 504 there and `date:2021` is 3,834, which is what this answered.
#
# The oracle is the bare-year window's, one precision down: a partial date must equal the range its
# own ends describe. `date>=2021-02 date<2021-03` is 504 on Scryfall as well, and the identity also
# holds for 2021-07 (593) and for 2021-12 (130), which crosses the year boundary.
@pytest.mark.parametrize(
    argnames=("query", "start", "after_end"),
    argvalues=[
        ("date:2021-02", "2021-02-01", "2021-03-01"),
        ("date=2021-02", "2021-02-01", "2021-03-01"),
        # December, whose window ends in the NEXT year -- the arm a mid-year month cannot exercise.
        ("date:2021-12", "2021-12-01", "2022-01-01"),
    ],
)
def test_year_month_is_its_own_month(parse_query, query: str, start: str, after_end: str) -> None:
    """A `date:YYYY-MM` covers exactly that month, on both parsers."""
    parsed = parse_query(query)
    context = QueryContext()
    sql = parsed.to_sql(context)

    assert "<= card.released_at AND card.released_at <" in sql
    assert start in context.values()
    assert after_end in context.values()


@pytest.mark.parametrize(
    argnames=("query", "expected_sql_fragment", "bound"),
    argvalues=[
        # `>=` and `<` read the START of the month; `>` and `<=` read the end of it.
        ("date>=2021-02", "card.released_at >= ", "2021-02-01"),
        ("date<2021-02", "card.released_at < ", "2021-02-01"),
        ("date>2021-02", "card.released_at >= ", "2021-03-01"),
        ("date<=2021-02", "card.released_at < ", "2021-03-01"),
    ],
)
def test_year_month_comparisons_read_the_end_they_need(parse_query, query: str, expected_sql_fragment: str, bound: str) -> None:
    """Each ordered comparison against a year-month reads its own end of that month's window."""
    parsed = parse_query(query)
    context = QueryContext()
    sql = parsed.to_sql(context)

    assert expected_sql_fragment in sql
    assert bound in context.values()


@pytest.mark.parametrize(argnames="query", argvalues=["date:2021-13", "date:2021-00"])
def test_year_month_rejects_an_impossible_month(query: str) -> None:
    """A month outside 1..12 raises, exactly as an impossible DAY already does.

    Scryfall 400s on `date:2021-13`, where this answered the whole of 2021.

    The hand parser only: the pyparsing grammar accepts calendar-impossible dates at EVERY
    precision already (`date:2021-13-05` and `date:2021-02-31` both parse there and are rejected
    here), so requiring it of the month alone would assert a rule that file does not follow.
    """
    with pytest.raises(ValueError, match="Failed to parse query"):
        parse_scryfall_query(query)


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
