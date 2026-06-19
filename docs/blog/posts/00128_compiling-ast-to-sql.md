---
title: "Compiling a Query AST to Parameterized SQL"
date: 2026-08-01
publishDate: 2026-08-01
tags: ["arcane-tutor", "sql", "postgres", "python"]
summary: "Each AST node emits a SQL fragment and bound parameters. How the node hierarchy works, how different field types generate different SQL, and why user input never touches the query string. Covers text/regex/arithmetic; JSONB operators and the count+results CTE are in separate posts."
---

`cmc+1<power` finds creatures where power exceeds mana value plus one — Gigantosaurus ({{< mana g >}}{{< mana g >}}{{< mana g >}}{{< mana g >}}{{< mana g >}}, 10/10) and Yargle, Glutton of Urborg ({{< mana 4 >}}{{< mana b >}}, 9/3) both qualify. The parser handles recognizing the arithmetic syntax and building the right AST; what this post is about is the other half: how that AST becomes a runnable SQL WHERE clause.

## How Parameters Accumulate

The entire compilation is three lines ([sql_generation.py](https://github.com/jbylund/arcane_tutor/blob/f23eff4ef5cdeb9fb0527193e1664ec3232fc549/api/parsing/sql_generation.py#L11-L14)):

```python
def generate_sql_query(parsed_query: Query) -> tuple[str, dict]:
    query_context = {}
    return parsed_query.to_sql(query_context), query_context
```

Every `to_sql(context)` method takes that dict and returns a SQL fragment. Leaf nodes — numbers, strings, regexes — store their value in the context under a generated name and return a `%(name)s` placeholder. Composite nodes recurse into their children and assemble the results.

The parameter name is a base64 slug derived from the value ([nodes.py](https://github.com/jbylund/arcane_tutor/blob/f23eff4ef5cdeb9fb0527193e1664ec3232fc549/api/parsing/nodes.py#L15-L26)):

```python
def param_name(ival: object) -> str:
    b64d = b64encode(str(ival).encode()).decode().rstrip("=")
    val_type = type(ival).__name__
    return f"p_{val_type}_{b64d}"
```

Two nodes holding the same value produce the same name, so identical literals in a query naturally deduplicate in the context dict. Different Python types get different prefixes even for equal values (`p_float_MS4w` vs `p_str_MS4w`).

## From Search Term to LIKE Pattern

`o:flying` should find cards with "flying" anywhere in their oracle text. The colon operator on a text column maps to a LIKE pattern, not equality ([card_query_nodes.py](https://github.com/jbylund/arcane_tutor/blob/f23eff4ef5cdeb9fb0527193e1664ec3232fc549/api/parsing/card_query_nodes.py#L923-L944)):

```python
words = ["", *(_escape_like_pattern(w) for w in txt_val.lower().split()), ""]
pattern = "%".join(words)
context[_param_name] = pattern
return f"(lower({lhs_sql}) LIKE %({_param_name})s)"
```

`o:flying` becomes `(lower(card.oracle_text) LIKE %(p_str_...)s)` with `"%flying%"` in the context. Multi-word queries like `o:"whenever you"` produce `"%whenever%you%"` — each word becomes a `%`-separated segment, so the words must appear in order but do not need to be adjacent.

The `lower()` on both sides is not just normalization. It enables a functional GIN index on `lower(card.oracle_text)`, which the query planner can use instead of a seq scan. The ILIKE alternative spent ~40ms in the planner for a ~3ms execution — the [S3 post](../00169_ilike-to-lower-like/) covers that in detail.

For regex patterns like `o:/^{T}:/`, the switch is one line:

```python
if isinstance(self.rhs, RegexValueNode):
    return f"({lhs_sql} ~* %({_param_name})s)"
```

PostgreSQL's `~*` operator does case-insensitive regex matching. The pattern goes into the context dict as-is; the user never touches the query string.

## Arithmetic Across Columns

`BinaryOperatorNode.to_sql` is fully recursive — it compiles left, compiles right, then assembles ([nodes.py](https://github.com/jbylund/arcane_tutor/blob/f23eff4ef5cdeb9fb0527193e1664ec3232fc549/api/parsing/nodes.py#L228-L233)):

```python
def to_sql(self, context: dict) -> str:
    sql_operator = self.operator if self.operator != ":" else "="
    return f"({self.lhs.to_sql(context)} {sql_operator} {self.rhs.to_sql(context)})"
```

For `cmc+1<power`, the parser produces a nested tree:

```
BinaryOperatorNode(
    BinaryOperatorNode(CardAttributeNode(cmc), "+", NumericValueNode(1.0)),
    "<",
    CardAttributeNode(creature_power)
)
```

Walking that tree:

- `CardAttributeNode(cmc).to_sql(ctx)` → `card.cmc`
- `NumericValueNode(1.0).to_sql(ctx)` → `%(p_float_MS4w)s`, sets `ctx["p_float_MS4w"] = 1.0`
- Inner node → `(card.cmc + %(p_float_MS4w)s)`
- `CardAttributeNode(creature_power).to_sql(ctx)` → `card.creature_power`
- Outer node → `((card.cmc + %(p_float_MS4w)s) < card.creature_power)`

The cross-attribute arithmetic falls out of the recursive structure with no special case.

One subtlety: `NotNode` preserves SQL's three-valued logic by emitting `NOT (...)` rather than an `IS FALSE` check. So `-power>2` on a card with no power attribute yields NULL, not FALSE — and NULL rows are excluded. Cards without a power do not appear in the results even under negation, which matches Scryfall's behavior.

## Why Injection Is Structurally Impossible

The context dict is the only path through which user-supplied values reach the database. Every `f"..."` string in every `to_sql` method contains only column names, SQL operators, and `%(name)s` placeholders. The values travel in the context dict and are bound by psycopg before the query executes.

This is not input sanitization layered on top of string concatenation. The structure makes injection impossible: a value that enters `StringValueNode.to_sql` is stored as a Python object and comes back as a placeholder. The SQL string and the user string never meet.

The one failure mode would be if a column name or operator were derived from user input. Column names come from `db_info.py`'s field map; operators come from the parser's fixed grammar. Neither is user-controlled.

The injection guarantee does not require discipline at call sites. There is no rule to remember and no validation to add. The structure makes it impossible: a user-supplied value enters `to_sql` as a Python object and leaves as a placeholder name. The SQL string and the user string are never in the same expression.
