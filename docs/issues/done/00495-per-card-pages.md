# Per-card pages

## Goal

Give every card printing a shareable URL so users can link to a specific card. Ctrl/middle-click a
card image in search results opens the card page in a new tab; plain click continues to show the
modal.

## Route

```
/card/{set_code}/{collector_number}
```

Examples: `/card/lea/1`, `/card/m21/100a`

Set+collector is unambiguous and avoids the URL-encoding hazards of card names (commas, slashes,
`//` in split cards).

## Page structure

The card page is a standalone HTML shell (a new template alongside `index.html`) served by a new
action in the `APIResource` action map in [api_resource.py](../../../api/api_resource.py). JavaScript
takes over from there:

1. **Card data** — fetched from the existing `/search` endpoint using `set:{set_code} cn:{collector_number}`,
   which returns exactly one result. No new backend endpoint needed.

2. **Card face** — rendered with the same markup and CSS as the modal
   (`modal-card-info`, `modal-card-name-mana-row`, etc.) so there is nothing new to style. The
   card image itself links out to `manapool.com/card/{set_code}/{collector_number}` (same
   referral-tagged URL and `.modal-image-link` styling as the search-results modal in
   [app.js](../../../api/static/app.js)).

3. **Other printings** — fetched by firing `!"<card name>"` at the existing `/search` endpoint
   with `fields=set_code,collector_number,set_name,illustration_id,price_usd,prefer_score` (see
   [00574-engine-custom-field-selection.md](./00574-engine-custom-field-selection.md)). The current printing is
   filtered out client-side (match on set code + collector number); the rest are grouped
   client-side by `illustration_id` — printings sharing art collapse into one thumbnail, badged
   `+N` for the collapsed count, with the highest-`prefer_score` printing in the group chosen as
   the representative. Groups are rendered as a wrapping grid (`.printings-strip`, CSS grid with
   `minmax(160px, 1fr)` columns, not a horizontal scroll strip) ordered by each group's max
   `prefer_score`, descending.

## Click behavior on search results

Card images are wrapped in `<a href="/card/{set_code}/{collector_number}">`. Plain clicks are
intercepted with `event.preventDefault()` to show the modal as today. Ctrl-click, middle-click,
and right-click → "Open in new tab" all work for free because of the underlying anchor.

The `showCardModal` path in [app.js](../../../api/static/app.js) (line ~796) remains unchanged — it
already receives full card data from the search result row.

## What is not in scope

- Name-slug routes (`/card/lightning-bolt`) — not worth the normalization complexity when the
  set/collector URL is already shareable.
- Rulings, price history, or any data beyond what `/search` already returns.
- Other printings in the modal — that section lives on the card page only.

## Implementation order

1. New `card.html` template + server route for `/card/{set_code}/{collector_number}` in `api_resource.py`.
2. JS on the card page: `GET /search?q=set:{set_code}+cn:{collector_number}`, render card face from the first result, then `GET /search?q=!"{card_name}"` and render other printings (excluding current).
3. Wrap search result images in anchors; intercept plain click to preserve modal behavior.

## Future: combining the two fetches

The card page currently makes two sequential `/search` calls (card face, then other printings).
They can't just be merged into a single `!"<card name>"` query as-is — the second query needs the
card's name, and the only thing we have from the URL is `set_code`/`collector_number`; the first
call is what tells us the name in the first place.

Options to get down to one round trip from the browser:

- **Embed the name server-side.** `card()` already knows `set_code`/`collector_number` when it
  renders `card.html`. It could look up the card there and embed the name into the page, so
  `card.js` only needs one `!"<name>"&unique=printing` fetch and loops over the results to split
  out the matching printing from the rest. Downside: `_build_card_html` is currently cached purely
  by `critical_css` (not per-card), so this adds a per-request lookup and gives up that cache.
- **A dedicated endpoint**, e.g. `get_card_and_printings(set_code, collector_number)`, that makes
  two calls against `self._engine` server-side (first to resolve the card at that printing, second
  by name for siblings) and returns both in one JSON response. Since `self._engine` is in-process
  (see `self._engine.query(...)` in [api_resource.py](../../../api/api_resource.py)), two engine calls
  server-side cost nothing like two browser round trips would — this is likely the best path if we
  revisit this.

Decided to leave the current two-fetch client-side approach as-is for v0; revisit if the extra
round trip proves to matter in practice.

## Related

- [local-format-legality-search.md](../local-format-legality-search.md) — example of another frontend-visible
  feature with a backend query change.
- [00574-engine-custom-field-selection.md](./00574-engine-custom-field-selection.md) — the `fields=` mechanism
  the "other printings" fetch uses to pull `illustration_id`/`price_usd`/`prefer_score`; also
  covers the SQL-path parity work (`RESULT_FIELD_COLUMNS` in api_resource.py) needed so field
  selection behaves identically whether the engine or SQL serves the request.
