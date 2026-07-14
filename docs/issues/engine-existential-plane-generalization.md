# Engine: generalize existential planes beyond legality (Y-predicate framework)

Follows [docs/workflows/performance-pr-workflow.md](../workflows/performance-pr-workflow.md).
GitHub: #680. Status: proposed, not started. Grew out of investigating #678's rarity follow-on
(two rejected attempts, see "Measured problem"). Scope widened in discussion to also cover
`border:` (previously #664/`docs/issues/done/engine-border-planes.md`, which deliberately stopped at
loose narrowing) — folded in here rather than filed separately, since it's the same registry
mechanism and strengthens the case that this is a general framework, not a legality-specific or
even a two-field trick.

## The general shape

Every query is `X` card-invariant predicates AND `Y` printing-varying predicates, ANDed with
whatever `Or`/`Not` structure the parser produced. `Y` — not which specific field a printing-
varying predicate belongs to — determines what's achievable:

- **Y=0**: today's fully-tight path. `compile_plane`, `all_match`, Step 2's popcount-skip walk,
  zero residual verification. Unaffected by anything below.
- **Y=1**: exactly one printing-varying predicate (any number of card-invariant siblings, which
  factor straight through the existential: `∃p: g(p) ∧ f_card` = `f_card ∧ ∃p: g(p)`). Total is
  exact from that predicate's own existence plane — zero scan. Row selection is bounded by the
  *emitted page* (≤ `limit`), not the candidate set: walk each returned card's printings for the
  first one satisfying the predicate, conjoined with any genuinely-residual sibling
  (`eval_plane_expr_for_printing(...) && (all_match || residual_matches(...))`).
- **Y≥2**: two distinct printing-varying facts can't be answered from independent existence planes
  — `∃p: f(p) ∧ g(p)` needs one printing to satisfy both at once, which `(∃p: f(p)) ∧ (∃p: g(p))`
  doesn't guarantee (a card can satisfy each via a *different* printing). No free count; each
  predicate's own plane can still loosely narrow the candidate set, but membership requires
  walking every surviving candidate's printings and evaluating all Y predicates jointly against
  one printing at a time.

This is already exactly what #667 built for legality — `format:A AND format:B` declining is a Y=2
instance, not a legality-specific rule. It only *looks* field-specific today because legality is
the only field with existence planes, so "two distinct facts" currently can only mean "two
distinct formats." The mechanism (`plane_expr_is_existential`, `collect_legality_formats`'s
shared-witness dedup, `eval_plane_expr_for_printing`) is written generically against raw plane
indices already — recursing through `Or` (safe: `∃` distributes over `∨`) and declining on 2+
distinct indices under an `And`. It needs its *range checks* widened to span fields, not its logic
rewritten.

## Measured problem

Two narrow_rec-level attempts at rarity's `special`/`bonus` values (sparse, currently
postings-backed while common/uncommon/rare/mythic are planes) both measured as no-ops, isolating
exactly why a field-local fix can't reach the win:

1. **Promote special/bonus to planes too** (widen `RARITY_PLANES` 4→6): net negative on wide
   comparisons. `rarity_plane_candidates` already unconditionally builds a `words_per_plane`
   bitmap for *every* rarity query; this just replaced a cheap ~370-entry postings scatter with
   two more full-width word-OR passes. `-r:mythic` went 488-579μs → 511-630μs.
2. **Skip the bitmap, return the postings union directly as `Candidates::Cards`**: no measurable
   change either. `r:special` stayed at ~74μs, `r:bonus` at ~3-4μs, isolated by comparing the two
   directly (0 vs. 370 candidates, same code path) — the ~70μs delta tracks candidate *count*, not
   narrowing *representation*. Because rarity's narrowing is `loose` (never `tight` — it's
   `PrintingDep` at the card level), the driver still walks and re-verifies every narrowed
   candidate against the real filter before it can sort/emit. That per-candidate residual pass,
   not the gather step either attempt touched, is the actual cost.

