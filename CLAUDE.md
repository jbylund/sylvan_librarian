# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Sylvan Librarian is an open-source Scryfall-compatible Magic: The Gathering card search engine. It parses a Scryfall-like query DSL, converts queries to optimized PostgreSQL, and serves results via a Falcon REST API with a vanilla JS frontend. It extends Scryfall syntax with arithmetic expressions (e.g., `cmc+1<power`).

## Commands

```bash
# Run all tests (779 total)
make test
# or: python -m pytest -vvv --capture=no --durations=10

# Run a single test file
python -m pytest api/parsing/tests/test_parsing.py -vvv

# Run a single test by name
python -m pytest -vvv -k "test_my_test_name"

# Everything except the testcontainers integration file. Docker is still required: an autouse
# session fixture in api/tests/conftest.py starts a Postgres container for all of api/tests/.
make test-unit

# Integration tests only (requires Docker)
make test-integration

# Coverage report
make coverage

# Lint (ruff + prettier)
make lint

# Auto-fix lint issues
python -m ruff check --fix --unsafe-fixes .
python -m ruff format .

# Start services (dev mode)
make dev-up

# Connect to local database (execs into the postgres container — no external port exposed)
make dbconn-blue  # blue environment
make dbconn-green # green environment
# also: dbconn-dev
```

## Architecture

### Request Flow

```
Browser → GET /search?q=<query>
  → api/api_resource.py (Falcon sink handler)
    → api/parsing/hand_parser.py (hand-written DSL parser → AST)
    → api/api_resource.py (AST → parameterized SQL)
    → PostgreSQL (magic schema)
  → JSON response (cached by CachingMiddleware)
```

### Key Directories

- **`api/parsing/`** — Core query parser (~2,500 lines). `hand_parser.py` is the production parser (`api/parsing/__init__.py` binds `parse_scryfall_query` to it); `parsing_f.py` only holds `balance_partial_query`, and `pyparsing_based.py` is the older pyparsing grammar. `nodes.py` defines AST node types; `card_query_nodes.py` has card-specific nodes; `db_info.py` maps query fields to DB columns.
- **`api/api_resource.py`** — Dispatch (Falcon sink), search logic, SQL generation from AST, and the public routes.
- **`api/admin_resource.py`** — Data-management handlers (import, backfill, tagging), mounted by `APIResource` under a path prefix rather than sharing the public namespace. Holds no reference to its parent; state the two resources share (connection pools, the query engine, cross-worker cache/import signals) lives on the `AppContext` both take a reference to at construction (`api/app_context.py`).
- **`api/utils/routing.py`** — The `@route` marker, route-table construction, and the 404 listing. A handler is reachable only if marked; the listing skips anything registered `advertise=False`, which is what makes the mount a boundary rather than a URL prefix.
- **`api/utils/param_binding.py`** — Resolves each handler's annotations once at registration and binds request parameters against a fixed plan.
- **`api/utils/page_rendering.py`**, **`api/utils/css_utils.py`**, **`api/utils/site_name.py`**, **`api/utils/caching.py`** — Page assembly, critical-CSS extraction, Host-header display names, and the settings-aware cache decorator.
- **`api/entrypoint.py`** + **`api/api_worker.py`** — Multi-process Bjoern WSGI server startup.
- **`api/db/`** — PostgreSQL schema SQL: `2025-09-29-great-reset.sql` plus dated migrations. With every migration applied, `magic.cards` carries 32 specialized indices (trigram GIN for text, GIN for JSONB arrays, B-tree for numerics).
- **`api/tests/`** — Integration tests using `testcontainers` (spins up a real PostgreSQL instance).
- **`api/parsing/tests/`** — 544 parser unit tests.
- **`api/static/`** — `app.js` (vanilla JS), `app.min.js` (minified for production).
- **`client/query_runner.py`** — Load testing / query diversity tool.
- **`scripts/`** — Font subsetting, minification, DB helpers.

### Middleware Stack (applied in order)

`TimingMiddleware` → `CachingMiddleware` → `CompressionMiddleware` (gzip/brotli/zstd) → `SecurityHeadersMiddleware` → `CORSMiddleware`

### Parser → SQL Pipeline

1. `hand_parser.py` converts a query string into a tree of AST nodes (defined in `nodes.py` and `card_query_nodes.py`).
2. Each node implements a method that emits a SQL fragment + bound parameters.
3. `api_resource.py` wraps the fragment in a `SELECT` against `magic.cards` with `ORDER BY` scoring logic and a `LIMIT` clause.
4. All user input reaches the database only via parameterized queries.

### Database

- PostgreSQL 17+, schema: `magic`
- Primary table: `magic.cards` — `scryfall_id` (UUID PK), numeric columns (`cmc`, `creature_power`, `creature_toughness`, `planeswalker_loyalty`), JSONB columns (`card_colors`, `card_color_identity`, `card_keywords`, `card_legalities`, `mana_cost_jsonb`, etc.), text columns (`card_name`, `oracle_text`, `flavor_text`).
- Tag system: `magic.oracle_tags` + `magic.oracle_tag_relationships` (renamed from `tags`/`tag_relationships` in `api/db/2026-06-21-01-bulk-tag-import.sql`; circular-reference trigger).
- Custom DB functions: `rarity_text_to_int()`, `rarity_int_to_text()`, `extract_collector_number_int()`, `get_tag_ancestors()`, `get_tag_descendants()`.

## Linting / Style

- **Python:** `ruff` (line length 132, Google docstring convention, target Python 3.13). Config in `pyproject.toml`.
- **HTML/JS:** `prettier` (config in `.prettierrc`).
- Tests relax many ruff rules (see `per-file-ignores` in `pyproject.toml`).

## Issue Tracking

`docs/issues/` holds the deep design/implementation notes for engine and product work — the primary
source of truth, tracked in git, with `done/` for finished work. GitHub issues are a secondary
triage layer that must stand on its own, and link to the doc rather than duplicating its depth.

Naming: `#####-slug.md` by GitHub issue number (`00623-engine-flavor-absent-gram-bitmap.md`), or a
prefix when there is no issue — `local-` for proposed work, `reference-` for material that is
deliberately not scheduled, `security-` for unfixed findings.

**`security-*` is gitignored and must never be committed** — this repo is public, so publishing an
unfixed finding is a working recipe against the live deployment, permanently. This applies to the
*design doc for the fix* as much as to the finding itself: the bar is not whether specifics were
omitted but whether a reader could derive the issue from what remains. Default to the prefix, keep
fix commit messages factual rather than descriptive of the defect, and rename off the prefix once
the fix ships.

Issue docs are exempt from the ~100-line ideal in the global markdown rules; **one shippable idea per
doc** governs instead.

Full conventions — prefix rationale, the security caveats and blast-radius rule, length and scope:
[docs/issues/README.md](docs/issues/README.md). Read it before creating or renaming an issue doc.
