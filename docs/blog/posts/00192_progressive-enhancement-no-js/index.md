---
title: "Progressive Enhancement: Two Card Renderers for the Price of One"
date: 2026-08-29
publishDate: 2026-08-29
tags: ["javascript", "frontend", "progressive-enhancement"]
summary: "/?q=fire always returns fully rendered HTML — no JavaScript required. JS layers typeahead and live updates on top. The cost: two card renderers that have to stay in sync."
---

Supporting a no-JS path means maintaining two card renderers that must produce the same HTML.
In this codebase that is `noscript_helpers.py` on the server and `createCardHTML()` in JavaScript —
roughly 250 lines of Python duplicating the work of the JS renderer,
with no automated parity check between them.
When they diverge, they diverge silently.

The no-JS path is a plain HTML form:
submit it and get a fully rendered page of results, no JavaScript required.
JavaScript layers on top of that baseline — live search, autocomplete, no page reloads.
The sections below trace how the two paths are built,
how they share work,
and where that maintenance cost accumulates.

## How the Form Works Without JavaScript

The search form is a standard `<form>` with `method="get"` and `action="/"`.
Submitting it sends the browser to `/?q=fire&orderby=edhrec&unique=card&prefer=default&direction=asc`.
The server receives that request, runs the search, renders the results server-side,
and returns a complete HTML page with cards already in it.

