# Card image ladder geometry and the fidelity-target policy

How many rungs the image ladder should have, how they should be spaced, and what fidelity
target the `sizes` budget should aim at. Split out of [local-homepage-lcp.md](./local-homepage-lcp.md),
which owns the LCP chain and the per-rung quality ladder; this doc owns the *geometry*.

**Status (2026-07-26):** [PR #773](https://github.com/jbylund/sylvan_librarian/pull/773) merged the
single-tier constant-upscale budget (supersedes [#616](https://github.com/jbylund/sylvan_librarian/pull/616)).
Next planned work is a 5-rung geometric ladder plus per-rung compression, in one re-encode pass.
The DPR-axis policy question below is deliberately **not** being chased yet.

## The quantization identity

With rungs `w_i = W_min · r^i` and `r = (W_max/W_min)^(1/(n-1))`, a request `R = m · N`
(where `N = slot × DPR` and `m` is the budget multiplier) always rounds up into
`chosen/R ∈ [1, r)`. So:

```
upscale U = N/chosen = (1/m)·(R/chosen)  ∈  ( 1/(m·r),  1/m ]

max upscale = 1/m        <- independent of n
min upscale = 1/(m·r)
window      = r          <- all of the n-dependence lives here
```

**The spread between best and worst realized upscale is exactly the ladder ratio.** Bytes follow
`(r^k − 1)/(k·ln r)` where `k ≈ 1.79` is the empirical bytes-vs-width exponent fitted to measured
fleet means (this closed form matches a numeric device sweep to three decimals).

| n | ratio r | window | min upscale | max upscale | byte overhead vs exact fit |
|---|---|---|---|---|---|
| 4 (today) | 1.386 | 38.6% | 1.08× | 1.50× | 1.358× |
| 5 | 1.277 | 27.7% | 1.17× | 1.50× | 1.255× |
| 6 | 1.216 | 21.6% | 1.23× | 1.50× | 1.198× |
| 8 | 1.150 | 15.0% | 1.30× | 1.50× | 1.136× |
| 12 | 1.093 | 9.3% | 1.37× | 1.50× | 1.084× |

Inverted for planning: **n = 1 + ln(W_max/W_min)/ln(window)**. On the current 280–745 span
(ratio 2.6607) a 25% window costs 6 rungs, 20% costs 7, 15% costs 9, 10% costs 12.

Adding rungs **does not move the ceiling** — it seats devices closer to it, reclaiming overshoot
that was paid for and can't be used. That makes it the one lever here that costs no quality.

### Costs of more rungs

Every rung is a separate CloudFront object: 4 rungs ≈ 388k objects at 97k printings, 6 ≈ 582k,
12 ≈ 1.16M. More objects means lower per-object hit rate and more cold misses — the 150–220 ms
TTFB problem that the placeholder work exists to mask
([#607](https://github.com/jbylund/sylvan_librarian/pull/607),
[#608](https://github.com/jbylund/sylvan_librarian/pull/608),
[#613](https://github.com/jbylund/sylvan_librarian/pull/613)). Past ~8 rungs you are trading cache
locality for single-digit byte percentages.

### The 538→745 gap is the worst-served region today

Any request between 539 and 745 pays the full 745. Measured example: MacBook Pro 16 (1728 CSS px,
DPR 2) requests 581 px and receives 745 — a **28% overshoot, 104 KB where ~61 KB would do**.
A rung anywhere in 584–650 fixes that class of wide DPR-2 display with no stretch increase, and it
falls out of a 5- or 6-rung geometric ladder for free.

## Two candidate fidelity targets, and why one needs buckets

Definitions for a slot of `S` CSS px on a screen of ratio `d`, served width `W`:

- **effective DPR** `E = W/S` — image pixels per CSS pixel
- **upscale** `U = S·d/W = d/E` — device pixels painted per image pixel

The mechanism only lets us set the `sizes` length; the browser computes `needed = sizes_length × d`
using the true continuous DPR.

**Case A — constant upscale U\*:** `sizes_length = S/U*` → `needed = S·d/U*` → realized upscale is
`U*` for every `d`. **The DPR cancels algebraically.** One multiplier, continuous in DPR, zero
buckets, exact. This is what #773 ships (`U* = 1.5`, multiplier 0.667).

**Case B — constant effective DPR E\*:** requires `needed = S·E*`, so `sizes_length = S·E*/d` —
i.e. it needs **1/d**. CSS cannot supply it: `resolution` exists only as a media *query* (a boolean
test, not a value), there is no `dpr()` function, and physical units are hard-tied to 96px/inch so
they carry no device information. Hence a staircase of `min-resolution` tiers.

So bucketing on the DPR axis is an artifact of Case B, not a design preference — and Case A needs
no buckets at all.

### The perceptual argument favours Case B

The blur kernel measured in CSS px is `S/W = 1/E`, and the CSS reference pixel is *defined* to be
roughly angle-normalized (a fixed visual angle at arm's length). So constant apparent blur ≈
constant **effective DPR**, independent of device DPR. Constant *upscale* is therefore not
perceptually uniform:

| DPR | 2.0 | 2.625 | 3.0 | 3.5 | 4.0 |
|---|---|---|---|---|---|
| E under #773's constant-upscale 1.5 | 1.33 | 1.75 | 2.00 | 2.33 | 2.67 |

Apparent sharpness *rises* with DPR — high-DPR screens get a better-looking image than low-DPR ones
from the same policy. #616's original framing (constant `E = 1.5`, multipliers `1.5/DPR`) was the
perceptually principled one, and its buckets were the price of that principle.

Caveat: angle-normalization is only approximate. Phone CSS px subtend ~0.032° at 30 cm versus
~0.019° for a laptop at 60 cm — a factor of 1.7 — so constant-E is the better invariant but not an
exact one.

### Bucket error if Case B is adopted

Same geometric identity as the rungs: **error = the ratio between a tier's assumed DPR and the real
DPRs it covers.** Real DPRs cluster at 1, 2, 2.625/2.75, and 3, so three tiers covers the populated
values essentially exactly; the residual is the rare 2.75/3.5/4.

| tiers | realized E spread (target 1.5) | generated clauses |
|---|---|---|
| 1 (≥1.9 → assume 2) | 2.00× | 10 |
| 2 (≥2.9→3, ≥1.9→2) | 1.38× | 15 |
| 3 (+ ≥2.5→2.625) | 1.33× | 20 |
| 4 (+ ≥3.4→3.5) | 1.14× | 25 |

**Open decision, deferred:** keep Case A (continuous, no buckets, non-uniform apparent quality) or
move to Case B with 3 tiers / 20 clauses (uniform apparent quality, ~10 extra clauses ≈ a few
hundred gzipped bytes per 100-card page). Revisit after the ladder and compression work.

## Why exact fit is unreachable

Landing every device precisely on the ceiling needs continuously-parameterized widths, and every
route there is blocked:

- **`srcset` enumerates.** It never reports the width the browser computed, so an origin cannot
  generate an exact match.
- **Client Hints are dead.** The original `DPR`/`Width`/`Viewport-Width` headers are deprecated and
  non-standard, Chrome/Edge/Opera only, [never implemented by Safari or Firefox in any version](https://caniuse.com/client-hints-dpr-width-viewport)
  (77.7% global). Their standardized replacements `Sec-CH-DPR`/`Sec-CH-Width` were specified but
  **never shipped in any browser**. Would also need `Vary` on every image response.
- **JS-computed URLs break the preload scanner.** The URL no longer exists at HTML parse time,
  which is in direct conflict with the SSR plan in [local-homepage-lcp.md](./local-homepage-lcp.md);
  slot measurement also requires layout, so it is strictly later than today's fetch. It additionally
  weakens the `create_card_html` ↔ `createCardHTML` fixture parity that guarantees no-JS support,
  and `devicePixelRatio` is unstable under browser zoom and monitor changes, where media queries
  re-evaluate for free.
- **Unbounded objects.** Exact fit is the pathological end of the cache-fragmentation curve.

Prize for reference: exact fit is 593 KB against #773's 764 KB on a 13-device sample — ~22%, most of
which is recoverable with rungs alone.

## Hard ceiling: the source is 745 px

Scryfall's PNGs are 745 wide, so no rung above 745 can carry real detail and the top of the ladder
is pinned. A 1-column DPR-3 phone needs 1033 device px and therefore stretches **≥1.35× no matter
what** — it is already doing so today, unbudgeted. No ladder or multiplier change reaches those
devices; they stay on the 745 under every industry-normal setting.

## Industry precedent for the ceiling

- A **"2× DPR cap"** — effective DPR 2, i.e. 1.5× upscale on a 3× screen — is shipped practice.
  Twitter's mobile app uses it reporting no perceivable quality loss; YNAP applied it to product
  listing pages and measured [iPhone 11 Pro image weight 1.7 MB → 949 KB, −45%](https://medium.com/ynap-tech/how-to-cap-image-fidelity-2x-and-save-45-image-weight-on-high-end-mobile-phones-b27b43124a94).
- [Android's density buckets](https://developer.android.com/training/multiscreen/screendensities) use
  3:4:6:8:12:16 ratios, so adjacent buckets differ by 1.333× or 1.5× — Google ships that scaling
  granularity as routine.
- [Lighthouse's "Properly size images"](https://developer.chrome.com/docs/lighthouse/performance/uses-responsive-images)
  penalizes intrinsic ≫ rendered × DPR and has **no audit for under-serving**, so the only tooling
  pressure is against the downsampling surplus this work reclaims.

## Perceptual testing: what was tried and why it didn't settle anything

`ignored/placeholder-prototype/stretch-lab/` holds a 2AFC harness (`make_cards.py`,
`make_variants.py`, `index.html`, `staircase.html`) that pins a grid cell at 1033 device px and
varies only the source width, with every image re-encoded through an identical
CDN-745 → PNG → `cwebp -q 75 -m 6 -sharp_yuv` path so generation count is constant.

Results: 20/20 at 1.92× vs the 1.386× that ships, then 34/34 across a 1.45–1.92× staircase.

**The harness is not fit for this question.** Flip-in-place A/B is a *change-detection* paradigm —
two images scaled by different non-integer factors land fine detail on different sub-pixel phases,
so toggling reveals a shimmer regardless of sharpness. It flagged a **4.4% pixel difference** (712 vs
745) at ceiling, which is a reductio: the method reports "these differ", never "this is worse", and
so cannot return a threshold. Two harness bugs found along the way and fixed: an under-powered
anchor (n=4 can't reach p<0.05 even at 4/4) and a generation-count confound that dominated the mild
levels.

Consequence: the 1.5× ceiling rests on the industry precedent above, **not** on this testing. If the
question is reopened, the right instrument is ecological — render two full 12-card grids at different
settings and pick the better *page* — not another psychophysical staircase.

## Next steps (in order)

1. **5-rung geometric ladder** (`[280, 358, 457, 584, 745]`, ratio 1.277). Window 38.6% → 27.7%,
   byte overhead 1.358× → 1.255×, and it closes the 538→745 gap. Keep the spacing geometric so
   relative quantization error stays uniform across an unknown demand distribution.
2. **Per-rung WebP quality** — today `WEBP_QUALITY = 75` is flat. This shifts the whole
   byte/quality curve rather than adding points to it; design and visual validation live in
   [local-homepage-lcp.md](./local-homepage-lcp.md#image-bytes-quality-ladder--fidelity-budget-designed-2026-07-06).
   Same overnight re-encode pass as (1).
3. **Then** revisit the Case A / Case B target decision above, with the clause-count and
   apparent-uniformity tradeoff measured rather than argued.

Rungs must be added to `ladder` in `api/static/card_images.json`; `noscript_helpers.py` and
`copy_images_to_s3.py` read it, `app.js` mirrors it under jest assertion, and the parity fixture
regenerates via `scripts/generate_card_html_fixture.py`.
