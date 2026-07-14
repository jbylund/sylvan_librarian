# Ignore unrecognized query parts (Scryfall-style) and report what was ignored

## Problem

When a user enters a search query that contains **both** valid and invalid portions, the app currently **fails the entire query** and returns an error (e.g. HTTP 400). On Scryfall, unrecognized parts are **ignored** and the search runs on the recognized parts. For example, on Scryfall the query `t:merfolk x>3` produces:

- A message: _Invalid expression "x>3" was ignored. One or both sides of your comparison must be a card value ("pow", "tou", "usd", "eur", etc)._
- Results: _1 – 60 of 331 cards where the card types include "merfolk"_

So Scryfall runs the search on `t:merfolk` and informs the user that `x>3` was ignored and why. Sylvan Librarian currently has no equivalent: any invalid or unrecognized portion causes the whole query to fail, and the response does not indicate which parts (if any) were valid or what was ignored.

## Desired behavior

1. **Ignore unrecognized portions and run on the rest**
   When the query contains some valid and some invalid/unrecognized sub-expressions, **parse and execute** only the valid parts and **ignore** the unrecognized parts, rather than failing the entire request.

2. **Return in the response what was ignored**
   The API response for a search that had ignored portions should include:
   - **Which portions** of the query were ignored (e.g. the raw substring such as `x>3`, or a list of such fragments).
   - **Optionally**, a short reason per fragment (e.g. _"One or both sides of your comparison must be a card attribute (pow, tou, cmc, etc.)"_), so the user can correct their query if desired.

3. **Placement of “ignored” in the response**
   Expose the ignored portions (and optional reasons) in the **existing search/card API response** (e.g. a field such as `query_ignored` or `ignored_expressions`), so the frontend can display them near the search input or results (e.g. “The following part of your query was ignored: …”).

4. **When to use “ignore” vs “fail”**
   - If **no** part of the query is valid (e.g. the whole string is invalid syntax or unknown attributes), it is acceptable to keep current behavior and return an error (e.g. 400).
   - If **at least one** part is valid, parse and run that part, and report the ignored parts in the response as above.

## Implementation notes

- **Parser**
  - The current parser uses a single pass with `parseAll=True` and raises on any parse failure (`api/parsing/parsing_f.py`: `parse_search_query`). To support “ignore unrecognized,” we need a strategy to **segment** the query into sub-expressions (e.g. by splitting on top-level AND/OR or by token boundaries), try parsing each segment, and combine the successfully parsed segments into one AST while collecting the raw text (and optionally error messages) of segments that failed to parse.
  - Alternative: use a **best-effort** or **partial parse** mode: e.g. parse until first error, optionally skip past the problematic token/segment, and continue parsing the rest, accumulating “ignored” spans. This may require parser changes to support non–parseAll behavior and to record character ranges or substrings for ignored parts.
  - Reuse or extend `create_parsing_error`-style context when building optional “reason” strings for ignored fragments (e.g. “unknown attribute ‘x’” or “invalid comparison”).

- **API**
  - In `api/api_resource.py`, the search path calls `parse_scryfall_query(query)` and on `ValueError` raises `HTTPBadRequest`. With the new behavior, the API should:
    - Call a new entry point that returns both a **Query AST** (from the valid parts) and a **list of ignored portions** (each with substring and optional reason).
    - If the AST is non-empty, run the search as today and add to the JSON response a field such as `query_ignored`: `[{ "fragment": "x>3", "reason": "..." }]` (or similar).
    - If the AST is empty (nothing valid), retain current behavior: return 400 with an error message.

- **Frontend**
  - In `api/static/app.js` (and any markup), when the API response includes `query_ignored` (or equivalent) with one or more entries, **display** them near the search input or results (e.g. “The following part of your query was ignored: …” with the fragment and optional reason), so the user can see what was dropped and why, similar to Scryfall.

- **Edge cases**
  - Quoted strings and regex patterns (e.g. `/"some pattern"/`) should not be split in the middle; segment boundaries must respect quoted and regex regions.
  - Parenthesized groups should ideally be treated as units: either the whole group parses or the whole group is reported as ignored, to avoid confusing partial parses.
  - Very long “ignored” lists could be truncated or summarized in the UI.

- **Tests**
  - Add unit tests for the parser (or new entry point) with queries like `t:merfolk x>3`, `cmc:2 invalid stuff pow>1`, and assert that the resulting AST matches the valid part only and that the list of ignored fragments/reasons is as expected.
  - Add API/integration tests that the search response includes `query_ignored` when appropriate and that results correspond to the valid part of the query.

## Summary

- **Current:** Any unrecognized or invalid part of a query causes the entire query to fail; the response does not indicate what was valid or what was ignored.
- **Goal:** Behave like Scryfall: ignore unrecognized portions, run the search on the recognized parts, and return in the response the portions of the query that were ignored (and optionally why), so users still get results and can see what was dropped.
