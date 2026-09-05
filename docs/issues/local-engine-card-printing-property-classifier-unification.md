# Card-vs-Printing Property Classifiers — Is One Canonical Table Achievable?

## Context

[docs/issues/done/local-engine-gathered-scan-undercosted-arith-existential-and.md](done/local-engine-gathered-scan-undercosted-arith-existential-and.md)
(Rounds 15-16) fixed the router's `cost_plane_nothing_to_verify` (`card_engine/src/lib.rs:9604`) by
deleting a field-identity check (`plane_touches_rarity_or_border`) and routing through
`planes.rs::plane_expr_is_existential` instead — a predicate keyed on *behavior* ("does this plane's
existential semantics force per-printing re-verification under `Mode::Card`") via the family-keyed
`PLANE_BLOCKS`/`ExistentialLeaf`/`needs_printing_verification` table. While confirming no other
router call site needed the same fix, Round 16 found three other classifiers in the crate that
independently reason about roughly the same card-vs-printing-level distinction, each for a different
purpose, each already documenting its own one-line stance on why it disagrees with the canonical
table on legality — but did not chase whether those three (four, including the canonical table
itself) could collapse into one shared table. This doc does that chase.

## The four classifiers, side by side

| # | Function | File:line | Operates over | Composition | Used for |
|---|---|---|---|---|---|
| A | `plane_expr_is_existential` / `needs_printing_verification` / `existential_leaf` / `PLANE_BLOCKS` | `planes.rs:1159,1171,1231,1119` | compiled `PlaneExpr` (post-`compile_plane`) | recursive And/Or/Not walk | Router cost-tier (`cost_plane_nothing_to_verify`, `lib.rs:9604`) and the executor's per-printing row-selection walk (`existential_plane_for`) — the SAME fact drives both, traced end-to-end in Round 16 |
| B | `has_printing_varying_leaf` | `estimator.rs:79` | raw `FilterExpr` | **ANY** (`.any`) | Standalone sound cardinality estimator's AND-lower-bound Bonferroni gate and NOT-branch selection (`estimate_rec`, `compose_and`, `estimator.rs:154-220`) |
| C | `printing_dependent` / `leaf_compares_printing_field` | `filter.rs:820,851` | raw `FilterExpr` | **ALL** (`.all`) | Verify-order heuristic — which And/Or child to evaluate first (`and_child_key`/`or_child_key`, `filter.rs:929`, `1269`) |
| D | `is_broadcast_leaf_shape` / `is_broadcast_composable` | `lib.rs:6781,6798` | raw `FilterExpr` | n/a (leaf-shape allow-list, not a tree walk) | Gates which leaf shapes `PrintingCompose`'s broadcast-card-bits-to-printings build arm supports (`is_printing_composable`, `lib.rs:6867`) |

Classifier D turns out **not to be a fourth independent opinion** — see "Proposed unification" below;
it already reads table A directly. There is also a close cousin of C worth naming up front:
**`touches_printing_field`** (`filter.rs:840`) shares C's exact per-leaf table
(`leaf_compares_printing_field`) but composes with `.any` instead of `.all`, feeding the router's
"is the residual card-invariant" checks (`lib.rs:11873`, `12079`, `12568`). `printing_dependent` (ALL)
and `touches_printing_field` (ANY) are two adapters already reading **one** shared per-leaf table
inside `filter.rs` — this is the ANY/ALL-composition-wrapper pattern the user's framing asks for,
already built, just not yet extended to cover B or A.

### Each classifier's own stated reasoning for special-casing

- **A** (`planes.rs:1132-1158`, `needs_printing_verification`'s doc): "For rarity and border that is
  every leaf — those are printing-varying by nature. For legality it is a per-FORMAT question... a
  legality plane outside that mask is card-invariant." Legality is the one field whose classification
  is *dynamic*, resolved per-store via `divergent_formats`.
- **B** (`estimator.rs:71-78`, `has_printing_varying_leaf`'s doc): "`Legality` is treated as varying
  here (conservative): divergent reprints genuinely vary per-printing (#667), even though
  `printing_dependent` ranks it invariant for its own (common-case) reason."
- **C** (`filter.rs:891-892`, the `Legality` arm's comment): "Divergent-legality cards defer to the
  printing, but they are a rare exception (non-tournament reprints); rank by the common card-level
  case."
- **D** (`lib.rs:6772-6780`, `is_broadcast_leaf_shape`'s doc): "Deliberately narrower than 'anything
  `compile_plane` handles': rarity/legality/border also compile via `compile_plane`, but they are
  EXISTENTIAL card-space facts (∃p: ...), and mixing them into a card-invariant broadcast here would
  reintroduce the #667/#680 shared-witness bug." D's exclusion list is driven by calling A directly
  (`is_broadcast_composable` calls `plane_expr_is_existential`), not by re-deriving the fact.

## The cross-reference table

Verdict key: **C**ard-level, **P**rinting-level, **Dyn** (legality: depends on `divergent_formats`
per format), **N/A** (classifier's domain doesn't include this field at all — see notes).

| Field | A (planes.rs) | B (`has_printing_varying_leaf`) | C (`printing_dependent`) | D (`is_broadcast_leaf_shape`) |
|---|---|---|---|---|
| `cmc` / `power` / `toughness` | C (compiles via `compile_numeric_cmp`, never in `PLANE_BLOCKS`) | C | C | **P is included** (`NumericCmp` arm, `lib.rs:6784`) |
| `color` / `color_identity` / `produced_mana` | C | C | C | **Included** (`ColorCmp` arm) |
| `devotion` | C (compiles, never existential) | C | C | **Included** (`Devotion` arm) |
| `type` (`TypeCmp`) | C (compiles via `compile_plane`, never existential) | C | C | **N/A — not in D's arm list at all** (see below) |
| `rarity` (all values incl. "hi"/special/bonus bucket) | **P**, unconditional | P | C | N/A (D explicitly excludes; has its own native compose arm) |
| `border` (all values incl. "other" bucket) | **P**, unconditional | P | P | N/A (same as rarity) |
| `legality` (any format) | **Dyn** — P iff that format's bit is set in `divergent_formats`, else C | **P, always** (conservative, ignores mask) | **C, always** (common-case, ignores mask) | N/A (native compose arm, reads A directly via `status_plane_bases`) |
| `mana_cost` (`ManaCostCmp`) | N/A (never compiles to a plane) | C | C | N/A (not in D's arm list) |
| `rarity_int` as a NumericCmp *inequality* (not the plane path) | N/A for A (A only sees the compiled plane form; `rarity>=rare` goes through `compile_rarity_cmp`, still lands in `PLANE_BLOCKS`'s rarity blocks) | P | P | N/A |
| `collector_number` | N/A (never a plane) | P | P | N/A |
| `price` (usd/eur/tix) | N/A | P | P | N/A |
| `date` / `year` (`released_at`) | N/A | P | P | N/A |
| `set_code` | N/A | P (`TextExact{SetCode}`/`TextRegex{SetCode}` arm, `estimator.rs:100-103`) | P | N/A |
| `watermark` | N/A | P (same arm) | P | N/A |
| `border` as a bare `TextExact` (not the NumericCmp/rarity path) | N/A for the raw `FilterExpr` (only reachable once compiled) | P (`TextExact{Border}` arm) | P | N/A |
| `artist` | N/A | P | P | N/A |
| `flavor_text` | N/A | P | P | N/A |
| `oracle_text` / `name` (contains/exact) | N/A | C | C | N/A |
| collections: `subtypes`/`keywords`/`otag` | N/A | C | C | N/A |
| collections: `art_tags`/`is_tags`/`frame_data` | N/A | P | P | N/A |
| `loyalty` / `edhrec_rank` | N/A | C | C | N/A |
| `prefer_score` | N/A | P | P | N/A |

### No further disagreements found

Re-verified every row above directly against the two files' live source (`estimator.rs:79-121`,
`filter.rs:851-910`) rather than trusting a first-pass transcription. `prefer_score`
(`NumField::PreferScore`) is in B's `num_varying` list (`estimator.rs:90`, printing-varying) and in
C's `true` block (`filter.rs:864`, printing-varying) — both agree. `set_code`/`watermark` are in B's
`TextExact`/`TextRegex` arm (`estimator.rs:100-103`, printing-varying) and C's identical-looking arm
(`filter.rs:880-885`) — both agree; B and C's `TextExact`/`TextRegex`/`CollectionCmp`/`NumericCmp`
field lists are, in fact, byte-for-byte the same set of fields with the same verdict everywhere
except `Legality`. **Legality is the ONLY disagreement between B and C** — every other field in both
files' leaf tables already matches. This is worth stating plainly since it changes the shape of the
unification story: B and C are not two independently-drifting classifiers that happen to agree most
of the time — they are two copies of what is effectively already one table, differing by exactly one
documented, deliberate row. Unifying them mainly removes the duplication risk (two copies that must be
kept in sync by hand across two files) rather than resolving live disagreement.

## The legality disagreement, resolved

**B** (`has_printing_varying_leaf`) always classifies `Legality` as printing-varying:
> "`Legality` is treated as varying here (conservative): divergent reprints genuinely vary
> per-printing (#667), even though `printing_dependent` ranks it invariant for its own (common-case)
> reason." (`estimator.rs:76-78`)

**C** (`leaf_compares_printing_field`, read by both `printing_dependent` and `touches_printing_field`)
always classifies `Legality` as card-level:
> "Divergent-legality cards defer to the printing, but they are a rare exception (non-tournament
> reprints); rank by the common card-level case." (`filter.rs:891-892`)

**Verdict: deliberate, not a bug — confirmed by tracing consequences, not just reading the comments.**

- **B's over-approximation stays sound.** `has_printing_varying_leaf` feeds two places in
  `estimator.rs`: `compose_and`'s Bonferroni-lower-bound gate (`varying <= 1`, `estimator.rs:211-217`)
  and `estimate_rec`'s `Not` branch selection (`estimator.rs:159-179`). In both, treating an
  actually-card-invariant legality format as printing-varying can only make the bound **looser**, never
  wrong: `compose_and`'s `varying > 1` branch forces `lo = 0`, which is trivially sound regardless of
  whether the extra "varying" child really is; `estimate_rec`'s printing-varying `Not` branch uses
  `finalize(0, ..., n, n)` — a hi of `n` (the loosest possible), never a hi that could be violated by
  the true, tighter answer. The estimator's own hard invariant ("SOUNDNESS is the hard invariant;
  tightness/cheapness are secondary", `estimator.rs:13`) is exactly what this preserves, and
  `fuzz_row_identity_matches_reference` (`tests.rs:2712`) exercises `estimate_cardinality`'s bound
  against thousands of random filter trees per run, including ones containing `Legality` leaves,
  asserting `lo <= true_count <= hi` every time (`tests.rs:2610-2617`) — this would fail loudly if the
  conservative direction were ever wrong, and it passes.
- **C's under-approximation only affects performance, never correctness.** `printing_dependent`/
  `touches_printing_field` only steer which child of an And/Or a verifier tries first
  (`and_child_key`/`or_child_key`) and which residual is deemed "card-invariant" for a router
  fast-path decision. Misclassifying a divergent-format legality leaf as card-level means it might get
  tried first when a genuinely card-settling sibling would have been cheaper to check — a suboptimal
  ORDER, never a wrong verification result, because whichever child runs still evaluates the real
  three-valued `tri()` logic regardless of what order it ran in.
- **Both are correctly scoped to their OWN purpose's error-cost asymmetry.** B must never let a
  looseness bias become an unsoundness — being wrong toward "more printing-varying than reality" is
  free. C must never let a looseness bias become a WRONG ANSWER — being wrong toward "assume the common
  case, order accordingly" is at most a slow path, and the actual value returned never depends on
  `leaf_compares_printing_field`'s answer. These are opposite biases because the two functions have
  opposite failure costs, not because one of them is right and the other wrong.

This is the same conclusion Round 16 stated in passing ("both are DOCUMENTED, ONE-DIRECTION
approximations... not a place where real per-printing work gets silently priced as free") — this doc
traces the actual consequence chain for each (which downstream branch reads the value, and what bound/
behavior it produces) rather than taking that framing on faith.

## Proposed unification

### D is not a fourth table — it already reads A

`is_broadcast_leaf_shape` is a **leaf-shape allow-list**, not a card/printing classifier: it decides
which specific `FilterExpr` SHAPES `PrintingCompose`'s broadcast-build arm has been wired up to accept
(`ColorCmp`, `Devotion`, `NumericCmp` on `cmc`/`power`/`toughness`, and `Not` of those). The actual
card-vs-printing check is `is_broadcast_composable`'s direct call to `plane_expr_is_existential`
(table A) — confirmed by reading `lib.rs:6798-6801`. Where D and A's *field lists* look like they
disagree (`TypeCmp` is card-level per A/B/C, but absent from D's arm list entirely), that is **not** a
classification disagreement — it's an unbuilt compose arm. `TypeCmp` compiles to a non-existential
plane per A (confirmed: `compile_plane`'s `TypeCmp` arm, `planes.rs:1317-1322`, never touches
`PLANE_BLOCKS`), so `is_broadcast_composable(TypeCmp, ..)` would return `true` if it were ever called
— but `is_broadcast_leaf_shape` never routes a `TypeCmp` there, and `is_printing_composable`
(`lib.rs:6818`) has no other arm for `TypeCmp` either, so **`t:goblin`-shaped leaves cannot reach
`PrintingCompose`'s composed-bits path at all today**, card-invariant or not. This is a separate,
narrower gap (missing engineering, not a wrong classification) — worth its own future doc if the win
is real, but out of scope here; D itself needs no unification work, since it already delegates to A.

### A single canonical table is achievable at the DATA level, not at the function-call level

The four (three, net of D) classifiers don't operate over the same tree shape: A walks a compiled
`PlaneExpr` (post `compile_plane`, which has already thrown away original field identity in favor of
plane indices, folded De Morgan through `Not`, and only ever contains the ~9 plane-eligible field
families); B and C walk the raw `FilterExpr` (the full ~25-variant leaf universe, most of which never
reaches a plane at all — price, dates, artist, flavor, set/watermark, non-Ge collections). There is no
single function both a `PlaneExpr` walker and a `FilterExpr` walker could call without also merging
those two representations, which is a far larger change than this doc's scope.

What **is** achievable: one canonical **data** table, keyed by logical field identity (the union of
`NumField`/`TextField`/`TextSearchField`/`CollField`/`ColorField` variants, plus `Legality`/`Devotion`/
`ManaCostCmp` as their own rows), each row holding a `Locality` value:

```rust
enum Locality {
    CardLevel,
    PrintingLevel,
    /// Legality's shape: printing-level iff this format's divergent-formats bit is set.
    /// Callers that need a single static answer (B's/C's per-purpose bias) must say which
    /// they want explicitly — see below — never read a silent default.
    DivergentByFormat,
}
```

`PLANE_BLOCKS` (table A) is already this shape for the 9 plane-eligible families — legality (dynamic,
exactly `DivergentByFormat`), rarity and border (both unconditionally `PrintingLevel`) — and would not
need to change at all; it would become the reference implementation the new field-level table is
checked against (or literally the source A's plane-index blocks are derived from, if someone wants to
also collapse the "which plane index" concern into the same table — not necessary for this
unification, since A's `BlockKind`-to-plane-index mapping is a separate, already-correct concern).

B's and C's field lists (`estimator.rs:79-121`, `filter.rs:851-910`) would become **thin adapters**
over the new table:
- Both are already per-leaf, so the per-leaf lookup is a direct table read for every field except
  `Legality`.
- For `Legality`, each caller passes an explicit policy at the call site — `Locality::PrintingLevel`
  for B (matching its documented "conservative" stance) or `Locality::CardLevel` for C (matching its
  documented "common-case" stance) — rather than the table silently picking one. This makes the
  existing one-line comments in each file into an explicit, typed argument instead of a hardcoded
  match arm, which is the concrete form of "it's OK to keep this as a COMMENT, but the branch itself
  must not key on field identity": the branch becomes "is this field `PrintingLevel`, or `CardLevel`,
  or (`DivergentByFormat` AND caller-policy-says-printing-level)" — never "is this field literally
  `Legality`".
- `printing_dependent` (ALL) and `touches_printing_field` (ANY) already share one leaf table
  (`leaf_compares_printing_field`) with two composition wrappers — this is the **existing precedent**
  for the ANY/ALL adapter the user's framing calls for; extending it to also cover B (a second ANY
  consumer, just with the opposite legality policy and a slightly different field-family list) is the
  same shape of change, not a new pattern.
- A itself needs zero changes — its callers (the router, the executor) already consume the
  behaviorally-correct fact directly and should keep doing so.

### Suggested migration order (independently landable, by risk and testability)

1. **`filter.rs`'s C first.** Lowest risk: `printing_dependent`/`touches_printing_field` only affect
   verify/paging ORDER, never a returned answer (see "risks" below) — a misclassification here is
   caught by nothing worse than a slower path, and the existing
   `verify_order_and_defers_printing_dependent_children` / `verify_order_or_defers_printing_dependent_
   children` tests (`tests.rs:11084`, `11106`) already assert the ordering outcome for representative
   shapes, giving a regression harness for free. Migrating C to read the shared table (with an explicit
   `Locality::CardLevel` policy for legality, matching its current behavior) should be a pure refactor
   with zero behavior change, verifiable by running those two tests plus a differential run of
   `bench_pairwise_ordering.py`/`bench_query_latency_ab.py` to confirm no ordering regression.
2. **`estimator.rs`'s B second.** Higher stakes (a SOUNDNESS invariant, not just an ordering
   heuristic) but well-covered: `fuzz_row_identity_matches_reference` already asserts
   `estimate_cardinality`'s bound against thousands of random trees per run, across every seed and the
   6k-card corpus fixture, and would catch a soundness regression from a botched migration
   immediately (the harness is EXISTING, not new work needed first). Migrate with the explicit
   `Locality::PrintingLevel` policy for legality (matching current behavior) and re-run the fuzz suite
   before and after as the gate.
3. **`planes.rs`'s A last, if at all — and maybe not.** A is already the reference table other rows
   would be checked against, is already correctness-critical for the router AND the executor (traced
   end-to-end in Round 16), and already has 3 dedicated property tests
   (`tests.rs`-adjacent property tests in `planes.rs:1656-1731`) asserting the exact family invariant
   per plane index across a `divergent_formats` mask matrix. There is little to gain from migrating A
   itself to read a new external table — it operates on the wrong tree shape (`PlaneExpr`, not
   `FilterExpr`) to share code with B/C directly, and it's already the thing being unified TO. Its only
   role in this unification is as validation: once a field-level table exists, a property test can
   assert every plane-eligible field's `PLANE_BLOCKS` classification agrees with the new table's
   `Locality` (mirroring the plane-index matrix test's own shape), closing the loop rather than
   touching A's logic.

### Not attempted here

No code changes were made — this is a design/investigation doc, per this round's brief. No new
`Locality` enum, table, or adapter was written or prototyped; the migration order above is a
recommendation for whoever picks this up next, not a plan this doc has started executing.

## Risks and staging

| Call site | Failure mode if a field's `Locality` is migrated wrong | Current test coverage (proxy for how safely a migration could be verified) |
|---|---|---|
| **A** (`planes.rs`, router cost-tier + executor row selection) | **Worst.** A wrong `CardLevel` verdict on an actually-printing-varying field silently zeroes the router's verify-cost tier (exactly the Round 15/16 bug shape) AND, independently, tells the executor's `existential_plane_for` to skip the per-printing row-selection walk — returning a row that does not actually satisfy the query for `unique=card` (a correctness bug, not just a mispriced plan). | Strong, direct: 3 dedicated property tests over the full `PLANE_BLOCKS` matrix (`planes.rs:1656-1731`) plus 2 concrete router-regression tests (`compose_tier_charges_border_existential_and_arith_range`, `compose_tier_charges_divergent_legality_existential_and_arith_range`) from Rounds 15-16. Not planned to move in this unification (see above) — listed for completeness. |
| **B** (`estimator.rs`, standalone cardinality estimator) | A wrong `PrintingLevel`→`CardLevel` flip could make a bound UNSOUND (the estimator's one hard invariant) — e.g. classifying a real printing-varying field as card-invariant could let `compose_and`'s Bonferroni path apply where it shouldn't, or let `is_total_two_valued`/the `Not` branch pick a bound formula that assumes no printing divergence. The opposite direction (over-classifying as printing-varying) stays sound, just looser — see "legality disagreement" above for why B's current bias is safe. | Strong but indirect: no dedicated unit test for `has_printing_varying_leaf` by name, but its only consumer's soundness is fuzzed exhaustively (`fuzz_row_identity_matches_reference`, 96 seeds × ~10-13 random trees each + a 6k-card corpus pass with 2,500+ row-identity checks) and asserts the exact invariant a migration bug would violate. Not currently wired into routing (`estimator.rs:1-5`: "NOT wired into query routing"), so a regression here has zero production blast radius today — the safest of the three to migrate for that reason alone. |
| **C** (`filter.rs`, verify/paging order) | A wrong classification only produces a suboptimal evaluation order — never a wrong row, never a wrong count, per the "legality disagreement" trace above (every And/Or child still gets correctly evaluated regardless of the order it's tried in). Worst case is a measurable latency regression on a narrow query shape, the same failure class the confirmation-pass benchmarks in the Round 15/16 doc already gate on. | Moderate, direct-but-narrow: 2 ordering tests (`verify_order_and_defers_printing_dependent_children`, `verify_order_or_defers_printing_dependent_children`) exercise the ordering OUTCOME for representative shapes, but neither is a per-field property-matrix test like A's — a migration should add one before shipping, mirroring A's `PLANE_BLOCKS` matrix test shape. |
| **D** (`lib.rs`, compose broadcast gate) | Not migrating (already reads A directly) — a hypothetical wrong `is_broadcast_leaf_shape` ADDITION (widening its arm list past what A would call safe) would be caught by the existing rarity/border negative-composability tests (`tests.rs:7825-7831`) plus `fuzz_row_identity_matches_reference`'s card-mode row-identity checks, which would surface a wrongly-broadcast existential leaf as a returned row that fails the trusted `ref_filter.matches` assertion. | Moderate: 1 direct negative test (rarity/border rejection) plus indirect coverage via the `compose_printing_bits`-vs-brute-reference comparison cases in the same test function and the crate-wide fuzz harness. |

**Cross-cutting risk this round's brief called out**: the reason this is a design doc and not a patch
is that a single migration commit touching A+B+C+D would span four modules with genuinely different
safety contracts (a soundness invariant, a verify-ordering heuristic, a compose-build correctness
gate, and a router cost-tier) — exactly the four failure modes tabulated above. The staged order
above is built so each step lands with its own existing (or one small added) regression gate, rather
than one commit whose blast radius spans all four at once.

## Explicitly out of scope / open questions

- **The `TypeCmp` compose-broadcast gap** (D's arm list excludes `TypeCmp` even though it's
  card-level per every classifier) — a real, separate opportunity (or non-opportunity; not measured)
  to extend `is_broadcast_leaf_shape`/`is_printing_composable`, unrelated to this unification. Would
  need its own win-rate measurement (same shape as
  [local-engine-plane-scope-printing-compose-executor.md](local-engine-plane-scope-printing-compose-executor.md)'s
  0/3,209 finding for a different gap) before deciding it's worth building.
- **Whether a fifth classifier exists that this round's four-classifier scope missed.** Not
  exhaustively searched (e.g. any admin/backfill/tagging code path in `api/` that reasons about
  card-vs-printing scope in Python, outside `card_engine/` entirely) — out of scope; this doc is
  scoped to the Rust engine's four (per Round 16's own enumeration).
- **Whether `is_total_two_valued`** (`estimator.rs:136`, a strictly narrower "safe to cleanly
  complement" classifier used only in `estimate_rec`'s `Not` branch selection) belongs in the same
  unification. It answers a related-but-different question (total two-valuedness, a superset
  restriction of card-level-ness — only `True`/`ColorCmp`/`TypeCmp` qualify, well short of every
  card-level field) and was not treated as a fifth peer here, since it isn't one of the four Round 16
  named; flagging it exists in case a future table's design wants to fold it in as a derived property
  (`CardLevel` AND "never Null") rather than leaving it as its own hand-maintained list.
