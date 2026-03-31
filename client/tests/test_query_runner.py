# """Tests for the client query runner."""

# from __future__ import annotations

# import pytest

# from client.query_runner import (
#     _generate_basic_queries,
#     _generate_combined_queries,
#     _generate_text_queries,
#     _generate_type_queries,
#     generate_random_queries,
# )


# class TestQueryGeneration:
#     """Test query generation functions."""

#     @pytest.mark.parametrize(
#         argnames="needle",
#         argvalues=[
#             "c:",
#             "cmc<",
#             "cmc=",
#             "cmc>",
#             "color:",
#             "id:",
#             "mv=",
#         ],
#     )
#     def test_generate_basic_queries(self, needle: str) -> None:
#         """Test that basic queries function returns a list."""
#         queries = _generate_basic_queries()
#         assert len(queries) > 0
#         assert any(needle in q for q in queries)

#     @pytest.mark.parametrize(
#         argnames="needle",
#         argvalues=[
#             "type:",
#             "t:",
#             "rarity:",
#             "r:",
#             "pow=",
#             "tou=",
#         ],
#     )
#     def test_generate_type_queries(self, needle: str) -> None:
#         """Test that type queries function returns a list."""
#         queries = _generate_type_queries()
#         assert len(queries) > 0
#         assert any(needle in q for q in queries)

#     def test_generate_combined_queries_returns_list(self) -> None:
#         """Test that combined queries function returns a list."""
#         queries = _generate_combined_queries()
#         assert isinstance(queries, list)
#         assert len(queries) > 0

#     def test_generate_combined_queries_has_multiple_criteria(self) -> None:
#         """Test that combined queries have multiple search criteria."""
#         queries = _generate_combined_queries()
#         # All combined queries should have at least one space (multiple terms)
#         assert all(" " in q for q in queries)

#     @pytest.mark.parametrize(
#         argnames="needle",
#         argvalues=[
#             "format:",
#             "oracle:",
#             "set:",
#         ],
#     )
#     def test_generate_text_queries(self, needle: str) -> None:
#         """Test that text queries function returns a list."""
#         queries = _generate_text_queries()
#         assert len(queries) > 0
#         assert any(needle in q for q in queries)

#     def test_generate_random_queries_aggregates_all(self) -> None:
#         """Test that generate_random_queries combines all query types."""
#         queries = generate_random_queries()
#         assert isinstance(queries, list)

#         # Should have queries from all categories
#         basic = _generate_basic_queries()
#         type_queries = _generate_type_queries()
#         combined = _generate_combined_queries()
#         text = _generate_text_queries()

#         expected_count = len(basic) + len(type_queries) + len(combined) + len(text)
#         assert len(queries) == expected_count

#     def test_generate_random_queries_no_duplicates_within_category(self) -> None:
#         """Test that generated queries don't have obvious duplicates."""
#         queries = generate_random_queries()
#         # While there might be intentional variations (color: vs c:),
#         # the list should still be reasonably large
#         assert len(queries) > 100

#     def test_queries_are_valid_strings(self) -> None:
#         """Test that all generated queries are valid strings."""
#         queries = generate_random_queries()
#         assert all(isinstance(q, str) for q in queries)
#         assert all(len(q) > 0 for q in queries)
#         # Queries should not start or end with spaces
#         assert all(q == q.strip() for q in queries)
