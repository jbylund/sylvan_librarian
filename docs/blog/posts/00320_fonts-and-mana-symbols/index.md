---
title: "Why Mana Symbols Are a Custom Webfont, Not SVG Sprites"
date: 2026-11-07
publishDate: 2026-11-07
tags: ["frontend", "fonts", "css", "mtg"]
summary: "Rendering Magic's mana symbols as a custom webfont: why a font beats SVG sprites or images, how subsetting drops a 200–300 KB file to under 40 KB, and the tradeoffs around font loading that the obvious approach misses."
---

The Scryfall data export encodes mana costs as strings: `{2}{R}{R}`, `{W/U}`, `{T}`.
Every card in the search results needs those tokens to render as the colored circle glyphs you see on real Magic cards.
Three approaches are worth considering: inline SVG sprites, individual images, or a custom webfont.
The font won — and the payload reduction from subsetting is obvious, but the subtler reason is what subsetting reveals: every other font-loading concern (CORS, layout shift, loading sequence) requires decisions that images and sprites sidestep entirely.

## What's Wrong with SVGs and Images

The naive approach is to ship a sprite sheet — one SVG containing all 64 symbols, reference each by id.
GitHub's emoji system and Font Awesome work this way.
SVG sprites render crisply at any size, they are style-able, and they are a single HTTP request.

The problem is DOM weight.
A search result page shows up to 100 cards.
Each card has a mana cost.
A card like Yore-Tiller Nephilim (`{W}{U}{B}{R}`) has four symbols;
a big spell with Phyrexian hybrid mana might have eight or more.
At 100 cards averaging three symbols each, that is 300 `<use>` elements or 300 `<img>` tags hitting the DOM on every render.
I profile `createCardHTML` later in the series ([post 00736](../00736_dom-nodes-vs-regex-40x/index.md)), and the DOM cost matters.

Individual images are worse: each one is a separate HTTP request, a separate decode, and separate paint work.
For a set of fixed symbols that never change, that is the worst tradeoff.

A font turns each symbol into a single CSS `content:` value.
The entire mana symbol set is one HTTP request for the font file.
Zero additional DOM nodes — the `<span>` elements already exist; the glyph is painted by the browser's text renderer.
The text renderer is fast, handles scaling automatically, and stays out of the DOM.

## The Official Mana Font

Beyond the mana symbols, the app uses two typefaces from physical Magic cards — Beleren Bold for card names and MPlantin for oracle text — and the same loading strategy applies to all three.
But the mana symbols are where the approach is most visible, so they are the right place to start.

