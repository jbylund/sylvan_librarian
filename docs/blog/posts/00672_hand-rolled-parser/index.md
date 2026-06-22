---
title: "Hand-Rolling a Recursive Descent Parser for 49× Speedup"
date: 2027-03-27
publishDate: 2027-03-27
tags: ["parser", "python", "performance"]
summary: "pyparsing's backtracking was the latency ceiling. How we identified it, built a hand-written recursive-descent parser with pyparsing as a live parity check, and caught 22 edge cases along the way."
---

## Identifying the bottleneck


## The plan: parity-first rewrite


## Recursive descent basics


## Keeping pyparsing as a live comparator


## The 22 parity failures


## Results: 49× throughput


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
