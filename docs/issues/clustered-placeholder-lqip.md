# Card image placeholders: frame templates + measured tint colors

**Status (2026-07-05, evening):** design converged after ~10 mockup iterations driven
by adversarial card examples, then a second measurement-driven pass (era split, border
sharing, ink-vote templates, trim-fit rework — findings below). Implemented-but-superseded
stages: [PR #607](https://github.com/jbylund/sylvan_librarian/pull/607) (k-means codebook,
unmerged — close when a successor lands) and
[PR #608](https://github.com/jbylund/sylvan_librarian/pull/608) (template + tint v1,
open — needs the deltas below before merge). Current design + WIP assets live on the
`placeholder-design-wip` branch; interactive mockup:
`ignored/placeholder-prototype/template-mockup.html`
(builder: `build_template_mockup.py`, corpus: `refresh_corpus.py` in the same dir).

## Problem

CloudFront misses on long-tail card images cost 150–220ms TTFB (measured: 192ms cold
from Boston) and the origin path is already at its floor (immutable cache headers,
preconnect, S3 Standard; no origin beats S3 — R2/Wasabi researched and rejected). So
the play is masking: a placeholder that paints before the image arrives, cheap enough
to ship for every card.

## Design

**Buckets** (codes) and **templates** (CSS cost) are deliberately decoupled: a bucket
code is a recipe — which template image + which trim constants + which border — and
recipes compose from ~33 template images plus near-free override rules. Axes, pure
functions of metadata — never pixels, never release year:

- generation ← Scryfall `frame`: `1993`/`1997` (separate *trim constants*, shared
  `old` template — measured ±5–16 RGB signed trim residual between eras, worst for
  land/artifact), `2003 → modern`, else `m15`
- treatment ← `border_color` / `full_art` / type line: `black`, `white` (a *modifier*:
  one `.ph-wb` background-color rule over the black-border bucket — wb interiors match
  their bb siblings within |Δ| 6–15, closer than the era merge; 8ED/9ED are 2003-frame
  white-borders), `borderless` (art to edges, real text box — most `full_art:true`
  cards), `fullart` (strip layout; `full_art` AND basic land)
- color group ← type line + colors: land > artifact > gold(2+) > mono > artifact

**Compact encoding**: 3 fixed chars `<gen><treatment><color>`, e.g. `3bg` = 93-frame
black green, `mwl` = modern white-border land, `5xd` = m15 borderless gold. Axes:
gen `{3,7,m,5}`, treatment `{b,w,x,f}`, color `{w,u,b,r,g,d,a,l}` (d = gold). Stable by
construction; 128-slot space (exactly 7 bits), ~60 realized. Client maps code →
template class + era trim class + optional `.ph-wb`.

**Template CSS asset**: 33 ring-trimmed shared templates + 41 trim-constant rules +
1 wb modifier = 61KB raw / **26.6KB brotli** — below shipped v1 (34.6KB br) while
covering borderless/fullart/modern-wb and the era split. Cost scales with distinct
template images (~1KB compressed each), not bucket combinatorics.

**Per-card stored value** (~24 chars): `"<bucket> <boxL> <boxR> <art>"`, e.g.
`mbl 8ab4c8 c8a468 3d5a55` (3ch bucket + 3 separators + 18ch color payload). Semantics matter: the two colors are the **text-box
halves**, not the frame trim. Within a bucket the trim hue is constant by
construction, while the box carries the per-card signal (dual-land gradients, gold
component tints, parchment vs colored, comic white). NULL = unmeasured → client
falls back to `ph-fb-<color>` derived from type line/mana cost.

**Rendering** (pure CSS, one element, no decode):

```css
[class^="ph-"] {
  background-image:
    var(--tpl),        /* shared grayscale template, multiply; transparent art window
                          AND border ring (ring-trimmed: background-color paints the
                          border, so bb/wb share the image) */
    var(--art-layer),  /* flat art color (or a data-URI thumb) in the window */
    var(--box-layer),  /* box: gradient(90deg, var(--frame-l) 0 40%, var(--frame-r) 60% 100%) */
    var(--trim);       /* DERIVED: color-mix(var(--frame-l) p%, base) — p, base fitted
                          per bucket, per ERA for old frames (the era rules override
                          only --trim) */
  background-blend-mode: multiply, normal, normal, normal;
  background-color: #0d0d0d;   /* border; .ph-wb overrides to #e8e0d0 */
}
```