Both attempts stayed inside `narrow_rec` (today's ceiling for anything not legality). Reaching the
Y=1 win requires the same three pieces legality has: an exact existence plane reaching
`compile_plane`, `all_match`-eligibility gated by mode, and a per-page row-selection walk — i.e.
promoting rarity all the way to where legality already lives, not a cheaper `narrow_rec` trick.

## Where the cost is

`compile_plane`/`split_planes`/`plane_expr_is_existential`/`collect_legality_formats`/
`eval_plane_expr_for_printing` (all `card_engine/src/planes.rs`) are today hardcoded to legality's
plane ranges specifically. Rarity's planes (`PLANE_RARITY`) exist but only feed `narrow_rec`,
always `loose`, always paying full residual verification over the whole candidate set — never
reaching the code that would let Y=1 skip the scan.

Border is a third instance of the same gap, with an extra wrinkle: `build_border_planes`
(`card_engine/src/lib.rs:1231-1251`) hardcodes a 3-value match (`"black"`, `"borderless"`,
`"white"` → a plane index; `_ => continue`), so any other border value — currently `gold` (1,238
printings) and `yellow` (90 printings, all from set `dft`/Aetherdrift, released 2025-02-14 —
confirmed real current data, not an ingestion artifact) — contributes to *no* plane at all.
Unlike rarity, this isn't just an unplaned tail of otherwise-enumerable values: `card_border` has no
DB-level enum/FK (`check_card_border_lowercase` only constrains case), so there's no schema
guarantee the domain is closed the way `valid_rarities` guarantees rarity's is. Any exact-plane
design for border has to construct that closure itself rather than lean on the DB for it.

## Proposed approach

**1. Generalize the existential-plane infrastructure to be field-agnostic.** Replace the
legality-specific range checks with a small registry of `(field, exists_base, absent_base_or_none,
per_printing_accessor)` entries that `plane_expr_is_existential`, the shared-witness dedup, and the
per-printing walk all consult, instead of only knowing about `LEGALITY_STATUS_TABLE`. The
shared-witness decline logic itself (dedupe distinct plane indices reachable under an `And`,
recursing safely through `Or`) needs no algorithm change — verified by hand against the
range-comparison case: `r>=rare AND r<=mythic` compiles each side to an `Or` of several rarity
plane indices (safe alone — `Or` distributes over `∃`), but the `And` of the two `Or`-trees
reaches 6 distinct plane indices total (rare/mythic double-counted, common/uncommon/special/bonus
added), correctly exceeding 1 and declining exactly like two distinct legality facts would. The
existing index-counting approach already covers this; it just needs rarity's indices in scope.

**2. Give rarity its own existence planes, wired into `compile_plane`, not just `narrow_rec`.**
`NumericCmp{field: RarityInt, op, val}` compiles to `Or` of the qualifying buckets' existence
planes — the same construction `rarity_plane_candidates` already does, just moved earlier so it
can reach `all_match`. One simplification specific to rarity: unlike legality (only 3 of 4 possible
status values are ever directly queried, so a dedicated `_ABSENT` plane per value is simpler than
building an unused 4th exists-plane), rarity's domain is fully enumerable *and* every value would
be planed here — so `!=val` needs no dedicated absent-plane at all, it's just `Or` of the other 5
values' exists-planes (`∃p: r(p)≠val` ⟺ `∃p: r(p)=v₀ ∨ ... ∨ r(p)=v₅` excluding `val`, since
exactly one value holds per printing). Simpler than legality's two-plane-per-value shape.

**Confirmed** (traced through code + schema, not just assumed): `card_rarity_int` is `Option<u8>`
(`NumVal::Null` short-circuits both `Eq` and `Ne` to `Tri::Null` — filter.rs's `NumericCmp` arm — so
a null-rarity printing never contributes `True` to either side of the equivalence, symmetrically). An
"unrecognized rarity" value outside the 6 buckets (`rarity_text_to_int` maps unmapped strings to
`-1`, not `None`) would break it, but the DB forecloses that: `fk_cards_rarity` FKs `card_rarity_int`
to `magic.valid_rarities`, which contains exactly rows 0-5 — a `-1` fails to load rather than
persisting. Domain really is exhaustive over `{null, 0..5}`; still worth a differential test at
implementation time as a regression guard, but the design itself needs no absent-plane fallback.

**3. Mode-gate and wire row selection**, reusing legality's exact shape: `plane_expr_is_existential`
gates `unique=card`-only promotion (`unique=printing`/`artwork` keep the full per-printing scan,
unaffected); `eval_plane_expr_for_printing` gets a rarity accessor (`printing.card_rarity_int`,
already exists) alongside legality's; the conjunction fix
(`eval_plane_expr_for_printing(...) && (all_match || residual_matches(...))`) applies unchanged in
shape to whichever field the existential leaf came from.

