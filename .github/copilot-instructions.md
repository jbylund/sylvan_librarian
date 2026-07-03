# Sylvan Librarian

Sylvan Librarian is an open-source Scryfall-compatible Magic: The Gathering card search engine.
It parses a Scryfall-like query DSL, converts queries to optimized PostgreSQL, and serves results
via a Falcon REST API with a vanilla JS frontend. It extends Scryfall syntax with arithmetic
expressions (e.g., `cmc+1<power`).

## Tech Stack

- **Python 3.13** — Falcon web framework, bjoern WSGI server (multi-process)
- **PostgreSQL 17+** — schema `magic`, primary table `magic.cards`
- **Rust extensions** (built with maturin/PyO3): `card_engine/` (in-memory query engine) and
  `shared_cache/` (cross-process cache)
- **Frontend** — vanilla JS/HTML in `api/static/`, served by the API
- **Docker Compose** — blue/green/dev/prod environments (see `envs/`)

## Environment Setup

Dependencies are pre-installed by `.github/workflows/copilot-setup-steps.yml`. If setting up
manually:

```bash
sudo apt-get install -y libev-dev   # needed to compile bjoern (the WSGI server)
python -m pip install uv
uv pip install --system -r requirements/base.txt -r requirements/test.txt -r requirements/webserver.txt
make engine   # builds the Rust card_engine extension (requires Rust toolchain + maturin)
```

## Build, Test, Validate

```bash
make test              # full test suite (~1,900 tests); integration tests need Docker
make test-unit         # unit tests only, no Docker (runs in seconds)
make test-integration  # testcontainers-based integration tests (real PostgreSQL)
python -m pytest -vvv -k "test_name"          # single test by name
make lint              # ruff (Python) + prettier (HTML/JS)
make coverage          # HTML coverage report
make dev-up            # start dev services via Docker Compose
```

To run the real server for end-to-end verification (e.g., frontend changes):

```bash
python api/entrypoint.py --port 8080 --workers 2
```

If no `PG*` environment variables are set, the server automatically starts an ephemeral
PostgreSQL container via testcontainers (requires Docker). The web UI is served at
`http://localhost:8080/`.

Before committing, always run:

```bash
python -m ruff check --fix --unsafe-fixes .
python -m ruff format .
npx prettier --write <changed .html/.js files>
```

## Repository Layout

- `api/parsing/` — query parser: `parsing_f.py` (grammar), `nodes.py` / `card_query_nodes.py`
  (AST nodes), `db_info.py` (field → DB column mapping); unit tests in `api/parsing/tests/`
- `api/api_resource.py` — all HTTP routing (Falcon sink), search logic, AST → SQL generation
- `api/middlewares/` — timing, caching, compression, security headers, CORS (applied in that order)
- `api/entrypoint.py` + `api/api_worker.py` — multi-process server startup
- `api/db/` — PostgreSQL schema and migration SQL files
- `api/static/` — frontend (`app.js`, minified `app.min.js`, `card.html`, …)
- `api/tests/` — API unit tests plus testcontainers integration tests
- `card_engine/`, `shared_cache/` — Rust extension crates
- `client/query_runner.py` — load testing / query diversity tool
- `docs/` — changelogs, technical docs, issue write-ups
- `scripts/` — font subsetting, minification, DB helpers

## Coding Conventions

- Follow existing patterns for parsing, testing, and API design; include Python type hints.
- All user input must reach the database only via parameterized queries.
- Prefer parameterized tests (`pytest.mark.parametrize`) over looping inside a test body.
- Never modify a migration file `api/db/*.sql` that already exists on main. New migrations are
  named `YYYY-MM-DD-##-description.sql` (the `##` sequence starts at 01 per day).
- For larger features, add a document in `docs/changelog/` prefixed `YYYY-MM-DD`.
- When unsure of query behavior, match the [Scryfall syntax guide](https://scryfall.com/docs/syntax).
- Python style is enforced by ruff (line length 132, Google docstrings, target Python 3.13);
  config lives in `pyproject.toml`. HTML/JS is formatted by prettier (`.prettierrc`).

## CI

GitHub Actions run on every push/PR: `unit-tests.yml` (pytest on Python 3.13 with cached Rust
extension wheels) and `lint.yml` (ruff). All tests must pass and ruff must be clean before merge.