Template luminance is normalized **per region** (rails and box each to ~235):
multiply can only darken, so absolute region levels must live in the color layers or
saturated rails cap out (m15 red rendered washed-out pink — this retires the old
"saturation ceiling" residual). Border ring geometry is per-gen, per-side
(`BORDER_INSETS`): old/modern ≈5% uniform, m15 = 3.2% sides / 7.5% bottom collector
strip (measured from stack luminance profiles; a uniform 5% swallowed m15's thin
rails and leaked its black bottom strip into the template). Renderer emits classes +
`--frame-l/--frame-r/--art` custom properties; real image paints over the stack.
Dual gradients degenerate to flat fills for mono cards with zero detection logic.
Needs a Safari/Firefox pass (`background-blend-mode`, `color-mix`).

**Measurement** (offline, in copy_images_to_s3.py where the 280px webp is on disk):
box halves = mean color of the text-box interior left/right (transition band split at
42/58%); art = mean of the art window inset 12%. Trim tints are solved as least-squares
`template × tint ≈ card` over the **top-chroma-quartile** pixels of the rail mask
(frame region minus border ring minus a **dilated** art rect minus box interior) —
full-mask solves average rail color with black text/bevel shadows/parchment margins
(red m15 rails measured muddy #a98276; text and shadows are near-achromatic and drop
out of the chroma selection). Borderless: art window starts below the opaque title
bar; box measured full-width (side slivers are art).

The per-bucket (per-era for old) trim model `trim ≈ p·box + (1−p)·base` is fitted over
**side-wise** observations (per-card averages cancel dual-land variance — real bug
found), with three guards, each added because a bucket broke without it:
1. **p fitted on chroma** (per-observation brightness removed): scan brightness is
   common-mode, and a raw RGB fit reads it as "box predicts trim", pinning p at the
   clamp — every frame rendered as its box color.
2. **Ridge shrinkage** (λ≈6): near-constant boxes (red m15 parchment) make the raw
   slope divide by ~zero variance; shrinkage makes "no box signal" mean "trim is a
   bucket constant" carried by base.
3. **Gamut cap** `p ≤ min(T̄/B̄)` per channel: base must be a real color. Gold-modern's
   boxes vary legitimately (component tints), the fit hit the clamp, and the intercept
   clipped to #720000 → rendered silver. Hue-tracking buckets (dual lands, p=75%) have
   ratios near 1 and keep their high p.

