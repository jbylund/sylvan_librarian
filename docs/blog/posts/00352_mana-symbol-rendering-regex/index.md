---
title: "Mana Symbol Rendering: Regex + Map for 47× Speedup"
date: 2026-11-21
publishDate: 2026-11-21
tags: ["javascript", "frontend", "performance"]
summary: "Replacing a per-symbol replaceAll loop with a single regex pass and a Map lookup cut mana symbol conversion time by 47×. The mechanism, the benchmark, and a gotcha with stateful regex."
---

The profiler said `convertManaSymbols` was the hottest function on the page. I did not believe it.

Mana symbol rendering is the step that turns `{W}{U}{B}{R}{G}` into the five colored glyphs you see
next to a card's name. [The previous post](../00320_fonts-and-mana-symbols/index.md) covers how those
glyphs work — a custom webfont where each symbol occupies a private-use Unicode code point, loaded
once and cached by the browser. The rendering step itself seemed trivial by comparison: look up each
symbol, emit a `<span>`.

It was not trivial. The original implementation was doing something wasteful on every single call.

## What the Old Code Was Actually Doing

```javascript
// Per-call: 70+ new RegExp objects, 70+ replace passes
Object.keys(hybridMap).forEach(symbol => {
  const regex = new RegExp(symbol.replace(/[{}]/g, '\\$&'), 'g'); // created 30 times
  converted = converted.replace(regex, ...);
});

Object.keys(manaMap).forEach(symbol => {
  const regex = new RegExp(symbol.replace(/[{}]/g, '\\$&'), 'g'); // created 40 times
  converted = converted.replace(regex, ...);
});
```

Two loops, 70 symbols total. Each iteration: escape the symbol string, construct a `RegExp` object,
call `.replace()` on the accumulating string. That is 70 fresh object allocations and 70 string
scans per mana cost string. (Modern engines cache compiled regex by source string, so the cost is
allocation overhead rather than full recompilation each time — but 70 allocations per call still
adds up.) And `convertManaSymbols` runs once for the mana cost and again for every oracle text block
on every card rendered.

The complexity is O(symbols × string length) per call. For a 100-card page, with oracle text
averaging a few hundred characters each, that is roughly 7,000 string scans per render.

## The Fix

Show it before explaining it. Here is the new version:

```javascript
// Constructor: done once
this.manaSymbolsMap = new Map(Object.entries({ ...hybridMap, ...manaMap }));
this.manaSymbolsRegex = /\{[^}]{1,5}\}/g;

// Per-call: one regex scan, one Map.get() per match
convertManaSymbols(manaCost, isModal = false) {
  if (!manaCost) return '';
  const symbolClass = isModal ? 'modal-mana-symbol' : 'mana-symbol';
  this.manaSymbolsRegex.lastIndex = 0; // see below
  return manaCost.replace(this.manaSymbolsRegex, match => {
    const replacement = this.manaSymbolsMap.get(match);
    if (replacement === undefined) return match;
    return `<span class="${symbolClass} ${replacement}"></span>`;
  });
}
```

The regex `/\{[^}]{1,5}\}/g` matches any brace-delimited token up to five characters long — enough
to cover three-color phyrexian symbols like `{W/U/P}`, the longest symbol in Scryfall's current
set. The `{1,5}` bound is a correctness assumption: if Scryfall ever adds a symbol longer than five
characters, the pattern will not match it and the raw token passes through unchanged. One scan, and
the callback does a single `Map.get()` for each match. If the symbol is not in the map, it comes
back `undefined` and the original text is returned unchanged — graceful degradation for unknown
future symbols.

The implementation lives in
[`api/static/app.js`](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/static/app.js#L1108-L1126).

## Why Map and Not a Plain Object

`Map` was a deliberate choice over a plain object (`{}`). For a fixed key set loaded at startup,
both are O(1) average. But a plain object carries a prototype chain: a symbol named `{constructor}`
or `{toString}` would collide with built-in properties and return a function instead of `undefined`.
With 70 symbols from a well-defined set, the risk is low — but `Map` eliminates it entirely.

## The Regex Flag Gotcha

The `/g` flag on a regex literal makes the regex stateful: the engine tracks `lastIndex`, the
position where the next search will start. When you call `.replace()` with a global regex, it
scans to the end and leaves `lastIndex` pointing past the final character. Call `.replace()` again
on the same cached regex object without resetting — as happens when the same instance renders the
next card — and the regex starts from the wrong position and finds nothing.

The fix is the `this.manaSymbolsRegex.lastIndex = 0` reset before each call. It is a one-liner,
but it is easy to miss, and the failure mode is silent: the function returns the input unchanged
rather than throwing. A benchmark run in a tight loop would catch it; a manual spot-check of one
card would not.

The alternative is to move the regex literal inside the function, guaranteeing a fresh `lastIndex`
on every call. But that allocates a new `RegExp` object each time — exactly the problem we were
solving. The explicit reset is the right tradeoff.

## Three Approaches, Not Two

Before landing on the simple pattern, I tried the obvious alternative first: build a single
alternation regex from all 70 symbols joined with `|`, longest first so `{W/U/P}` is tried before
`{W/U}` and `{W}`, avoiding prefix ambiguity. That produces a correct single-pass regex, but the
pattern string is over 1,000 characters long and it needs to be regenerated whenever the symbol
table changes.

| Approach | Time (10k × 14 cases) | vs. Original |
|---|---|---|
| forEach loops (original) | 2,118 ms | 1× |
| Cached alternation | 45 ms | 47× |
| Simple pattern + Map (chosen) | 45 ms | 47× |

The two optimized approaches are within measurement noise of each other on this hardware. The simple
pattern wins on legibility: `/\{[^}]{1,5}\}/g` is 12 characters; the alternation pattern is 1,000+.

*(Benchmark: Node.js 22, M2 Pro, 10,000 iterations × 14 test cases covering simple, hybrid,
phyrexian, and special symbols. The test is reproducible:
[`api/tests/test_mana_symbol_performance_comparison.js`](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/tests/test_mana_symbol_performance_comparison.js).
The PR documentation reported 61× on its test machine; running the same benchmark on M2 Pro shows
47×. The discrepancy is machine-level variation in JS engine JIT behavior, not a regression.)*

See [PR #271](https://github.com/jbylund/arcane_tutor/pull/271) for the full diff.

The profiler was right.
