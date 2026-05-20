# Simplify and Fix Search Request Lifecycle

**Date:** 2026-03-26  
**PR:** #446

## Overview

Rewrites the debounce/cancellation/result-rendering logic in `app.js` around a cleaner set of
invariants, fixing several bugs. Only `app.js` changed.

## Problems Fixed

- **`clearSearch()` didn't abort in-flight requests.** A pending fetch could complete and repopulate
  results after the user clicked the header to clear the search.
- **`clearSearch()` didn't clear `lastRequestedUrl`.** Re-typing the same query after clearing
  would be silently skipped.
- **Two conflated staleness mechanisms** (`lastRequestedUrl` + `requestId`) made the lifecycle hard
  to reason about and left a window where stale responses could still render after an abort.
- **`showLoading()` was immediately followed by `clearMessages()`**, erasing the "Loading…" text.
- **In-flight requests weren't aborted during the debounce window**, so an older request could
  complete and flash stale results while the user was still typing.

## New Design

**State reduced from four fields to three:**

| Field | Purpose |
|-------|---------|
| `debounceTimeout` | Debounce timer handle |
| `currentController` + `currentRequestUrl` | In-flight AbortController and its URL |
| `lastCompletedUrl` | URL whose results are currently displayed; `null` when results are cleared |

`requestId` is removed. `lastRequestedUrl` is replaced by the split between `currentRequestUrl`
(in-flight) and `lastCompletedUrl` (completed).

**Single staleness mechanism:** stale responses are detected by checking
`controller.signal.aborted` after every `await` point. Since JS is single-threaded, this closes
all gaps.

**Unified dedup in `performSearch`:**
- Skip if same URL is in-flight and not yet aborted.
- Skip if `lastCompletedUrl` already matches (results already showing).

**All clear paths are consistent:** `clearSearch()`, the empty-query branch of `handleSearch`, and
the `popstate` empty-query branch all abort the in-flight request, clear `lastCompletedUrl`, and
clear the UI in the same order.

**`_processQuery` helper** consolidates the autocomplete → balance → normalize pipeline,
previously duplicated across callers.

**Early abort during debounce:** `handleSearch` now aborts the in-flight request immediately when
the processed query has changed, rather than waiting for the debounce timer to fire.
