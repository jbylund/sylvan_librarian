---
title: "Autocomplete Without a Round-Trip: Suggestions from a Frozen In-Memory Type List"
date: 2026-09-26
publishDate: 2026-09-26
tags: ["javascript", "frontend", "ux", "rust"]
summary: "When you type t:cre, the search box completes it to t:Creature before any network request leaves your browser. The type list lives in a frozen in-memory index, populated once at page load from the Rust engine."
---

The first time I looked at the autocomplete path, I noticed that completing `t:cr` required filtering all 600+ card types on every keystroke.
A flat array, `.filter()`, `.sort()` — fresh allocation each time a key went down.
The debounce was 50 ms.
The catalog was small enough that it did not matter in practice, but the pattern was wrong.
There was a parallel problem: the catalog needed to arrive in the browser before the user typed their first character, not after.

## The Data Does Not Come from the Database

The first design ran a CTE over `magic.cards` to unnest `card_types` and `card_subtypes` JSONB arrays and count occurrences:

```sql
WITH card_types AS (
    SELECT jsonb_array_elements_text(card_types) AS type_name
    FROM magic.cards WHERE card_types IS NOT NULL
),
card_subtypes AS (
    SELECT jsonb_array_elements_text(card_subtypes) AS subtype_name
    FROM magic.cards WHERE card_subtypes IS NOT NULL
),
card_types_and_subtypes AS (
    SELECT type_name FROM card_types
    UNION ALL
    SELECT subtype_name FROM card_subtypes
),
counted AS (
    SELECT type_name, count(1) AS num_occurrences
    FROM card_types_and_subtypes GROUP BY type_name HAVING count(1) >= 5
)
SELECT type_name, num_occurrences FROM counted ORDER BY type_name
```

