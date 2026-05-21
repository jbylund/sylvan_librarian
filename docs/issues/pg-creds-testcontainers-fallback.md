# Separate test vs. production DB connection paths

## Problem

`make_pool()` in `api/utils/db_utils.py` uses a single code path with a silent fallback:

```python
creds = get_pg_creds()
if not creds:
    creds = get_testcontainers_creds()
```

This conflates two distinct situations:

- **Tests** — no `PG*` env vars are set intentionally; testcontainers is the right backend.
- **Production** — no `PG*` env vars is a misconfiguration; testcontainers is the wrong backend
  and will silently spin up an ephemeral container instead of surfacing the error.

`QueryLogMiddleware._connect` has the same issue: it calls `get_pg_creds()` directly and would
call `psycopg.connect("")` if no env vars are set, which either errors or picks up unexpected
local defaults.

## Proposed fix

Detect the test context explicitly (e.g. `PYTEST_CURRENT_TEST` env var, which pytest sets
automatically) and route accordingly:

```python
def make_pool() -> psycopg_pool.ConnectionPool:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        creds = get_testcontainers_creds()
    else:
        creds = get_pg_creds()
        if not creds:
            raise RuntimeError("No PG* environment variables set — cannot connect to database")
    ...
```

This makes the test path intentional and the production path fail fast with a clear error rather
than masking misconfiguration.

`QueryLogMiddleware._connect` should reuse `make_pool`'s connection config rather than
re-implementing credential lookup independently.

## Notes

- `PYTEST_CURRENT_TEST` is set by pytest for the duration of each test and unset otherwise —
  no test-specific code needs to be added to the production path.
- Any place that currently calls `get_pg_creds()` directly and handles the empty-creds case
  ad-hoc should be audited and consolidated.
