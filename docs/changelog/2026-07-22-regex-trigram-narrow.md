# Regexes narrowed by their guaranteed literal factors (#734)

A regex query used to scan every card — `narrow_rec` had no `TextRegex` arm. Now the engine extracts
the regex's **guaranteed literal factors** (substrings present in every match) via `regex-syntax`'s
HIR, trigram-narrows to a loose candidate set, and lets the walk re-verify with the real regex. Same
"narrow then verify" shape the `TextContains` trigram arm already uses — just with a regex verifier.

Factor extraction is conservative (pass one): a run of literal bytes is a factor only where every
match must contain it. Anything a match can skip — an optional/`*` quantifier, a character class, a
zero-width anchor, an alternation — ends the run. So `draw .* cards?` → `["draw ", " card"]` (the
optional `s` dropped), `^flying$` → `["flying"]` (anchors are zero-width; the literal between them
survives), `dragon$` → `["dragon"]`. Concatenation means all factors must appear, so their candidate
sets are intersected. Bare alternation (`exile|destroy`) and literal-free patterns (`^[aeiou]`, `\d+`)
find no factor and stay a full scan (alternation-union is a deferred follow-up).

Measured on the 97,206-printing corpus (`limit=100`, min of a timed window), totals byte-identical:

| query | unique | before (μs) | after (μs) | speedup |
|---|---|---:|---:|---:|
| `o:/draw .* cards?/` | printing | 1734 | 437 | 3.97× |
| `name:/dragon$/ year:2021` | card | 192 | 61 | 3.14× |
| `o:/^flying$/` | printing | 316 | 132 | 2.4× |
| `o:/draw .* cards?/ color:w` | card | 484 | 200 | 2.42× |

A 1,500-query branch-vs-main survey: **0 total-count mismatches, 0 regressions, 18 wins ≥1.15×**;
tail down p99 821→704 μs (−15%), p100 2011→1564 μs (−22%).

`TextRegex` is ranked as a second-tier `And` source (like ranges): its literal factor may be broad
(`flying`), so the narrow is paid only after cheap plane/posting sources — the `And` early-stop skips
it when a selective sibling (`type:dragon`) already narrowed below the threshold, so
`o:/^flying$/ type:dragon` stays as fast as before rather than paying a wasted broad-trigram pass.

Complements the metacharacter-free `regex → substring` lowering (#735, parser side): that lowers pure
literals to an *exact* `TextContains` (no verify); this narrows the regexes that keep metacharacters.
