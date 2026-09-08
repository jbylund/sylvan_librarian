# Sylvan Librarian

<table>
  <tr>
    <td align="center"><img src="https://d1hot9ps2xugbc.cloudfront.net/badges/logo-github.svg" alt="GitHub" width="40" height="40"></td>
    <td>
      <a href="https://github.com/jbylund/sylvan_librarian/issues"><img src="https://img.shields.io/github/issues/jbylund/sylvan_librarian" alt="Issues"></a>      <br>
      <a href="https://github.com/jbylund/sylvan_librarian/pulls"><img src="https://img.shields.io/github/issues-pr/jbylund/sylvan_librarian" alt="Pull requests"></a>      <br>
      <a href="https://github.com/jbylund/sylvan_librarian/stargazers"><img src="https://img.shields.io/github/stars/jbylund/sylvan_librarian" alt="Stars"></a>
    </td>
    <td rowspan="4" align="center">
      <img src="https://d1hot9ps2xugbc.cloudfront.net/badges/languages.svg" alt="Lines of code by language">
      <br>
      <a href="https://github.com/jbylund/sylvan_librarian/commits/main"><img src="https://img.shields.io/github/last-commit/jbylund/sylvan_librarian" alt="Last commit"></a>
      <a href="https://github.com/jbylund/sylvan_librarian/blob/main/LICENSE"><img src="https://img.shields.io/github/license/jbylund/sylvan_librarian" alt="License"></a>
    </td>
  </tr>
  <tr>
    <td align="center"><img src="https://d1hot9ps2xugbc.cloudfront.net/badges/logo-python.svg" alt="Python" width="40" height="40"></td>
    <td>
      <a href="https://github.com/jbylund/sylvan_librarian/actions/workflows/python-tests.yml?query=branch%3Amain"><img src="https://img.shields.io/github/actions/workflow/status/jbylund/sylvan_librarian/python-tests.yml?branch=main&label=tests" alt="Python tests"></a>
      <br>
      <a href="https://github.com/jbylund/sylvan_librarian/actions/workflows/python-tests.yml?query=branch%3Amain"><img src="https://img.shields.io/endpoint?url=https%3A%2F%2Fd1hot9ps2xugbc.cloudfront.net%2Fbadges%2Ftests-python.json" alt="Python test count"></a>
    </td>
  </tr>
  <tr>
    <td align="center"><img src="https://d1hot9ps2xugbc.cloudfront.net/badges/logo-rust.svg" alt="Rust" width="40" height="40"></td>
    <td>
      <a href="https://github.com/jbylund/sylvan_librarian/actions/workflows/rust-tests.yml?query=branch%3Amain"><img src="https://img.shields.io/github/actions/workflow/status/jbylund/sylvan_librarian/rust-tests.yml?branch=main&label=tests" alt="Rust tests"></a>
      <br>
      <a href="https://github.com/jbylund/sylvan_librarian/actions/workflows/rust-tests.yml?query=branch%3Amain"><img src="https://img.shields.io/endpoint?url=https%3A%2F%2Fd1hot9ps2xugbc.cloudfront.net%2Fbadges%2Ftests-rust.json" alt="Rust test count"></a>
    </td>
  </tr>
  <tr>
    <td align="center"><img src="https://d1hot9ps2xugbc.cloudfront.net/badges/logo-javascript.svg" alt="JavaScript" width="40" height="40"></td>
    <td>
      <a href="https://github.com/jbylund/sylvan_librarian/actions/workflows/js-tests.yml?query=branch%3Amain"><img src="https://img.shields.io/github/actions/workflow/status/jbylund/sylvan_librarian/js-tests.yml?branch=main&label=tests" alt="JavaScript tests"></a>
      <br>
      <a href="https://github.com/jbylund/sylvan_librarian/actions/workflows/js-tests.yml?query=branch%3Amain"><img src="https://img.shields.io/endpoint?url=https%3A%2F%2Fd1hot9ps2xugbc.cloudfront.net%2Fbadges%2Ftests-js.json" alt="JavaScript test count"></a>
    </td>
  </tr>
