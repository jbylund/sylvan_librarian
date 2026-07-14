# Query Log Middleware

**Branch:** `joe/query_log_middleware` → `main`

## What

Adds a `magic.query_log` table and a `QueryLogMiddleware` that records one row per `/search`
request, enabling search performance analysis and slow-query identification.

## Changes

### `api/db/2026-05-20-03-query-log.sql`

New table with columns for query string, cache hit flag, DB execute/fetch times, total wall-clock
ms, result count, and error flag; indexes on `logged_at DESC` and `execute_ms DESC`; enables
`pg_stat_statements`.

### `api/middlewares/query_log_middleware.py`

- Background writer thread inserts rows without blocking request processing.
- `threading.Event` stop signal + `queue.get(timeout=0.05)` poll loop; `stop()` method for clean
  shutdown and testing.
- Bounded queue (`maxsize=10_000`); `put(timeout=0.001)` drops with a warning on overflow.
- `_connect` returns `None` when no `PG*` env vars are set; drain loop drops entries and logs a
  warning every 100 drops so the condition isn't lost in startup noise.
- `had_error` checks both `req_succeeded` and `resp.status[:1] in ("4", "5")` — handles
  `falcon.HTTPError` responses (e.g. 400 for invalid queries) that leave `req_succeeded=True`.
- Path check uses `req.path.strip("/") == "search"` to match `/search/` as well.
- Timing extraction traverses `inner_timings._children.<name>._meta.duration_ms`; the previous
  `inner.get("execute_query")` always returned `None`.
- Non-dict `resp.media` logs a warning before returning.

### `api/middlewares/caching_middleware.py`

Injects `cache_hit: true` into a copy of the response media on cache hits so `QueryLogMiddleware`
can distinguish them from misses without mutating the cached response.

### `api/middlewares/tests/`

- `test_query_log_middleware.py` — 11 new tests covering path filtering, field population, timing
  extraction, cache-hit null timings, `had_error` from both `req_succeeded=False` and 4xx/5xx
  status, queue-full warning, and stop-event thread exit.
- `test_caching_middleware.py` — two new tests asserting the `cache_hit` flag is set on cache hits
  and the original cached media is not mutated.

### `api/utils/__init__.py`

Removed eager `from api.utils import db_utils, error_monitoring` imports. These triggered a falcon
circular import during test collection when `QueryLogMiddleware` was added to the middleware
package `__init__.py`.

### `api/api_worker.py`

Registers `QueryLogMiddleware` in the middleware stack.

### `docs/issues/local-pg-creds-testcontainers-fallback.md`

Tracks the broader issue of `make_pool`'s silent testcontainers fallback — the right fix is to
route explicitly on `PYTEST_CURRENT_TEST` rather than falling back silently in production.

## Schema

```sql
CREATE TABLE magic.query_log (
    id           BIGSERIAL PRIMARY KEY,
    logged_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    q            TEXT,
    orderby      TEXT,
    unique_by    TEXT,
    cache_hit    BOOLEAN NOT NULL DEFAULT false,
    execute_ms   REAL,   -- NULL on cache hits
    fetch_ms     REAL,   -- NULL on cache hits
    total_ms     REAL,
    result_count INTEGER,
    total_cards  INTEGER,
    had_error    BOOLEAN NOT NULL DEFAULT false
);
```

## Test plan

- [ ] Apply migration against a local DB and confirm table + indexes exist
- [ ] Start the API (`make dev-up`) and run a few searches; confirm rows appear in `magic.query_log`
- [ ] Confirm cache hits are logged with `cache_hit = true` and NULL DB timings
- [ ] Confirm a failed request (invalid query) sets `had_error = true`
- [ ] Restart the API mid-load; confirm the background writer reconnects without dropping the process
- [ ] `make test-unit` passes
