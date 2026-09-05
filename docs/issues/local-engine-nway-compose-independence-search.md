# N-way `And` composition: a general strategy, not one shape at a time

## The problem

`compose_printing_estimate`'s `And` arm has grown a specific tightening for each leaf-pair shape
that turned out to matter: `PairTotals` (border/rarity/frame/legality/cmc/power/toughness pairs),
`arith_tuple_count` (2+ of cmc/power/toughness), `compile_plane`+`eval_planes` popcount
(card-invariant/existential planes), `cn`×`set` density (Round 33), the `set`/`color`/`identity`×
subtype tables (Round 34), `set`×`set` disjointness (Round 35), the subtype×(cmc,power,toughness)
cube (Round 36), and — since this doc was first written — a generalized, registry-driven independence
step (Rounds 38/40, see "What's built now" below). Each one was real, each one was hand-verified
against the real corpus, and the list keeps growing — every round in this arc found another leaf-pair
whose correlation the existing mechanisms missed.

That's the pattern worth generalizing. For an `And` of `N` leaves (`A ∧ B ∧ C ∧ D ∧ ...`), the
question isn't "does *this specific pair* have a hand-written mechanism" — it's "what's the tightest
valid combination of whatever mechanisms *are* available, applied in the right groupings." Two things
make this tractable rather than combinatorially hopeless: real queries almost never have many leaves,
and two of the existing mechanisms already solve the "many leaves" case for free within their own
domain.

**Status as of Round 40**: the registry-driven independence step (below) is a real generalization —
one mechanism, scanned over every residual leaf against a re-validated safety table, replacing several
rounds' worth of what would otherwise have been one-hard-coded-pair-at-a-time branches. It is NOT the
bounded partition search this doc originally set out to describe: no multi-leaf packing, no
triple-level safety, no cost-aware ordering. Rounds 37-40's real contribution is as much the
*measurement infrastructure* (below) as the estimator change itself — the actual search this doc
envisions can now be built and graded against a real baseline, which wasn't true when this doc was
first written.

## What's built now (Rounds 37-40)

Nothing here existed when this doc was first written. It doesn't replace the design below — it's what
makes attempting the rest of it tractable to verify rather than a leap of faith.

- **`and_trace`** (`AcquireFacts`, Round 37a): structured, always-on provenance for the outermost
  `And` node's own evaluation, exposed on `explain()`/`explain_analyze()`. A recursive tree of
  `{"kind": "leaf", ...}` / `{"kind": "op", "op": "min_fold"|"joint_lookup"|"independence", ...}`
  nodes (every node self-contained with its own card/printing/artwork numbers — no separate "final"
  field to keep in sync), plus a `considered` list of every 2-or-3-leaf combination the arm's fixed
  sequence actually attempted, hit or miss. A `hit: false` entry is as informative as a hit — it says
  a mechanism looked at this exact combination and found nothing, not that nothing was ever checked.
  Scoped to the outermost `And` only (no nested `And`-within-`And` recursion) — sufficient for every
  shape the harness below generates, not yet exercised on deeply nested filters.
