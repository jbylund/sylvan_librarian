---
title: "Building a Scryfall-Compatible Query DSL with pyparsing"
date: 2026-07-18
publishDate: 2026-07-18
tags: ["arcane-tutor", "parser", "pyparsing", "python"]
summary: "How I used pyparsing to build a Scryfall-compatible query grammar, including the surprising corners: implicit AND preprocessing, query balancing in two languages, and arithmetic expression detection."
---

## The Problem

[Scryfall](https://scryfall.com) is the de facto Magic: The Gathering card search engine.
The query box is the primary interface — there is an advanced search form,
but it cannot even express everything the query language can.
You type `t:creature cmc<=3 id:g` and get every green creature costing three or less.

Arcane Tutor aims to be compatible with that syntax.
A query is a sequence of conditions joined by boolean logic.
Explicit `AND` and `OR` are supported, and adjacent terms with no operator are implicitly ANDed:

```
type:creature power>3           # creature with power greater than 3
cmc<=3 mana:{1}{G}              # CMC ≤ 3 and green mana in cost
!"stormchaser's talent"         # exact card name
o:/\bflying\b/ t:enchantment    # oracle text matches regex, type is enchantment
(r:m OR r:r) f:legacy           # mythic or rare, legal in legacy
```

Arcane Tutor extends the syntax with arithmetic expressions across numeric fields:

```
cmc+power<toughness+cmc         # arithmetic on both sides of the comparison
power-toughness>=0              # subtraction (not negation)
```

The full grammar has atoms (`field:value`, `field>=value`, `!name`, `"quoted"`, `/regex/`),
boolean combinators (`AND`, `OR`, implicit AND, negation with `-`),
grouping with parentheses,
and arithmetic over numeric attributes.
That ruled out several approaches before I got to pyparsing.

## Choosing a Parsing Approach

### Regex

Regex can match individual atoms like `field:value` or `cmc>=3` without difficulty.
The problem is everything else: nested parentheses require a context-free grammar,
not a regular one,
and the disambiguation between `-` as negation (`-t:instant`) versus arithmetic subtraction (`power-toughness`)
is context-sensitive in a way that regular expressions cannot express.
A regex-based approach would require a hand-built combinator layer on top —
at which point you have essentially written a parser anyway.

### Hand-Rolled Recursive Descent

Recursive descent is the natural fit for a grammar this size:
write a function per production rule, call them recursively, return AST nodes.
This is fast, debuggable, and has no dependencies.

I did not start here because of unfamiliarity and complexity (real or perceived).
It felt like a much larger jump to go from zero to something functional with recursive descent than it would be with pyparsing.
I later wrote a recursive descent parser, but it was easier to prototype with something with a lower barrier to entry.

### ANTLR

ANTLR is the industrial-strength option.
You write a grammar in a `.g4` file, run a Java tool,
and it generates a Python (or Java, or C#, or…) parser for you.
The generated code handles lexing, parsing, and visitor/listener dispatch.

The barrier for me was the toolchain.
ANTLR requires a Java runtime to generate the parser,
which means a separate build step, a checked-in generated file or a generation script in CI,
and a dependency on `antlr4-python3-runtime` at runtime.
For a side project, that felt like too much ceremony to take on before I had even validated the grammar shape.

### Lark

Lark is a pure-Python parser library with no build-time toolchain.
You write grammar rules in a separate string using a BNF-like syntax,
and Lark compiles and runs it.
A Lark grammar for this DSL ends up about 35 lines:

```python
from lark import Lark

GRAMMAR = r"""
start: expr

expr:     and_expr (OR and_expr)*
and_expr: factor (AND? factor)*
factor:   "-" primary | primary

primary: "(" expr ")"
       | "!" DQUOTED         // exact name: !"lightning bolt"
       | "!" ATOM
       | arith NUM_OP arith  // numeric/arithmetic comparison: power+toughness>cmc
       | ATOM NUM_OP val     // attribute = value: mana=1{G}, cmc=3
       | ATOM ":" val        // attribute : value: type:creature, o:flying
       | DQUOTED | SQUOTED   // quoted string → name search
       | REGEX               // /pattern/ → oracle regex
       | ATOM                // bare word → name search

arith:      "-"? arith_atom (ARITH_OP arith_atom)*
arith_atom: ATOM | NUMBER | "(" arith ")"

val: DQUOTED | SQUOTED | REGEX | MANA | NUMBER | ATOM

// Word-boundary lookahead prevents OR/AND from matching inside words like "oracle"
OR:       /(?i:or)(?![a-zA-Z0-9_\-])/
AND:      /(?i:and)(?![a-zA-Z0-9_\-])/
NUM_OP:   ">=" | "<=" | "!=" | ">" | "<" | "="
ARITH_OP: /[+\-*\/]/
REGEX:    /\/([^\/\\]|\\.)*\//
MANA:     /[0-9WUBRGCXYZwubrgcxyz]*(\{[^}]+\})+[0-9WUBRGCXYZwubrgcxyz]*/
NUMBER:   /\d+(\.\d*)?/
ATOM:     /[a-zA-Z0-9_][a-zA-Z0-9_\-]*/
DQUOTED:  /\"[^\"\\]*(?:\\.[^\"\\]*)*\"/
SQUOTED:  /'[^'\\]*(?:\\.[^'\\]*)*'/

%ignore /\s+/
"""

parser = Lark(GRAMMAR, parser="earley", ambiguity="resolve")

tree = parser.parse("type:creature power>3")
print(tree.pretty())
# start
#   expr
#     and_expr
#       factor
#         primary
#           type
#           val     creature
#       factor
#         primary
#           arith
#             arith_atom  power
#           >
#           arith
#             arith_atom  3
```

This grammar was tested against the project's 121-query corpus — it passes all of them.

The `AND?` in `and_expr` is where LALR(1) fails.
`ATOM` appears in both `ATOM: val` (attribute comparison) and `arith NUM_OP arith` (arithmetic),
and LALR(1) cannot decide which rule applies with only one token of lookahead.
Earley handles the ambiguity at parse time without grammar restructuring.

The O(n³) complexity is a legitimate concern on paper.
In practice, for search queries that are typically 5–20 tokens long,
Earley is fast enough that it would not be the performance bottleneck.
The implicit AND problem does not disappear with Lark either:
it just moves from "requires preprocessing in pyparsing" to "requires Earley or preprocessing in Lark."

The main trade-off: the grammar lives in a string rather than in Python,
so there is no autocomplete and no inline documentation.
pyparsing keeps the grammar and the transformation logic in the same place —
which, for a project where the grammar evolved incrementally over time, made iteration faster.

### pyparsing

pyparsing is a pure-Python PEG-style library where you build the grammar
by composing Python objects directly in your source file.
There is no separate grammar file and no tooling step:

```python
from pyparsing import CaselessKeyword, Literal, Optional, QuotedString, Regex, ZeroOrMore

# atoms
quoted_string = QuotedString('"', esc_char="\\") | QuotedString("'", esc_char="\\")
regex_pattern = QuotedString("/", esc_char="\\", unquote_results=False)
word = Regex(r"[a-zA-Z_][a-zA-Z0-9_-]*[a-zA-Z0-9_]|[a-zA-Z_]")
op = one_of(": = != >= <= > <")
condition = word + op + (quoted_string | regex_pattern | word)

# boolean structure
operator_and = CaselessKeyword("AND")
operator_or  = CaselessKeyword("OR")
operator_not = Literal("-")

expr = Forward()
factor = Optional(operator_not) + (condition | Group(Literal("(").suppress() + expr + Literal(")").suppress()))
and_expr = factor + ZeroOrMore(operator_and + factor)
expr <<= and_expr + ZeroOrMore(operator_or + and_expr)
```

The grammar lives in the same codebase as everything else.
Iteration is fast: change a line, run the test, see what breaks.
The library also handles packrat memoization and provides `infixNotation` as a convenience
for grammars with layered precedence — both of which I used.

Like Lark, pyparsing does not natively support implicit AND
(the grammar above requires explicit `AND` between terms).
I handled that with a preprocessing pass,
which turned out to be one of the more interesting problems in the project.

## The Decision

pyparsing won on iteration speed and zero tooling overhead.
The grammar was readable in Python,
the library handled the mechanics I did not want to write myself,
and I was satisfied with the performance.
Parse times under 2ms are not a meaningful fraction of total request latency
until the database round-trip is already under 10ms —
and at that point there are bigger things to optimize first.

---

## Implicit AND and Operator Precedence

The query `type:creature power>3` means `type:creature AND power>3`.
Adjacent terms without an explicit boolean are ANDed together.
The grammar above requires explicit `AND` keywords,
so the implicit AND has to be injected as a preprocessing step before the grammar sees the string.

### The Original Tokenizer

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

Ordering matters.
`string_value_tok` (`[a-zA-Z0-9_]([a-zA-Z0-9_-]*[a-zA-Z0-9_])?`) comes _before_ `mana_tok`
so `bar` does not get consumed as the mana letter `b` + `ar`.
AND/OR keywords come before word tokens so they are not swallowed as card-attribute names.

## Quoted Strings and Apostrophes

The grammar supports both `"double-quoted"` and `'single-quoted'` strings.
The `!` prefix does an exact name search: `!"stormchaser's talent"`.

### Query Balancing

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

They exist for different reasons.
The JS balancing is about UX:
a user mid-typing `name:"lig` has not made a mistake, they just have not finished yet.
Balancing client-side lets the app fire a real search and show live results instead of an error.
The Python balancing is about correctness for callers who never touch the browser at all —
direct API users, scripts, and anything that hits `/search` without going through the frontend.
Those requests skip the JS entirely, so the server has to be equally tolerant of partial input.

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

## Regex Literals

Scryfall's syntax supports regex searches on oracle text (`o:/\bflying\b/`),
and I added the same to Arcane Tutor in [PR #246](https://github.com/jbylund/arcane_tutor/pull/246).
The delimiters are forward slashes, which `QuotedString` does not handle by default,
but passing `"/"` as the quote character works correctly with two non-default options:

```python
regex_pattern = QuotedString(
    "/",
    esc_char="\\",
    unquote_results=False,
    convert_whitespace_escapes=False,
)
```

`QuotedString` normally strips the delimiters and processes escape sequences.
`unquote_results=False` keeps the surrounding `/…/` intact in the token,
and `convert_whitespace_escapes=False` is critical:
without it, pyparsing would convert `\b` to a literal backspace character
before the pattern ever reached Python's `re` module.
The parse action then strips the slashes manually and converts `\/` back to `/`
(for patterns that themselves contain a forward slash):

```python
def make_regex_pattern_value(tokens):
    pattern = tokens[0][1:-1]       # strip leading and trailing /
    pattern = pattern.replace("\\/", "/")
    return ("regex", pattern)
```

Regex values are accepted wherever a text field value can appear —
oracle text (`o:`), flavor text (`ft:`), card name (`name:`) —
and are passed straight through to PostgreSQL as `~ pattern` comparisons.

## Arithmetic Comparisons

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
I did not have production query distribution data when the decision was made,
and given that the whole pyparsing approach was later replaced,
it was never worth revisiting.

Eventually pyparsing itself became the bottleneck — but that is a story for [another post](./2027_03_27_hand-rolled-parser.md) ([#482](https://github.com/jbylund/arcane_tutor/pull/482)).
