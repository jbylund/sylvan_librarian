---
title: "Choosing a Parser for a Query DSL: Regex, ANTLR, Lark, and pyparsing"
date: 2026-07-03
publishDate: 2026-07-03
tags: ["parser", "pyparsing", "python"]
summary: "How I evaluated parsing approaches for Scryfall's query language: why regex and ANTLR were ruled out, how Lark compares to pyparsing, and why pyparsing won."
---

## The Problem

[Scryfall](https://scryfall.com) is the de facto Magic: The Gathering card search engine.
The query box is the primary interface — there is an advanced search form,
but it cannot even express everything the query language can.
You type `t:creature cmc<=3 id:g` and get every green creature costing three or less.

{{< sitename >}} aims to be compatible with that syntax.
A query is a sequence of conditions joined by boolean logic.
Explicit `AND` and `OR` are supported, and adjacent terms with no operator are implicitly ANDed:

```
type:creature power>3           # creature with power greater than 3
cmc<=3 mana:{1}{G}              # CMC ≤ 3 and green mana in cost
!"stormchaser's talent"         # exact card name
o:/\bflying\b/ t:enchantment    # oracle text matches regex, type is enchantment
(r:m OR r:r) f:legacy           # mythic or rare, legal in legacy
```

{{< sitename >}} extends the syntax with arithmetic expressions across numeric fields:

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

### Why Regex Fails at Nested Structure

Regex can match individual atoms like `field:value` or `cmc>=3` without difficulty.
The problem is everything else: nested parentheses require a context-free grammar,
not a regular one,
and the disambiguation between `-` as negation (`-t:instant`) versus arithmetic subtraction (`power-toughness`)
is context-sensitive in a way that regular expressions cannot express.
A regex-based approach would require a hand-built combinator layer on top —
at which point you have essentially written a parser anyway.

### Why I Didn't Start with Recursive Descent

Recursive descent is the natural fit for a grammar this size:
write a function per production rule, call them recursively, return AST nodes.
This is fast, debuggable, and has no dependencies.

I did not start here because of unfamiliarity and complexity (real or perceived).
It felt like a much larger jump to go from zero to something functional with recursive descent than it would be with pyparsing.
I later wrote a recursive descent parser, and it was substantially faster — pyparsing's overhead eventually became the bottleneck, which is what motivated the rewrite. But pyparsing ran in production for a long time while the grammar was still evolving, and the lower barrier to entry was the right tradeoff at that stage.

### Why ANTLR's Build-Time Toolchain Was Too Much

ANTLR is the industrial-strength option.
You write a grammar in a `.g4` file, run a Java tool,
and it generates a Python (or Java, or C#, or…) parser for you.
The generated code handles lexing, parsing, and visitor/listener dispatch.

The barrier for me was the toolchain.
ANTLR requires a Java runtime to generate the parser,
which means a separate build step, a checked-in generated file or a generation script in CI,
and a dependency on `antlr4-python3-runtime` at runtime.
For a side project, that felt like too much ceremony to take on before I had even validated the grammar shape.

### Lark: Right Result, Wrong Tradeoffs for This Project

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

### pyparsing: Grammar in Python, Zero Build Steps

pyparsing is a pure-Python PEG-style library where you build the grammar
by composing Python objects directly in your source file.
There is no separate grammar file and no tooling step:

```python
from pyparsing import (
    CaselessKeyword, Forward, Group, Literal, Optional,
    QuotedString, Regex, ZeroOrMore, one_of,
)

op       = one_of(": = != >= <= > <")
arith_op = one_of("+ - * /")
lparen   = Literal("(").suppress()
rparen   = Literal(")").suppress()

quoted_string = (
    QuotedString('"', esc_char="\\") | QuotedString("'", esc_char="\\")
).set_parse_action(lambda t: ("quoted", t[0]))

regex_pattern = QuotedString(
    "/", esc_char="\\", unquote_results=False, convert_whitespace_escapes=False
).set_parse_action(lambda t: ("regex", t[0][1:-1].replace("\\/", "/")))

word   = Regex(r"[a-zA-Z_][a-zA-Z0-9_-]*[a-zA-Z0-9_]|[a-zA-Z_]")
number = (
    Regex(r"\b\d+\.\d*\b").set_parse_action(lambda t: float(t[0]))
    | Regex(r"\b\d+\b").set_parse_action(lambda t: int(t[0]))
)

# each attribute class maps to a different set of column names and SQL generation
numeric_attr = create_attribute_parser(ParserClass.NUMERIC)  # cmc, power, toughness, loyalty
text_attr    = create_attribute_parser(ParserClass.TEXT)     # oracle, name, type, artist, ...
mana_attr    = create_attribute_parser(ParserClass.MANA)
color_attr   = create_attribute_parser(ParserClass.COLOR)
# rarity, legality, date, year follow the same pattern

expr = Forward()

# arithmetic: both sides of a comparison can be expressions over numeric attrs
arith_term = numeric_attr | number | (lparen + expr + rparen)
arith_expr = Forward()
arith_expr <<= arith_term + arith_op + arith_term + ZeroOrMore(arith_op + arith_term)
arith_expr.set_parse_action(make_chained_arithmetic)

numeric_cmp = (arith_expr | numeric_attr | number) + op + (arith_expr | numeric_attr | number)
numeric_cmp.set_parse_action(make_binary_operator_node)

text_cmp = text_attr + op + (regex_pattern | quoted_string | word)
text_cmp.set_parse_action(make_binary_operator_node)

condition = numeric_cmp | text_cmp | mana_cmp | color_cmp  # one branch per attr class

# boolean structure
AND = CaselessKeyword("AND")
OR  = CaselessKeyword("OR")
NOT = Literal("-")

factor   = Optional(NOT) + (condition | Group(lparen + expr + rparen))
factor.set_parse_action(handle_negation)

and_expr = factor + ZeroOrMore(AND + factor)
and_expr.set_parse_action(lambda t: AndNode(t[0::2]) if len(t) > 1 else t[0])

expr   <<= and_expr + ZeroOrMore(OR + and_expr)
expr.set_parse_action(lambda t: OrNode(t[0::2]) if len(t) > 1 else t[0])
```

Two non-default options on `regex_pattern` are worth naming. `unquote_results=False` keeps the surrounding `/…/` in the token rather than stripping them — the parse action needs to distinguish a regex from a plain string. `convert_whitespace_escapes=False` is the one that catches you if you miss it: pyparsing's default converts `\b` to a literal backspace before your code ever sees the token, which silently corrupts word-boundary patterns.

The grammar lives in the same codebase as everything else.
Iteration is fast: change a line, run the test, see what breaks.
The library also handles packrat memoization and provides `infixNotation` as a convenience
for grammars with layered precedence — both of which I used.

Like Lark, pyparsing does not natively support implicit AND
(the grammar above requires explicit `AND` between terms).
I handled that with a preprocessing pass,
which turned out to be one of the more interesting problems in the project.

## The Decision

Lark would have worked for the grammar as written. The 35-line Earley grammar passes the same 121-query corpus, and for a search DSL with short queries the O(n³) parse time is acceptable in practice, if not desirable in principle.

pyparsing's advantage is that the grammar is Python. That sounds like a style preference, but it has a concrete consequence: grammar components can be generated programmatically. The attribute parsers in this project are built with `create_attribute_parser(ParserClass.NUMERIC)` — a function that takes a class enum and returns a parser component wired to the right column names and SQL generation logic:

```python
def create_attribute_parser(parser_class: ParserClass) -> ParserElement:
    aliases = {alias for field in PARSER_CLASS_TO_FIELD_INFOS[parser_class]
               for alias in field.search_aliases}
    parser = make_regex_pattern(aliases)  # matches "cmc", "power", "toughness", ...
    parser.set_parse_action(
        lambda tokens: CardAttributeNode(tokens[0].lower(), parser_class)
    )
    return parser
```

One call produces a parser that matches any alias for that field class and returns a typed AST node. Doing that in a grammar string would require template substitution to generate the alternation and a separate transformer method to produce the node — one complexity replaced with another.

The other consequence is that parse actions sit next to the rules they transform. When a production changes, the transformation changes on the adjacent line. With Lark's tree transformer pattern, the grammar and the transformation live in separate places — workable, but one more indirection to track as the grammar evolves.

The next post covers what pyparsing required in practice: implicit AND injection, query balancing in two languages, and the edge cases that showed up along the way.
