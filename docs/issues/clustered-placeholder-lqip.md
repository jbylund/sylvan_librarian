# Card image placeholders: frame templates + measured tint colors

**Status (2026-07-05):** design converged after ~10 mockup iterations driven by
adversarial card examples. Implemented-but-superseded stages:
[PR #607](https://github.com/jbylund/sylvan_librarian/pull/607) (k-means codebook,
unmerged — close when a successor lands) and
[PR #608](https://github.com/jbylund/sylvan_librarian/pull/608) (template + tint v1,
open — needs the deltas below before merge). Current design + WIP assets live on the
`placeholder-design-wip` branch; interactive mockup:
`ignored/placeholder-prototype/template-mockup.html`
(builder: `build_template_mockup.py` in the same dir).

## Problem

CloudFront misses on long-tail card images cost 150–220ms TTFB (measured: 192ms cold
from Boston) and the origin path is already at its floor (immutable cache headers,
preconnect, S3 Standard; no origin beats S3 — R2/Wasabi researched and rejected). So
the play is masking: a placeholder that paints before the image arrives, cheap enough
to ship for every card.

## Design

**Buckets** (shared, ~48–56): frame generation × color group, pure functions of
metadata — never pixels, never release year:

- generation ← Scryfall `frame`: `1993/1997 → old`, `2003 → modern`, else `m15`
- treatment ← `border_color` / `full_art` / type line: `black`, `white` (a *modifier*
  on the generation, not a generation — 8ED/9ED are 2003-frame white-borders),
  `borderless` (art to edges, real text box — most `full_art:true` cards), `fullart`
  (strip layout; `full_art` AND basic land)
- color group ← type line + colors: land > artifact > gold(2+) > mono > artifact

**Compact encoding**: 3 fixed chars `<gen><treatment><color>`, e.g. `obg` = old black
green, `mwl` = modern white-border land, `5xd` = m15 borderless gold. Axes:
gen `{o,m,5}`, treatment `{b,w,x,f}`, color `{w,u,b,r,g,d,a,l}` (d = gold). Stable by
construction; 96-slot space, ~50 realized; packs to 7 bits if ever needed.

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
    var(--tpl),        /* bucket grayscale template, multiply; transparent art window */
    var(--art-layer),  /* flat art color (or a data-URI thumb) in the window */
    var(--box-layer),  /* box: gradient(90deg, var(--frame-l) 0 40%, var(--frame-r) 60% 100%) */
    <trim layer>;      /* DERIVED: color-mix(var(--frame-l) p%, base) — p, base fitted per bucket */
  background-blend-mode: multiply, normal, normal, normal;
  background-color: <border constant per bucket>;
}
```

Renderer emits class + `--frame-l/--frame-r/--art` custom properties; real image
paints over the stack. Dual gradients degenerate to flat fills for mono cards with
zero detection logic. Needs a Safari/Firefox pass (`background-blend-mode`,
`color-mix`).

**Measurement** (offline, in copy_images_to_s3.py where the 280px webp is on disk):
box halves = mean color of the text-box interior left/right (transition band split at
42/58%); art = mean of the art window inset 12%. The per-bucket trim model is fitted
by the builder: regress trim tint against box color over **side-wise** observations
(per-card averages cancel dual-land variance out of the regression — real bug found).
Trim tints are solved as least-squares `template × tint ≈ card` (dividing out the
template's grayscale shading so text lines don't darken the fit), over the frame
region minus border ring minus a **dilated** art rect (underestimated art windows leak
asymmetric art) minus box interior. Borderless: art window starts below the opaque
title bar; box measured full-width (side slivers are art).

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
- Accepted residuals: borderless title-bar sliver inherits box color; old-frame
  saturation ceiling under multiply; 8ED washed-out frames near noise floor; exotic
  layouts (sagas/planeswalkers/split) get nearest standard template; per printing,
  not per face.

## Deltas: mockup-validated design vs PR #608 as pushed

#608 ships trim-measured colors, no borderless/fullart split, no modern-wb, long
bucket names, no box layer. Port = rewrite of `placeholder_measurement.py` masks +
`bucket_for`, builder (`build_placeholder_templates.py`) with per-bucket trim fit +
new corpus queries (incl. full-art basics), 3-char codes, regenerated artifact pair,
updated truth-table tests + parity fixtures. Value format/plumbing/serving unchanged.

## Prototype / evidence locations

- `ignored/placeholder-prototype/template-mockup.html` — stage-grid mockup (bucket
  defaults → flat → 4×3/8×6/16×12 → real, toggle for load transition)
- `build_template_mockup.py` + `frame_corpus.json` (color labels via frame:/border:
  queries) + `corpus_meta.json` (per-card Scryfall fields from the local
  `data/api/blue/default_cards/` dump — Scryfall API now requires a custom UA)
- `art_fidelity_sheet2.png` — flat vs 4×3 vs 8×6 vs 16×12 vs real, with byte costs
- k-means era: PR #607, `index.html`/`cluster_images2.py` in the same dir, and the
  blog draft `docs/blog/posts/00960_placeholders-kmeans-rediscovered-metadata/`
