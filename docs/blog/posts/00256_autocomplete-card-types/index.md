---
title: "Autocomplete Without a Round-Trip: Suggestions from a Frozen In-Memory Type List"
date: 2026-10-10
publishDate: 2026-10-10
tags: ["javascript", "frontend", "ux", "sql"]
summary: "Suggesting the most common card types as the user types a t: query — without a round-trip. Data source, ranking, and the trick of starting the fetch before CSS even loads."
---

A user types `t:dr` in the search box and gets dragon results before they finish the word. No suggestions dropdown, no per-keystroke network request, no debounce on the completion itself. The query reaching the server is already `t:dragon`. Getting there without a per-keystroke round-trip means knowing all the valid types before the user starts.

## Extracting a Ranked Type List From the Database

Magic: The Gathering cards have both types (Creature, Instant, Enchantment) and subtypes (Elf, Warrior, Dragon, Wizard). Both live in separate JSONB columns — `card_types` and `card_subtypes` — and the `t:` query operator matches either. The autocomplete list needs both.

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
with_min_count AS (
    SELECT type_name, count(1) AS num_occurrences
    FROM card_types_and_subtypes
    GROUP BY type_name
    HAVING count(1) >= 5
)
SELECT type_name AS t, num_occurrences AS n
FROM with_min_count
ORDER BY type_name;
```

The `HAVING count(1) >= 5` filter is doing real work. MTG has accumulated thousands of subtypes over 30 years, many printed on a single card or a handful. Without the threshold, a user typing `t:wa` might land on "Wanderer" (one printing) before "Warrior" (hundreds). The cutoff removes the long tail of curiosities and keeps the suggestion list grounded in types the user is actually likely to be looking for. In practice, 381 of the 437 distinct types and subtypes pass the filter; the 56 excluded are mostly one-off subtypes introduced in a single set.

The endpoint returns 8.6 KB of JSON uncompressed; zstd compression brings the wire size to 2.8 KB. It sets `Cache-Control: public, max-age=3600` — the list only changes when a new set releases, so browser and CDN caches can hold it for an hour without staling. The tradeoff is that when a new set does release, types from that set will not appear as suggestions for up to an hour after the server is updated.

## Starting the Fetch Before CSS Loads

The JavaScript class that drives the search UI initializes after the browser has parsed the HTML, fetched and parsed CSS, and executed the deferred application script. For a user who lands on the page and starts typing immediately, the class might not have fully initialized before they are mid-word.

The fix is to start the fetch in the first `<script>` tag in `<head>`, before any other resource request ([index.html:7–22](https://github.com/jbylund/arcane_tutor/blob/f087668/api/static/index.html#L7-L22)):

```html
<head>
  <meta charset="UTF-8" />
  <!-- Start fetching common card types immediately (as early as possible in head) -->
  <script>
    (function () {
      if (!window.commonCardTypesPromise) {
        window.commonCardTypesPromise = fetch('/get_common_card_types')
          .then(response => {
            if (!response.ok) throw new Error('Failed to fetch common card types');
            return response.json();
          })
          .catch(() => []);
      }
    })();
  </script>
  <link rel="stylesheet" href="/static/app.css" />
  ...
```

The promise is stored on `window` so the application class can pick it up when it does initialize — even if the fetch has already resolved by then. The class `fetchCommonCardTypes` method becomes a one-liner:

```js
async fetchCommonCardTypes() {
  this.commonCardTypes = await (window.commonCardTypesPromise || Promise.resolve([]));
}
```

This is the change from [PR #392](https://github.com/jbylund/arcane_tutor/pull/392). The original implementation initiated the fetch inside `fetchCommonCardTypes`, which meant waiting for class construction. Moving the fetch to the top of `<head>` means the browser can issue the request in parallel with everything else it needs to load — the CSS, the fonts, the script itself.

On a warm cache, the endpoint returns in 26–48ms end-to-end (server-side total: ~0.2ms; the rest is network latency). A cold-cache request hits the database, taking ~180ms server-side and ~220ms end-to-end. Either way, the fetch is in flight while the browser parses CSS and executes the application script, so on most page loads the type list is resolved before the first search can fire.

If the fetch fails, `.catch(() => [])` resolves the promise to an empty array. The application class picks that up, `this.commonCardTypes` is empty, and autocomplete silently does not fire. No error is shown, no behavior changes other than the completion not happening — the search still works, just without type expansion.

## Matching by Prefix, Ranked by Frequency

The autocomplete runs inside `_processQuery`, which is called when the debounce fires — once per pause, not once per keypress. It looks for a `t:` or `type:` token at the end of the query string — and only at the end, so `t:elf power>3` does not retroactively complete `t:elf` after the user moves on ([app.js:370–418](https://github.com/jbylund/arcane_tutor/blob/f087668/api/static/app.js#L370-L418)):

```js
autoCompleteQuery(query) {
  const typeMatch = query.match(/(?:^|\s)(?:t|type):([a-zA-Z]*)$/i);
  if (!typeMatch) return query;

  const prefix = typeMatch[1].toLowerCase();
  if (prefix.length < 2) return query;

  // Filter by prefix, then take the most frequent match
  const bestMatch = this.commonCardTypes
    .filter(type => type.t.toLowerCase().startsWith(prefix))
    .sort((a, b) => b.n - a.n)[0];

  if (!bestMatch) return query;
  ...
}
```

The two-character minimum prevents the list from completing too eagerly. `t:c` alone would complete to `t:creature` — correct, but potentially confusing if the user is about to type `t:cat` or `t:changeling`. Two characters reduces the noise enough that completions are almost always what the user intended.

Frequency ranking means the most-printed type wins when multiple types share a prefix. `t:dr` completes to `t:dragon` rather than `t:drake` or `t:dryad`: Dragon appears on 1,499 cards, Drake on 249, Dryad on 158.

## Preserving What the User Typed

The completion mirrors the user's capitalization. If the user typed `t:DR`, the completion is `t:DRAGON`. If they typed `t:Dr`, the typed characters are kept as-is and only the remainder is appended:

```js
let completedType = bestMatch.t;
if (originalPrefix === originalPrefix.toUpperCase()) {
  completedType = bestMatch.t.toUpperCase();
} else if (originalPrefix === originalPrefix.toLowerCase()) {
  completedType = bestMatch.t.toLowerCase();
} else {
  // Mixed case: keep what was typed, append the rest
  completedType = originalPrefix + bestMatch.t.slice(originalPrefix.length);
}
```

The parser lowercases type values before matching against the database, so capitalization is irrelevant for correctness. But users who type `t:Dragon` get `t:Dragon` back, not `t:dragon`. Changing what is in the input box without the user's action would be surprising; rewriting their casing while their cursor is still in that token would be worse.

There is no visible UI affordance for any of this. No ghost text, no dropdown, no underline. The user types `t:dr`, the search fires for `t:dragon`, and dragon cards appear. If they did not want dragon, they keep typing and `t:dra` completes to the same thing — `t:dragon` is still the top result — until they have enough characters to distinguish their actual intent.

A dropdown with live suggestions is the conventional alternative, but it does not fit the existing search UI and would require a network round-trip per keystroke. Since the type list changes only when a new set releases — a few times a year — a single cached fetch at page load is faster in practice than per-keystroke lookups, and requires no additional UI state to manage.

The constraint that limits the whole approach is the one that makes it viable: card types change only when a new set releases. A dataset that changes daily would need a different design. This one does not.
