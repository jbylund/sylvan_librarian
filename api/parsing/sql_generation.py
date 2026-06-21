"""SQL generation from parsed query ASTs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from api.parsing.nodes import QueryContext

if TYPE_CHECKING:
    from api.parsing.nodes import Query


def generate_sql_query(parsed_query: Query) -> tuple[str, QueryContext]:
    """Generate a SQL WHERE clause string from a parsed Query AST."""
    context = QueryContext()
    return parsed_query.to_sql(context), context
