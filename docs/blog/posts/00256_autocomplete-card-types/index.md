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

The first design ran a CTE over `magic.cards` to unnest `card_types` and `card_subtypes` JSONB arrays and count occurrences ([`get_common_card_types.sql`](https://github.com/jbylund/sylvan_librarian/blob/637c98052ba2c5ea41bef6f5f4db453585897765/api/sql/get_common_card_types.sql)):

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
It was the right approach at the time — the Rust engine did not exist yet.
Once the engine landed, it became possible to replace the SQL with a walk over `preferred_indices` directly from the mmap archive.
[PR #545](https://github.com/jbylund/sylvan_librarian/pull/545) made that switch: — the engine's deduplicated list of one preferred printing per oracle card:

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

([`lib.rs`, lines 1255–1285](https://github.com/jbylund/sylvan_librarian/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/card_engine/src/lib.rs#L1255-L1285))

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
              // Engine not ready yet — retry in 10–15 seconds
              var delay = 10000 + Math.random() * 5000;
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

([`index.html`, lines 8–34](https://github.com/jbylund/sylvan_librarian/blob/d5239ab8dbabc01b2d93be8d13f4c816724cd267/api/static/index.html#L8-L34))

The promise is stored on `window` so the app class can await it later without re-fetching.
By the time the user types their first character, the catalog has usually resolved.
The retry loop handles a cold-start race: if the engine has not finished loading when the page loads, the first `/get_catalog` returns 503, and the fetch silently retries rather than leaving autocomplete permanently broken.

There was a caching bug lurking here.
`CachingMiddleware` caches any non-5xx response.
Before the 503 path existed, a cold-start request returned an empty list — which the middleware happily cached for an hour.
The fix was to make an unloaded engine return 503, which `CachingMiddleware` explicitly skips.

## Why the Lookup Is Fast Per Prefix Letter

When `fetchCommonCardTypes()` resolves, it builds a `CatalogMap` from the JSON payload.
The structure went through two iterations.

The first version ([`app.js`, lines 9–44](https://github.com/jbylund/sylvan_librarian/blob/1ad70788c8239adedba5207cf7bf4c661ddefcfe/api/static/app.js#L9-L44)) grouped entries into 26 first-letter buckets, sorted each bucket alphabetically, and scanned on each `getBestMatch` call, breaking early once the candidate's prefix exceeded the query.
Bucket access is O(1); the scan is O(k) where k is the number of entries sharing the first letter.
For a 381-entry catalog the typical `"c"` bucket holds around 90 entries and the scan terminates after a handful of matches.

The second version ([`app.js`, lines 10–62](https://github.com/jbylund/sylvan_librarian/blob/d5239ab8dbabc01b2d93be8d13f4c816724cd267/api/static/app.js#L10-L62)) is a sparse depth-3 prefix trie backed by a flat sorted array.
At construction time, all entries are sorted alphabetically into `_words`, then iterated in frequency-descending order to fill three plain-object lookup tables: `_d1` (one-char prefix → best word), `_d2` (two-char), `_d3` (three-char).
First-write-wins gives the highest-count word at each node without any comparison.
`getBestMatch` returns directly from the table for prefix lengths 1–3; for longer prefixes it binary-searches `_words` to find the first candidate and scans forward.

A benchmark loop in Node 26 on an Apple M5 Max (500,000 iterations, 381-entry catalog, JIT warmed) shows the two implementations at:

| Prefix length | Bucket scan | Sparse trie |
|---|---|---|
| 2 chars (`t:cr`) | ~44 ns | ~9 ns |
| 3 chars (`t:cre`) | ~41 ns | ~6 ns |
| 6+ chars | ~41 ns | ~40 ns |

Neither number is user-perceptible at 381 entries — both implementations are effectively instant.
The trie is faster and the code complexity is acceptable.

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

([`app.js`, lines 430–460](https://github.com/jbylund/sylvan_librarian/blob/d5239ab8dbabc01b2d93be8d13f4c816724cd267/api/static/app.js#L430-L460))

The regex requires at least two letters after the colon.
A single letter like `t:c` would match Creature, Clue, Combat, Conspiracy, and dozens of other types — the highest-frequency result would almost always be Creature, which is rarely what a user means when they have typed only one letter.
At two characters, `t:cr`, the match set narrows enough that `getBestMatch("cr")` returns `"Creature"` (count: ~12,000) with enough confidence to be useful.
The query sent to the server becomes `t:Creature` rather than `t:cr`.

The same structure extends naturally to keywords.
[PR #553](https://github.com/jbylund/sylvan_librarian/pull/553) added `count_common_keywords` to the engine (same pattern as `count_common_types`), included the counts in `/get_catalog`, and wired `kw:`/`keyword:` into the same `autoCompleteQuery` branch.
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
