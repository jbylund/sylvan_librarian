---
title: "40× Faster Card Rendering: Why the Safe Approach Was Creating 1,400 Throwaway DOM Nodes"
date: 2027-05-22
publishDate: 2027-05-22
tags: ["javascript", "frontend", "performance"]
summary: "createCardHTML called escapeHtml up to 14 times per card. The old DOM-node implementation looked safe and worked correctly — except for double quotes inside attributes. A single-pass regex fixed both the performance and the bug."
---

The first time I looked at the profiler output for a 100-card render, I expected the bottleneck to be image loading or DOM mutation. It was a three-line function that escaped HTML entities.

```javascript
escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}
```

That pattern is everywhere on the internet. It reads as obviously correct: assign text content, read back the escaped HTML the browser produces. It even handles edge cases the author never thought of. And it was allocating a throwaway DOM element on every single call.

## Why 14 Calls Per Card Adds Up Fast

`createCardHTML` builds the full HTML string for one search result: the image srcset, the card name, type line, set name, oracle text, power/toughness, and the `alt` and `title` attributes on the `<img>` tag. Every field that touches the DOM needs escaping. Count the calls per card render when all branches execute:

```javascript
// 4 URL escapes for the responsive image srcset
const srcset = `${this.escapeHtml(image280)} 280w, ${this.escapeHtml(image388)} 388w,
                ${this.escapeHtml(image538)} 538w, ${this.escapeHtml(image745)} 745w`;

// 1 for the default src
const imageHtml = `<img src="${this.escapeHtml(image388)}" srcset="${srcset}" …`;

// up to 3 for alt text (name, mana cost, oracle text)
// 1 for data-card-id, 1 for card name, 1 for type_line
// 1 for set_name, 2 for power and toughness
```

Up to 14 calls per card. For a 100-card result page: 1,400 `document.createElement('div')` calls — elements created, used for one string transformation, and immediately abandoned.

The [full createCardHTML function](https://github.com/jbylund/arcane_tutor/blob/63df43fb1a59e13616f4a770f5c65d3dd246bc58/api/static/app.js#L720-L796) shows all the call sites.

## The Latent Double-Quote Bug

Before getting to the numbers, there is a correctness problem worth naming. The DOM approach converts text to HTML by setting `.textContent` and reading back `.innerHTML`. Browsers encode `<`, `>`, and `&` this way because they are meaningful in HTML text content. But `"` inside a text node is valid unescaped — browsers do not need to encode it to parse the node correctly.

That is fine when the result goes into element content. It is not fine when the result goes into a double-quoted attribute. The `alt` attribute on the card image is built by concatenating the card name, the mana cost as Unicode text, and a 300-character truncated excerpt of oracle text. Magic oracle text does contain double quotes — reminder text in cards like Ixalan's *Commune with Dinosaurs* uses them. After escaping each piece with the DOM approach, those quotes survive unencoded. The concatenated result then lands directly in the template:

```javascript
altText += this.escapeHtml(truncatedText);  // " passes through unchanged
// …
`<img … alt="${altText}" title="${altText}" />`
//          ^^^ " here closes the attribute early
```

The DOM approach passed it through without encoding it. The regex approach encodes `"` as `&quot;`, which is correct in both text content and attribute values. [PR #486](https://github.com/jbylund/arcane_tutor/pull/486) fixed it alongside the performance change.

## Three Constants Replace the Whole Pattern

Hoisted to module scope so they are not re-allocated on each call:

```javascript
const HTML_ESCAPE_RE = /[&<>"]/g;
const HTML_ESCAPE_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' };
const htmlEscapeChar = c => HTML_ESCAPE_MAP[c];

escapeHtml(text) {
  if (text === null || text === undefined) return '';
  return String(text).replace(HTML_ESCAPE_RE, htmlEscapeChar);
}
```

The `g` flag is load-bearing — without it, `replace` stops after the first match. Single quotes are intentionally absent from the map: all attributes in this codebase use double quotes, and single quotes are safe in HTML text content.

The [current implementation](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/static/app.js#L1081-L1088).

## Benchmarks: 2,005 ns → 50 ns Per Call

The benchmark at [`scripts/bench_escape_html.js`](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/scripts/bench_escape_html.js) runs both implementations over 14 representative card strings — names with apostrophes, oracle text with angle brackets, mana symbol notation, image URLs, and a long 40-word oracle text. The DOM implementation uses jsdom v26.1.0's `document.createElement`, which is the same DOM API the browser exposes. The benchmark verifies both implementations produce identical output for all strings before timing either.

Results on a MacBook Pro M3 Max, Node.js v26.0.0, 500,000 iterations with a 1,000-iteration warm-up:

```
Per-call (500,000 iterations):
  DOM              1002.5 ms  |  2005 ns/call
  Regex              24.8 ms  |    50 ns/call

Simulated render (1,400 calls per render, 500 renders):
  DOM                2.73 ms/render
  Regex              0.07 ms/render
```

40× per call; 39× per render.

One caveat on the ratio: the DOM implementation uses jsdom v26.1.0's `document.createElement`, not Chrome's Blink. jsdom is a pure-JS DOM implementation; its `createElement` carries more overhead than Blink's C++ allocator, which means the 40× figure likely overstates the in-browser gain. A rough estimate: Chrome's DOM allocation is typically 5–10× faster than jsdom's for throwaway elements, which would put the real-browser gain somewhere in the 5–15× range rather than 40×.

What does not change across environments is the allocation structure. The regex path allocates no heap objects per call — the pattern and callback are module-level constants. The DOM path allocates one GC-traced node on every call, regardless of runtime. Even at a conservative 5× real-browser speedup, the improvement is meaningful at 1,400 calls per render.

## What the DOM Approach Was Actually Doing

The objection worth voicing: "browser DOM operations are fast and well-optimized." That is true for elements that stay in the document. Throwaway elements — allocated, used once, and abandoned without being attached to the tree — impose allocation and GC pressure without benefiting from any of the rendering optimizations that make real DOM work fast. Every `createElement` call allocates memory, wires up a prototype chain, and registers the node with the document's internal state. The GC collects it eventually, but not before you have done it 1,400 times.

This is the category of bug that looks correct in isolation and only shows up under a usage profile the original author did not anticipate. The pattern is fine for an occasional call. It is not fine as a hot loop inside a render function.

## Where This Helps and Where It Does Not

The fix works because `escapeHtml` here is stateless and its contract is narrow: convert four ASCII characters to their HTML entity equivalents in plaintext. A regex over `[&<>"]` is exactly the right tool for that.

It would not generalize to stripping tags, sanitizing HTML with allowed elements, or handling malformed input — those require a real sanitizer. If the input is already HTML that should pass through unchanged, escaping it would break the output.

The 2.8 ms shown in the benchmark table is not interesting because it moves the page-load needle — network and database time swamp it. It is interesting because it was 2.8 ms of synchronous JavaScript on the main thread, blocking layout and input, spent allocating objects that were thrown away immediately. The DOM pattern looks like it is delegating work to the browser. It is actually creating work for the browser's allocator and GC.

The right tool for plaintext-to-HTML encoding is not the browser's own HTML parser.
