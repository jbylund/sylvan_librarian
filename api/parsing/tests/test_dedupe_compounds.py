"""Tests for post-budget flatten+dedupe compound normalization."""

from __future__ import annotations

import pytest

from api.parsing import generate_sql_query, parse_scryfall_query
from api.parsing.query_budget import QueryBudgetExceeded
from api.parsing.regex_budget import MAX_REGEX_LEAVES_PER_QUERY


@pytest.mark.parametrize(
    argnames=["query", "canonical_query"],
    argvalues=[
        ("color=g color=g", "color=g"),
        ("color=g (color=g color=g)", "color=g"),
        ("cmc<2 c=w cmc<2 color=w", "cmc<2 c=w"),
        ("(cmc<2 c=w) (color=w cmc<2)", "cmc<2 c=w"),
        ("t:creature or t:instant or t:creature", "t:creature or t:instant"),
        ("is:dual is:dual", "is:dual"),
        (
            "t:instant or (cmc<2 c=w) or (c=w cmc<2)",
            "t:instant or (cmc<2 c=w)",
        ),
    ],
    ids=[
        "duplicate_leaf",
        "nested_duplicate_and",
        "duplicate_mixed_aliases",
        "order_insensitive_and_group",
        "duplicate_or_disjunct",
        "duplicate_derived_predicate",
        "order_insensitive_and_under_or",
    ],
)
def test_deduplicate_compound_operands(parse_query, query: str, canonical_query: str) -> None:
    """Duplicate AND/OR operands normalize to the same AST and SQL as the minimal form."""
    assert parse_query(query) == parse_query(canonical_query)
    assert generate_sql_query(parse_query(query)) == generate_sql_query(parse_query(canonical_query))


def test_regex_budget_counts_duplicates_before_dedupe() -> None:
    """Identical regex leaves still hit the leaf limit even though dedupe would collapse them."""
    query = " ".join("o:/(?=draw)/" for _ in range(MAX_REGEX_LEAVES_PER_QUERY + 1))
    with pytest.raises(QueryBudgetExceeded) as exc_info:
        parse_scryfall_query(query)
    assert exc_info.value.kind == "regex_leaves"
