# Card Grid Column Sizing: auto-fit / auto-fill Tradeoffs

## Decision

**Keep the current implementation.** The complexity reduction from switching to `auto-fit` is real but modest, and the current approach gives precise control over breakpoints and first-row detection with no hidden edge cases.

---

## Current implementation

The main card results grid (`.results-container`) uses two layers that duplicate the same breakpoints:

1. **CSS** (`api/static/styles.css`): Five media queries at 410px, 750px, 1370px, and 2500px set `grid-template-columns: repeat(N, 1fr)` for 1–5 columns.

2. **JavaScript** (`api/static/app.js`):
   - `getColumnsFromViewportWidth()` mirrors the same four breakpoints and returns 1–5.
   - On load and on every resize, `updateGridColumns(cardCount)` overrides the CSS with `repeat(actualColumns, 1fr)` where `actualColumns = Math.min(columnsFromWidth, cardCount)`. This caps columns at the actual card count so a small result set (e.g. 2 cards) fills the row rather than leaving empty columns.
   - `calculateFirstRowCount()` uses the same breakpoint logic to mark above-the-fold images `fetchpriority="high"` and everything else `loading="lazy"`.
   - The same breakpoints are echoed in the `<img sizes="...">` attribute so the browser picks the right source from the srcset.

Any change to column behavior requires updating all three locations.

Note: `prefer_score_tuner.html` already uses `repeat(auto-fill, minmax(240px, 1fr))` for `.printings-grid`, so auto-fill is not foreign to the codebase.

---

## The auto-fit option

Replacing the media queries and JS with a single CSS rule:

```css
.results-container {
  grid-template-columns: repeat(auto-fit, minmax(Xpx, 1fr));
}
```

**What you gain:**

- Removes 5 media query blocks from CSS.
- Removes `updateGridColumns`, `getColumnsFromViewportWidth`, and the resize listener from JS.
- Column count responds continuously to container width rather than jumping at fixed breakpoints.
- `auto-fit` (not `auto-fill`) collapses empty tracks, so a small result set still grows to fill the row — the same behavior the JS cap achieves today.

**What you lose or complicate:**

1. **Exact breakpoints change.** The current breakpoints (410 / 750 / 1370 / 2500px) don't derive from a single minimum card width. Any `minmax(Xpx, 1fr)` value will produce different transition points. Close enough is probably fine, but it's a behavior change.

2. **`fetchpriority` and lazy loading become approximate.** `calculateFirstRowCount` needs to know how many cards are in the first row. With CSS-driven column count, JS no longer controls this directly. Options are: (a) parse `getComputedStyle(grid).gridTemplateColumns` to count columns, (b) use a ResizeObserver and check which cards share the same `offsetTop` as the first card, or (c) keep a rough heuristic. All are more fragile than the current explicit calculation. Being wrong by one card is minor for LCP but it is a regression.

3. **`<img sizes>` still needs manual breakpoints.** The browser needs `sizes` to choose the right srcset entry before layout. Even with auto-fit, you still have to provide a media-query-based `sizes` string that approximates the column count at each viewport width. So the breakpoints don't fully disappear — they move from the grid definition into the `sizes` attribute.

---

## Why keep the current approach

The JS already handles the only case where auto-fit and the current approach meaningfully differ (small result sets filling the row). So auto-fit wouldn't change the visual output. What it would remove is the resize listener and `updateGridColumns` — but `getColumnsFromViewportWidth` would need to stay for `fetchpriority`. The net reduction in code is smaller than it looks, and the explicit breakpoints make the layout behavior easy to reason about and adjust.
