# `card_layout` has no index, so `is:flip` scans 31,508 cards to find 20

**Deprioritized 2026-08-05** — layout predicates are judged rare in real use, which the traffic caveat
below already flagged as the deciding unknown. Kept as a scoped, measured, ~4.5 KB fix if that judgement
ever changes; do not pick it up ahead of
[the artwork-mode walk gap](done/local-engine-orderby-walk-modes.md) (now shipped) or the rest of the
`is:`/`frame:` family ([local-engine-is-frame-predicates.md](done/local-engine-is-frame-predicates.md)),
which is 24% of remaining dispatch time against this one's share of it.

`is:flip` matches **20 oracle cards**. It takes **187 µs** and scans the whole corpus to get there,
because `card_layout_id` — an interned string on `OracleCard`, 14 distinct values — has no index and no
`narrow_rec` arm. `is:dfc` is the worst at **426 µs**.

Found by ranking uniform sampled traffic by dispatch time rather than by regret; see
[the slow-vs-regret finding](#why-this-was-invisible-before) for why that reordering mattered.

## The field is the ideal shape for postings and gets none

| layout | printings | cards | share of cards |
| --- | --: | --: | --: |
| `normal` | 94,502 | 30,396 | **96.47%** |
| `transform` | 984 | 388 | 1.23% |
| `saga` | 354 | 157 | 0.50% |
| `adventure` | 342 | 134 | 0.43% |
| `split` | 288 | 123 | 0.39% |
| `modal_dfc` | 260 | 98 | 0.31% |
| `mutate` / `prepare` / `class` | 255 | 114 | 0.36% |
| `meld` / `leveler` / `prototype` / `flip` / `case` | 221 | 98 | 0.31% |

Thirteen of the fourteen values cover **3.5% of cards between them**. So a card-space postings list per
value, with the dominant `normal` dropped the way `frame_data` already drops its dense values (#628),
is ~1,112 card ids ≈ **4.5 KB** and turns every one of these queries into a lookup.

## Measured

Sole predicate, `unique=printing orderby=edhrec limit=60`, from the uniform sample:

| query | matching cards | now |
| --- | --: | --: |
| `is:dfc` | (union) | **425.6 µs** |
| `is:transform` | 388 | 207.3 µs |
| `is:mdfc` | 98 | 195.4 µs |
| `is:split` | 123 | 195.2 µs |
| `is:meld` | 21 | 189.4 µs |
| `is:flip` | **20** | 186.7 µs |
| `is:leveler` | 25 | 185.1 µs |

The acquire confirms nothing narrows: `count_source: candidates`, `eval_domain: 31,508` (every card),
`scan_units: 97,206` (every printing). The estimate is `matches: 38,882` for **every** `is:` value
alike — a fixed fraction, so `is:flip`'s 36 matching printings are estimated **1,080x** over. That is a
second-order problem; the scan is the finding.

For contrast, the same query shape on a field that *has* postings: `watermark:mps`, 5 matches, **18.5 µs**.

## The fix

`TextField::Layout` needs the arm `TextField::Watermark` already has, one space over:

```rust
FilterExpr::TextExact { field: TextField::Watermark, op: CmpOp::Eq, value } => {
    Narrowed::tight(Candidates::Printings(indexes.watermarks.get(value.as_str())...))
}
```

Layout is **card-space** (it lives on `OracleCard`, so it is card-invariant), which makes it a
`Candidates::Cards` analogue of that arm plus a `layout: TagIndex` built like `set_codes`/`watermarks`
and thresholded like `frame_data`. `Narrowed::tight` is correct: the postings are exact for `Eq`.

Absence must not be treated as proof of emptiness for a thresholded index — that is precisely the
`complete: false` distinction `CollectionCmp`'s arm already draws for `frame_data`, and dropping
`normal` makes this index incomplete in the same way. Mirror it rather than reinventing it.

One archive-format bump; `ARCHIVE_FORMAT_VERSION` must move to the actual current date.

## What this is worth, and the honest caveat

In `uniform` sampled traffic, layout predicates are 2.5% of queries and **6.3% of all dispatch time**
(118.7 ms of 1,886 ms over 52,330 queries), mean 90.3 µs against 34.6 µs for everything else.

**But `is:` and `layout:` are not in `REALISTIC_FAMILY_WEIGHTS` at all**, so realistic-mode traffic as
modelled never generates one and the realistic share is 0% by construction. That is the difference
between this and [the eur/tix index](./done/local-engine-eur-tix-range-index.md), which was 16.0% of
**realistic** wall time and therefore unambiguous.

So the per-query win is certain and large (185-426 µs to a lookup) and the aggregate win is unknown,
because nothing here models how often real users type `is:split` or `is:dfc`. Two ways to settle it,
in order of cost: add `is`/`layout` to the sampler's realistic weights with a defensible number, or
read actual query logs. **Do not price this off the uniform figure** — uniform deliberately flattens
family weights to surface slow shapes, not to estimate traffic.

## Why this was invisible before

Ranking the same traffic by REGRET puts none of these queries in view: they have **0.0 µs regret**,
because the router correctly picks the best plan available and every plan is slow. Regret measures the
router; it cannot see a missing index.

Quantified over 52,330 uniform queries:

| | |
| --- | --: |
| queries with < 1 µs recoverable regret | **96.4%** |
| total positive regret as a share of all dispatch time | **2.5%** |
| slowest decile's share of all dispatch time | 75% |
| of the 100 slowest queries, how many have any recoverable regret | **14** |
| their mean time / mean regret | 1,025 µs / **10.2 µs (1.0%)** |

A perfect router would take the slowest decile from 1,414 ms to 1,383 ms. **2.5% is the ceiling on all
cost-model work**, and ~1% on the queries that are actually slow — which is why the same sweep that
found this was worth more than the constant being chased at the time
([local-engine-compose-build-rates.md](./local-engine-compose-build-rates.md)).

The token-level slowness table from that sweep, for whoever picks the next one (base rate 10%):

| token | n | P(in slowest decile) | mean |
| --- | --: | --: | --: |
| `is:` | 3,226 | **28%** | 88.2 µs |
| `tix` / `cn` / `usd` / `eur` | ~3,250 each | 19-20% | 66-70 µs |
| `id:` | 3,289 | 18% | 62.3 µs |
| `year` | 1,861 | 20% | 69.0 µs |
| `cmc` / `r:` | ~3,500 each | 15-16% | 55-56 µs |
| `border` / `frame` | ~3,350 each | 9% | 31-36 µs |

`usd`, `eur` and `tix` now sitting together at 19-20% is the eur/tix index's acceptance criterion met
from the other direction: the three behave identically because they now share a representation.

## Status

Not implemented, and deprioritized — see the note at the top. The corpus distribution and every timing above are measured on the production corpus;
the traffic weight is the open question and the reason this is not already done.

`is:` covers more than layout, and the rest is separate work: `is:new`/`is:old` resolve to
`card_frame_data` (already indexed, but *thresholded*, and `2015` is the dropped dense value — hence
`frame:2015` at 785 µs); `is:permanent` becomes an `Or` over `card_types`; and `is:vanilla` expands to
nothing at all since `37ce298`, reaching the engine as `FilterExpr::VanillaFace` — a whole-corpus
verify with no `narrow_rec` arm, which is
[its own doc](./local-engine-vanilla-face-narrowing.md). Those are three different problems behind one
prefix; this doc is only the layout one.
