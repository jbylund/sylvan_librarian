# Query Explanations (Scryfall-style)

**Date:** 2026-05-15  
**PR:** #419

## Overview

Search results now include a plain-language explanation of the query, displayed below the search
box. For example, `(power>3 or toughness>3) and id=g f=m` shows:
_"(the power is greater than 3 or the toughness is greater than 3) and the color identity is G and
it's legal in Modern"_

## Implementation

Each AST node type gained a `to_human_explanation()` method that recursively builds a readable
string. The approach is OO — no `isinstance` dispatch. Key behaviors:

- Attribute shorthands expand to readable names (e.g. `id` → "color identity", `f` → "legal in")
- Color codes and format codes expand using the existing `COLOR_CODE_TO_NAME` and
  `FORMAT_CODE_TO_NAME` maps in `db_info.py`
- OR groups nested inside AND expressions are wrapped in parentheses to reflect actual precedence
- Operators are mapped to prose (e.g. `>` → "is greater than", `:` → "is")

The explanation is returned in the existing search API response as a `query_explanation` field.
The frontend renders it in a `<div>` below the search box when the field is non-empty.

## Tests

34 new unit tests covering representative queries, operator prose, color/format expansion, and
nested grouping. Parse-error responses omit the field.