**Template estimator** (`ink_vote_lum`): text ghosts can't be averaged away — glyph
rows align across cards, so mean/median/trimmed-mean all converge to smudged
pseudo-text. Each card ABSTAINS where it deviates from its own local blur (ink of
either polarity); clean cards vote per pixel. No-quorum pixels split by cross-card
agreement: consistent (box outlines, bevels) keep the crisp trimmed value; divergent
(aligned text) hole-fill from the vote result's clean neighbors via normalized
convolution. Box interior additionally gets a box-pixels-only smoothing pass (low-
frequency scan glare survives the vote — it isn't high-frequency). Measured: box
high-pass energy 25–40% below plain mean, with crisp structure.

## Art fidelity tiers (decision: flat default, thumbs opt-in)

The art window can hold a webp data-URI thumb instead of a flat color — one CSS var,
no architecture change. DCT/blurhash rejected: at 4×3 terms a plain pixel grid is
perceptually identical (measured, `art_fidelity_sheet2.png`) and needs no decoder in
either renderer; parity + no-JS + paint-before-JS all favor pre-encoded URIs. Prefix
`data:image/webp;base64,` is constant → prepended client-side, not stored. Size curve
is flat to 16×12 (container overhead dominates), knee after:

| tier | value chars | /search raw (100 cards, columnar) | gzipped |
|---|---|---|---|
| baseline (shape=columnar, PR #612) | — | 23.6KB | 1.88KB |
| flat art | ~24 | +11.5% | **+79% (+15B/card)** |
| flat art, 50% backfilled | — | +6.9% | +41% (+8B/card — null runs compress ~free) |
| art 16×12 | ~173 | +69.9% | **+671% (+126B/card)** |

(Row-shaped numbers are ~10% milder relatively; absolute per-card costs identical —
the values are incompressible entropy, and columnar ships each key once so field
naming is free.) The gzipped baseline is tiny; thumbs would make /search a ~15KB
endpoint whose payload is mostly placeholder data shown for ~200ms. **Ship flat art in defaults; expose thumbs via
`fields=` if a flash test (mockup + delay slider, not built) ever justifies them.**
Engine string table: flat ≈ +2.5MB @100k printings; 16×12 ≈ +18MB.

## Adversarial findings (each drove a design change; keep for regression review)

- `plst MIR-235`, retro-frame reprints: release year mislabels frames both directions
  (M15 shipped mid-2014; The List/DMR print old frames today) → always use `frame`.
  The query parser supports `frame:1993/1997/2003/2015` and `border:`.
- `sld 806` (Miku Command Tower): pixel border-ring gates misfile dark-edged
  borderless cards → metadata only. Scryfall marks it `full_art:true`.
- `dmu 380` (borderless Shivan Reef): **borderless ≠ fullart** — Scryfall
  `full_art:true` includes textbox-borderless ('inverted' effect, ~78% of borderless
  corpus). Discriminator: basic land.
- 9ED in old-wb: white border must not override the frame field (modifier, not gen).
- `7ed` Llanowar Elves vs `5ed` Wild Growth: one tint can't do saturated trim AND
  parchment box → box measured per card, trim derived via fitted color-mix.
- `30a 578` Underground Sea + modern duals (Sulfur Falls, Hallowed Fountain): dual
  land gradients live in the box; old-dual trim is uniform brown. Also the per-side
  fit bug above.
- `stx 186` Expressive Iteration: gold trim carries no component colors; box does.
  (A mana-cost→swatch-pair hack worked but was deleted — box measurement subsumes it.)
- g09 judge fetches: their box gradients are REAL (modern fetch frames are two-toned)
  — the measurement was right, we were wrong.
- 93 vs 97 in one bucket: merged fit leaves ±5–16 RGB era-correlated trim residual
  (land/artifact worst); the gain lives in the fit constants, not template pixels →
  split constants, share the template (+160B gz vs +9.4KB for split images).
- wb templates from 25–60 old scans were half sampling noise (split-half: error ∝
  1/√n, ~4–6 at n=25) — border sharing dissolves the thin buckets instead of feeding
  them.
- `clu 276` etc.: text-box glyph ghosts survive ANY all-cards estimator (rows align)
  → ink-vote abstention; per-pixel median rejected (mode-snapping: max|Δ| 2–3× worse
  under contamination, +10–16% URI).
- basics' watermark mana symbol + giant centered rules text: aligned LOW-frequency
  content — abstention can't see it, majority wins the vote → basics excluded from
  land template corpora (fullart excepted). Name-dedupe considered and rejected:
  same-name printings are different sets/arts, legitimate frame evidence.
- gold-old rendered pink parchment: brightness-confounded trim fit (p pinned at
  clamp) → chroma fit. Red m15 stayed washed: near-zero box variance → ridge. Gold-
  modern rendered silver: out-of-gamut intercept (#720000) → gamut cap. See
  Measurement.
- m15 frames washed + chunky border: uniform 5% ring vs measured 3.2% sides / 7.5%
  bottom → per-gen per-side `BORDER_INSETS`; dark template rails capped saturated
  colors under multiply → per-region normalization.
- Accepted residuals: borderless title-bar sliver inherits box color; 8ED washed-out
  frames near noise floor; exotic layouts (sagas/planeswalkers/split) get nearest
  standard template (and are excluded from template corpora); per printing, not per
  face. ~~Old-frame saturation ceiling under multiply~~ — retired by per-region
  template normalization.

## Deltas: mockup-validated design vs PR #608 as pushed

#608 ships trim-measured colors, no borderless/fullart split, no modern-wb, long
bucket names, no box layer, per-bucket template images. Port = rewrite of
`placeholder_measurement.py` masks (chroma-selected tint solve, per-gen
`BORDER_INSETS`) + `bucket_for` (era in gen axis, wb as modifier), builder
(`build_placeholder_templates.py`) with ink-vote templates, per-region normalization,
guarded trim fit (chroma p + ridge + gamut cap), era/wb rule emission + new corpus
build (from the local default_cards dump: layout/lang/scan hygiene, basics excluded
from land buckets, ~250–300/bucket, old era-stratified — see `refresh_corpus.py`),
3-char codes, regenerated artifact pair, updated truth-table tests + parity fixtures.
Value format/plumbing/serving unchanged. CPU note: full-dataset templates are ~2.2
core-minutes for all 97k printings (1.4ms/image decode+resize) — sampling is a
prototype-laptop constraint, not a production one; the S3-copy pass already has every
image on disk.

## Prototype / evidence locations

- `ignored/placeholder-prototype/template-mockup.html` — stage-grid mockup (bucket
  defaults → flat → 4×3/8×6/16×12 → real, toggle for load transition; rows annotated
  with era/wb so shared-template rendering is inspectable)
- `build_template_mockup.py` (current pipeline: ink-vote, regional norm, guarded fit)
  + `refresh_corpus.py` → `frame_corpus.json`/`corpus_meta.json` (~8.3k cards sampled
  from the local `data/api/blue/default_cards/` dump — Scryfall API now requires a
  custom UA; images re-fetchable from the CDN, not committed)
- `art_fidelity_sheet2.png` — flat vs 4×3 vs 8×6 vs 16×12 vs real, with byte costs
- k-means era: PR #607, `index.html`/`cluster_images2.py` in the same dir, and the
  blog draft `docs/blog/posts/00960_placeholders-kmeans-rediscovered-metadata/`
