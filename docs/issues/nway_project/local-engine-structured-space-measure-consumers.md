# Stop flattening the estimate's two channels at consumer boundaries

`SpaceMeasure` carries two independent answers per space — `guaranteed` (the tightest PROVEN upper bound on the true count) and `estimate` (the best GUESS, documented as possibly undershooting). Every consumer boundary below the estimator flattens them to one scalar via `best() = min(estimate, guaranteed)`, and from that point on nothing downstream can tell which channel a number came from.

The rule for reading them correctly is real, correct, and written down at length — "soundness consumers read `guaranteed`, accuracy consumers read `best()`", `lib.rs:9378`. It is also **prose, not type**. Every call site has to be independently disciplined, forever, and the type offers no help. This doc proposes making it mechanical.

## The evidence that this is a defect class, not a style preference

Three findings, all from 2026-09-05, all the same root cause.

**1. `best()` reports a zero that no mechanism proved.** Measuring a proposed `total == 0` fastpath over 8,000 uniform-sampled queries found 4 where the acquire reported `matches == 0` and the executor returned rows. Tracing all four: `guaranteed` held every time, and the zero came entirely from `printing_estimate`.

| query | true | `card_guaranteed` | `printing_guaranteed` | `printing_estimate` | `matches` |
|---|---|---|---|---|---|
| `t:ferret usd>=0.29 usd<=14.62` | 1 | 1 | 1 | 0 | 0 |
| `set:drb pow>6` | 3 | 15 | 15 | 0 | 0 |
| `c:bu t:whale` | 1 | 17 | 35 | 0 | 0 |
| `set:phel cn<11` | 1 | 5 | 5 | 0 | 0 |

The estimator is behaving exactly as designed — `op=min_fold, mechanism=None`, an independence product flooring to zero, which the admission rule correctly bars from writing `guaranteed`. Round 59 has no hole. The damage is done by the flattening: a sound bound and an unsound guess become one number, and a consumer that needed the bound silently gets the guess.

**2. An unproven zero collapses all three spaces.** `matches` normally follows `unique=` correctly (`f:modern border:white` reports 978 / 3,117 / 1,501 per mode). On `c:bu t:whale` it reports **0 in all three modes**, while `card` best is 17 and `artwork` best is 19. The inference "no printings ⟹ no cards and no artworks" is sound in truth — it is the same cross-space argument that makes an empty-page fastpath safe — but it is being applied to the estimate channel, where the zero is not proven. A consumer that could see the channel could not have made this mistake.

**3. A known latent soundness read, parked because it could not be made safe locally.** `narrow_floor` reads `best()` and feeds it into the `guaranteed` channel (`lib.rs:12709-12710`). This is already on the queue as its own item, deferred with the note that it "stays latent and separate" — it is not currently biting the card channel at roots (0 of 27,459 measured). It is latent *because* of a numeric coincidence, not because of a barrier. Under the change proposed here it stops being expressible.

Finding 1 is a wrong-answer bug waiting for a consumer. Finding 2 is a live one. Finding 3 is one held off by luck.

## The end state

Consumers take the structured measure. The two reads get names that say which consumer they are, so intent is greppable and a review can see a soundness site reading a guess:

- `proven() -> Option<usize>` — the `guaranteed` channel. `None` means "no mechanism proved a bound", never zero. The only read licensed to authorise a short-circuit, an exactness claim, or anything written back into `guaranteed`.
- `routing_cardinality() -> Option<usize>` — today's `min(estimate, guaranteed)`. Clamping a guess to a proven ceiling is correct and stays in one place; open-coding this min at call sites is how Round 55's bug (a guess lowering a proven bound) happened, so the operation is kept, not deleted.

`best()` itself goes away as a name. The rename is the point: it reads like a default, and a default is what a soundness consumer reaches for without thinking.

The boundary that matters most is `mk_plan_feats` (`lib.rs:17216`), where each acquire branch passes `matches: u32` and the channel distinction is destroyed for the whole routing layer. `PlanFeatures.matches` is a bare `u32` (`cost.rs:91`).

## What this does NOT claim

It does not claim an estimate of 0 is wrong. It is a legitimate guess; truth may be 0, and routing legitimately runs on guesses. What is wrong is a consumer treating an *expected* zero as a *certain* one — cost terms scale on `matches`, so a predicted 0 prices a plan at nearly free, it wins the argmin, and then does real work. Only a `guaranteed` zero licenses assuming no work. Fixing the plumbing does not fix the pricing, and that is a separate item.

## `None` is the same defect one type up, and domain-seeding closes it

