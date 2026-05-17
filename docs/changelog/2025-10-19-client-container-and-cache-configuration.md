# Client Container and Cache Configuration

**Date**: 2025-10-19

## Overview

Restored the client container functionality and added configurable caching support to the API service. These features enable index analysis and performance optimization.

## Features Added

### 1. Configurable API Caching

The API now supports runtime cache configuration via the `ENABLE_CACHE` environment variable.

**Benefits:**

- Improved performance for repeated queries
- Reduced database load
- Configurable per deployment environment

**Configuration:**

```bash
# Enable caching
export ENABLE_CACHE=true

# Start services
docker compose up
```

**Default Behavior:**

- Caching is **disabled by default** to maintain backward compatibility
- Can be enabled by setting `ENABLE_CACHE` to `true`, `1`, or `yes`

### 2. Client Query Runner Container

A dedicated container that generates random queries to test API performance and index usage.

**Features:**

- Generates 144 unique query patterns
- Configurable query rate and batch size
- Statistics reporting
- Runs automatically with all services

**Usage:**

```bash
# Client runs automatically with all services
docker compose up

# Or with make
make up
```

**Configuration:**

- `API_URL`: API endpoint (default: `http://apiservice:8080`)
- `QUERY_DELAY`: Delay between queries in seconds (default: `1.0`)
- `BATCH_SIZE`: Queries before reporting stats (default: `50`)

## Implementation Details

### Cache Implementation

- Created `Settings` class for runtime configuration
- Modified `api/api_resource.py` with Settings class
- Updated `cached()` decorator to always create cached function and check settings at runtime
- Cache tests use `@pytest.mark.skipif` decorator
- All 687 tests pass with caching enabled

### Client Container

- Defined in `docker-compose.yml` (runs automatically with all services)
- Built from `client/Dockerfile`
- Runs `client/query_runner.py` module
- 16 unit tests covering query generation

### Query Patterns Generated

The client generates diverse queries covering:

- **Color queries**: Single and multicolor combinations
- **CMC queries**: Exact values and ranges
- **Type queries**: All card types and rarities
- **Combined queries**: Multi-criteria searches
- **Text searches**: Keywords, oracle text, sets, formats

## Testing

- All 683 existing tests pass with default configuration
- 4 cache-specific tests skip when caching disabled
- All 687 tests pass when `ENABLE_CACHE=true`
- Client tests: 16 passing

## Documentation Updates

- Added environment variables section to README.md
- Updated client/README.md with Docker Compose instructions
- Created this changelog entry

## Use Cases

### Index Analysis

Run the client to generate load and analyze PostgreSQL index usage:

```bash
# Start services (client runs automatically)
docker compose up

# In another terminal, check index usage
make dbconn
# Then run: SELECT * FROM pg_stat_user_indexes WHERE schemaname = 'magic';
```

### Performance Testing

Enable caching to test performance improvements:

```bash
export ENABLE_CACHE=true
docker compose up

# Monitor query performance in client logs
```

## Related Issues

This work addresses the issue: "Bring Back Client Container" which was deleted in commit 97cc27a.

The implementation focuses on:

1. Making the client container functional
2. Adding cache enable/disable configuration
3. No database migrations (as per instructions)

## Future Enhancements

Possible improvements based on the use of this feature:

- Database index optimization based on query patterns
- Additional query pattern generators
- Performance metrics collection
- Cache hit rate monitoring
