# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Arcane Tutor is an open-source Scryfall-compatible Magic: The Gathering card search engine. It parses a Scryfall-like query DSL, converts queries to optimized PostgreSQL, and serves results via a Falcon REST API with a vanilla JS frontend. It extends Scryfall syntax with arithmetic expressions (e.g., `cmc+1<power`).

## Commands

```bash
# Run all tests (779 total)
make test
# or: python -m pytest -vvv --capture=no --durations=10

# Run a single test file
python -m pytest api/parsing/tests/test_parsing.py -vvv

# Run a single test by name
python -m pytest -vvv -k "test_my_test_name"

# Unit tests only (no Docker required)
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

# Connect to local database
make dbconn   # PostgreSQL at 127.0.0.1:25432, db=magic, user=foouser, password=foopassword
```

## Architecture

### Request Flow

```
Browser → GET /search?q=<query>
  → api/api_resource.py (Falcon sink handler)
    → api/parsing/parsing_f.py (pyparsing DSL → AST)
    → api/api_resource.py (AST → parameterized SQL)
    → PostgreSQL (magic schema)
  → JSON response (cached by CachingMiddleware)
```

### Key Directories

- **`api/parsing/`** — Core query parser (~2,500 lines). `parsing_f.py` drives the pyparsing grammar; `nodes.py` defines AST node types; `card_query_nodes.py` has card-specific nodes; `db_info.py` maps query fields to DB columns.
- **`api/api_resource.py`** — All HTTP routing (Falcon sink), search logic, SQL generation from AST, and bulk import endpoints.
- **`api/entrypoint.py`** + **`api/api_worker.py`** — Multi-process Bjoern WSGI server startup.
- **`api/db/`** — PostgreSQL schema SQL (`2025-09-29-great-reset.sql`). The `magic.cards` table has 22 specialized indices (trigram GIN for text, GIN for JSONB arrays, B-tree for numerics).
- **`api/tests/`** — Integration tests using `testcontainers` (spins up a real PostgreSQL instance).
- **`api/parsing/tests/`** — 544 parser unit tests.
- **`api/static/`** — `app.js` (vanilla JS), `app.min.js` (minified for production).
- **`client/query_runner.py`** — Load testing / query diversity tool.
- **`scripts/`** — Font subsetting, minification, DB helpers.

### Middleware Stack (applied in order)

`TimingMiddleware` → `CachingMiddleware` → `CompressionMiddleware` (gzip/brotli/zstd) → `SecurityHeadersMiddleware` → `CORSMiddleware`

### Parser → SQL Pipeline

1. `parsing_f.py` converts a query string into a tree of AST nodes (defined in `nodes.py` and `card_query_nodes.py`).
2. Each node implements a method that emits a SQL fragment + bound parameters.
3. `api_resource.py` wraps the fragment in a `SELECT` against `magic.cards` with `ORDER BY` scoring logic and a `LIMIT` clause.
4. All user input reaches the database only via parameterized queries.

### Database

- PostgreSQL 17+, schema: `magic`
- Primary table: `magic.cards` — `scryfall_id` (UUID PK), numeric columns (`cmc`, `creature_power`, `creature_toughness`, `planeswalker_loyalty`), JSONB columns (`card_colors`, `card_color_identity`, `card_keywords`, `card_legalities`, `mana_cost_jsonb`, etc.), text columns (`card_name`, `oracle_text`, `flavor_text`).
- Tag system: `magic.tags` + `magic.tag_relationships` (with circular-reference trigger).
- Custom DB functions: `rarity_text_to_int()`, `rarity_int_to_text()`, `extract_collector_number_int()`, `get_tag_ancestors()`, `get_tag_descendants()`.

## Linting / Style

- **Python:** `ruff` (line length 132, Google docstring convention, target Python 3.13). Config in `pyproject.toml`.
- **HTML/JS:** `prettier` (config in `.prettierrc`).
- Tests relax many ruff rules (see `per-file-ignores` in `pyproject.toml`).
