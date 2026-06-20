---
title: "Whitespace as an Operator: Parsing Scryfall's Implicit AND"
date: 2026-07-25
publishDate: 2026-07-25
tags: ["arcane-tutor", "parser", "pyparsing", "python"]
summary: "The Scryfall query language implicitly ANDs adjacent terms — pyparsing does not. This post covers the preprocessing layer built on top: implicit AND injection, query balancing in two languages, and the edge cases that required fixes."
---

The query `type:creature power>3` means `type:creature AND power>3`. That is not how pyparsing sees it. Adjacent terms with no operator between them are a parse error — the grammar requires an explicit `AND` or `OR`. Closing that gap is the first thing that had to be built on top of the grammar.

## Implicit AND and Operator Precedence

Injection is straightforward between complete terms. The difficulty is
knowing where not to inject — particularly around `-`, which is both a
negation prefix (`-type:instant`) and an arithmetic operator
(`power-toughness`), and inside `field:value` pairs where the colon and
value are part of one term, not two.

### The First Attempt: A Hand-Rolled Character Loop

The first version of `preprocess_implicit_and` was a hand-rolled character-by-character loop.
It handled quoted strings (find the closing quote), parentheses, multi-char operators (`>=`, `<=`, `!=`), and words.
The AND-insertion logic was a set of guard conditions over the current and next token.

```python
# Original: hand-rolled char loop
while i < len(query):
    char = query[i]
    if char in ['"', "'"]:
        end_quote = query.find(quote_char, i + 1)
        tokens.append(query[i : end_quote + 1])
        i = end_quote + 1
    elif char.isspace():
        i += 1
    elif char in "><=!+-*/":
        if i + 1 < len(query) and query[i : i + 2] in [">=", "<=", "!="]:
            tokens.append(query[i : i + 2]); i += 2
        else:
            tokens.append(char); i += 1
    else:
        # read alphanumeric word
        ...
```

