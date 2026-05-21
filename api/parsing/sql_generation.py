"""SQL generation from parsed query ASTs."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from api.parsing.nodes import Query


def generate_sql_query(parsed_query: Query) -> tuple[str, dict]:
    """Generate a SQL WHERE clause string from a parsed Query AST."""
    query_context = {}
    return parsed_query.to_sql(query_context), query_context