**4. Give border four exact planes (black/borderless/white/gold) plus one shared loose "other"
plane**, closing its domain by construction instead of relying on a schema guarantee. `gold` gets
promoted to a real exists-plane alongside the existing three (cheap: a 5th 3.9KB-class plane is
noise against the archive). `yellow` — and any future Scryfall border value, since nothing stops
one from appearing — folds into a single `has_other_border_printing` plane: *∃ a printing whose
border is Known but isn't one of the 4 tracked values*. This is the piece rarity gets for free from
`valid_rarities` and border doesn't: with `other` in place, `{black, borderless, white, gold, other,
null}` is exhaustive by construction, so `!=val`/`Not(border:val)` on any of the 4 tracked values is
`Or` of the other 3 tracked planes plus `other` — safe for the same reason rarity's Or-of-others is
safe (whatever a printing's real border is, it lands in exactly one of these buckets or is null).

This is a genuine widening of point 1's registry, not just more data in the same shape: rarity's (and
legality's) bucket lists are *all* exact/existential-eligible, but border needs the registry to
distinguish "K exact, existential-eligible buckets" from "1 shared loose bucket, valid as an
Or-of-others disjunct and for candidate narrowing, never itself promoted through `compile_plane`/
`all_match`." A query naming a value that isn't one of the 4 tracked ones (`border:yellow`, or a
hypothetical future color) can't be answered from `other` alone — it's a disjunction over
unspecified values, not a specific one — so it narrows candidates via `other` (strictly better than
today's zero narrowing for such values) and still falls through to a full residual walk to confirm
the actual value, the same fallback shape rarity's pre-generalization special/bonus already used.
Shared-witness Y≥2 decline needs no new logic: `border:black AND border:borderless` already
declines correctly today via loose narrowing + residual (#664's `border_shared_witness_correctness`
test) — the exact-plane version must reproduce the same decline via index-counting (2 distinct
tracked indices under an `And`), which is exactly what point 1's mechanism already does. Row
selection needs a `printing.card_border_id` accessor in `eval_plane_expr_for_printing` alongside
legality's and rarity's, same conjunction-fix shape.

**5. Y≥2 stays declined, not solved.** The joint per-printing evaluator
(`docs/issues/engine-printing-varying-plane-repair-pattern.md`'s `eval_plane_expr_for_divergent_card`
design, prototyped for legality and never shipped) is *not* part of this issue. Once rarity and
border are second and third fields, `r:special AND f:modern` and `border:gold AND r:mythic` become
real, plausible Y=2 query shapes — more plausible than `format:A AND format:B` ever was — worth
remeasuring after this ships to see if they show up enough in the broad survey to justify building
the joint evaluator later. Building it speculatively
now is exactly the kind of unrequested complexity to avoid; the existing decline-and-fall-back
behavior (loose per-field narrowing + full residual verification) is correct today for any Y≥2
shape and stays correct after this generalization, just reachable via more field combinations.

## Acceptance

1. Baseline (targeted rarity script `scripts/bench_rarity_planes.py`, targeted border script
   `scripts/bench_border_planes.py`, broad survey, memory) on `main`, same corpus/seed convention as
   #678/#664.
2. Targeted: `r:special`/`r:bonus` solo and negated should approach the few-microsecond floor
   legality's sparsest values hit (`banned:alchemy` at 5μs); compound with card-invariant siblings
   (`r:special t:creature`); the Y=2 declines (`r:special AND f:modern`, `r>=rare AND r<=mythic`)
   verified both to decline *and* to still produce correct results via the fallback (not just a
   decline check — mirror `legality_shared_witness_and_falls_back_to_correct_result`); `unique=
   printing`/`artwork` unaffected; controls unaffected.
3. Targeted, border: `border:black`/`borderless`/`white`/`gold` solo and negated (expect the same
   few-microsecond floor for the three non-declining ones — `border:black` alone still declines via
   the existing broadness guard, unchanged from #664); compound with a card-invariant sibling
   (`border:gold type:creature`); `border:yellow` (expect improved-but-not-zero-scan — narrowed via
   `other`, verified via residual, strictly faster than today's unindexed full scan but not the Y=1
   floor); a fixture with a synthetic 6th/unenumerated border value exercising `other`'s safety-net
   role specifically (proving a future Scryfall value can't silently reopen the domain-completeness
   gap); `border:black AND border:borderless` (must still return zero matches — the #664 correctness
   canary, now re-verified at the exact-plane level, not just loose narrowing); `unique=printing`/
   `artwork` unaffected; controls unaffected.
4. Correctness review at the same rigor #667 needed — it found three separate holes
   (mode-aware `all_match`, row-selection picking the wrong printing, the conjunction fix) across
   multiple review rounds despite careful design up front, and a fourth (a ~15% regression the
   targeted script couldn't see, only the broad survey could). Expect the same here: broad survey
   is not optional, and the row-selection/conjunction tests need to check the actual returned
   `scryfall_id` on a fixture where the preferred printing is deliberately the non-matching one, not
   just totals. For border specifically, also re-verify the `other`-bucket safety net directly: a
   store where a card's only printings carry an unenumerated border value must still evaluate
   correctly (via residual fallback) for both positive and negated queries on the tracked values.
5. Total-row-count parity on every config, every run.
6. Re-measure and iterate; open PR.

## Related

- `docs/issues/engine-legality-divergent-carveout.md` (#667/#676) — the concrete Y=1 instance this
  generalizes; every correctness hole found there is a hole to specifically re-check here
- `docs/issues/engine-legality-banned-restricted-planes.md` (#678) — where the rarity follow-on
  (and the two rejected narrow_rec-level attempts motivating this doc) came from
- `docs/issues/engine-rarity-planes.md` — the existing `narrow_rec`-only rarity planes this
  extends
- `docs/issues/done/engine-border-planes.md` (#664) — the existing loose-only, 3-value border
  planes this extends to 4 exact + 1 loose catch-all; its shared-witness correctness test
  (`border_shared_witness_correctness`) is the regression this issue's exact-plane version must
  keep passing, not just the loose-narrowing version
- `docs/issues/engine-printing-varying-plane-repair-pattern.md` — the joint per-printing evaluator
  this issue deliberately does not build (Y≥2 stays declined)
