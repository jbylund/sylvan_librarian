# Order Verifier Children by Evaluation Cost

Companion to [engine-probe-before-and-skip](./00749-engine-probe-before-and-skip.md),
from the same 2026-07-08 discussion. Unlike that issue, this one's
beneficiaries are the *slow* queries: broad drivers and full scans, where
verification IS the query cost.

## The gap

The narrowing pass already orders And children by cost (`and_child_rank`,
lib.rs ~1998). The verifier does not: `tri` (filter.rs ~719) walks And/Or
children in **written order**, short-circuiting on the first False (And) /
True (Or). So for a broad query like `o:/^{T}:/ t:creature c:g`, whether
each of ~90k cards pays the regex before or after the cheap plane checks
depends on how the user typed it.

The tri accumulation is commutative (False/True dominate; the Null and
PrintingDep flags just OR together), so reordering is semantics-preserving.
The per-printing residual pass inherits the And's child order, so one sort
fixes both phases.

## Cost tiers (grounded in FilterExpr post-bind, filter.rs ~267)

0. True, ExactName, NumericCmp on direct fields, Date/YearCmp, color/type
   mask and plane-backed checks
1. Bind/memoize-resolved id sets — ArtistMatch, FlavorMatch, NameMatch,
   OracleMatch, tag membership (integer binary search per candidate)
2. Unmemoized TextContains (substring scan over oracle/name text)
3. TextRegex on oracle/name (regex engine per candidate; artist/flavor
   regexes are already resolved to id sets at bind and belong to tier 1)

Composite nodes (And/Or/Not) rank as the max of their children. Stable sort
within tier preserves written order → deterministic.

## Where to hook

After `memoize_text_predicates` in run_query (memoization flips a
TextContains from tier 2 to tier 1, so ranking must run after it). A
recursive stable sort of And/Or children by tier; filter trees are tiny, so
per-query cost is noise. Guard with a `CARD_ENGINE_VERIFY_ORDER` env
override (same LazyLock pattern as the calibrated constants) for A/B.

## Expected effect

Savings = (expensive-child eval count) x (rejection rate of the cheap
children that now run first). Concrete: an oracle regex over a ~90k-card
scan at ~1us/card is ~90ms; with `t:creature` evaluated first (~60%
rejection) the regex runs on ~36k cards. Worst case is bounded: cost-only
ordering never adds work — when nothing short-circuits, every child ran
anyway, in a different order.

Honest scoping: memoization already rewrites broad-domain TextContains to
tier 1, so the big wins are confined to (a) regex conjuncts, (b) contains
predicates under the memo gate (mid-size domains), (c) Or chains mixing
tiers. Single-expensive-predicate queries (`o:/regex/` alone) get nothing —
they need index-side attacks (e.g. trigram prefilter on regex literals),
not ordering. Wild-corpus incidence of regex is near zero, but per the
sampling caveat in the probe-before-skip issue, linked-URL traffic
under-samples power users — and regex queries are the single worst
per-query cost the engine has, so this is tail insurance more than geomean.

## Future refinement

Within tier 1, memoized sets know their own size (ids.len()) — sorting
tier-1 And children by ascending set size is free, data-driven selectivity
ordering. Tiers 2/3 have no cheap selectivity estimate; don't guess.

## Measurement

Reuse the #647 harness: constructed family = broad driver + regex/contains
conjunct written expensive-first vs cheap-first, A/B via the env toggle,
parity on totals (the #641 differential oracle also covers this). The
written-order sensitivity itself is the baseline: today those two spellings
of the same query have different costs; after this change they must not.