The server-side rendering happens in
[`noscript_helpers.py`](https://github.com/jbylund/sylvan_librarian/blob/22d06df7106284160125518c098881409ebe15bc/api/noscript_helpers.py#L171)
([PR #319](https://github.com/jbylund/sylvan_librarian/pull/319)).
It generates the same card HTML that the JavaScript renderer produces:
responsive `<img>` tags with a four-size `srcset`,
mana symbols converted to CSS icon spans,
oracle text truncated at 200 characters.
The truncation is mana-symbol-aware —
a naive cut at 200 characters could land inside a token like `{W/U}`, producing broken HTML.
The [fix](https://github.com/jbylund/sylvan_librarian/blob/22d06df7106284160125518c098881409ebe15bc/api/noscript_helpers.py#L240)
counts open and close braces and backs up to before the last `{` if they are unbalanced:

```python
if len(oracle_text) > MAX_ORACLE_TEXT_LENGTH:
    truncated = oracle_text[:MAX_ORACLE_TEXT_LENGTH]
    if truncated.count("{") > truncated.count("}"):
        truncated = truncated.rpartition("{")[0]
```

The first four cards get `fetchpriority="high"` and eager loading; the rest are lazy.

When JavaScript is present,
it intercepts the form's `submit` event and calls `e.preventDefault()`,
then runs the search through the JSON API instead.
Without JavaScript, the form submits normally and the server handles everything.

## Saving a Round Trip on Shared URLs

Clicking a link to `/?q=lightning+bolt` and typing `lightning bolt` into the search box produce the same URL.
That means a shared search link is also a valid form submission —
and the server treats it the same way, running the search and embedding results in the HTML.

When JavaScript initializes on that page,
it would normally look at the query in the URL and fire a fetch to `/search?q=lightning+bolt` to get the results.
That is a wasted round trip:
the server already ran the same search to build the page the browser just loaded.

To avoid it, the server embeds the search results as JSON directly in a `<script>` block
([PR #288](https://github.com/jbylund/sylvan_librarian/pull/288)).
On a warm cache, a `/search` request returns in 30–50ms and transfers 1–36 KB compressed depending on result count
(measured with `curl` from a MacBook Pro M5 Max over the public internet to the production server,
median of runs 2–3 to exclude cold-cache effects).
The embedded JSON skips that round trip entirely for shared-URL loads:

```python
search_results_json = orjson.dumps(search_results).decode("utf-8")
embedded_data = f"window.EMBEDDED_SEARCH_RESULTS = {search_results_json};"
html_content = html_content.replace("<!-- SERVER_SIDE_EMBEDDED_DATA -->", embedded_data)  # index.html L215
```

On initialization, the JS checks for `window.EMBEDDED_SEARCH_RESULTS`.
If it exists, `displayResults()` is called directly with the data — no fetch:

```javascript
if (window.EMBEDDED_SEARCH_RESULTS) {
  this.displayResults(window.EMBEDDED_SEARCH_RESULTS, initialQuery, null);
  delete window.EMBEDDED_SEARCH_RESULTS;
} else {
  this.performSearch(initialQuery);
}
```

There is a second optimization layered on top.
[`displayResults()`](https://github.com/jbylund/sylvan_librarian/blob/22d06df7106284160125518c098881409ebe15bc/api/static/app.js#L624)
checks whether the server has already rendered cards into the DOM:

```javascript
const hasSSRContent = this.resultsContainer && this.resultsContainer.children.length > 0;
if (!hasSSRContent) {
  this.resultsContainer.innerHTML = cards.map(...).join('');
}
```

If the SSR cards are already there, JS skips re-rendering entirely.
This preserves any image loads the browser has already started from the HTML —
discarding and re-inserting the `<img>` tags would cancel those in-flight requests
and delay the largest contentful paint.

## Live Search: Debounce, Abort, Replace

Once the page is loaded, the JavaScript search path takes over for subsequent queries.
Every `input` event calls
[`handleSearch()`](https://github.com/jbylund/sylvan_librarian/blob/22d06df7106284160125518c098881409ebe15bc/api/static/app.js#L365),
which manages debounce and in-flight request cancellation:

```javascript
handleSearch(query) {
  clearTimeout(this.debounceTimeout);

  // Without this, a slow response for "fir" can arrive after a fast response
  // for "fire", overwriting correct results with stale ones.
  if (this.currentController && !this.currentController.signal.aborted) {
    const inFlightQuery = this.currentRequestUrl
      ? new URLSearchParams(this.currentRequestUrl.split('?')[1]).get('q')
      : null;
    if (inFlightQuery !== this._processQuery(query)) {
      this.currentController.abort();
    }
  }

  this.debounceTimeout = setTimeout(() => {
    this.performSearch(query);
  }, 50);
}
```

The 50ms debounce is short by the standards of most typeahead implementations — many use 200–300ms.
It only needs to prevent firing multiple requests for a single fast keystroke,
not to throttle expensive work.

`performSearch()` fetches `/search?q=...` with `Accept: application/json`.
When the response arrives,
`displayResults()` replaces `resultsContainer.innerHTML` with the new cards
and updates the URL with `history.replaceState` so the current query is bookmarkable.

JS also caps the grid column count based on how many cards were returned —
a query that matches two cards should not render them in a four-column grid with two empty slots.
The server cannot do this:
it has no knowledge of the viewport and cannot predict how many columns CSS will produce.
On the no-JS path the grid reflows correctly as the viewport narrows,
but it will not collapse columns to match a small result set.

## The Ongoing Cost of the No-JS Path

The cost of the no-JS path is `noscript_helpers.py`.
Both renderers produce the same structure —
responsive srcset with the same four image sizes and the same `sizes` breakpoints,
mana symbols as `<span>` elements with the same CSS classes,
oracle text at the same 200-character limit.

Keeping them in sync is a real maintenance burden.
[PR #520](https://github.com/jbylund/sylvan_librarian/pull/520) has a concrete example of drift:
`noscript_helpers.py` was not rendering the card name and mana cost on the same line that `createCardHTML()` was.
The discrepancy was caught during a latency audit — not through any dedicated parity check.
When the image breakpoints were tuned, the `sizes` attribute had to be updated in two places.
There is no test that enforces parity between them.

Whether the no-JS path is worth maintaining is a fair question.
The honest answer is that most users have JavaScript enabled,
the path is mostly invisible,
and it rarely breaks.
But when it does fall out of sync, it falls out of sync silently —
the no-JS version just shows something slightly wrong.
That is the kind of bug that gets filed by someone running a screen reader or a CLI browser,
not by a typical user,
which means it tends to stay unfixed longer than it should.
