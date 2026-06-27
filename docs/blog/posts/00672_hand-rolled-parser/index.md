---
title: "Hand-Rolling a Recursive Descent Parser for 49× Speedup"
date: 2027-04-10
publishDate: 2027-04-10
tags: ["parser", "python", "performance"]
summary: "pyparsing's backtracking was the latency ceiling (~3.2k parses/sec). How we identified it, built a hand-written recursive-descent parser with pyparsing as a live parity check, and caught 22 edge cases along the way."
---

The `TimingMiddleware` spans in the request handler showed the parser taking 0.3–2ms per query. For a search endpoint with a 5ms response time target, the parser was consuming 10–40% of that budget before a single database query ran. The previous post on choosing pyparsing noted that recursive descent was the right architecture but felt like a larger jump than it actually was. This post is about taking that jump — and what the parity testing found along the way.

## Why pyparsing Tops Out at ~3.2k Parses/Second

pyparsing builds a grammar by composing Python objects — `ZeroOrMore`, `Forward`, `Group` — that evaluate at parse time by trying each alternative and backtracking on failure. The architecture is elegant and iteration is fast (change a line, run the test), but every parse involves creating and discarding a large number of Python objects: `ParseResults` lists, match results, exception objects when an alternative fails.

For this grammar, pyparsing also runs in two passes. The first pass is a tokenizer that detects where adjacent terms should be separated by implicit AND — a pyparsing grammar in its own right. The second pass is the full parse. Both passes execute on every query, with only the grammar objects themselves cached across calls. Packrat caching memoizes `(position, rule)` pairs within a single call, which helps on simple queries (it cuts redundant re-parses within the same expression) but barely moves the needle on complex ones. Deeply nested parentheses hit many unique positions; packrat cannot help with that.

The cold parse times illustrate the problem. A simple `field:value` query takes about 0.2ms; a query with deep nesting takes 2.2ms — a 10× range depending on structure. At warm throughput, simple queries reach ~10k/s, but complex nested queries bottom out at ~544/s. The geometric mean across a diverse query set is roughly 3.2k/s. That is the ceiling.

## Parity First, Speed Second

The rewrite strategy was: do not touch the SQL generation layer, and keep pyparsing running as a live parity check. The AST node types in `nodes.py` and `card_query_nodes.py` are the contract between the parser and everything downstream. Both parsers produce the same node types and call the same `to_sql()` methods. If the hand-rolled parser produces the same AST, it produces the same SQL — and the whole downstream stack can be left untouched.

The parity test is `test_parser_parity.py`. It runs every query in the corpus through both parsers and asserts identical SQL output (or that both raise an exception):

```python
def test_both_parsers_agree(query: str) -> None:
    """Both parsers must produce identical SQL for every query in TESTCASES."""
    try:
        hand_result = generate_sql_query(parse_scryfall_query(query))
    except Exception as exc:
        hand_exc = exc
    try:
        pyp_result = generate_sql_query(parse_search_query(query))
    except Exception as exc:
        pyp_exc = exc

    assert (hand_exc is None) == (pyp_exc is None), ...
    if hand_result is not None:
        assert hand_result == pyp_result, ...
```

The parity test is a falsifiable claim, not a description. When it passes, both parsers agree on every query in the corpus. When it fails, the error message shows the exact SQL divergence and the query that triggered it.

There was a wrinkle that proved the value of the approach before the hand-rolled parser was even written. Setting up the parity suite to run against the corpus immediately found bugs in the existing pyparsing grammar — AND/OR precedence was wrong, a parenthesized arithmetic LHS crashed with `TypeError`, and negated parenthesized expressions were rejected. These were not hypothetical edge cases: they were queries already in the test corpus that were producing wrong results or exceptions in production. Running two independent parsers against the same corpus turned out to be more effective than running one parser against its own tests.

## Recursive Descent Basics

A recursive descent parser has one function per grammar rule. The grammar has four levels:

```
expr     → and_expr (OR and_expr)*
and_expr → factor (AND? factor)*    # AND? handles implicit AND
factor   → (-) primary | primary
primary  → (group) | !name | "quoted" | word | number | mana
```

The parser walks the token stream left to right. Each function peeks at the current token to decide which branch to take, consumes tokens as it goes, and returns an AST node. There is no backtracking: each decision point is resolved by the current token and one or two tokens of lookahead.

The token stream itself is produced by a hand-rolled lexer. Every token carries a `space_before` flag — whether there was whitespace before it in the input. This flag is how the parser disambiguates `-` as negation (space before, like `t:creature -t:instant`) from arithmetic subtraction (no space, like `power-toughness`). The two-pass pyparsing pipeline handled implicit AND detection in its preprocessing step; the hand-rolled parser handles it inline by checking `_can_start_factor()` in the `and_expr` loop.

