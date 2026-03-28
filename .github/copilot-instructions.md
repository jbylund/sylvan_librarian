# Scryfall OS

Scryfall OS is an open source implementation of Scryfall, a Magic: The Gathering card search engine.
This project consists of a Python-based API service, a simple HTML/JavaScript web client, and a PostgreSQL database, designed to be deployed via Docker Compose.

Always reference these instructions first and fallback to search or bash commands only when you encounter unexpected information that does not match the info here.

## Working Effectively

### Prerequisites and Environment Setup
- **Install uv** (recommended package manager):
  - `python -m pip install uv` -- takes ~3 seconds. Modern Python package manager.
- **Create virtual environment** (required for Python 3.12+):
  - `python -m uv venv .venv` -- takes ~1 second. Creates virtual environment at `.venv/`
  - `source .venv/bin/activate` -- activates the virtual environment
  - Alternative: Use `make venv` to create virtual environment
- **Install Python dependencies using uv** (after activating virtual environment):
  - `uv pip install -r requirements/base.txt -r requirements/test.txt` -- takes ~2-3 seconds.
  - Alternative: Legacy method with pip (not recommended): `python -m pip install -r requirements/base.txt -r requirements/test.txt` -- takes ~4-6 seconds.

### Modular Dependency Structure
- **requirements/base.txt**: Core application dependencies for testing and development
- **requirements/test.txt**: Testing and linting dependencies
- **requirements/webserver.txt**: Web server dependencies (bjoern) only needed for local API server
- **requirements/fonts.txt**: Font subsetting dependencies for font optimization

### For Web Server Development (Optional)
**Only needed if you plan to run the local API server**:
- Install system dependencies:
  - `sudo apt-get update` -- takes ~7 seconds. NEVER CANCEL.
  - `sudo apt-get install -y libev-dev` -- takes ~5 seconds. NEVER CANCEL.
- **Install web server dependencies** (in virtual environment):
  - `uv pip install -r requirements/webserver.txt` -- takes ~3 seconds. Includes bjoern compilation.
  - Legacy method: `python -m pip install -r requirements/webserver.txt` -- takes ~5 seconds.

### Build and Test Workflow
- **Run tests**: `python -m pytest -vvv` -- takes ~15-20 seconds. All 438 tests should pass.
- **Run linting**: `python -m ruff check` -- takes <1 second. Should pass with "All checks passed!"
- **Run pylint**: `find . -type f -name "*.py" | xargs python -m pylint --fail-under 7.0 --max-line-length=132` -- takes ~7 seconds. Currently scores 9.01/10 and passes.
- **Format code**: `python -m ruff check --fix --unsafe-fixes` -- takes <1 second. Auto-fixes style issues.
- **Format HTML**: `npx prettier --write api/index.html` -- takes ~2 seconds.

### Docker Environment (Working)
- **Build images**: `make build_images` -- takes ~30-60 seconds. NEVER CANCEL.
- **Start services (dev)**: `make dev-up` -- will start PostgreSQL and API services.
- **Start services (prod)**: `make prod-up` -- will start PostgreSQL and API services.

### Local Development (Recommended)
- **Run API locally**: `python api/entrypoint.py --port 8080 --workers 4` -- starts the API. Requires bjoern installation.
- **Test API help**: `python api/entrypoint.py --help` -- shows available command line options.
- **API serves web interface**: Visit http://localhost:8080/ to see the HTML interface.

## Validation

### Manual Testing Scenarios
- **ALWAYS run the complete test suite** after making changes: `python -m pytest -vvv` -- should show "438 passed"
- **Test parsing functionality**: `python -c "from api.parsing import parse_scryfall_query; print(parse_scryfall_query('cmc=3'))"` -- should output Query AST structure
- **Test API entry point**: `python api/entrypoint.py --help` -- should show command line options
- **Test API functionality**: Start API with `python api/entrypoint.py --port 8080 --workers 2` then visit http://localhost:8080/ to see web interface
- **BEFORE committing**: Run `python -m ruff check --fix --unsafe-fixes` to fix style issues
- **BEFORE committing**: Run `npx prettier --write api/index.html` to format the HTML frontend.