</table>

![Web Interface](screenshot.webp)

*Web interface in dark mode: `t:beast` matching 525 cards, returned in 3 ms*

**Legal Notice**: Magic: The Gathering is trademark and property of Wizards of the Coast LLC, a subsidiary of Hasbro, Inc. This project is unofficial Fan Content permitted under the [Wizards of the Coast Fan Content Policy](https://company.wizards.com/en/legal/fancontentpolicy).
Not approved/endorsed by Wizards of the Coast.
Portions of the materials used are property of Wizards of the Coast. © Wizards of the Coast LLC. Card data sourced from [Scryfall](https://scryfall.com) with attribution.
See [docs/legal.md](docs/legal/legal.md) for full details.

## Table of Contents

1. [Project Overview](#project-overview)
1. [Functionality Comparison](#functionality-comparison)
   1. [Recommended Development Priorities](#recommended-development-priorities)
1. [Code Organization](#code-organization)
1. [Developer Quick Start](#developer-quick-start)
1. [Card Tagging System](docs/technical/card_tagging.md)
1. [API Documentation](#api-documentation)
1. [Development Notes](#development-notes)
1. [Security](#security)
1. [Data Sources & Attribution](#data-sources--attribution)
1. [Contributing](docs/CONTRIBUTING.md)

## Project Overview

Sylvan Librarian is an open source implementation of Scryfall, a Magic: The Gathering card search engine.

### Sylvan Librarian vs Official Scryfall

| Feature                    | Syntax                                        | Scryfall | Sylvan Librarian | Description                                               |
|----------------------------|-----------------------------------------------|----------|-------------|-----------------------------------------------------------|
| **Basic Search**           | `name:`, `oracle:`                            | ✔        | ✔           | Full substring search with pattern matching               |
| **Type Search**            | `type:`, `t:`                                 | ✔        | ✔           | Exact matching with intelligent autocomplete              |
| **Flavor Text**            | `flavor:`                                     | ✔        | ✔           | Full text search with pattern matching                    |
| **Artist Search**          | `artist:`, `a:`                               | ✔        | ✔           | Full text search with trigram indexing                    |
| **Set Search**             | `set:`, `s:`                                  | ✔        | ✔           | Dedicated indexed column with exact matching              |
| **Rarity Search**          | `rarity:`, `r:`                               | ✔        | ✔           | Integer-based ordering with all comparison operators      |
| **Frame Search**           | `frame:`                                      | ✔        | ✔           | Card frame type and visual properties search              |
| **Watermark Search**       | `watermark:`                                  | ✔        | ✔           | Card watermark and visual properties search               |
| **Mana Production**        | `produces:`                                   | ✔        | ✔           | Search for lands and mana-producing cards                 |
| **Numeric Attributes**     | `cmc:`, `power:`, `toughness:`, `loyalty:`    | ✔        | ✔           | Complete with all comparison operators                    |
| **Colors & Identity**      | `color:`, `identity:`, `c:`, `id:`            | ✔        | ✔           | JSONB-based with complex color logic                      |
| **Pricing Data**           | `usd:`, `eur:`, `tix:`                        | ✔        | ✔           | Complete with all comparison operators                    |
| **Advanced Logic**         | `AND`, `OR`, `NOT`, `()`                      | ✔        | ✔           | Full boolean logic support                                |
| **Keywords**               | `keyword:`                                    | ✔        | ✔           | JSONB object storage                                      |
| **Mana Costs**             | `mana:`, `m:`                                 | ✔        | ✔           | Both JSONB and text representations                       |
| **Oracle Tags**            | `oracle_tags:`, `ot:`                         | ✔        | ✔           | Standard Scryfall feature                                 |
| **Date Search**            | `date:`, `year:`                              | ✔        | ✔           | Card release date filtering with comparison operators     |
| **Devotion Search**        | `devotion:`                                   | ✔        | ✔           | Mana cost devotion calculations with split mana support   |
| **Format Legality**        | `format:`, `legal:`, `banned:`, `restricted:` | ✔        | ✔           | Competitive play support                                  |
| **Collector Numbers**      | `number:`, `cn:`                              | ✔        | ✔           | Card collector number search                              |
| **Card Layout**            | `layout:`                                     | ✔        | ✔           | Card layout types (normal, split, transform, etc.)        |
| **Card Border**            | `border:`                                     | ✔        | ✔           | Border colors (black, white, borderless, etc.)            |
| **Special Properties**     | `is:`                                         | ✔        | ✔           | Card classifications (creature, spell, permanent, etc.)   |
| **Comparison Operators**   | `=`, `<`, `>`, `<=`, `>=`, `!=`, `<>`         | ✔        | ✔           | All comparison operators supported                        |
| **Regular Expressions**    | `/pattern/`                                   | ✔        | ✔           | Pattern matching with regex syntax                        |
| **Collection Features**    | `cube:`, `papersets:`                         | ✔        | ✘           | Collection and cube inclusion features                    |
| **Arithmetic Expressions** | `cmc+1<power`, `power-toughness=0`            | ✘        | ✔           | Advanced mathematical expressions                         |


### Sylvan Librarian Unique Features

- **Arithmetic operations** - Mathematical expressions like `cmc+1<power`
- **Typeahead search with intelligent completion** - Enhanced UX for query building
- **In-memory Rust query engine** - Sub-millisecond search for most queries (~76x faster than the SQL path), with PostgreSQL as a transparent fallback
- **Optimized database schema for low latency queries** - Performance improvements
- **Larger data fetch capabilities** - No 175 card/page limit like Scryfall
- **Data synchronization tools** - Tools to sync from upstream Scryfall
- **Local deployment** - Run your own instance with Docker

### Core Components

1. **Search DSL Parser** - A hand-rolled recursive-descent parser for Scryfall's query syntax supporting text search, numeric comparisons, color identity, and advanced operators (~49x faster than the previous pyparsing implementation, which is retained for parity testing)
1. **Rust Query Engine** - An in-process Rust (PyO3) filter engine that evaluates parsed queries against a shared-memory card store (rkyv + mmap), serving most searches without touching the database (~76x faster than the SQL path)
1. **SQL Query Path** - Converts parsed queries into optimized, parameterized PostgreSQL queries; kept in parallel as the fallback when the engine is cold or errors
1. **Data Import Tools** - Bulk data loading from Scryfall exports with incremental updates and card tagging integration
1. **Web Interface** - A responsive HTML/JavaScript application providing search functionality with card display similar to Scryfall
1. **Card Tagging System** - Extended functionality for importing and managing Scryfall's card tags with hierarchy support
1. **RESTful API** - Falcon-based web service with multi-process worker support and comprehensive search endpoints

## Functionality Comparison

### Recommended Development Priorities

1. Support for double faced cards
1. More comprehensive tagging info - per card, per card-printing, per artwork
1. `cube:`, `papersets:`


### Missing Functionality - Complexity vs Impact Grid

Based on [comprehensive functionality analysis](docs/technical/scryfall_functionality_analysis.md), here's the updated priority matrix:

| **Complexity**   | **Lower impact**             | **Higher impact**                                                                             |
| ---------------- | -----------------------------|-----------------------------------------------------------------------------------------------|
| **Simpler**      | **Cube Inclusion** (`cube:`) |                                                                                               |
| **More complex** |                              | **Reprint Info** (`papersets:`) - [Scryfall Docs](https://scryfall.com/docs/syntax#reprints)  |

### Implementation Status

- **Current API Success Rate**: 100% for supported features (enhanced coverage with flavor text search)
- **Test Coverage**: see the per-suite test-count badges at the top of this file — they are regenerated daily, so they do not go stale the way a number written into prose does
- **Performance**: In-memory Rust query engine (sub-millisecond for most queries) backed by optimized PostgreSQL with proper indexing including full-text search capabilities
- **Data Quality**: Regular comparison testing against official Scryfall API

## Code Organization

```
sylvan_librarian/
├── api/                         # Python API service (main application)
│   ├── db/                      # Database schema and migrations
│   ├── middlewares/             # HTTP middleware components
│   ├── parsing/                 # Query parser implementation
│   │   ├── tests/               # Parser unit tests (1,302 tests)
│   │   ├── nodes.py             # AST node definitions
│   │   ├── hand_parser.py       # Main parser (hand-rolled recursive descent)
│   │   ├── pyparsing_based.py   # Legacy pyparsing parser (parity testing only)
│   │   └── card_query_nodes.py  # Card-specific query node types
│   ├── sql/                     # SQL query templates
│   ├── tests/                   # Integration and API tests
│   ├── api_resource.py          # Falcon web framework resources
│   ├── api_worker.py            # Multi-process worker implementation
│   ├── entrypoint.py            # API server entry point and CLI
│   └── index.html               # Web frontend (single-file app)
├── card_engine/                 # Rust (PyO3) in-memory query engine
├── client/                      # Query runner client for index analysis
├── configs/                     # Configuration files
├── docs/                        # Project documentation and analysis
├── requirements/                # Requirements files
│   ├── base.txt                 # base requirements
│   ├── test.txt                 # testing requirements
│   └── webserver.txt            # webserver requirements - requires building libev
├── scripts/                     # Utility and maintenance scripts
├── docker-compose.yml           # Container orchestration
└── makefile                     # Build automation
```

### Specialized Documentation

- **[Client Query Runner](client/README.md)** - Query runner client for testing and index analysis
- **[Scripts Documentation](scripts/README.md)** - Detailed information about utility scripts including the Scryfall comparison tool
- **[API Tests Documentation](api/tests/README.md)** - Testing framework and integration test information
- **[CI/CD Workflows](docs/workflows/readme_ci_monitor.md)** - Continuous integration and monitoring documentation

## Developer Quick Start

### Prerequisites

- Python 3.13+ (tested with 3.13)
- PostgreSQL 17+ (for full functionality)
- Rust toolchain with maturin (for the in-memory query engine; `make engine` builds it)
- Docker and Docker Compose (for containerized development)
- Node.js 26.1.0+ — required, not optional: `make <env>-up` minifies `app.js` with `npx terser`
  before starting anything, so the build fails without it
- `jq` (shipped with recent macOS; `apt install jq` on Debian/Ubuntu)

On macOS, `python3` from the Xcode command line tools is enough — the makefile finds either `python`
or `python3`, and falls back to `sysctl` where GNU `nproc` is absent.

### Setup Instructions

1. **Clone and Install Dependencies**

   ```bash
   git clone git@github.com:jbylund/sylvan_librarian.git
   cd sylvan_librarian

   # Install core dependencies
   python -m pip install --upgrade pip
   python -m pip install -r requirements/base.txt -r requirements/test.txt
   ```

1. **Optional: Web Server Dependencies**

   ```bash
   # Only needed for local API server (includes bjoern compilation)
   sudo apt-get update && sudo apt-get install -y libev-dev
   python -m pip install -r requirements/webserver.txt
   ```

1. **Validate Installation**

   ```bash
   # Run test suite (should pass all 1,821 tests)
   python -m pytest -vvv

   # Verify linting
   python -m ruff check

   # Test parser functionality including rarity search
   python -c "from api.parsing import parse_scryfall_query; print(parse_scryfall_query('rarity>uncommon'))"
   ```

### Development Workflows

#### Docker Development (Recommended)

```bash
# Quick start
make dev-up          # Builds images, starts all services (dev environment)

# Or step by step:
make build_images     # Build Docker images (~30-60 seconds)
make dev-up          # Start PostgreSQL and API services (dev)
make blue-up         # Start PostgreSQL and API services (blue, a production environment)
```

#### Environments

Three stacks share the one `docker-compose.yml`, told apart by `--project-name` and a file in
`envs/`: `dev` on port 28080, plus the `blue` (18080) and `green` (18081) production pair. Each
stack is complete and independent — its own PostgreSQL, its own volume, its own copy of the card
data — so one can be torn down and rebuilt while the other keeps serving.

Both production stacks normally run, with nginx routing to whichever is live. `make rolling-deploy`
takes them down one at a time: blue is stopped, rebuilt, restarted, and only once it reports healthy
does green follow. Because the other stack serves throughout, a deploy has no downtime even when the
new containers need to re-import the card data from scratch.

#### Binding and Reverse Proxies

The API port binds to `127.0.0.1` by default, so a fresh stack is reachable from the host it runs on
and nowhere else. This is deliberate: Docker publishes ports by inserting DNAT rules that are
evaluated *before* the `INPUT` chain where `ufw` and `firewalld` operate, so a published port on
`0.0.0.0` stays reachable even when the host firewall appears to deny it. Defaulting to loopback fails
visibly instead of silently.

Pick the line that matches your setup:

- **Reverse proxy on the same host** (nginx, Caddy, or similar as a host process) — nothing to do.
  Point the proxy at `127.0.0.1:${API_PORT}`.
- **Reverse proxy in a container on the same Docker network** — delete the `ports:` block for
  `apiservice` entirely and have the proxy reach `apiservice:8080` directly. No host port needed.
- **Direct access with no proxy**, e.g. from elsewhere on your LAN — set `BIND_ADDR=0.0.0.0` in the
  stack's file under `envs/`, or a specific interface address to narrow it. Note that this serves
  plain HTTP with no TLS, so prefer a proxy if the stack is reachable from outside your network.

#### Environment Variables

The following environment variables can be configured:

**Docker Compose:**

Read by Compose while it renders `docker-compose.yml`, not by the API process — inside its container
the API always listens on `0.0.0.0:8080`, and these only decide how that socket is published to the
host. Set them in the stack's file under `envs/` (`envs/dev`, `envs/blue`, `envs/green`) so the value
applies every time that stack starts; exporting one in the shell overrides the file for a single
`make <env>-up`.

- `BIND_ADDR` - Host address the published API port binds to (default: `127.0.0.1`)
  - Set to `0.0.0.0` to expose the API on all interfaces, or a specific interface address
  - See "Binding and Reverse Proxies" above before changing it
- `API_PORT` - Host port the API is published on (default: `28080`)
  - Already set per environment: `dev` is 28080, `blue` 18080, `green` 18081

**API Service:**
- `ENABLE_ENGINE` - Enable/disable the in-memory Rust query engine (enabled in all environments)
  - When enabled, searches are served from the shared-memory card store with PostgreSQL as fallback
  - When disabled, all searches go through the SQL path
- `ENABLE_CACHE` - Enable/disable API response caching (default: `false`)
  - Set to `true`, `1`, or `yes` to enable caching
  - Improves performance for repeated queries
  - Can be set in docker-compose.yml or exported before starting services
- `ENVIRONMENT` - Deployment label for logging and monitoring (default: `dev`)
  - Set to `prod` for production deployments (`envs/blue` and `envs/green` use this)
  - Does not change CORS behavior; see **CORS** below
- `PREFER_SCORE_BACKFILL_TIMEOUT_MS` - Statement timeout for the prefer-score backfill (default: `120000`)
  - The backfill rescores the whole corpus in one UPDATE, so its runtime tracks disk speed
  - Raise it if an import dies in `backfill_prefer_scores` on slow storage (a virtualized
    Docker-for-Mac volume, a small cloud disk); `0` disables the timeout
  - Must be a non-negative integer — a malformed value fails at startup rather than being ignored
- `CDN_URL` - CDN URL for static assets (default: `https://d1hot9ps2xugbc.cloudfront.net`)
  - Override to use a different CDN provider
  - Used in Content-Security-Policy headers
  - Format: `https://your-cdn-domain.com`
- **CORS** — The public search API intentionally sets `Access-Control-Allow-Origin: *` on every
  response (Scryfall-compatible, browser-readable from any origin). `CORSMiddleware` always emits
  the wildcard; neither `ENVIRONMENT` nor any env var currently restricts origins. Operator-configurable
  origin allowlists may be added in a future release but are **not implemented today**. Introducing
  cookie- or session-based authentication would require revisiting this policy.
- `ADMIN_PASSWORD` - Shared secret required to reach any `_admin/` route (see [Admin Endpoints](#admin-endpoints))
  - Generated automatically into `env.json` on first boot; never overwrites an existing value
  - Retrieve it with `jq -r .ADMIN_PASSWORD env.json`
  - If unset, every `_admin/` request is rejected rather than left open

**Client Service:**
- `API_URL` - URL of the API service (default: `http://apiservice:8080`)
- `QUERY_DELAY` - Delay between queries in seconds (default: `1.0`)
- `BATCH_SIZE` - Number of queries before reporting statistics (default: `50`)

Example with caching enabled:
```bash
# Caching is controlled per environment in envs/<name> via ENABLE_CACHE
make dev-up   # ENABLE_CACHE=false (dev default)
make blue-up  # ENABLE_CACHE=true (blue/green default)
```

#### Local Development

```bash
# Start API server locally
python -m api.entrypoint --port 8080 --workers 2

# Visit web interface
open http://localhost:8080/
```

#### Testing and Quality Assurance

```bash
# Run specific test suites
make test            # All tests
make test-unit       # Unit tests only
make test-integration # Integration tests (requires Docker)

# Code quality
make lint            # Run ruff and pylint
python -m ruff check --fix --unsafe-fixes  # Auto-fix style issues
npx prettier --write api/index.html        # Format frontend code
```

#### Query Runner Client (for Index Analysis)

The client container runs automatically when you start all services with `make dev-up` (or any other environment).

```bash
# Client runs automatically with all services
make dev-up

# Or run locally for development
python -m client.query_runner

# See client/README.md for more details
```

### Development Tips

- **Fast validation cycle**: `python -m pytest -vvv && python -m ruff check` (completes in ~2 seconds)
- **Parser testing**: Use `api/parsing/tests/` for comprehensive query parser validation
- **Database connection**: Use `make dbconn` to connect to local PostgreSQL instance
- **API comparison**: Run `python scripts/scryfall_comparison_script.py` to compare against official Scryfall API


## API Documentation

### Search Endpoints

- **GET /** - Web interface (serves `index.html`)
- **GET /search** - Card search with query parameter support
- **GET /favicon.ico** - Favicon for web interface

### Scryfall-Compatible Endpoints

Every route Scryfall documents under `/cards`, answering with Scryfall's own response objects and
175-per-page pagination, so a client can be pointed here by swapping its base URL:

- **GET /cards** and **GET /cards/search** - list routes (`format=json|csv`)
- **GET /cards/named**, **/cards/autocomplete**, **/cards/random** - single-card and catalog lookups
- **POST /cards/collection** - up to 75 identifiers at once
- **GET /cards/:id**, **/cards/:code/:number(/:lang)**, and the multiverse / mtgo / arena /
  tcgplayer / cardmarket id namespaces
- **GET /cards/:id/rulings** and its four sibling addressings

`format=text` and `format=image` are available on the single-card routes. What is *not* identical —
chiefly that the corpus is a filtered subset of Scryfall's — is recorded in
[docs/issues/local-scryfall-cards-api.md](docs/issues/local-scryfall-cards-api.md).

### Admin Endpoints

Data-management routes — importing card data, running score/tag backfills, applying schema
migrations — live under `_admin/` rather than the public namespace, and require HTTP Basic Auth.
`setup_schema` and `import_data` both run automatically on startup, so a fresh instance already has
card data; these exist for triggering a re-import or backfill on demand.

- **GET /_admin/import_data** - Re-import card data from Scryfall's bulk data API
- **GET /_admin/backfill_prefer_scores** - Recompute `prefer_score` for all cards
- **GET /_admin/backfill_cubecobra_scores** - Recompute `cubecobra_score` for all cards
- **GET /_admin/import_oracle_tags** / **/_admin/import_art_tags** / **/_admin/import_all_is_tags** - Import Scryfall's oracle, art, and `is:` tags

Authenticate with any username and the `ADMIN_PASSWORD` value from `env.json` (see
[Environment Variables](#environment-variables)):

```bash
curl -u admin:$(jq -r .ADMIN_PASSWORD env.json) http://localhost:28080/_admin/import_data
```

### Query Parameters

The `/search` endpoint accepts the following query parameters:

- `q` or `query` (string): The search query in Scryfall-compatible syntax (see [syntax analysis](docs/technical/scryfall_syntax_analysis.md) for complete documentation).
- `limit` (integer, default `100`): Maximum number of results to return. Must be an integer between `0` and the continuously growing pagination ceiling: `floor((current Unix timestamp - 1_409_018_789) / 3_155)`, which adds approximately 10,000 per year.
- `offset` (integer, default `0`): Number of results to skip before returning cards. Must be an integer between `0` and the continuously growing pagination ceiling: `floor((current Unix timestamp - 1_409_018_789) / 3_155)`, which adds approximately 10,000 per year.
- `orderby` (string, default `edhrec`): Field to sort by (`name`, `cmc`, `power`, `toughness`, `edhrec`, `rarity`, `usd`, `cubecobra`).
- `direction` (string, default `asc`): Sort direction (`asc` or `desc`).
- `unique` (string, default `card`): Result deduplication mode (`card`, `art`/`artwork`, `prints`/`printing`).
- `prefer` (string, default `default`): Preferred printing selection when grouping (`oldest`, `newest`, `usd-low`, `usd-high`, `promo`, `default`).
- `fields` (string): Comma-separated list of fields to return per card (see `RESULT_FIELD_COLUMNS` in `api/api_resource.py`).
- `shape` (string, default `rows`): Response layout format (`rows` for list of card objects, `columnar` for columnar field dictionary).

## Development Notes

### Current Limitations

- **Missing Features**: See functionality grid above for complete list

### Future Enhancements

1. **Features**: Implement highest-priority missing functionality from grid above
1. **Testing**: Expand API comparison coverage and add performance benchmarks

For detailed technical analysis, see [functionality analysis documentation](docs/technical/scryfall_functionality_analysis.md).

## Data Sources & Attribution

### Card Data

Sylvan Librarian uses card data from [Scryfall's official bulk data API](https://api.scryfall.com/bulk-data).
We are grateful to Scryfall for maintaining comprehensive, high-quality Magic: The Gathering card information and making it available to the community.

**Data Attribution**: Card data provided by [Scryfall](https://scryfall.com).
Sylvan Librarian is an independent implementation and is not affiliated with, endorsed by, or sponsored by Scryfall.

### Intellectual Property

All Magic: The Gathering card names, artwork, and game content are © Wizards of the Coast LLC. This project respects all intellectual property rights and operates under the [Wizards of the Coast Fan Content Policy](https://company.wizards.com/en/legal/fancontentpolicy).

**Important**: This is unofficial Fan Content.
Not approved/endorsed by Wizards of the Coast.

### Security

All user input reaches the database only via parameterized queries; HTTP responses include CSP,
X-Frame-Options, and `Access-Control-Allow-Origin: *` (intentional public API contract). See
[docs/security/security_best_practices.md](docs/security/security_best_practices.md) for development
guidelines. To report a vulnerability, see [SECURITY.md](SECURITY.md).

### Legal Compliance

For complete information about data sources, intellectual property attribution, and compliance with relevant policies, see [docs/legal.md](docs/legal/legal.md).

For attribution, IP rights, terms of service, and privacy policy, see [docs/legal/](docs/legal/).

## How Sylvan Librarian Differs from Scryfall

While we use Scryfall's data, Sylvan Librarian is a distinct implementation:

- **Original codebase**: All code written from scratch (no copied code from Scryfall)
- **Different database schema**: Custom PostgreSQL schema optimized for our use cases
- **Unique features**: Arithmetic expressions in queries, larger data fetch capabilities
- **Independent search algorithms**: Original query parser and search ranking
- **Different visual design**: Custom UI layout and styling
- **Open source**: Transparent, community-driven development

Our goal is to provide an open-source alternative that respects both Wizards of the Coast's intellectual property and Scryfall's valuable contribution to the MTG community.

