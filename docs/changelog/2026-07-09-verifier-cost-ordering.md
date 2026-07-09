# Verifier orders And/Or children by evaluation cost

The narrowing pass has ordered And children by materialization cost since
#637, but the verifier still walked And/Or children in written order. For a
query that full-scans (regex conjuncts, legality/arithmetic partners), whether
each card paid the expensive text predicate before or after the cheap mask
checks depended on how the user typed the query — `o:/draw .* cards?/
f:pauper` and `f:pauper o:/draw .* cards?/` differed by ~2.4×.

`run_query` now sorts And/Or children cheapest-verification-tier-first after
text-predicate memoization (which changes tiers), using a stable sort so
equal-cost children keep written order. Within an And's memoized-set tier,
children refine to ascending set size — a smaller set rejects more per
identical binary-search cost. The tri accumulation is commutative, so
reordering is semantics-preserving; totals are unchanged on every benchmarked
query. `CARD_ENGINE_VERIFY_ORDER=0` restores written order for A/B runs.

See `docs/issues/engine-verifier-cost-ordering.md` for the design discussion
and the PR for measured results.
