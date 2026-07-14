# Autocomplete Type Lookup: Partitioned Map with Early Termination

## Background

`autoCompleteQuery` in `api/static/app.js` completes `t:` queries against a
client-side list of all 437 card types and subtypes fetched at page load.
The current implementation filters and re-sorts the full list on every debounce:

```js
const bestMatch = this.commonCardTypes
  .filter(type => type.t.toLowerCase().startsWith(prefix))
  .sort((a, b) => b.n - a.n)[0];
```

This is O(n) filter + O(k log k) sort on every completion, where n=437 and k is
the number of types sharing the prefix.

## Proposed Optimization

The SQL endpoint returns types ordered alphabetically by `type_name`. That ordering
can be exploited: build a `Map` from first letter to the alphabetically-sorted
sublist at `fetchCommonCardTypes` time, then at query time look up the bucket in O(1)
and scan only those types — terminating early once the type's first `prefix.length`
characters exceed the prefix.

```js
// Build once at init (types already alphabetically sorted from SQL)
buildTypeMap(commonCardTypes) {
  const map = new Map();
  for (const type of commonCardTypes) {
    const letter = type.t[0].toLowerCase();
    if (!map.has(letter)) map.set(letter, []);
    map.get(letter).push(type);
  }
  return map;
}

// Replace the filter+sort in autoCompleteQuery
findBestMatch(typeMap, prefix) {
  const bucket = typeMap.get(prefix[0]) ?? [];
  let bestMatch = null;
  for (const type of bucket) {
    const typeLower = type.t.toLowerCase();
    if (typeLower.slice(0, prefix.length) > prefix) break; // early termination
    if (typeLower.startsWith(prefix)) {
      if (!bestMatch || type.n > bestMatch.n) bestMatch = type;
    }
  }
  return bestMatch;
}
```

`this.typeMap` would be built inside `fetchCommonCardTypes` alongside
`this.commonCardTypes`, and `autoCompleteQuery` would call `findBestMatch`
instead of the inline filter+sort.

## Benchmark

Tested against live data from `https://sylvan-librarian.com/get_common_card_types`
(381 types, pre-HAVING-removal cache), 24 representative prefixes,
100,000 iterations each:

| Approach | Total (ms) | Per call (µs) |
|---|---|---|
| Current (filter + sort) | 5,235 | 2.18 |
| Map + early termination | 387 | 0.16 |
| **Speedup** | | **13.5×** |

Largest bucket is `s` (51 types); average bucket is ~17 entries.

## Should We Do It?

Both approaches are fast enough that neither will ever be user-visible — 2µs is
lost inside the 50ms debounce. The case for implementing anyway:

- It is the right data structure given alphabetically-sorted input: index once, read cheap.
- The post describing this feature (`docs/blog/posts/00256_autocomplete-card-types/`)
  currently says the linear scan is "already fast enough not to matter," which is true
  but slightly unsatisfying as an explanation.

The case against: adds a `buildTypeMap` call at init and requires `typeMap` alongside
`commonCardTypes` as instance state — small complexity cost for a gain that is
invisible in practice.

**Verdict**: low priority, nice-to-have for post accuracy. Do it if touching `app.js`
for another reason.

## Outcome

Shipped, and the production version goes beyond this proposal. The `CatalogMap` class in
[app.js](../../../api/static/app.js) precomputes best-match answer tables for prefix lengths 1–3
(built frequency-first so first-write-wins stores the highest-`n` word), making short prefixes a
single O(1) property lookup with no scan; prefixes of 4+ characters use binary search over one
flat sorted array. The bucket-scan design proposed above lives on as
[catalogmap.js](../../../api/static/catalogmap.js), alongside an intermediate trie variant
[fanout_catalogmap.js](../../../api/static/fanout_catalogmap.js) — both are comparison baselines
for [scripts/bench_catalog_map.js](../../../scripts/bench_catalog_map.js), not loaded by the app.
