---
title: "Compiling a Query AST to Parameterized SQL"
date: 2026-08-01
publishDate: 2026-08-01
tags: ["arcane-tutor", "sql", "postgres", "python"]
summary: "Each AST node emits a SQL fragment and bound parameters. How the node hierarchy works, how different field types generate different SQL, and why user input never touches the query string. Covers text/regex/arithmetic; JSONB operators and the count+results CTE are in separate posts."
---

<!-- {{< mana >}} shortcodes render as inline mana symbols using the same font inlined from the main site. -->
`cmc+1<power` finds creatures where power exceeds mana value plus one — Gigantosaurus ({{< mana g >}}{{< mana g >}}{{< mana g >}}{{< mana g >}}{{< mana g >}}, 10/10) and Yargle, Glutton of Urborg ({{< mana 4 >}}{{< mana b >}}, 9/3) both qualify. The parser handles recognizing the arithmetic syntax and building the right AST; what this post is about is the other half: how that AST becomes a runnable SQL WHERE clause.

## How Parameters Accumulate

The entire compilation is three lines ([sql_generation.py](https://github.com/jbylund/arcane_tutor/blob/1209803e65e82fab5b1e038fe2d5bafb55dc7d9a/api/parsing/sql_generation.py#L13-L16)):

```python
def generate_sql_query(parsed_query: Query) -> tuple[str, QueryContext]:
    context = QueryContext()
    return parsed_query.to_sql(context), context
```

Every `to_sql(context)` method takes that context and returns a SQL fragment. `QueryContext` is a thin subclass of `dict` with one extra method — `add` — that does three things at once: computes a parameter name, stores the value, and returns the `%(name)s` placeholder ([nodes.py](https://github.com/jbylund/arcane_tutor/blob/1209803e65e82fab5b1e038fe2d5bafb55dc7d9a/api/parsing/nodes.py#L15-L27)):

```python
class QueryContext(dict[str, object]):
    def add(self, value: object) -> str:
        """Register a bound parameter and return its %(name)s placeholder."""
        b64d = b64encode(str(value).encode()).decode().rstrip("=")
        name = f"p_{type(value).__name__}_{b64d}"
        self[name] = value
        return f"%({name})s"
```

Because `add` returns the placeholder directly, leaf nodes can register a value and get back the SQL fragment in one call. `ValueNode.to_sql` is a single line:

```python
def to_sql(self: ValueNode, context: QueryContext) -> str:
    return context.add(self.value)
```

The name scheme — base64 of the value, prefixed by Python type — means identical literals in a query deduplicate automatically. Different Python types get different prefixes even for equal values (`p_float_MS4w` vs `p_str_MS4w`). Subclassing `dict` instead of wrapping it means all existing code that reads `.values()`, iterates, or compares with `==` continues to work without any changes.

## From Search Term to LIKE Pattern

`o:flying` should find cards with "flying" anywhere in their oracle text. The colon operator on a text column maps to a LIKE pattern, not equality ([card_query_nodes.py](https://github.com/jbylund/arcane_tutor/blob/1209803e65e82fab5b1e038fe2d5bafb55dc7d9a/api/parsing/card_query_nodes.py#L902-L918)):

```python
words = ["", *(_escape_like_pattern(w) for w in txt_val.lower().split()), ""]
pattern = "%".join(words)
return f"(lower({lhs_sql}) LIKE {context.add(pattern)})"
```

`o:flying` becomes `(lower(card.oracle_text) LIKE %(p_str_...)s)` with `"%flying%"` in the context. Multi-word queries like `o:"whenever you"` produce `"%whenever%you%"` — each word becomes a `%`-separated segment, so the words must appear in order but do not need to be adjacent.

The `lower()` on both sides is not just normalization. It enables a functional GIN index on `lower(card.oracle_text)`, which the query planner can use instead of a seq scan. The ILIKE alternative spent ~40ms in the planner for a ~3ms execution — the [S3 post](../00384_ilike-trap-postgres-planner/) covers that in detail.

For regex patterns like `o:/^{T}:/`, the switch is one line:

```python
if isinstance(self.rhs, RegexValueNode):
    return f"({lhs_sql} ~* {context.add(self.rhs.value)})"
```

PostgreSQL's `~*` operator does case-insensitive regex matching. The pattern goes into the context via `add` and is bound by psycopg before the query executes. The user never touches the query string.

## Arithmetic Across Columns

`BinaryOperatorNode.to_sql` is fully recursive — it compiles left, compiles right, then assembles ([nodes.py](https://github.com/jbylund/arcane_tutor/blob/1209803e65e82fab5b1e038fe2d5bafb55dc7d9a/api/parsing/nodes.py#L209-L214)):

```python
def to_sql(self: BinaryOperatorNode, context: QueryContext) -> str:
    sql_operator = self.operator
    if sql_operator == ":":
        sql_operator = "="
    return f"({self.lhs.to_sql(context)} {sql_operator} {self.rhs.to_sql(context)})"
```

For `cmc+1<power`, the parser produces a nested tree:

```python
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
- Final context: `{"p_float_MS4w": 1.0}`

The cross-attribute arithmetic falls out of the recursive structure with no special case.

## NULL Under Negation

Cards without a power attribute — lands, instants, sorceries — are expected to be absent from both `power>2` and `-(power>2)`. SQL's three-valued logic delivers this for free. `creature_power` is NULL for non-creature cards; `NULL > 2` evaluates to NULL, not FALSE; `NOT NULL` is still NULL; and the WHERE clause excludes NULL rows. The null exclusion is symmetric across positive and negative forms with no special case required.

## Why Injection Is Structurally Impossible

The context is the only path through which user-supplied values reach the database. Every `f"..."` string in every `to_sql` method contains only column names, SQL operators, and `%(name)s` placeholders. The values travel in the context and are bound by psycopg before the query executes. This is not input sanitization layered on top of string concatenation — the SQL string and the user string never meet.

The one failure mode would be if a column name or operator were derived from user input. Column names come from `db_info.py`'s [field map](https://github.com/jbylund/arcane_tutor/blob/1209803e65e82fab5b1e038fe2d5bafb55dc7d9a/api/parsing/db_info.py#L112-L130); operators come from the parser's fixed grammar. Neither is user-controlled:

```python
FieldInfo(db_column_name="cmc",               search_aliases=["cmc", "mv", "manavalue"]),
FieldInfo(db_column_name="creature_power",     search_aliases=["power", "pow"]),
FieldInfo(db_column_name="creature_toughness", search_aliases=["toughness", "tou"]),
```

The user types `power`; the parser matches it against `search_aliases` and produces a `CardAttributeNode` wrapping `"creature_power"`. That column name was never in the user's input.