The `parse_and_expr` function is the core of implicit AND:

```python
def parse_and_expr(self) -> QueryNode:
    operands = [self.parse_factor()]
    while self._can_start_factor():
        if self.peek().type == TT.WORD and self.peek().value.upper() == "AND":
            self.consume()
        operands.append(self.parse_factor())
    return operands[0] if len(operands) == 1 else AndNode(operands)
```

If `_can_start_factor()` returns True and the next token is not the keyword `AND`, the parser inserts an implicit AND. If it is `AND`, the explicit keyword is consumed and the next factor is parsed. The logic is identical for both cases; the implicit version just skips the consume step.

## The 22 Parity Failures

The parity suite started with 22 failing cases. All were fixed; the fixes landed across both parsers. Four categories:

**AND/OR precedence in pyparsing.** The query `a OR b AND c` was evaluated left-to-right as `(a OR b) AND c` instead of `a OR (b AND c)`. AND should bind more tightly than OR — the same rule as arithmetic `+` and `*`. The fix was splitting the pyparsing grammar into two levels (`and_expr = factor + ZeroOrMore(AND + factor)`, then `expr = and_expr + ZeroOrMore(OR + and_expr)`), matching the structure the hand-rolled parser already had.

**AST flattening divergence.** Queries like `(foo bar) baz` produced different tree shapes. pyparsing was flattening `AndNode(a, b, c)` into an n-ary node; the hand-rolled parser kept them nested as `AndNode(AndNode(a, b), c)`. Neither representation is wrong, but they produce different SQL. The fix was moving `flatten_nested_operations` from `pyparsing_based.py` into `nodes.py` and calling it in both parsers, so both always produce canonical n-ary AND/OR trees.

**pyparsing crash on parenthesized arithmetic LHS.** Queries like `(2*power)-1>3` or `(power+toughness)-cmc>0` raised `TypeError: 'str' object is not callable` when `.to_sql()` was called. The cause: `arithmetic_term` used `Group(lparen + expr + rparen)`, which wrapped the inner parse result in a `ParseResults` sublist. `create_value_node` did not recognize a `ParseResults` as a `QueryNode`, so a raw `ParseResults` ended up in the AST. Removing the `Group` wrapper fixed it — since `lparen` and `rparen` are suppressed, `(lparen + expr + rparen)` yields exactly one `QueryNode` as intended.

**Hand-rolled parser gaps — all around the negation/subtraction disambiguation.** Three variants:

- A space before `-` was always treated as an AND boundary, so `power - cmc` (arithmetic with spaces) was misread. Fixed by adding `_spaced_sub_tail`, which triggers when `-` has `space_before=True` and the following token is a numeric term.
- `parse_num_expr_value` consumed ` -cmc` (space before MINUS, no space after) as arithmetic subtraction, leaving `> 0` unmatched. Fixed by breaking out of `parse_num_expr_value` when the MINUS has `space_before=True` and the following token has `space_before=False`.
- Spaced arithmetic between paren groups, like `(power + 1) - (cmc - 1) > 0`, failed because `_spaced_sub_tail` only handled `-`. Replaced with `_spaced_arith_tail`, which handles all four operators with spaces, keeping the `space_before` guard on `-` only.

The negation/subtraction disambiguation is the only part of this grammar that genuinely requires context. Everything else resolves with one or two tokens of lookahead.

## Results: 49× Throughput

Benchmark methodology: each query ran for 2 seconds warm after one cold parse. Numbers are from `ignored/bench_parsers.py`, measured 2026-06-17 on a MacBook Pro M2. The pyparsing numbers include both passes (preprocessing + full parse). The hand-rolled parser is single-pass.

| Query | pyparsing warm (/s) | hand-rolled warm (/s) | speedup |
|---|---|---|---|
| bare word | 7,242 | 410,492 | 56.7× |
| field:value | 9,966 | 318,782 | 32.0× |
| parens | 1,198 | 85,879 | 71.7× |
| exact name | 4,612 | 444,285 | 96.3× |
| nested parens | 791 | 60,302 | 76.2× |
| arithmetic comparison | 3,674 | 204,520 | 55.7× |
| deep nesting | 544 | 38,427 | 70.7× |

The speedup range is 22× (two-field queries) to 96× (exact name search). The geometric mean across the full 23-query benchmark set is roughly 49×, consistent with the commit message claim of "158k vs 3.2k parses/sec." The table above is a representative sample; the full 23-query dataset is in `ignored/bench_parsers.py`.

The cold parse numbers close the loop on the opening motivation. The `TimingMiddleware` spans that showed 0.3–2ms came from pyparsing's cold performance: 0.2ms for simple queries, 1.7ms for a paren group, 2.2ms for deep nesting. The hand-rolled parser runs 0.01–0.05ms cold regardless of query structure — a flat cost with no nesting penalty, because there is no memoization table to populate and no `ParseResults` objects to allocate per recursion level.

