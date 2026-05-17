# Regex-Based Search Support

**Date:** 2025-02-02

## Overview

Added support for regex-based search using forward-slash delimiters, matching Scryfall's regex search syntax.

## Syntax

Use forward slashes `/` instead of quotes to perform regex pattern matching on text fields:

```
attribute:/pattern/
```

### Examples

From Scryfall documentation:

- `t:creature o:/^{T}:/` - Creatures that tap with no other payment
- `t:instant o:/\spp/` - Instants that provide +X/+X effects
- `name:/\bizzet\b/` - Card names with "izzet" but not words like "mizzet"

## Supported Attributes

Regex patterns work with the following text field attributes:

- `name:` - Card name
- `oracle:` or `o:` - Oracle text
- `flavor:` - Flavor text

## Regex Features

Scryfall regex patterns support:

- **Anchors**: `^` (start), `$` (end)
- **Alternation**: `(a|b)` - match a or b
- **Character classes**: `\d` (digit), `\w` (word), `\s` (whitespace)
- **Word boundaries**: `\b`
- **Brackets**: `[abc]` - match any of a, b, or c
- **Quantifiers**: `*`, `+`, `?`, `.*?` (non-greedy)
- **Lookahead**: `(?!pattern)`

## Implementation Details

- **Case-insensitive**: All regex searches are case-insensitive (uses PostgreSQL `~*` operator)
- **Multiline mode**: The `.` character does NOT match newlines
- **Escaping**: Forward slashes within patterns must be escaped: `\/`
- **No backreferences**: `\1`, `\2`, etc. are not supported

## Technical Notes

### Database

- Uses PostgreSQL's `~*` operator for case-insensitive regex matching
- Regular text searches continue to use `ILIKE` with `%` wildcards

### AST

- New `RegexValueNode` class represents regex patterns in the query AST
- Parser uses pyparsing's `QuotedString` with forward-slash delimiter
- Preprocessing handles forward-slash delimiters alongside quotes and parentheses

### Testing

- 28 comprehensive tests covering regex parsing and SQL generation
- All existing tests (473 total) continue to pass with no regressions
- Tests validate examples from Scryfall documentation
