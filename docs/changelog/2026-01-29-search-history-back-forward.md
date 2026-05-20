# Search History and Back/Forward Support

**Date:** 2026-01-29  
**PR:** #414

## Overview

Browser back/forward now navigate between meaningful search states. Previously the app was a
single-page app that never updated browser history, so back/forward either left the SPA or did
nothing useful.

## Behavior

- After a successful search, the URL is updated via `history.replaceState` to reflect the current
  query and dropdown state.
- A new `history.pushState` entry is created only when the user has been viewing a result set for
  at least **2.5 seconds** before moving to a different search. This prevents history spam during
  rapid typing while still bookmarking states the user dwelt on.
- The arrival time is stored in `history.state` so the dwell check works correctly when navigating
  between already-pushed entries.
- `popstate` events restore the search input, dropdowns, and results (via a re-fetch) for the
  popped state, so back/forward correctly return to prior results.

## Implementation

All changes are in `api/static/app.js`. The dwell timer starts when results are first rendered and
is cancelled if the user triggers a new search before it fires. The `popstate` handler reads query
and ordering from `event.state` and calls `performSearch` directly, bypassing the debounce.