## The Tradeoffs

The hand-rolled parser is 29k bytes of Python and needs to be maintained. Any grammar change — adding a new operator, a new attribute type, a new precedence rule — requires updating the hand-rolled parser alongside the pyparsing grammar. That cost is bounded because both parsers share the same AST node types and the parity suite will immediately flag any divergence.

The parity suite also has a scope limit: it covers the query corpus, not all possible inputs. If a grammar extension introduces behavior for queries not in the corpus, neither parser is constrained by the parity test. New query types should go into the corpus first.

The pyparsing implementation is not being removed. It runs as the comparison target in `test_parser_parity.py` and remains the canonical reference for what the grammar should do. The two parsers acting as checks on each other is worth more than the simpler maintenance story of having one.

For queries that are already cached — which is most real-world traffic after warmup — the parser is not on the critical path at all. The 49× improvement matters at startup, for cache-miss queries, and for any load pattern that generates novel queries at high rate. Whether the speedup is visible in end-to-end latency depends on how much of the request budget the parser was consuming to begin with.

## What the Rewrite Was Really For

The 49× throughput improvement is real, and the `TimingMiddleware` spans now show the parser as a rounding error rather than a budget item. But the more durable outcome was the parity suite. It caught 22 edge cases — three of which were bugs in the pyparsing grammar that had been running in production undetected, found before a single line of the hand-rolled parser was written. The rewrite was the occasion; the test was the find.

<!-- BENCHMARK CONTEXT (from ignored/bench_parsers.py, measured 2026-06-17)

Benchmark ran each query for 2s warm after one cold parse. Numbers:

Query                                          cold pyp(ms)  warm pyp(/s)  warm hand(/s)  speedup
simple: bare word                                      0.23          7242        410492     56.7x
simple: field:value                                    0.20          9966        318782     32.0x
simple: two fields                                     0.33          7296        165330     22.7x
simple: numeric comparison                             0.33          9819        337328     34.4x
simple: negation                                       0.31          4917        272230     55.4x
moderate: implicit AND x3                              0.37          3984        109974     27.6x
moderate: explicit AND                                 0.28          5546        148272     26.7x
moderate: explicit OR                                  0.54          5503        146901     26.7x
moderate: parens                                       1.72          1198         85879     71.7x
moderate: quoted string                                0.42          5044        293996     58.3x
moderate: exact name                                   0.42          4612        444285     96.3x
moderate: regex                                        0.54          3029        161878     53.4x
moderate: mana cost                                    0.55          2872        158272     55.1x
moderate: negation+attr                                0.31          4116        149170     36.2x
moderate: hyphenated                                   0.30          5647        141734     25.1x
complex: nested parens                                 1.62           791         60302     76.2x
complex: arithmetic                                    0.55          3674        204520     55.7x
complex: arithmetic both sides                         0.42          3246        142669     44.0x
complex: arith+bool                                    0.94          1302         71680     55.1x
complex: deep nesting                                  2.17           544         38427     70.7x
complex: mixed ops                                     1.41           826         55826     67.6x
complex: OR chain                                      1.13          1408         74440     52.9x
complex: AND+arith+quoted                              0.64          1843         82162     44.6x

Pipeline note: the pyparsing numbers reflect the full two-pass pipeline —
`preprocess_implicit_and` (a pyparsing tokenizer pass) followed by the main
`parse_string` call. Both passes run on every query; only the tokenizer grammar
object itself is cached across calls. The hand-rolled parser is single-pass with
no preprocessing step.

Key observations:
- Cold parse times: 0.2ms (simple) to 2.2ms (deep nested parens) for pyparsing.
  Hand-rolled is 0.01-0.05ms cold across the board.
- Warm pyparsing: packrat helps for simple queries (~2-3x vs cold) but barely
  moves the needle for complex ones — packrat memoizes (position, rule) pairs
  within a call, so deeply nested queries still hit many unique positions.
- Parens are the expensive case for pyparsing: one paren group costs ~1.4ms cold
  vs ~0.2ms for a simple query. Deep nesting hits 2.17ms cold / 544/sec warm.
- Speedup range: 22x (simple two-field) to 96x (exact name search).
- The "49x" headline is roughly the geometric mean across a diverse set.
- The commit message claimed "158k vs 3.2k parses/sec on a diverse query set".
  3.2k is plausible as a geometric mean of our moderate/complex results (544-5500/sec).
  158k is consistent with hand-rolled moderate queries.
- Earlier memory of "10-50ms parse times" likely refers to the first-ever cold parse
  when pyparsing lazily constructs the grammar (happens once per process startup),
  or was total request latency including DB round-trip, not parser time alone.
-->