- **`scripts/nway_estimate_truth_survey.py`** (Round 37b): a checked-in, deterministic, curated-shape
  query generator (every leaf-pair this doc names, a same-family-twice supplement `QuerySampler`
  itself can't draw, an OR-rooted baseline, a broad/pathological N=1..8 catch-all), measuring both the
  cheap estimate and the real ground truth in all three spaces. **Primary metric is plan-choice
  agreement** (`explain()`'s own `picked` bool, free), not raw ratio — a ratio of predicted=1 against
  true=0 reads as "infinitely wrong" yet is completely benign, and predicted=29,000 against
  true=31,000 reads as "6.9% off" and is *also* benign, for the same reason: neither is near a
  threshold that would change the router's pick. Ratio is graded second, floored at `true_total >=
  100`, as a diagnostic for locating where the estimator is loose. `--compare` diffs two isolated
  builds; `--report` summarizes one run alone.
- **`and_estimate_ns`** (`AcquireFacts`, Round 39): a real, single-shot nanosecond timer on the
  acquire-time `PrintingCompose` estimate — deliberately not multi-trial, since the target question is
  an aggregate distribution across thousands of queries, not one query's precise cost. Baseline on the
  real corpus: median 750ns, p90 4.4µs, p99 11.6µs, populated on exactly the fraction of queries whose
  acquire actually reaches that branch. This is the number any future search's own "tax" gets graded
  against — Round 40's own registry generalization moved it to ~917ns median, a real, measured,
  accepted cost for real accuracy gained.
- **A re-validated leaf-pair safety registry** (`IndepClass`/`independence_safe_pair`, Rounds 38/40):
  see "The safety bar is empirical, not provable independence" below for the methodology and the
  concrete confirmed list.

## Card/artwork space's own asymmetry, closed (Round 41)

Scoping this doc's own bounded partition search — "do we have what's needed to hand it to an agent" —
turned up a live gap unrelated to the partition-search question itself. Checking this doc's own worked
example (`color:G AND format:pioneer AND t:elf`, below) against the real engine found card/artwork
space badly under-tightened: `t:elf` already has an exact solo count in all three spaces (the same
per-leaf lookup every bare containment leaf uses), and printing space already floors on it, but
card/artwork space never did — `exact_domain_cards`/`exact_domain_artworks` were populated only when a
genuine multi-leaf mechanism fired, never subsequently folded against the OTHER leaves' own
already-exact counts. Fixed in Round 41 (see the ledger's own "Round 41" section) by flooring
`result_space.card`/`.artwork` on each uncovered leaf's own count, gated by the same breadth guard
`narrow_rec` already uses, scoped so `exact_domain` (what `scan_units`'s real cost pricing reads) is
untouched. This tightens the bound on queries like the worked example below; it does not make them
exact — the underlying "no true 3-leaf joint exists yet" problem (below) is still open.

## What already generalizes for free

`compile_plane`+`eval_planes` and `arith_tuple_route`/`ArithTupleIndex` are not pairwise mechanisms —
they each absorb *however many* eligible leaves are present in one shot, with no search:

- `compile_plane` computes one exact joint popcount over every card-invariant leaf plus up to one
  existential leaf (the shared-witness rule caps it at one existential fact, not at two leaves).
- `arith_tuple_route` narrows any combination of `cmc`/`power`/`toughness`/`loyalty` — including
  compound linear expressions like `power+toughness>cmc+cmc` — via the existing `#743` index,
  confirmed exact (verified directly against the engine: `format:modern id:g t:creature
  power+toughness>cmc+cmc` reads ratio 1.00 in every mode).

So the real algorithmic question is only about the **residual** — leaves that don't compile to a
plane and aren't part of an arith combination (subtypes, `set`, price/date ranges, and similar). Real
`And`s rarely have more than 2-4 such residual leaves; the design below assumes this is checked
against real traffic (including deliberately pathological many-leaf queries) before being trusted as
a bound, not asserted — still true, still unmeasured (see "What's not yet done").

## Three things naive strategies get wrong

### 1. Contraction doesn't launder correlation (transitivity)

If leaf `A` is correlated with leaf `B`, contracting `A` with some other leaf `C` into an atom `AC`
(via an exact 2-leaf mechanism) does not make `AC` independent of `B` — the correlation is still in
there, just hidden behind the atom's boundary. Treating `AC` as independent of `B` because `AC` is
"exact" is exactly the same mistake as treating `A` as independent of `B` was, applied one level up.

### 2. There is no fixed "always contract via whatever's available" rule

Verified directly against the real corpus for `color:G AND format:pioneer AND t:elf`:

```
n_cards = 31,724
color:G                    = 6,450      format:pioneer = 14,817      t:elf = 660
color:G AND format:pioneer = 3,097      (already exact today, via compile_plane)
color:G AND t:elf          =   560
ALL THREE (real)           =   246

contract (color, legality) first [the pair that's "free" via compile_plane],
  then × P(elf) independently        = 64.4   (ratio 0.26x)
contract (color, elf) first [the pair that's actually correlated],
  then × P(legality) independently   = 261.6  (ratio 1.06x)
```

Same three leaves, same "contract-then-multiply" shape, only the *order* differs — a 4x swing in
accuracy. `compile_plane`'s automatic, unconditional absorption of `color`+`legality` is a liability
here, not a shortcut: it grabs the pair that happens to be cheap to combine, not the pair that's
actually correlated. This isn't a one-off — a systematic check across 6 color×subtype tribal pairs
(Dragon/Wurm/Giant-style "big creature" correlations, and the color-pie ones: `G`×Elf, `U`×Wizard,
`B`×Zombie, `R`×Warrior, `W`×Human, `W`×Soldier) × 3 different "safe" third dimensions (legality,
`cmc` bound) found the same failure in **all 18 combinations**: wrong grouping 0.20x–0.57x, right
grouping 0.86x–1.11x.

### 3. Comparing an estimate against an exact value by magnitude is unsound, not just risky (Round 40)

Every EXACT/upper-bound mechanism (`PairTotals`, `arith_tuple_count`, `compile_plane`,
`SubtypeArithBox`, plain min-fold) guarantees `count(A∧B) ≤ min(count(A), count(B))` — so among that
class, "pick the smallest available candidate" is always the tightest CORRECT choice. Independence
(and `SetCollectorRange`'s Round 33 density estimate) has no such guarantee: it's a central estimate
that lands on either side of the truth (confirmed directly: roughly half of 610 real calibration rows
had independence undershoot, half overshoot). A selection rule that picks "smallest across everything"
would let an undershooting estimate silently win over a correct exact answer — a real error dressed up
as a tighter bound, found live in Round 38's own test (two EXACT mechanisms tied at the same value,
masking the bug until Round 40's registry generalization made a real conflict possible). The fix is a
strict class priority, not a magnitude race: an estimate-class candidate may only fill a leaf subset
no exact/bound mechanism covers at all, never be magnitude-compared against or allowed to override one
for an overlapping subset. Any future search-selection logic has to preserve this distinction — it is
not solely a Round 40 implementation detail, it's a property any combinator over a mix of exact and
estimate mechanisms must have.

**This class distinction is now structural, not just documented (Round 46).** Every mechanism folds
its result through one shared `Candidate` enum (`Exact{printings,cards,artworks}` /
`Estimate{printing}`) and one `fold_candidate` function — which variant a mechanism constructs IS the
class, visible at the call site, rather than something to re-derive from surrounding prose. The same
function also carries a `debug_assert!` enforcing `cards <= artworks <= printings` on every `Exact`
candidate (see "What's not yet done" for the census this already ran).

### 4. Min-folding multiple estimate-class candidates for the SAME target is a different, real bias — not just "risky"

Everything shipped so far min-folds candidates that are either (a) EXACT/bound mechanisms — always
safe, since any true sub-conjunction's own count is a guaranteed ceiling on the full `And`, so folding
more of them in only tightens toward the truth, never crosses below it — or (b) independence estimates
that each drop a *different* leaf, so each one estimates a *different, strictly larger* marginal (the
star investigation's own measured direction confirms this: `color:G cmc<=3 usd<=10` predicted 6450
against true 3363, an OVERshoot, because both candidates ignore a real constraint the other covers).
Picking the smaller of several over-approximations of different, looser targets moves toward the
truth — this is why `.min()` has been safe everywhere it's been used so far.

That reasoning does NOT extend to a case nothing has shipped yet but the partition search below would
create if built naively: multiple ESTIMATE-class candidates that each estimate the SAME target (e.g.
two different partitions/groupings of the identical `N` leaves, each producing its own
independence-flavored estimate of the full joint), with the search reporting whichever number is
smallest. That is order-statistics selection bias, not looseness: if several independent, individually
reasonable estimators of one quantity are computed and the smallest is always reported, the expected
value of that *procedure* is below the true quantity, even if every individual estimator is itself
unbiased — a real, systematic undercount, structurally different from either safe case above. Nothing
shipped triggers this today (every registered independence pair is keyed to a unique class-pair, so no
two formulas ever compete to estimate the identical leaf subset yet) — but the general partition search
this doc describes, if it ever evaluates multiple full-scope groupings and picks the tightest number,
would. The fix is the same shape as point 3's, one level up: partition selection among ESTIMATE-heavy
candidates must never be a magnitude comparison — prefer whichever partition is backed by *more*
exact/bound coverage structurally, and when forced to choose between two independence-heavy groupings,
decide on domain grounds (which leaves are *actually* correlated, per point 2's own worked example),
never by comparing their numbers.

**A qualification to the (a)/(b) framing above, demonstrated concretely by Round 55 rather than
reasoned about.** Case (a)'s "always safe in any order" holds only *among* exact/bound candidates. The
moment an ESTIMATE-class candidate min-folds against an EXACT one whose subset it OVERLAPS, order
starts to matter — and not as a subtlety, as a real broken test. An undershooting estimate permanently
pulls `result` below the truth the instant it folds, and no later exact candidate can raise it back
(`fold_candidate`'s `Exact` arm only tightens via `.min()` too). The guard that's supposed to prevent
this — "an estimate may only fill a gap no exact mechanism covers for that exact subset" (point 3 /
Round 40) — is implemented via `covered`, which by construction only reflects mechanisms that have
*already run*. So the guard is only as good as the mechanism's POSITION in the arm: Round 55's
fallback, placed before `SubtypeArithBox`, let an undershot independence guess for two subtype leaves
beat that mechanism's own available, tighter-but-larger exact box hit on the same leaves. The standing
rule this yields: **an estimate-class mechanism must be positioned after every exact mechanism whose
leaves it could compete for, not merely written to respect `covered`.** Numbers and the specific tests
in [local-engine-gathered-scan-card-printing-varying-depth.md](local-engine-gathered-scan-card-printing-varying-depth.md)'s
Round 55 section.

## The safety bar is empirical, not provable independence (revised after Rounds 38/40)

The original version of this doc treated "is this pair independence-safe" as answerable from a static
survey (a 46,184-row pass across 250 leaf-type pairs) and copied its verdicts into prose. Building
Round 40's real registry against that prose directly surfaced two problems worth stating as standing
principles, not one-off fixes:

**The prose itself was self-contradictory and wrong in a fixable way.** `legality×{cn,price,set,year}`
was listed safe, `legality×date/set` unsafe, in the same paragraph — `legality×set` in both lists.
Resolved by domain semantics: Modern/Pioneer-style format legality is *defined* by a release-date
cutoff, and `set:X`/a date/a year all pin the same underlying variable legality already depends on —
not a correlation with exceptions, the same variable observed twice. `legality×year` was a second,
un-flagged instance of the identical error. **`legality×{set,date,year}` is deliberately excluded from
the independence registry entirely** — not because independence measures badly there (it does, but
that's not the reason) — because a materially better answer exists: `card_legalities` is already real
per-printing ground truth, so an exact per-(set, format) table (which fraction of a set's printings
are legal in a format — the same shape as Round 34's `SubtypePairIndexes`) should answer this
precisely. Flagged as a follow-on round, not attempted.

> **Round 57 correction — the "same variable observed twice" reasoning above is WRONG for the date
> axis, and the conclusion it supported was right by luck.** Measured: legality is CARD-level while
> `released_at` is PRINTING-level, so reprints scatter a format's legal cards across the entire axis.
> Every format except `oldschool` has legal printings back to **1993-08-05**, including modern, pioneer,
> standard and premodern. The cutoff governs SET legality, not the printing population an estimate
> sees — so this is an ordinary correlation-with-exceptions after all, not a variable observed twice.
>
> What actually breaks independence here is per-format **temporal density**, and the relationship is an
> identity rather than a correlation: independence substitutes a format's global legal density for its
> local density, so its error is exactly `global_density / window_density`. `premodern`'s 3.69 skew in
> its own era predicts a 1/3.69 = 0.27x undershoot, which is precisely what was measured. Skew spreads
> run from **1.0x** (`legacy`/`commander`/`vintage`/`oathbreaker`/`duel` — independence is essentially
> exact there, and min-fold already returns 1.002-1.010x) to **250x** (`oldschool`), so "unsafe" was a
> per-FORMAT property that the blanket per-PAIR exclusion papered over: it discarded the formats where
> the fix is free in order to avoid two where it is bad. Over 460 measured (format, date-predicate)
> pairs independence would have cut wrong-side-of-1,024 rows from 17.2% to 7.6%.
>
> The exclusion's *conclusion* nonetheless stands, for the reason given in the second half above rather
> than the first: an exact answer does exist and is now shipped. Round 57's `LegalityDateTotals` answers
> `(format, status) × released_at` **exactly** in printing space for every range shape, at +148.8 KB, so
> there is nothing left for independence to approximate on that axis. **`legality × set` is still open**
> — it was never separately measured, and the per-(set, format) table this paragraph proposes remains
> unbuilt. Do not carry the "same variable observed twice" argument over to it without measuring;
> reprints break that reasoning for sets the same way they break it for dates.

**"No true independence" is the norm in this domain, not the exception, and that's fine.** Every pair
has *some* real exception if you look hard enough — even `legality×price`, the cleanest-looking safe
pair, has Alpha (Reserved-List overrepresented, commands an "original printing" premium independent of
playability). That doesn't make the pair unsafe. The actual bar is empirical and aggregate: does
`min(fold, independence)` net-improve over plain fold across a real sample that deliberately includes
the hard cases, not whether a plausible correlation story can be told. Two pairs the original survey
called UNSAFE reversed on this bar when actually measured: `id×set` (median `|log ratio|` 1.15→0.11,
118/122 improved) and `pow×set` (1.11→0.15, 72/73 improved) — independently re-confirmed on a fresh
seed before trusting a reversal this surprising, not just the implementing agent's own sample. A
follow-up investigation of `safe:legality+usd`'s own regressed tail (real, ~36% of rows, but a net
median improvement) found the same lesson at smaller scale: `f:pauper`'s worst individual cases (ratio
down to 0.28) have a real, nameable cause — Pauper legality effectively requires common rarity, and
rarity drives price directly, a genuine shared variable smaller in scope than `legality×date` but the
same species of problem — yet isolating pauper+penny barely moves the AGGREGATE regressed count (497 of
801 vs. 547 of 861 overall), confirming most of that tail is ordinary independence noise, not a hidden
structural flaw in the pair.

**Confirmed registry, as of Round 40** (printing space, `n≈300` draws per pair unless noted):
`legality×cn` (0.246→0.041), `legality×usd/eur/tix` (0.188→0.011 / 0.195→0.019 / 0.401→0.077),
`type×released` (0.478→0.178), `type×usd` (0.479→0.189), `color/identity/cmc×{usd,eur,tix}` (Round 38,
confirmed uniform across all three currencies), `id×set` (1.151→0.106), `pow×set` (1.114→0.154). A
grid search over a multiplicative bias (`fudge × independence`, 1.0–2.0) found `fudge = 1.0` — no
bias at all — strictly optimal on both median and mean error for every pair checked so far, including
`legality×usd` specifically (re-run after the Pauper investigation, isolated to rows where independence
actually won: median signed error ≈ 0 at `fudge=1.0`).

**Round 56 re-tested the fudge factor independently, on a different mechanism, and reached the same
verdict for a sharper reason — record it here so it stops being re-proposed on intuition.** The
proposal is appealing: anchored independence produces some under-estimates (44% of rows on the measured
population, worst 0.62x), so bias the product up slightly (`× 1.15`) to shift the distribution toward
the safe direction. Swept at 1.05/1.10/1.15/1.25/1.50 over 70 queries drawn at random from the full
population of `star:identity+cmc+usd` (deliberately not the straddling tail — calibrating a constant on
the rows selected for being wrong is fitting the tail), every non-trivial factor made **routing worse**:
plain independence leaves 0 rows on the wrong side of the `STREAM_MIN_MATCHES` boundary, 1.10–1.25 put
one back, 1.50 put two. The reason generalizes beyond this mechanism: **when the error population being
corrected is already dominated by over-estimates (83% of all routing-relevant misses in the survey), a
uniform upward bias pushes genuinely-small queries back across the very threshold the fix exists to get
them under.** A factor also fails at its stated purpose — the worst under-estimate moves only
0.62x→0.69x at 1.15, reaching 0.94x only at 1.50 where the whole distribution is wrecked — because
those under-estimates come from real positive correlation on particular leaf combinations, not a uniform
downward bias, so no single multiplier can target them. Two independent searches, two mechanisms, same
answer: `fudge = 1.0`. Declined despite looking plausible via plain independence: same-currency
price crosses (mixed signal, `usd×eur` net worse in printing space while `usd×tix`/`eur×tix` net
better). ~~Don't re-attempt these without new evidence~~ — **`usd×eur` specifically closed in Round
53**, not by adding it to this registry (plain independence really is the wrong tool here — the eur/usd
ratio spans p10=0.346 to p90=1.357, too wide for the single-multiplier shape this registry's own
`printing_indep`/`card_indep` formula assumes), but with a dedicated `PriceJointTable`: a real,
Pearson r=0.877-validated correlation, captured via a quantile-bucketed 2D joint histogram instead of
an independence product, feeding the SAME `by_class`/`IndepUnit` pairing machinery as one more unit so
it still combines with other classes via the existing loop unmodified. ~~`usd×tix`/`eur×tix` remain
correctly untouched (r=0.336, weak -- the existing plain treatment already reasonable)~~ — **also
closed, in Round 54**: a fresh full-corpus survey run once `usd×eur` stopped dominating it surfaced
both as the next-worst shapes, still at 0% mechanism coverage (plain independence, despite this doc's
own "net better" finding above, had never actually been wired up for either). The real, methodologically
important finding: Pearson r only measures LINEAR correlation, and a direct 2D joint-histogram
simulation (not plain independence) found both pairs have a real, exploitable, NON-linear relationship
despite their weak r — 1.70x/1.35x/0.87x on real tail queries in simulation (1.00-1.92x once actually
shipped and independently re-verified), dramatically better than plain independence's own ~10-11x on
the same queries. `PriceJointTable` generalized to cover all three pairs via one shared builder/dispatch
rather than three hand-copied near-duplicates. The 3-way `usd+eur+tix` case remains genuinely open — see
the followup queue's own item. `set×type` (similarly mixed across spaces) is unrelated and still open —
don't re-attempt it without new evidence.
`color×identity` needs no registry entry: confirmed already 100%-covered by the pre-existing
`PlanePopcount` mechanism, no live gap.

**Triple-level safety, investigated and resolved into a narrower, real, CONFIRMED problem — not the
one this doc used to describe.** The literal question ("does joint 3-way independence hold") turned
out not to be reachable at all: building `independence_safe_pair`'s adjacency graph found no triangle
among the 9 confirmed pairs (`Price` is a hub with 5 partners — `Legality`/`ColorId`/`ColorIdentity`/
`Cmc`/`Type` — none of which are registered against each other; `SetCode`'s 2 partners,
`ColorIdentity`/`Pow`, aren't registered against each other either) — so no query can ever trigger a
true 3-way joint-independence assumption today. The doc's own motivating claim (`color`×`identity`
"invisible pairwise, real correlation as a triple") describes a combination that was never added to
the registry in the first place (Round 40 found it's 100%-covered by exact `PlanePopcount`, not an
independence candidate at all) — re-spot-checked directly (`c:r id:ru t:dragon`-shaped queries):
PRINTING space is fine (abs log ratio 0.001-0.35, two independent exact mechanisms — `PlanePopcount`
and `SubtypePairIndexes` — happen to cross-cover it), but CARD space is not (0.48-1.18, e.g. predicted
568 vs. true 174) — a real, narrower-than-claimed gap, in a SEPARATE code path
(`exact_result_total`'s card-space `matches`, not `compose_printing_estimate`'s own `eval_domain`,
which is well-behaved here) — not investigated further, flagged as its own (small) open item.

**What IS real, reachable by shipped code today, and CONFIRMED bad**: a "star" — two of a hub class's
registered partners both present alongside the hub, where the partners themselves aren't a registered
pair (e.g. `color:G cmc<=3 usd<=10`: `ColorId`×`Price` and `Cmc`×`Price` are both registered safe,
`ColorId`×`Cmc` isn't) fires BOTH independence estimates simultaneously and `.min()`-folds them — a
composition NEITHER pair's own 2-leaf calibration (Round 38/40) ever measured. Measured directly
(curated `star:*` shapes added to `scripts/nway_estimate_truth_survey.py`, three independent seeds):
`star:color+cmc+usd` and `star:identity+cmc+usd` are substantially worse than either component's own
baseline (median abs-log-ratio ~3.5-32x worse across three seeds, `and_trace` directly confirmed both
`Independence` groups fire on real queries like `color:G cmc<=3 usd<=10`); `star:identity+pow+set` and
`star:cmc+type+usd` show the same direction on smaller samples. `star:legality+cmc+usd` and
`star:legality+type+usd` are only mildly worse, close to their components' own noise. **`color+cmc+usd`
and `identity+cmc+usd` — the two substantially-bad ones — are CLOSED, Round 44**: a new exact
`(colors|identity) x cmc` table (`ColorCmcTable`) removes the need for `ColorId`/`ColorIdentity`×`Cmc`
to ever go through independence at all; median abs-log-ratio 0.80→0.58 / 0.71→0.57 (two independent
seeds), the pure 2-leaf case now exact in all three spaces. `star:identity+pow+set`/`star:cmc+type+usd`
(smaller-sample, same direction) and `star:legality+cmc+usd`/`star:legality+type+usd` (mild) are
UNCHANGED by Round 44 — none of them involve `colors`/`identity`×`cmc` — still open if they ever matter.
An UNPLANNED,
independently-reproduced-on-a-third-seed second finding: the 3 star candidates that get swept by an
EXACT mechanism before independence ever fires (`legality+color+usd`, `legality+identity+usd`,
`color+identity+usd` — `PlanePopcount` claims the two card-invariant leaves, leaving the price leaf
plain-min-folded) are ALSO worse than either component's own baseline (median 0.97-1.05) — a genuine
3-way `legality`/`color`/`identity`/`price` correlation the current `min(exact-2-leaf-joint,
solo-price-leaf)` fold misses, unrelated to the double-independence question but found by the same
investigation. **Neither finding is fixed here** — this was scoped as measurement only. See the
ledger's own section for the full per-shape numbers and a recommended next round (the simplest
candidate: decline BOTH independence estimates, falling back to plain min-fold, when a hub class and
2+ of its DIFFERENT registered partners are simultaneously present in the residual — the same
"ambiguous → decline" precedent the registry already uses for same-class duplicates and the
`SubtypePairEstimate` fallback — rather than assuming "pick the tighter one" is safe, since Round 40's
own class-priority finding is that an estimate isn't a guaranteed bound and "tighter" isn't the same as
"more accurate").

## The corrected model: partition search, not a fixed pipeline

Rather than "contract, then independence," the right framing is: **find a partition of the `N` leaves
into groups such that (1) each group either has an exact/cheap mechanism or is left as singletons,
and (2) every pair of leaves that ends up in *different* groups is independence-safe at the
individual-leaf level** — not "atom vs. atom," since an atom's constituent leaves carry their own
correlations forward. Where condition (2) can't be satisfied for some cross-group pair, those two
leaves either need to be forced into the same group, or the whole comparison falls back to the
existing conservative min-fold for that cross-term. This makes leaf-level independence-safety a
**constraint** on which partitions are valid, not a combination step applied after the fact — still
the target architecture; what's shipped (Round 40) is one flat pairwise scan over the residual, not
this general partition framing.

**"Which grouping wins a leaf" is a non-issue for the EXACT/bound class — Round 42 confirmed this
directly, not just reasoned about it.** Any true sub-conjunction's own exact count is a valid upper
bound on the full `And` no matter what other leaves are present or what other mechanism also fired
(intersecting more constraints only shrinks or preserves a matching set) — so `.min()`-folding every
candidate grouping any registered EXACT mechanism can compute, in any order, over overlapping or
disjoint leaf subsets, is always sound. No priority/placement rule is needed for this class; a general
partition search over EXACT mechanisms only needs to enumerate every applicable subset and fold the
min, same as Round 42 did for one mechanism. The genuinely open version of this question is narrower
than the doc used to frame it, and resolves into two distinct sub-questions, not one: (a) an
ESTIMATE-class candidate applying to a leaf subset an EXACT mechanism ALSO covers — resolved by point
3 above (class priority: the estimate may only fill a gap, never override or be compared against an
exact candidate for an overlapping subset); (b) two ESTIMATE-class candidates competing to estimate
the SAME target (not merely touching the same leaves, but the identical full-scope quantity, as two
different partitions of the whole `And` would each produce) — resolved by point 4 above: this is NOT
a `.min()`-safe situation the way (a) or the EXACT class are, because picking the smallest of several
noisy estimates of one quantity is a systematically biased-low selection procedure, not mere
looseness. Partition selection among estimate-heavy candidates must be structural/domain-driven, never
a magnitude race, once (or if) more than one such candidate can arise for the same full leaf set.

## Bounding the search

Given the residual is typically small, the search doesn't need to be clever — it needs to be
*bounded*, so a pathological query (10+ leaves, adversarial or just unusual) degrades gracefully
instead of blowing up:

- Enumerate subsets of the residual up to size 3 (or 4) — `O(N^3)`, trivial even at `N=20`.
- For each subset, check whether any registered mechanism (exact or verified-independent) applies to
  exactly that shape.
- Greedily pick a set of non-overlapping winning subsets (a small packing problem — real queries
  rarely have more than one or two candidates active at once). **The pick, when 2+ candidate packings
  are both viable, must never be "whichever produces the smaller number" if more than one of the
  competing packings is estimate-heavy** (point 4 above) — prefer the packing backed by more exact/
  bound coverage structurally; break a genuine estimate-vs-estimate tie on domain grounds, not
  magnitude. Magnitude comparison stays fine whenever every candidate in contention is EXACT/bound
  class (Round 42's own finding) or when only one packing under consideration is estimate-class at all.
- Combine whatever's left via independence, respecting the leaf-level safety constraint above; worst
  case, behave exactly like today's min-fold.

Not built. Round 40's scan is pairwise-only over the residual (every registry-confirmed-safe PAIR of
present leaf classes gets its own independence candidate, each separately narrowing the same `result`
via `min`) — never a genuine subset search, never a packing decision among competing groupings larger
than a pair. The residual-size distribution this bound is reasoned from still hasn't been measured
against real (or deliberately pathological) traffic.

## Efficiency: don't pay for the search itself

Two principles, one already validated against real (if narrow) evidence — both still fully open,
unchanged since this doc was first written, because no new EXPENSIVE mechanism has been added since
(Round 40's registry entries are all `O(1)` hashmap-style lookups, same cost class as the leaves'
own solo estimates, hence why `and_estimate_ns`'s own tax from Round 40 was real but modest — see
above):

- **Prefer cheap mechanisms over expensive ones, not just tight ones.** The hashmap-based exact
  lookups (`PairTotals`, the subtype tables, `cn`×`set`) are `O(1)`; `compile_plane`+`eval_planes`
  costs real, measured time (`O(leaves × n_cards/64)`, ~4-9μs measured directly for the
  `color`×`identity` case, cheaper for simpler existential-only combinations). The search should rank
  by cost as well as tightness, defaulting to the free lookups and only paying for a plane popcount
  when nothing cheaper covers the leaves in question — and even then, a real cost/benefit check found
  it "leans net win, but not decisive" for the one case measured this way (a same-build-canary
  latency check found no clean signal at the whole-query level, though the routing-flip-rate argument
  favored keeping the exact path). Moot until a future round adds a mechanism in this cost class.
- **Never redo the same plane intersection twice.** `popcount_with_bits` (`lib.rs`, the `And` arm's
  existing existential-leaf loop) currently rebuilds and re-`eval_planes`s the *entire* card-invariant
  plane list from scratch, once per existential leaf present — real, measurable waste whenever 2+
  existential leaves co-occur (rare in practice, but a genuine bug of the same shape this whole
  section is about avoiding in the *new* machinery). The fix, and the design principle for anything
  new: compute the shared/base intersection once, cache the resulting bit-vector, and treat every
  additional candidate as an incremental extension of that cached base — never recompute a shared
  prefix from scratch per candidate. Still unmeasured for real-traffic frequency; still not attempted.

## What's not yet done

- **The `cards <= artworks <= printings` invariant is violated widely — confirmed via a real census,
  not just the one `c:w t:plains` example (Round 46).** Walking every `and_trace` tree in a full
  65,478-row sweep found 10,269 root-level violations across 3,421 distinct queries, all `artworks >
  printings`, all attributable to Round 41's own already-known unclamped floor (a leaf's own solo count
  folded in with no clamp against `result.printing`). The good news, also confirmed by the same census:
  ZERO of the six EXACT mechanisms (`PairRangeSum`, `PlanePopcount`, `ArithIdProbe`,
  `SubtypePairIndexes`, `ColorCmcTable`, `SubtypeArithBox`) themselves produce an inconsistent triple —
  every individual candidate is already self-consistent; the violation is purely a composition-step
  gap, confirming the "push self-consistency into each estimator" principle already holds for every
  mechanism that's been checked. ~~`arith_tuple_count` is structurally invisible to the census~~ —
  **closed in Round 51**: `ArithTupleIndex` gained `totals: Vec<SpaceTotals>`, one exact
  (printing,card,artwork) triple per distinct key, summed once at build time from that key's own
  postings (the same `offsets`/`artwork_base` spans, computed once instead of scaled per query) —
  rejected a query-time alternative using the already-existing `arith_tuple_ids` sibling (would pay an
  allocation on every query this common shape reaches, versus nothing extra at build time). Renamed to
  `arith_tuple_totals`, now folds `Candidate::Exact` at its primary call site, closing the census gap;
  independently reproduced against two real, pre-validated corpus populations (`cmc>=8 power<=2`:
  printing 30→21, now exactly matching the true 21; `cmc<=1 power>=1 tou>=1`: 3225→2786, exact). Fixing
  Round 41's own unclamped floor (the actual source of the 10,269 violations above) remains a separate,
  still-open item. ~~A NEW gap surfaced during this round's own verification: `unique=artwork`'s
  top-level acquire path routes through a separate `artwork_estimate` function~~ — **closed in Round
  52**: `acquire_plan_features`'s `unique=card`/`unique=artwork` branches now consult
  `est.result.card`/`.artwork` (the SAME `ComposeEstimate` already computed a few lines earlier) as an
  ADDITIONAL `.min()` tightening on top of the pre-existing calibrated-estimate baseline — never a
  replacement for it. That distinction mattered: a first attempt that replaced the baseline outright
  (reasoning that `est.result.card`/`.artwork`, like `exact_result_total`, is "exact") caused a real
  170x regression on a real corpus query (`id:ruw usd:0.50 cmc>=2`, artwork mode, 123→21,048) — because
  unlike `exact_result_total` (every arm gated to require an exact whole-filter shape-match, confirmed
  by re-reading each arm's own guard), `est.result.card`/`.artwork` can legitimately come from a
  mechanism covering only a SUBSET of the And's children (the same residual-scan architecture Rounds
  42/48/50 built on purpose for printing space) — a genuine upper bound, but sometimes an extremely
  loose one. Caught by the corpus sweep before shipping, fixed to a strict `.min()`-tightening, and now
  a dedicated regression test. Independently re-verified: both motivating populations from Round 51 are
  now exact in artwork mode, the regression scenario stays correct on both wheels, and a direct
  row-by-row sweep comparison (not just the aggregate stat) found zero regressed rows.
- ~~A real, separate nondeterminism bug (Round 46)~~ — **closed in Round 47.** `top_n_and_rest_max`
  (`build_subtype_pair_tables`'s shared cutoff) now extends past `n` to include every pair tied with
  the boundary card count, rather than a plain `sort_unstable_by_key` + `truncate` with no tiebreak.
  Independently re-verified: 5 fresh index builds of the same wheel now give byte-identical results for
  every previously-flipping query in both affected dimensions (`set`, `colors`), and the now-stable
  table hits are confirmed exact against ground truth. `identity` and the `SubtypeArithBox`'s own
  (unrelated, already-correct) cutoff were unaffected. **Still open**: the harness's own query-generation
  nondeterminism (identical seed, same corpus, two engine loads produced different query sets) — a
  related instance of the same underlying class of bug, in `scripts/nway_estimate_truth_survey.py`
  rather than the engine itself, not chased down in either round.
- **Most leaf types still report `card: None, artwork: None` on their own solo estimate** — `Price`
  confirmed affected (same shape as `SetCode`, fixed for `SetCode` only in Round 45); a full census of
  which `FilterExpr` variants use the card/artwork-less `ComposeEstimate::leaf` path vs. the two that
  carry real values was not completed as part of Round 45 and would need its own pass before deciding
  which are worth fixing next.
- **The actual bounded partition search** — subset enumeration up to size 3/4, greedy packing of
  multiple simultaneous non-overlapping tightenings across arbitrary leaf groupings. Round 40 ships a
  flat pairwise scan over the residual, not this. This is the single biggest remaining gap between
  "what's built" and "what this doc describes."
- ~~`SubtypeArithBox`'s own whole-query shape gate~~ — **closed in Round 48**, the same generalization
  Round 42 already did for `SubtypePairIndexes`. But doing so exposed, with real measured numbers, a
  cost of `covered`'s existing leaf-occupancy semantics that was previously only a soundness concern:
  once `SubtypeArithBox` covers a subtype leaf via one exact pairing, `Independence` can never try that
  same leaf against a *different* partner, even when that estimate would have been tighter (`t:elf
  usd<0.20 cmc>=2`: printing 425→1865 against true 366, a real regression, not a synthetic one). ~~Loosening
  `covered` to be subset-identity-based rather than leaf-occupancy-based~~ — **closed in Round 49**:
  `covered` became `CoveredState { flags, subsets: Vec<u64> }`, keeping the original leaf-level `flags`
  (unchanged, still read by `SubtypePairEstimate`'s own narrow-leaf fallback) and adding `subsets`, one
  bitmask per genuine joint hit. The independence registry no longer excludes a leaf just because SOME
  mechanism touched it; it declines a candidate pairing only when that pairing's own combined leaf-mask
  exactly matches an already-recorded subset. Independently reproduced: `t:elf usd<0.20 cmc>=2` recovers
  exactly to printing=425 (matching the pre-Round-48 answer), and a fresh sweep (66,366 shared rows)
  found the ratio diagnostic flip from Round 48's own "B is LESS accurate" (+0.001) to "B is MORE
  accurate" (mean −0.034, 95% CI excludes 0), with all 378 plan-choice flips confined to `root=and`'s
  `*+usd` star/cube shapes and zero elsewhere.
- **"Anchored independence" — a new candidate shape, partially built (Round 50).** Even with Round 49's
  fix, `SubtypeArithBox`'s exact joint stays blind to any residual leaf, and raw-marginal `Independence`
  estimates for the same query are often looser than the box's own bound (a leaf's own MARGINAL is much
  broader than its ACTUAL joint with the subtype). Round 50 multiplies the box's exact joint by a single
  residual `Price` leaf's own solo rate instead — `t:elf cmc>=5 usd<10` tightens 241→188 against true 177
  (1.36x→1.06x), and `t:elf usd<0.20 cmc>=2` (Round 49's own motivating case) improves further, 425→370
  (1.16x→1.01x). This is conceptually the same "combine an exact prefix with an independent residual"
  idea this doc's own design describes for the general bounded partition search — Round 50 is a single,
  narrowly-scoped instance of it (one mechanism, one class), not the general mechanism. Deliberately not
  generalized yet to other residual classes, other anchor mechanisms (`SubtypePairIndexes`/
  `ColorCmcTable` have the same shape but no validated example), or combining multiple safe residual
  classes into one product — see
  [local-engine-nway-followup-queue.md](local-engine-nway-followup-queue.md)'s item #1 for the three
  separate directions left.
- ~~The `color:G format:pioneer t:elf`-shaped 3-leaf joint~~ — **closed in Round 42.** This was
  originally framed as needing a placement rule (`compile_plane` claims `color`+`legality` together
  first in source order, so `SubtypePairIndexes` would need to "win" the leaf instead). That framing
  was wrong: `exact_domain_*`'s existing `.map_or(x, |d| d.min(x))` chaining across mechanisms already
  composes correctly regardless of order — any true sub-conjunction's exact count is always a valid
  bound on the full `And`, so there's no race to adjudicate for the EXACT/bound class at all. The real
  gap was just that `SubtypePairIndexes` never computed a candidate past `v.len() == 2`. Round 42
  generalized the gate (no reordering of `compile_plane`), and the existing `.min()`-chain automatically
  picked whichever mechanism was tighter. See the ledger's "Round 42" section, including a real
  first-pass gap (skipping `covered` leaves for the exact-hit branch, not just the estimate branch)
  caught before merging.
- **Cost-aware mechanism ordering** — moot so far; no expensive mechanism has entered the registry
  since this was written.
- **The `popcount_with_bits` redundancy fix** — still needs a real-traffic frequency measurement
  before it's worth shipping on its own; still not done.
- ~~A fix for the confirmed "star" degradation~~ — **`color+cmc+usd`/`identity+cmc+usd` closed in
  Round 44** via an exact `(colors|identity) x cmc` table, not the "decline both estimates" fallback
  this entry used to recommend (a real exact mechanism beat a conservative decline). The swept
  `legality`/`color`/`identity`×`price` trio (a different mechanism — `PlanePopcount` plus a
  plain-min-folded price leaf, not double-independence) is UNCHANGED, still open, still a natural next
  round if it ever matters for real routing regret. `star:identity+pow+set`/`star:cmc+type+usd`
  (smaller-sample) and `star:legality+cmc+usd`/`star:legality+type+usd` (mild) also remain open — none
  are `colors`/`identity`×`cmc`, so Round 44 doesn't touch them.
- **The residual-size distribution for real (and deliberately pathological) 5+-leaf queries** — the
  `N choose 3/4` bound is still reasoned from what's been sampled, not confirmed at the tail. The
  harness's own `broad:n1..n8` catch-all generates this population; it hasn't been specifically
  analyzed for this question yet.
- **An exact `legality×{set,date,year}` mechanism** — flagged above as a better answer than
  independence for this specific family; a genuinely promising, scoped follow-on (Round 34-shaped),
  not attempted.
- **`t:enchantment power<10`-shaped queries** (a main type that mostly *excludes* having a value at
  all, combined with a broad arithmetic bound) — real ratio 7.4x over via naive independence, verified
  against the corpus — haven't been checked against the *existing* `compile_plane`+`arith_tuple_route`
  combination to see whether this shape is already handled correctly or is a live gap; structurally
  similar to the already-verified-exact `format:modern id:g t:creature power+toughness>cmc+cmc`, but
  the "mostly no value at all" population shape hasn't specifically been tested. Unchanged since this
  doc was first written — still open.
- **`safe:legality+usd`'s Pauper/Penny tail** — a real, small, explainable exception (see above); not
  urgent, but a candidate for a narrow follow-up if it ever matters for real routing regret.

## Related docs

- [local-engine-nway-followup-queue.md](local-engine-nway-followup-queue.md) — the active queue of
  what's left, in the order it's meant to be tackled. This doc is the architecture/rationale; that one
  is the todo list.
- [local-engine-gathered-scan-card-printing-varying-depth.md](local-engine-gathered-scan-card-printing-varying-depth.md)
  — the round-by-round ledger this whole arc is tracked in. Rounds 33-36 are the hand-written
  mechanisms this doc originally generalized from; Rounds 37-40 are the measurement infrastructure
  (`and_trace`, the survey harness, `and_estimate_ns`) and the first real generalization (the
  independence registry, the class-priority fix) — read there for the full round-by-round numbers,
  not repeated here.
- [00852-engine-compose-acquire-p3-p4-ranking.md](00852-engine-compose-acquire-p3-p4-ranking.md) —
  the original `StreamedSelect`/`GatheredScan` routing investigation this whole cardinality-estimation
  arc grew out of.