### Quick Validation Workflow
```bash
# Modern approach with uv (recommended)
python -m pip install uv              # Install uv if not available
python -m uv venv .venv               # Create virtual environment
source .venv/bin/activate             # Activate virtual environment
uv pip install -r requirements/base.txt -r requirements/test.txt
python -m pytest -vvv
python -c "from api.parsing import parse_scryfall_query; print(parse_scryfall_query('cmc=3'))"
python -m ruff check

# Legacy approach (fallback)
python -m venv .venv                  # Create virtual environment
source .venv/bin/activate             # Activate virtual environment
python -m pip install -r requirements/base.txt -r requirements/test.txt
python -m pytest -vvv
python -c "from api.parsing import parse_scryfall_query; print(parse_scryfall_query('cmc=3'))"
python -m ruff check

# Additional setup for web server testing (adds ~15 seconds)
sudo apt-get update && sudo apt-get install -y libev-dev
uv pip install -r requirements/webserver.txt  # Required for local API testing (in venv)
```

### Current Limitations
- **bjoern dependency**: Separated into requirements/webserver.txt for modular installation
- **Database integration testing**: Requires running PostgreSQL or Docker compose setup
- The project builds and tests successfully for both local Python development and containerized deployment.

## Common Tasks

### Development Commands That Work
```bash
# Install dependencies (run once) - modern approach with uv
python -m pip install uv                   # Install uv package manager
make venv                                  # Create virtual environment (or: python -m uv venv .venv)
source .venv/bin/activate                  # Activate virtual environment
uv pip install -r requirements/base.txt -r requirements/test.txt

# Legacy approach (fallback)
python -m venv .venv                       # Create virtual environment
source .venv/bin/activate                  # Activate virtual environment
python -m pip install --upgrade pip
python -m pip install -r requirements/base.txt -r requirements/test.txt

# Test and validate changes (works without system dependencies)
python -m pytest -vvv                    # Run tests (~15-20 seconds, 438 tests)
python -m ruff check --fix --unsafe-fixes # Fix style issues (<1 second)
npx prettier --write api/index.html      # Format HTML (~2 seconds)
python api/entrypoint.py --help          # Test API entrypoint (shows help)

# Additional setup for web server (optional)
sudo apt-get update && sudo apt-get install -y libev-dev
uv pip install -r requirements/webserver.txt  # Required for local API testing (in venv)

# Docker workflow (works)
make build_images                        # Build Docker images (~30 seconds)
make dev-up                              # Start all services - dev environment
make prod-up                             # Start all services - prod environment
```

### Development Commands With Known Issues
```bash
# These commands work but have caveats:
make lint                               # Works but requires installing pylint first
./.github/copilot-setup.sh             # Automated setup script (creates virtual environment, needs activation after)
```

### Timing Expectations - NEVER CANCEL
- **System package installation**: 5-7 seconds per package. NEVER CANCEL.
- **Virtual environment creation**: ~1 second. NEVER CANCEL.
- **Python dependency installation**:
  - With uv: 2-3 seconds for standard packages. NEVER CANCEL.
  - With pip: 4-6 seconds for standard packages. NEVER CANCEL.
- **uv installation**: ~3 seconds. NEVER CANCEL.
- **bjoern compilation**: ~3 seconds including C compilation. NEVER CANCEL.
- **Unit tests**: ~15-20 seconds for full test suite (438 tests).
- **Linting**: <1 second for ruff, ~7 seconds for pylint.
- **Docker builds**: 30-60 seconds when working. NEVER CANCEL.
- **HTML formatting**: ~2 seconds. NEVER CANCEL.

## Project Structure Reference