The hard part was the `-` character: it is both a negation prefix (`-type:instant`) and an arithmetic operator (`power-toughness`).
The heuristic was "if both sides are known card attributes, treat it as arithmetic; otherwise treat it as negation."
That worked until `id=r -o:enchantment` broke it —
the value `r` happened to be followed by `-o`, which looked like arithmetic subtraction on a card attribute.
The fix was a `prev_is_comparison` guard:
if the token before the `-` was a comparison operator,
the `-` is the start of a negated operand, not infix subtraction.
([#430](https://github.com/jbylund/arcane_tutor/pull/430))

### Rewriting the Tokenizer with pyparsing ([#433](https://github.com/jbylund/arcane_tutor/pull/433))

The char loop grew fragile.
Hyphenated values (`otag:dual-land`, `is:modal-dfc`) needed special handling,
and regex literals (`/foo/`) were completely absent.
The rewrite replaced the loop with a pyparsing grammar using the same token boundaries as the main grammar:

```python
one_token = (
    quoted_raw       # "..." or '...'
    | regex_raw      # /…/
    | lparen_tok | rparen_tok
    | and_tok | or_tok
    | comparison_tok # >=, <=, !=, :, =, >, <
    | arithmetic_tok # + - * /
    | float_tok
    | string_value_tok   # words, hyphenated-words, 40k-model
    | mana_tok           # {W}{U}, {2/R}
)
```

Token ordering is important: `string_value_tok` (`[a-zA-Z0-9_]([a-zA-Z0-9_-]*[a-zA-Z0-9_])?`) comes before `mana_tok`
so `bar` is not consumed as the mana letter `b` + `ar`, and AND/OR keywords come before word tokens so they are not swallowed as card-attribute names.

## Query Balancing

Typeahead searches fire on every keystroke.
If the user has typed `name:"lig` the query is syntactically broken — the string is not closed.
Rather than showing an error mid-typing, the query is balanced before sending it:
append whatever closing characters are needed to make the structure valid.

The algorithm is a small stack machine: push open delimiters; pop on matching close.
Quotes are their own mirror (a `"` both opens and closes).
When done, drain the stack in reverse, appending mirror characters.

The same algorithm runs in two places.
The JS version fires on every keystroke in the browser before the request goes out.
The Python version runs in the API on every search request.

The JS version handles UX — a user mid-typing `name:"lig` has not made a mistake, they just have not finished yet, and balancing client-side lets the app show live results instead of an error.
The Python version is for callers who hit `/search` directly and never touch the browser: direct API users, scripts, anything that skips the frontend entirely, where the server has to be equally tolerant of partial input.

The practical result is that the algorithm is specified twice, in two languages, and has to stay in sync:

**JavaScript (`app.js`):**
```javascript
balanceQuery(query) {
  const charToMirror = { '(': ')', "'": "'", '"': '"', ')': '(' };
  const quoteChars = new Set(["'", '"']);
  const stack = [];
  for (const char of query) {
    if (stack.length > 0 && quoteChars.has(stack[stack.length - 1])) {
      if (char === stack[stack.length - 1]) stack.pop();
      continue;
    }
    const mirrored = charToMirror[char];
    if (!mirrored) continue;
    if (stack.length > 0 && stack[stack.length - 1] === mirrored) stack.pop();
    else stack.push(char);
  }
  let closing = '';
  while (stack.length > 0) closing += charToMirror[stack.pop()];
  return query + closing;
}
```

**Python (`parsing_f.py`):**
```python
def balance_partial_query(query: str) -> str:
    char_to_mirror = {"(": ")", "'": "'", '"': '"', ")": "("}
    quote_chars = {"'", '"'}
    current_stack = []
    for char in query:
        if current_stack and current_stack[-1] in quote_chars:
            if char == current_stack[-1]:
                current_stack.pop()
            continue
        mirrored_char = char_to_mirror.get(char)
        if not mirrored_char:
            continue
        if current_stack and current_stack[-1] == mirrored_char:
            current_stack.pop()
        else:
            if char in {")"}:
                raise ValueError(f"Unbalanced closing character '{char}'")
            current_stack.append(char)
    while current_stack:
        query += char_to_mirror[current_stack.pop()]
    return query
```

### The Apostrophe Bug ([#477](https://github.com/jbylund/arcane_tutor/pull/477))

The original versions of both functions did not track whether they were inside a quoted string.
Given `!"stormchaser's talent"`:

```
push "    → stack: ["]
push '    → stack: [", ']   ← wrong: the ' is inside a double-quoted string
push "    → stack: [", ', "] ← second " doesn't close the first
result: !"stormchaser's talent""'"   ← corrupted
```

The fix: when the top of the stack is a quote character,
skip every character except the matching closing quote.
That single `continue` branch, visible in both snippets above, was the fix.

## Parsing Arithmetic on Both Sides of a Comparison

Scryfall's syntax supports numeric comparisons like `power>3`.
Arcane Tutor extends this: both sides of a comparison can be arithmetic expressions
over numeric card attributes and literal numbers:

```
cmc+1<power             # CMC plus one is less than power
power-toughness>=0      # power is at least as high as toughness
cmc+power<toughness+cmc # sum of both sides
```

The grammar separates the arithmetic layer from the comparison layer.
An `arithmetic_term` is a numeric attribute, a literal number, or a parenthesised expression.
An `arithmetic_expr` is at least two terms joined by `+`, `-`, `*`, or `/`.
A numeric comparison is an `arithmetic_expr`, bare attribute, or literal on each side
of a comparison operator:

```python
arithmetic_term = numeric_attr_word | literal_number | paren_expr_term
arithmetic_expr <<= (
    arithmetic_term + arithmetic_op + arithmetic_term
    + ZeroOrMore(arithmetic_op + arithmetic_term)
)

numeric_comparison_lhs = arithmetic_expr | paren_expr_term | numeric_attr_word | literal_number
unified_numeric_comparison = numeric_comparison_lhs + DEFAULT_OPERATORS + numeric_comparison_lhs
```

The parse action `make_chained_arithmetic` folds the flat token list
into a left-associative tree of `BinaryOperatorNode` pairs:

```python
def make_chained_arithmetic(tokens):
    # [a, +, b, +, c]  →  BinaryOperatorNode(BinaryOperatorNode(a, +, b), +, c)
    result = create_value_node(tokens[0])
    for i in range(1, len(tokens), 2):
        result = BinaryOperatorNode(result, tokens[i], create_value_node(tokens[i + 1]))
    return result
```

The same `BinaryOperatorNode` type represents both arithmetic sub-expressions
and the top-level comparison — the SQL generation code walks the tree recursively
and emits the right SQL fragment at each level.

## Performance: Packrat Caching ([#66](https://github.com/jbylund/arcane_tutor/pull/66))

pyparsing is a backtracking PEG parser.
For a grammar with significant alternation — seven different condition types tried in order,
each with its own alternatives — the parser may evaluate the same position many times
before settling on the right production rule.
Packrat parsing fixes this by memoizing `(position, rule) → result`
so each combination is only evaluated once per parse.

Benchmarking before and after tells a more complicated story than "quick win":

```
Query                              no packrat   packrat   speedup
simple: field:value                    17,147    10,540      0.6x
simple: numeric comparison             15,320    10,010      0.7x
moderate: parens                        1,166     1,284      1.1x
complex: nested parens                    632       795      1.3x
complex: deep nesting                     100       571      5.7x
```

Simple queries are slower with packrat enabled.
Every grammar rule at every position now pays a cache lookup,
and for a short query that terminates quickly the overhead is not worth it.
The win only appears once the query is complex enough that the parser
would otherwise revisit the same positions many times —
the deeply-nested case goes from 100 to 571 parses/sec.

Whether packrat is a net win in production depends entirely on the query distribution.
If most real searches are simple `field:value` pairs (they probably are),
packrat is a small net negative.
If a meaningful fraction of users write deeply nested boolean expressions,
it pays for itself.
The only honest answer without production query distribution data is that it was a guess —
and eventually pyparsing itself became the bottleneck for reasons packrat could not address.
A later post covers the hand-rolled rewrite that replaced it.