[Mana font](https://github.com/andrewgioia/mana) (v1.12.3) is an open-source icon font for the full set of Magic symbols.
It assigns each symbol to a Unicode Private Use Area (PUA) codepoint — characters in the range `U+E600`–`U+E9FF` that are not assigned meaning by the Unicode standard and are available for private use.
The CSS maps those codepoints to `::before` pseudo-element content.

```css
.ms-w::before { content: "\e600"; }
.ms-u::before { content: "\e601"; }
.ms-tap::before { content: "\e61a"; }
```

A `<span class="ms ms-w ms-cost"></span>` renders the white mana pip with no image request and no SVG in the DOM.

The problem with the full font: it covers every Magic set symbol, every loyalty counter variant, and every obscure token symbol.
The full Mana font is 200–300 KB.
We need 64 symbols.

## Subsetting to 64 Glyphs

`pyftsubset` from the [fonttools](https://github.com/fonttools/fonttools) library strips any glyph not in the requested set.
The subsetting script ([`scripts/subset_mana_font.py`](https://github.com/jbylund/sylvan_librarian/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/scripts/subset_mana_font.py)) downloads the font from GitHub, runs `pyftsubset` with the PUA Unicode ranges for the 64 symbols the app actually uses, and writes both WOFF2 and WOFF outputs.
The core invocation is:

```bash
pyftsubset mana-original.woff2 \
  --output-file=mana-subset.woff2 \
  --flavor=woff2 \
  --unicodes=U+E600-E6FF,U+E900-E9FF \
  --layout-features=* \
  --desubroutinize
```

The `--unicodes` range covers exactly the PUA block the Mana font uses: 64 glyphs covering basic colors, generic mana 0–16, variables, special symbols (tap, untap, energy, snow), ten 2-color hybrids, five generic hybrids (2/W, 2/U…), five Phyrexian hybrids, and ten 3-color Phyrexian combinations.

The result: the subset WOFF2 is under 40 KB.
That is an 80–90% reduction from the full font.
The full font is not even an option worth loading — it would take longer to download than the page takes to render.

## From Glyph to Rendered Pip

The `@font-face` declaration points at the CloudFront-hosted subset:

```css
@font-face {
  font-family: "Mana";
  src: url('https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/mana/mana-subset.woff2') format('woff2'),
       url('https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/mana/mana-subset.woff') format('woff');
  font-weight: normal;
  font-style: normal;
  font-display: swap;
}
```

The `font-display: swap` line matters: without it, the browser will show a blank where the symbol should be until the font finishes loading.
With `swap`, it falls back to text (the raw `{W}` string) and swaps to the glyph when the font arrives.
For symbols displayed in oracle text inline with readable prose, a brief flash of the raw string is less jarring than an invisible hole.

Hybrid mana symbols use both `::before` and `::after` to layer two half-glyphs, with a CSS gradient as the background:

```css
.ms-cost.ms-wu {
  background: linear-gradient(135deg,
    var(--ms-mana-w) 0%, var(--ms-mana-w) 50%,
    var(--ms-mana-u) 50%, var(--ms-mana-u) 100%);
}
.ms-cost.ms-wu::before { content: "\e600"; }  /* white half */
.ms-cost.ms-wu::after  { content: "\e601"; }  /* blue half */
```

That two-glyph layering is the part that initially broke.
The first attempt used a different font (the npm `mana-font` package) and hybrid symbols rendered as two non-overlapping icons side-by-side rather than a single split-pip.
After reverting ([14ff527](https://github.com/jbylund/sylvan_librarian/commit/14ff527)), the fix was switching to andrewgioia/mana directly, whose CSS positions the two pseudo-elements with `absolute` offsets inside the circle.
The test page `api/static/mana-symbols-test.html` exists because of that debugging session — a grid showing all 64 symbols in both emoji and font-rendered form, so it is obvious at a glance when something is off.

## Non-Blocking Load for All Three Fonts

The app actually loads three webfonts, not one.
Physical Magic cards use two typefaces beyond the symbol font:

- **Beleren Bold** for card names and type lines
- **MPlantin** for oracle text (a historical humanist serif, close to Plantin MT)

Both are subsetted to Latin characters only.
Beleren goes from ~58 KB to ~25 KB WOFF2 (57% reduction) — only the characters that appear in English card names.
MPlantin is similarly reduced.

All three fonts use the print/load pattern to avoid blocking initial paint:

```html
<link
  href="https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/mana/mana-subset.css"
  rel="stylesheet"
  type="text/css"
  media="print"
  onload="this.media = 'all'"
/>
<noscript>
  <link href="https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/mana/mana-subset.css"
    rel="stylesheet" type="text/css" />
</noscript>
```

`media="print"` means the browser fetches the stylesheet without blocking render.
`onload="this.media = 'all'"` promotes it to full scope once it arrives.
The `<noscript>` fallback covers browsers with JavaScript disabled, where the `onload` attribute is never executed.
Beleren gets an additional `<link rel="preload">` because it is the most visible font — the site title uses it, and a layout shift there is immediately noticeable.

## The Layout Shift Problem

`font-display: swap` trades invisible glyphs for layout shift.
When Beleren or MPlantin arrive, any element using the system fallback font reflowed to a different size.
On a slow connection, that reflow is visible: text jumps as the font swaps in.

The fix is synthetic fallback `@font-face` declarations with tuned metrics:

```css
@font-face {
  font-family: 'Beleren Fallback';
  src: local('Arial Bold'), local('Liberation Sans Bold');
  font-weight: bold;
  font-style: normal;
  size-adjust: 94.6%;
  ascent-override: 96.2%;
  descent-override: 89.9%;
  line-gap-override: 23.9%;
}
```

The `size-adjust`, `ascent-override`, and `descent-override` values were computed from the actual font metrics using fonttools.
The repo includes [`api/static/font-fallback-tuner.html`](https://github.com/jbylund/sylvan_librarian/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/static/font-fallback-tuner.html), a local debug page that renders the same text in both the real font and a candidate fallback side-by-side, with sliders for each metric override.
You tune `size-adjust` until the line heights match, then `ascent-override` and `descent-override` until descenders align, then commit the values.
Arial Bold, scaled and adjusted to match Beleren's cap height and descender depth, occupies nearly the same number of pixels as Beleren Bold.
When the real font arrives, the reflow is small enough to be invisible.

This only applies to Beleren and MPlantin.
The Mana font renders into `<span>` elements sized in `em` units against the surrounding text, so the swap has no layout impact.

## What a Font Cannot Do

The font approach has one limit: CORS.
Browsers enforce CORS on font files loaded from cross-origin URLs.
The S3 bucket behind the CloudFront distribution needs a CORS configuration that allows `GET` and `HEAD` from any origin, or the font will silently fail to load in some browsers.
The subsetting script configures this automatically when it uploads.

It also means you cannot serve the font from an arbitrary CDN without checking whether that CDN sets the right response headers.
The original setup used `cdn.jsdelivr.net/npm/mana-font@latest` — which works, but loads the full unsorted font and introduces a dependency on an external CDN.
Moving to a self-hosted subset under a domain we control means the font payload is 80–90% smaller and we are not dependent on another service's uptime for core rendering.

Three webfonts, three subsets, one loading pattern, and a handful of CSS numbers derived from actual font metrics: that is the full cost of rendering Magic cards the way they look in print.
The next post covers what happens when the JavaScript that converts `{W}` tokens to `<span>` elements turned out to be the unexpected bottleneck.