### Repository Root
```
.
├── README.md                 # Basic project description
├── NOTES.md                 # Development notes
├── makefile                 # Build automation
├── docker-compose.yml       # Container orchestration
├── package.json             # Node.js dependencies (prettier)
├── pyproject.toml           # Python project configuration
├── requirements/            # Python dependencies
│   ├── base.txt             # Core runtime dependencies
│   ├── fonts.txt            # Font subsetting dependencies
│   ├── test.txt             # Test dependencies
│   └── webserver.txt        # Web server dependencies
├── api/                     # Python API service
├── client/                  # HTML/JS client
├── configs/                 # Configuration files
└── scripts/                 # Build helper scripts
```

### Key API Files
```
api/
├── entrypoint.py           # Main API server entry point
├── api_resource.py         # Falcon web framework resources
├── api_worker.py           # Multi-process worker implementation
├── index.html              # Web frontend (single file)
├── parsing/                # Scryfall query parser
│   ├── parsing_f.py        # Parser implementation
│   ├── nodes.py            # AST node definitions
│   └── tests/              # Parser unit tests
└── db/                     # Database schema files
```

### GitHub Actions CI
- **Unit tests workflow**: `.github/workflows/unit-tests.yml`
- **Lint workflow**: `.github/workflows/lint.yml`
- **CI Monitor workflow**: `.github/workflows/ci-monitor.yml` -- automated failure detection
- **Runs on every push**: Installs Python 3.13, system deps, uses uv for Python deps, runs pytest and ruff
- **Expected to pass**: All 438 tests should pass in CI environment
- **Uses uv**: CI workflows use uv package manager for faster dependency installation
- **Automated setup script**: `.github/copilot-setup.sh` -- automated environment setup with uv support

## Key Development Notes
- **Database schema**: Complex PostgreSQL schema in `api/db/` with Magic card data structures
- **Query parser**: Implements Scryfall's search DSL in `api/parsing/`
- **Web framework**: Uses Falcon for lightweight, fast API development
- **Multi-process**: API uses bjoern WSGI server with multiple worker processes (requires separate bjoern installation)
- **Package management**: Uses uv for fast dependency installation (preferred over pip)
- **Code quality**: Project maintains good code quality with ruff (passing) and pylint (9.01/10 score)
- **Testing**: Excellent test coverage with 438 tests for parsing logic, query translation, and API functionality
- **Web interface**: Single-file HTML/CSS/JS application served by the API at `/`
- **CI/CD**: Automated monitoring with issue creation for failed CI runs

## GitHub Copilot Integration

### Best Practices for Contributors
- **Follow existing patterns**: The codebase has established patterns for parsing, testing, and API design
- **Use type hints**: All new code should include proper Python type annotations
- **Test coverage**: Write tests for new functionality following existing test patterns in `api/tests/` and `api/parsing/tests/`
- **Code formatting**: Always run `python -m ruff check --fix --unsafe-fixes` before committing
- **Performance**: Use uv for dependency management when possible for faster builds
- **Database migrations**: Never change a migration file `api/db/*.sql` that already exists on main
- **Migration naming**: All migration files should be prefixed with YYYY-MM-DD-## (year, month, day, integer number) where the number is a sequence starting at 01 - this allows the addition of more than one migration per day
- **Scryfall syntax**: Check the [Scryfall syntax guide](https://scryfall.com/docs/syntax) when unsure of what behavior should be
- **Feature documentation**: For larger features add a document in `docs/changelog/` again prefixed with YYYY-MM-DD
- **Test structure**: Prefer parameterized tests rather than looping over test cases within a test - this gives better developer visibility into where the issue is

### Development Workflow with Copilot
1. **Setup environment**: Run `./.github/copilot-setup.sh` for automated setup
1. **Make changes**: Use GitHub Copilot to assist with code generation
1. **Test locally**: Run `python -m pytest -vvv` to ensure all 438 tests pass
1. **Format code**: Run `python -m ruff check --fix --unsafe-fixes`
1. **Commit changes**: CI will automatically test with Python 3.13 and uv

### Common Integration Points
- **Parser development**: Focus on `api/parsing/` for Scryfall query syntax
- **API endpoints**: Add new resources in `api/api_resource.py`
- **Database queries**: SQL files in `api/db/` for complex queries
- **Testing**: Comprehensive test suites in both `api/tests/` and `api/parsing/tests/`