`proven()` returning `Option<usize>` reproduces the original hazard in the type that was supposed to fix it: `None` means "no mechanism proved a bound", a careless consumer reads it as zero, and we are back to a wrong answer. The fix is queue item #7 — seed every space with the domain size rather than `UNKNOWN`. The corpus total is a true upper bound that needs no specific proof, so a space starts at `{ guaranteed: n_cards, estimate: n_cards }` and only ever tightens. `guaranteed` becomes total, `proven()` returns `usize`, and `proven() == 0` is unambiguous.

Item #7 also records what `None` really overloads today — a genuine unknown, a not-applicable (a printing-only mechanism has no card opinion), and a structural proxy for "did a trusted source produce this". Seeding collapses the first two into a number and forces the third to be stated explicitly, which is the same move stage 1 makes for the channel. Round 60 measured how normal absence is: **41,838 of 147,660** tree nodes have `printing_guaranteed` absent while `printing` is present.

**What seeding does NOT do**, and the doc should not be read as claiming otherwise:

- **It does not fix the four lies.** Those are `best() = min(0, guaranteed)`; a seeded bound leaves that min at zero. Only reading the proven channel fixes them.
- **It does not fix `narrow_floor`'s laundering.** `range_too_broad_to_narrow` discards a seeded full-domain child before the `min`, so the laundering path is untouched and item #2 is required regardless.
- **Only ONE gate genuinely breaks under it** — the `is_and && card.guaranteed.is_some()` narrowing exemption, which becomes unconditionally true. The two card folds are no-ops under seeding (`x <= n_cards` already) and `card_invariant_domain_exact` is a value test that survives.

**The real cost is verification, and it is the reason to sequence carefully.** Rounds 58/59/60 were each verified by byte-identical survey output, the strongest guard this arc has, and seeding makes that unavailable by construction — those 41,838 absences become values, so `and_trace` diffs are non-empty on purpose. That is weaker evidence for a change whose entire point is that it changes nothing, which is why the explicit-flag half goes first, while byte-identical verification is still available.

## `best()` is two jobs, and both can be retired rather than renamed

Measured 2026-09-05 over the survey's 32,745 traced queries, every node in every tree:

| space | both channels present | `estimate > guaranteed` | share |
|---|---|---|---|
| card | 57,282 | **0** | 0.00% |
| artwork | 57,264 | **0** | 0.00% |
| printing | 122,124 | **6,888** | **5.64%** |

`estimate <= guaranteed` already holds in two of the three spaces. Where it fails, the estimate is provably wrong — `pow>=2 pow<=2` reads `guaranteed=13,388` against `estimate=24,351`, a guess 82% above a proven ceiling. So `best()` is doing two separable jobs:

1. **Clamping the guess to the proven ceiling** — those 6,888 printing nodes. Retired by enforcing `estimate <= guaranteed` in the mutators. **Behaviour-neutral for every current consumer**, since they read `min(e, g)`, which already equals `g` exactly there.
2. **Falling back when a channel is absent** — 8,007 printing nodes carry only `guaranteed`, 17,604 only `estimate`, and card has 33,222 guaranteed-only. Retired by domain-seeding, which makes both channels total.

With both done, `best()` is not renamed but **deleted**: it becomes identically `.estimate`, and consumers read `.estimate` for accuracy or `.guaranteed` for soundness with no third accessor. That also dissolves `SpaceMeasure::add`'s documented asymmetry, which today must sum `best()`s rather than `estimate`s precisely because the two can diverge.

**The cost, stated plainly: clamping at write time throws away the raw guess.** On those 6,888 nodes we lose the ability to see that the estimator overshot by 82%, which is the signal estimator-calibration work reads. That is the same lossiness `best()` is being retired for, relocated to the write. The alternative is to store the raw estimate and keep a clamping read, which is `best()` again under a better name. Taking the clamp means the diagnostic trace, not the stored value, becomes the place estimator error is preserved.

## Staging

Each stage is separately landable and separately measured.

**STATUS as of 2026-09-09.** Stages 0, 1 and 4 are landed, plus a rename that was not in the original list. Stage 2 left this arc. Stages 3 and 5 remain, and 3 is much smaller than written below.

| stage | state |
|---|---|
| 0 — explicit exact-card-source flag | **landed** (`card_proven`), byte-identical |
| 1 — enforce `estimate <= guaranteed` | **landed**, delta confined to the predicted 6,888 trace nodes |
| 1½ — rename `best()`, add `proven()`, audit all 16 read sites | **landed**, byte-identical; not in the original plan |
| 2 — seed the domain | **left the arc** — re-filed as queue item #7, a latency item gated on the measured 272→176 byte shrink. Consequence: `best()` is renamed, not deleted, which was the accepted trade |
| 3 — carry both channels to the routing boundary | **open, and re-scoped much smaller — see below** |
| 4 — stop an unproven zero collapsing all three spaces | **landed** as a guard, not as unexpressibility — see below |
| 5 — `narrow_floor` | **open**, fully designed as queue item #8 |

