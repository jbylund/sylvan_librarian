"""Post-parse pipeline — shared transforms after any front-end parser."""

from __future__ import annotations

from typing import TYPE_CHECKING

from api.parsing.nodes import Query
from api.parsing.prefer import lift_prefer
from api.parsing.query_budget import check_query_byte_length
from api.parsing.regex_budget import validate_regex_patterns
from api.parsing.rewrite import flatten_and_deduplicate_compounds, rewrite_query

if TYPE_CHECKING:
    from collections.abc import Callable


def finalize_query(query: Query) -> Query:
    """Lift `prefer:`, then apply semantic rewrites, regex static limits, then flatten+dedupe normalization."""
    root, prefer = lift_prefer(query.root)
    query = rewrite_query(Query(root))
    validate_regex_patterns(query)
    query = flatten_and_deduplicate_compounds(query)
    query.prefer = prefer
    return query


def parse_query(query: str | None, parser_fn: Callable[[str | None], Query]) -> Query:
    """Run *parser_fn* on *query*, then ``finalize_query``.

    Front-end parsers (``hand_parser.parse_str_to_query``, ``pyparsing_based.parse_str_to_query``)
    each turn a string into a raw AST; this is the single entry for the full public pipeline.
    """
    if query is not None:
        check_query_byte_length(query)
    return finalize_query(parser_fn(query))
