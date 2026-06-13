# Arcane Tutor

![Web Interface](screenshot.webp)

*Web interface in dark mode showing cards with CMC less than 10, ordered by USD price descending*

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

Arcane Tutor is an open source implementation of Scryfall, a Magic: The Gathering card search engine.

### Arcane Tutor vs Official Scryfall

| Feature                    | Syntax                                        | Scryfall | Arcane Tutor | Description                                               |
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


### Arcane Tutor Unique Features

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
- **Test Coverage**: 1,821 total tests including 1,302 parser tests with comprehensive validation
- **Performance**: In-memory Rust query engine (sub-millisecond for most queries) backed by optimized PostgreSQL with proper indexing including full-text search capabilities
- **Data Quality**: Regular comparison testing against official Scryfall API

## Code Organization

```
arcane_tutor/
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
- Node.js 26.1.0+ (for HTML formatting tools)

### Setup Instructions

1. **Clone and Install Dependencies**

   ```bash
   git clone git@github.com:jbylund/arcane_tutor.git
   cd arcane_tutor

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
   python -c "from api.parsing import parse_search_query; print(parse_search_query('rarity>uncommon'))"
   ```

### Development Workflows

#### Docker Development (Recommended)

```bash
# Quick start
make dev-up          # Builds images, starts all services (dev environment)

# Or step by step:
make build_images     # Build Docker images (~30-60 seconds)
make dev-up          # Start PostgreSQL and API services (dev)
make prod-up         # Start PostgreSQL and API services (prod)
```

#### Environment Variables

The following environment variables can be configured:

**API Service:**
- `ENABLE_ENGINE` - Enable/disable the in-memory Rust query engine (enabled in all environments)
  - When enabled, searches are served from the shared-memory card store with PostgreSQL as fallback
  - When disabled, all searches go through the SQL path
- `ENABLE_CACHE` - Enable/disable API response caching (default: `false`)
  - Set to `true`, `1`, or `yes` to enable caching
  - Improves performance for repeated queries
  - Can be set in docker-compose.yml or exported before starting services
- `ENVIRONMENT` - Environment mode (default: `dev`)
  - Set to `prod` for production mode with restricted CORS
  - Controlled via `APP_ENV` in `envs/dev` / `envs/prod`
- `CDN_URL` - CDN URL for static assets (default: `https://d1hot9ps2xugbc.cloudfront.net`)
  - Override to use a different CDN provider
  - Used in Content-Security-Policy headers
  - Format: `https://your-cdn-domain.com`
- `CORS_ALLOWED_ORIGINS` - Additional CORS allowed origins (optional)
  - Comma-separated list of origins to allow
  - Example: `https://example.com,https://app.example.com`
  - Supplements environment-specific defaults

**Client Service:**
- `API_URL` - URL of the API service (default: `http://apiservice:8080`)
- `QUERY_DELAY` - Delay between queries in seconds (default: `1.0`)
- `BATCH_SIZE` - Number of queries before reporting statistics (default: `50`)

Example with caching enabled:
```bash
# Caching is controlled per environment in envs/dev / envs/prod via ENABLE_CACHE
make dev-up   # ENABLE_CACHE=false (dev default)
make prod-up  # ENABLE_CACHE=true (prod default)
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

The client container runs automatically when you start all services with `make dev-up` or `make prod-up`.

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

### Tagging Endpoints

- **GET /update_tagged_cards** - Import cards for specific tags
- **GET /discover_and_import_all_tags** - Bulk tag discovery and import

### Query Parameters

The search endpoint supports comprehensive Scryfall syntax.
See [syntax analysis](docs/technical/scryfall_syntax_analysis.md) for complete documentation.

## Development Notes

### Current Limitations

- **Missing Features**: See functionality grid above for complete list

### Future Enhancements

1. **Features**: Implement highest-priority missing functionality from grid above
1. **Testing**: Expand API comparison coverage and add performance benchmarks

For detailed technical analysis, see [functionality analysis documentation](docs/technical/scryfall_functionality_analysis.md).

## Data Sources & Attribution

### Card Data

Arcane Tutor uses card data from [Scryfall's official bulk data API](https://api.scryfall.com/bulk-data).
We are grateful to Scryfall for maintaining comprehensive, high-quality Magic: The Gathering card information and making it available to the community.

**Data Attribution**: Card data provided by [Scryfall](https://scryfall.com).
Arcane Tutor is an independent implementation and is not affiliated with, endorsed by, or sponsored by Scryfall.

### Intellectual Property

All Magic: The Gathering card names, artwork, and game content are © Wizards of the Coast LLC. This project respects all intellectual property rights and operates under the [Wizards of the Coast Fan Content Policy](https://company.wizards.com/en/legal/fancontentpolicy).

**Important**: This is unofficial Fan Content.
Not approved/endorsed by Wizards of the Coast.

### Security

All user input reaches the database only via parameterized queries; HTTP responses include CSP, X-Frame-Options, and CORS headers. See [docs/security/security_best_practices.md](docs/security/security_best_practices.md) for development guidelines. To report a vulnerability, see [SECURITY.md](SECURITY.md).

### Legal Compliance

For complete information about data sources, intellectual property attribution, and compliance with relevant policies, see [docs/legal.md](docs/legal/legal.md).

For attribution, IP rights, terms of service, and privacy policy, see [docs/legal/](docs/legal/).

## How Arcane Tutor Differs from Scryfall

While we use Scryfall's data, Arcane Tutor is a distinct implementation:

- **Original codebase**: All code written from scratch (no copied code from Scryfall)
- **Different database schema**: Custom PostgreSQL schema optimized for our use cases
- **Unique features**: Arithmetic expressions in queries, larger data fetch capabilities
- **Independent search algorithms**: Original query parser and search ranking
- **Different visual design**: Custom UI layout and styling
- **Open source**: Transparent, community-driven development

Our goal is to provide an open-source alternative that respects both Wizards of the Coast's intellectual property and Scryfall's valuable contribution to the MTG community.