That query ran once per server start, cached for an hour.
It was correct but redundant: the Rust engine already holds every card in a memory-mapped archive.
When [PR #545](https://github.com/jbylund/arcane_tutor/pull/545) landed, the SQL query was replaced with a walk over `preferred_indices` — the engine's deduplicated list of one preferred printing per oracle card:

```rust
pub(crate) fn count_common_types(data: &Archived<CardData>) -> HashMap<String, u32> {
    let mut type_counts = [0u32; 14];
    let mut subtype_counts: HashMap<&str, u32> = HashMap::new();

    for &idx in data.preferred_indices.iter() {
        let card = &data.cards[u32::from(idx) as usize];

        // Decode card_types bitmask with bit manipulation — no string work in hot loop
        let mut bits = u16::from(card.card_types);
        while bits != 0 {
            let pos = bits.trailing_zeros() as usize;
            type_counts[pos] += 1;
            bits &= bits - 1;  // clear lowest set bit
        }

        // Subtypes are stored as &str slices borrowed from the mmap archive
        for subtype in card.card_subtypes.iter() {
            *subtype_counts.entry(subtype.as_str()).or_insert(0) += 1;
        }
    }
    // ... convert to owned strings once at the end
}
```

([`lib.rs`, lines 1255–1285](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/card_engine/src/lib.rs#L1255-L1285))

The 14 card supertypes and types — Artifact, Creature, Enchantment, and so on — are encoded as a bitmask.
The hot loop uses `trailing_zeros` plus `bits &= bits - 1` to extract each set bit without branching.
Subtype strings (`&str` slices into the mmap) are accumulated into a `HashMap` keyed by borrowed reference, so no allocation happens per card.
Owned strings are created once at the end.
The result is `{"Creature": 12847, "Instant": 9231, "Goblin": 1134, ...}`.

The `/get_catalog` endpoint wraps the result, adds the same treatment for keywords, and sets a one-hour `Cache-Control` header.
The response is JSON.

## Getting the Catalog to the Browser Before It Is Needed

The catalog fetch starts in the first `<script>` block in `<head>` — before the stylesheet, before the app JS:

```html
<script>
  (function () {
    if (!window.commonCardTypesPromise) {
      function fetchWithRetry() {
        return fetch('/get_catalog')
          .then(function (response) {
            if (response.status === 503) {
              // Engine not ready yet — retry in 30–40 seconds
              var delay = 30000 + Math.random() * 10000;
              return new Promise(function (resolve) {
                setTimeout(resolve, delay);
              }).then(fetchWithRetry);
            }
            if (!response.ok) return { types: {}, keywords: {} };
            return response.json();
          })
          .catch(function () { return { types: {}, keywords: {} }; });
      }
      window.commonCardTypesPromise = fetchWithRetry();
    }
  })();
</script>
```

([`index.html`, lines 8–34](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/static/index.html#L8-L34))

The promise is stored on `window` so the app class can await it later without re-fetching.
By the time the user types their first character, the catalog has usually resolved.
The retry loop handles a cold-start race: if the engine has not finished loading when the page loads, the first `/get_catalog` returns 503, and the fetch silently retries rather than leaving autocomplete permanently broken.

There was a caching bug lurking here.
`CachingMiddleware` caches any non-5xx response.
Before the 503 path existed, a cold-start request returned an empty list — which the middleware happily cached for an hour.
The fix was to make an unloaded engine return 503, which `CachingMiddleware` explicitly skips.

## Why the Lookup Is Fast Per Prefix Letter

When `fetchCommonCardTypes()` resolves, it builds a `CatalogMap` from the JSON payload:

```javascript
class CatalogMap {
  constructor(mapping) {
    this._map = new Map();
    for (const [v, n] of Object.entries(mapping)) {
      const letter = v[0].toLowerCase();
      if (!this._map.has(letter)) this._map.set(letter, []);
      this._map.get(letter).push({ v, n });
    }
    for (const bucket of this._map.values()) {
      bucket.sort((a, b) => a.v.localeCompare(b.v));
    }
  }

  getBestMatch(prefix) {
    const bucket = this._map.get(prefix[0]) ?? [];
    let best = null;
    for (const entry of bucket) {
      const lower = entry.v.toLowerCase();
      if (lower.slice(0, prefix.length) > prefix) break;  // early termination
      if (lower.startsWith(prefix) && (!best || entry.n > best.n)) best = entry;
    }
    return best?.v ?? null;
  }
}
```

([`app.js`, lines 9–43](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/static/app.js#L9-L43))

The map is keyed by first letter.
A lookup for `"cr"` goes directly to the `"c"` bucket — around 90 entries out of 600+ — and scans alphabetically until the candidate's prefix exceeds the query.
Bucket access is O(1); the scan is O(k) where k is the number of entries sharing the prefix, which in practice terminates after a handful of entries.
The result is the highest-frequency match within the scanned window.

The previous implementation did `entries.filter(...).sort(...)` on each keystroke: a fresh allocation, a full scan of all 600+ entries, and a sort, on every keydown.
A `performance.now()` loop in Node 26 on an Apple M5 Max (10,000 iterations, catalog of ~630 entries, JIT warmed) shows the old path at ~2 µs per call; `getBestMatch` on the same catalog is ~0.4 µs — a 5× difference.
Neither number matters for user experience — the network dominates — but the allocation pressure on every keystroke was unnecessary and the pattern does not scale if the catalog grows.
The `CatalogMap` pays its build cost once at catalog load.

## Where Autocomplete Fires

The autocomplete check runs inside `autoCompleteQuery`, which fires on every keystroke via `_processQuery` before the debounced search goes out:

```javascript
autoCompleteQuery(query) {
  const catalogMatch = query.match(/(?:^|\s)(kw|keyword|t|type):([a-zA-Z]{2,})$/i);
  if (!catalogMatch) return query;

  const selector = catalogMatch[1].toLowerCase();
  const prefix   = catalogMatch[2].toLowerCase();
  const catalog  = (selector === 'kw' || selector === 'keyword')
                 ? this.keywordMap : this.typeMap;
  const bestMatch = catalog.getBestMatch(prefix);

  if (!bestMatch) return query;
  // ... preserve capitalization, splice completion into the query string
}
```

([`app.js`, lines 412–441](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/static/app.js#L412-L441))

The regex requires at least two letters after the colon.
A single letter like `t:c` would match Creature, Clue, Combat, Conspiracy, and dozens of other types — the highest-frequency result would almost always be Creature, which is rarely what a user means when they have typed only one letter.
At two characters, `t:cr`, the match set narrows enough that `getBestMatch("cr")` returns `"Creature"` (count: ~12,000) with enough confidence to be useful.
The query sent to the server becomes `t:Creature` rather than `t:cr`.

The same structure extends naturally to keywords.
[PR #553](https://github.com/jbylund/arcane_tutor/pull/553) added `count_common_keywords` to the engine (same pattern as `count_common_types`), included the counts in `/get_catalog`, and wired `kw:`/`keyword:` into the same `autoCompleteQuery` branch.
`kw:fl` autocompletes to `kw:flying`; `kw:tr` to `kw:trample`.
The design did not need to change — only the catalog needed a second map.

## What Does Not Autocomplete

The regex anchors to end-of-string: autocomplete only fires when `t:` or `kw:` is the last token in the query.
`t:creature t:dr` will autocomplete the trailing `dr` to `Dragon`; `t:cr name:bolt` will not autocomplete anything because the `t:cr` token is not at the end.

The two-character minimum means single-letter prefixes produce no suggestion.
There is no visible dropdown — the completion happens inline in the query string that gets sent to the server, not in a separate UI widget.
The user sees the results change as the query resolves; they do not see autocomplete candidates to select among.

A visible dropdown would be more explicit, but it requires tracking keyboard focus, intercepting arrow keys and Tab, and keeping a visible overlay in sync with the input — none of which the current single-`<input>` layout has plumbing for.
The inline-completion approach has a genuine failure mode: a user who types `t:cr` and presses Enter before the catalog promise resolves (on a slow connection or a cold server) sends `t:cr` to the server, which matches nothing.
This is uncommon in practice because the catalog fetch starts at the top of `<head>`, before any other blocking resources, and the debounce is 50 ms — but it is a real edge.

The catalog only covers types and keywords.
Card names, set codes, and artist names do not get autocomplete — those require a server round-trip because the candidate space is too large and too dynamic to freeze at page load.
