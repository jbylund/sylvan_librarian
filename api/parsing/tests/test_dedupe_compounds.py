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


# Leaves that differ only by prefix used to compare equal (CardAttributeNode compared the DB column
# alone), so dedup dropped one of them and the query silently widened.
@pytest.mark.parametrize(
    argnames=["query", "operand_count"],
    argvalues=[
        ("restricted:vintage legal:vintage", 2),
        ("f:modern banned:modern", 2),
        ("date:2020 year:2020", 2),
        ("is:promo not:promo", 2),
        ("t:elf t:elf", 1),
        ("f:modern legal:modern format:modern", 1),
        ("c=w color=w", 1),
    ],
    ids=[
        "restricted_vs_legal",
        "legal_vs_banned",
        "date_vs_year",
        "is_vs_not",
        "true_duplicate",
        "legality_synonyms",
        "color_synonyms",
    ],
)
def test_dedup_only_merges_leaves_that_mean_the_same_thing(parse_query, query: str, operand_count: int) -> None:
    """Two leaves dedupe only when they resolve to the same field AND ask the same question of it."""
    root = parse_query(query).root
    observed = len(root.operands) if hasattr(root, "operands") else 1
    assert observed == operand_count, f"{query!r} normalized to {root!r}"


def test_prefix_sensitive_leaves_both_reach_the_sql() -> None:
    """The dropped operand was a real filter: both legality statuses must survive to the SQL."""
    sql, params = generate_sql_query(parse_scryfall_query("restricted:vintage legal:vintage"))
    assert sql.count("card.card_legalities") == 2
    assert {"vintage": "restricted"} in params.values()
    assert {"vintage": "legal"} in params.values()