### Stage 3, re-scoped: one bool, not a widened struct

The original plan widened everything `mk_plan_feats` accepts. The call-site audit undercut that: every consumer downstream of the routing boundary is an ACCURACY consumer, and for those the flattening to one scalar is correct — the cost model prices work and wants the best available number.

There is exactly one exception, and it is the whole of stage 3's remaining justification. `cost.rs:1077` reads `f.matches == 0` as a STRUCTURAL fact:

```rust
if stream_runs_small_gather(f) || f.matches == 0 || u64::from(f.offset) >= u64::from(f.matches) {
```

Post-stage-4 that is sound, because a zero can now only come from the proven channel. But it is sound by a distant invariant rather than by the type, which is the situation this doc exists to end. The cheap fix is to carry Tier 1's existing `provably_empty` flag into `PlanFeatures` and branch on that — the flag is already computed in every acquire branch, so this is one field and one predicate, not a widening. (The `offset >= matches` clause beside it is pricing, not answering, and a guess is fine there.)

### Stage 4, landed as a guard rather than as unexpressibility

`raise_unproven_zero_estimate` plus `Candidate::BoundedEstimate` fixed the collapse and it is tested. But the DERIVATION is unchanged: `est_cards_before_and_arm` is still `calibrated_balls_into_bins(printing_matches, n_cards)`, so card is still derived FROM printing and the collapse is prevented rather than made impossible. Closing that properly needs a card estimator that does not derive from printing, and `est_cards`' own comment records why adopting `est.result.card` outright cannot be it — that regressed `id:ruw usd:0.50 cmc>=2` by two orders of magnitude, because the And-arm mechanism can cover a subset of children and be blind to a restrictive residual. Treat the unexpressibility goal as needing its own estimator round, not as a loose end here.

0. **The explicit exact-card-source flag** (item #7's own prescribed first commit). Replaces the one genuine PRESENCE test, `is_and && card.guaranteed.is_some()`, with a signal recorded where the structure happens. Behaviour-neutral and byte-identical-verifiable, and it must land while that guard is still available.
1. **Enforce `estimate <= guaranteed` in the mutators.** Retires `best()`'s clamping job. Expected byte-identical on every value consumer — they already read `min(e, g)` — with the delta confined to the trace's `{space}_estimate` keys on the 6,888 nodes, which is precisely where it should show up and nowhere else. Same byte-identity guard as stage 0.
2. **Seed the domain** (item #7 proper). Retires `best()`'s fallback job; both channels become total. Lands on a codebase where no consumer reads presence any more, so it is provably inert. Verification is necessarily weaker here — semantic scalars plus an explicit diff of the one behavioural site — which is exactly why stages 0 and 1 go first.
   - With 1 and 2 both done, **delete `best()`**: it is now identically `.estimate`. Each of the 23 `lib.rs` call sites becomes `.estimate` or `.guaranteed` according to which consumer it is, and the classification is forced by the code rather than by a prose contract. Retiring `SpaceMeasure::add`'s `best()`-summing asymmetry belongs here too.
3. **Carry the emptiness PROOF to the routing boundary** (re-scoped, see above). Still a hot path, so it needs a paired A/B isolating this change, per `.claude/rules/benchmark-methodology-review.md` — one added `bool` is cheap but not free, and a measurement of a nearby change does not cover it.
4. **Stop an unproven zero collapsing all three spaces.** Landed as a guard; see above for why the stronger form is its own round.
5. **Revisit `narrow_floor`.** Finding 3, which by then has no way to spell itself — though note seeding alone does not reach it, so item #2's own fix is still required.

The site of finding 2 was located in the end rather than left to evaporate: card is derived from printing through `calibrated_balls_into_bins`, and the And arm's card bound is applied only as a `.min()` clamp, which can lower but never raise. That is why a printing estimate of zero took card and artwork with it.

## Measurement

`scripts/bench_empty_page_provable.py` reports the lie count directly and is the regression test for stage 1 — it must read 0 lies where it currently reads 4. Raw output for the current build: [measurements/2026-09-05-empty-page-provable-uniform.txt](measurements/2026-09-05-empty-page-provable-uniform.txt).

Stage 1's zero-delta guard is `scripts/nway_estimate_truth_survey.py --compare` on `predicted_matches` / `picked_plan` / `and_mechanism` / `count_source`.
