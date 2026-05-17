# Integration Tests using Testcontainers

This directory contains integration tests that use [testcontainers-python](https://github.com/testcontainers/testcontainers-python) to spin up real PostgreSQL containers for testing database-dependent functionality.

## Overview

The integration tests are located in `test_integration_testcontainers.py` and provide true integration testing by:

1. **Spinning up a real PostgreSQL container** using testcontainers
1. **Loading minimal test schema and data** from the `fixtures/` directory
1. **Testing actual database interactions** without mocks
1. **Ensuring proper isolation** - each test class gets its own container

## Benefits over Mocked Tests

- **Real database operations**: Tests actual SQL queries, transactions, and database constraints
- **Schema validation**: Ensures database schema works correctly with the application code
- **Performance testing**: Can identify slow queries or connection issues
- **Migration testing**: Validates that database migrations work correctly

## Running Integration Tests

### Prerequisites

- Docker must be installed and running
- Python dependencies installed: `pip install -r requirements.txt -r test-requirements.txt`

### Run integration tests only:

```bash
python -m pytest api/tests/test_integration_testcontainers.py -v
```

### Run all tests (unit + integration):

```bash
python -m pytest -v
```

### Run with more verbose output:

```bash
python -m pytest api/tests/test_integration_testcontainers.py -vvv --tb=short
```

## Test Structure

### Fixtures

- **postgres_container**: Manages PostgreSQL testcontainer lifecycle
- **db_connection**: Provides database connection to the test container
- **setup_test_database**: Loads test schema and data
- **api_resource_with_test_db**: Creates APIResource configured for test database

### Test Data

The `fixtures/` directory contains:

- `test_schema.sql`: Minimal database schema for testing
- `test_data.sql`: Sample card data for testing search functionality

### Test Coverage

Current integration tests cover:

- Database connectivity and readiness
- Card search by various criteria (type, name, color, CMC, power/toughness)
- Query parsing and SQL generation
- Tag functionality with real database
- Database operation isolation

## Adding New Integration Tests

1. Add new test methods to the `TestContainerIntegration` class
1. Use the `api_resource_with_test_db` fixture for database-dependent tests
1. Add any additional test data to `fixtures/test_data.sql` if needed
1. Ensure tests are properly isolated and don't depend on external services

## Performance Considerations

- Container startup adds ~2-3 seconds per test class
- Tests reuse the same container within a class (class-scoped fixtures)
- Each test gets a fresh APIResource instance but shares the database container
- Database is reset between test classes but not between individual tests within a class

## Troubleshooting

### Docker Issues

If tests fail with Docker-related errors:

- Ensure Docker daemon is running: `docker ps`
- Check Docker permissions: User must be able to run Docker commands
- Verify Docker images can be pulled: `docker pull postgres:15-alpine`

### Database Connection Issues

- Check that no other services are using the exposed ports
- Verify network connectivity to the container
- Look for PostgreSQL startup errors in container logs

### Test Data Issues

- Verify `fixtures/test_schema.sql` contains all required tables
- Check that `fixtures/test_data.sql` provides sufficient test data
- Ensure foreign key constraints are satisfied in test data
