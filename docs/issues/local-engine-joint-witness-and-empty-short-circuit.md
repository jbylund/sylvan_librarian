# Joint Witnesses, and Short-Circuiting a Proven-Empty Query

Parked, not scheduled — filed from a Round 63 observation so it isn't lost. Two connected ideas: one
is small and shippable on its own, the other reframes a question the cost model currently answers by
estimate.

## The observation

Round 63 Part 2 tried to fold a disjointness proof's card/artwork counts as exact zeros and had to
back it out: `result.card` is consumed as the **domain a materializing plan walks**, so proving the
answer empty priced `border:white border:black`'s `GatheredScan` at ~0 against a realized
`cards_visited` of 2,059. See the ledger's Round 63 section.

But backing it out only avoided mispricing a scan we should not be running at all. `leaves_are_disjoint`
is a **proof**, not an estimate. When it fires we already know the result: nothing. The cost-model
question ("how expensive is scanning to discover emptiness") is the wrong question — the right one is
why any plan runs.

**The shippable piece: a proven-empty query should short-circuit to an empty result**, with no plan
selected, no acquire, no scan. Today `pair_bounded_min` returns 0 and `covered.flags.fill(true)`, and
the router then prices plans for a query whose answer is already known. This is a new route, not an
estimator change, so it sits outside the And-arm arc and is verified differently: the check is that
result rows and `result_total` are identical while `plans` shows nothing ran.

Watch two things. Paging and `offset` must still behave (an empty result with a non-zero offset is
still empty, but the response shape has to match what a real plan would have produced), and `explain`
/`explain_analyze` must keep reporting something coherent — a short-circuit that makes the diagnostics
lie about what would have happened would cost more than it saves.

## The frame: when must printings be walked?

The reason a proof of emptiness is possible at all is the same reason some queries need a per-card
printing walk and others don't. A predicate is either card-level (every printing of a card agrees) or
printing-level (printings of one card can disagree — border, rarity, legality, date/year, price,
collector number). The question a plan actually faces is whether a single printing can be found that
satisfies every printing-level predicate **simultaneously** — a *joint witness*.

Three cases, and they are exhaustive:

1. **0-1 printing-level predicates.** No joint-witness risk. One predicate cannot conflict with
   itself, so any matching printing is a witness and there is nothing to verify across printings —
   the card-level answer settles it.
2. **2+ printing-level predicates.** A witness must be found, so printings within each candidate card
   have to be walked until one satisfies all of them together. Card-level counts genuinely cannot
   answer this: a card with a white-bordered printing and a separate black-bordered printing satisfies
   each predicate but has no joint witness.
3. **2+ printing-level predicates that are provably contradictory.** A joint witness is *impossible*.
   No visit, no walk, no plan — case 3 is exactly the short-circuit above.

Case 1 vs case 2 is the interesting part, because it is a **structural** property of the query that
the cost model currently pays for by estimate. `scan_units`/`eval_domain` exist to price the walk; a
query in case 1 needs no walk at all, and knowing that from the filter shape is cheaper and exact.
Whether the executor already exploits this is unverified — check before designing anything.

## Why this connects to the estimator arc

The recurring failure in Rounds 61-63 is a number that is exactly right for one question being fed to
a consumer asking a different one. This taxonomy names the two questions that keep getting conflated:

- *How many cards match?* — an answer cardinality.
- *How many cards must be visited to establish that?* — a witness-search cost.

They coincide in case 1 and diverge in case 2, which is precisely the population where `est.candidate`
and `est.result` part company. So this may be the honest replacement for the
`printing_tightened`/`domain_cards` machinery rather than another patch on it: the domain is not "the
answer, unless something tightened" but "the candidate set the witness search must walk", which is a
function of how many printing-level predicates there are.

## Before doing any of it

- Confirm case 3 is reachable often enough to matter. Round 63's survey saw `leaves_are_disjoint` fire
  40 times in 1,410 traced rows, but the query sampler over-generates contradictions relative to real
  traffic — check against real logs, not the sampler.
- Confirm the executor does not already skip the walk in case 1.
- The short-circuit (case 3) is independently shippable and does not depend on the case 1/2 work.

## Related

- [local-engine-gathered-scan-card-printing-varying-depth.md](local-engine-gathered-scan-card-printing-varying-depth.md)
  — Round 63's section for the mispricing that prompted this.
- [local-engine-nway-followup-queue.md](local-engine-nway-followup-queue.md) — the standing principle
  "'exact' is not one property: a number can be an exact ANSWER and still be the wrong DOMAIN".
- The existential-plane `all_match` trap is the same hazard one level down: per-printing-varying fields
  reaching `compile_plane` break row selection precisely because a card-level plane cannot express a
  joint witness.
