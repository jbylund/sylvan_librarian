# JavaScript Test Suite for index.html

This directory contains comprehensive tests for the JavaScript code in `api/index.html`.

## Test Files

### 1. Browser-Based Test Runner (`api/index-test.html`)

An interactive HTML test page that runs tests in the browser with a visual interface.

**Features:**
- Visual test results with pass/fail indicators
- Collapsible test suites
- Filter by test status (passed/failed/skipped)
- Performance metrics
- Auto-runs on page load

**Usage:**
```bash
# Start the API server
python api/entrypoint.py --port 8080

# Open in browser
open http://localhost:8080/index-test.html
```

Or simply open the file directly in a web browser.

### 2. Node.js Test Runner (`api/tests/test_index_js.mjs`)

A command-line test runner for CI/CD integration.

**Features:**
- Runs in Node.js without browser dependencies
- Colored terminal output
- Exit code 0 on success, 1 on failure
- Suitable for CI/CD pipelines

**Usage:**
```bash
# Run via npm
npm test

# Or run directly
node api/tests/test_index_js.mjs
```

## Test Coverage

The test suite covers the following areas:

### ManaSymbolConverter Class

- ✅ Basic mana symbols (W, U, B, R, G, C)
- ✅ Hybrid mana symbols (W/U, B/R, etc.)
- ✅ Numeric costs (0-16, X, Y, Z)
- ✅ Special symbols (T, Q, E, P, S, CHAOS, PW, ∞)
- ✅ Phyrexian mana (W/P, U/P, etc.)
- ✅ Three-color phyrexian (W/U/P, etc.)
- ✅ Modal vs. non-modal rendering
- ✅ Text conversion to emoji
- ✅ Unknown symbol preservation
- ✅ Performance benchmarks

### Utility Functions

- ✅ `escapeHtml()` - HTML escaping for XSS prevention
- ✅ `balanceQuery()` - Query string balancing
- ✅ Grid column calculation logic
- ✅ URL parameter defaults

### Test Statistics

- **Total Tests:** 42
- **Test Suites:** 8
- **Average Duration:** ~13ms

## Adding New Tests

### Browser Tests (index-test.html)

Add tests within the `<script>` section using the simple test framework:

```javascript
describe('My New Feature', () => {
  it('should do something', () => {
    const result = myFunction('input');
    expect(result).toBe('expected');
  });

  it('should handle edge cases', () => {
    expect(myFunction(null)).toBe('');
  });
});
```

### Node.js Tests (test_index_js.mjs)

Add tests using the same describe/it pattern:

```javascript
describe('My New Feature', () => {
  it('should do something', () => {
    const result = myFunction('input');
    assert.strictEqual(result, 'expected');
  });
});
```

## Available Assertion Methods

### Browser Tests

- `expect(value).toBe(expected)` - Strict equality
- `expect(value).toEqual(expected)` - Deep equality via JSON
- `expect(value).toContain(substring)` - String contains
- `expect(value).toBeGreaterThan(n)` - Numeric comparison
- `expect(value).toBeLessThan(n)` - Numeric comparison
- `expect(value).toBeNull()` - Null check
- `expect(value).toBeUndefined()` - Undefined check
- `expect(value).toBeTruthy()` - Truthy check
- `expect(value).toBeFalsy()` - Falsy check
- `expect(value).toMatch(regex)` - Regex match

### Node.js Tests

Uses Node.js built-in `assert` module:

- `assert.strictEqual(actual, expected)`
- `assert.ok(value)` / `assert.ok(value, message)`
- `assert.deepStrictEqual(actual, expected)`

## CI/CD Integration

The Node.js test runner is designed for CI/CD:

```yaml
# Example GitHub Actions workflow
- name: Run JavaScript Tests
  run: npm test
```

The test runner:
- Exits with code 0 on success
- Exits with code 1 on any test failure
- Provides clear console output
- Runs in under 20ms

## Test Philosophy

Following the project's test patterns:

1. **Parameterized tests** - Testing multiple cases efficiently
2. **Edge case coverage** - Null, undefined, empty strings
3. **Performance tests** - Ensuring code efficiency
4. **Clear naming** - Descriptive test names using "should" pattern
5. **Minimal dependencies** - No heavy test frameworks required

## Future Test Additions

Potential areas for expansion:

- [ ] Integration tests with mock fetch API
- [ ] CardSearch class method tests
- [ ] ThemeManager tests
- [ ] Modal interaction tests
- [ ] URL parameter parsing tests
- [ ] Image lazy loading tests
- [ ] Search debouncing tests
- [ ] AutoComplete logic tests

## Performance Benchmarks

Current performance (1000 iterations):

- Mana symbol conversion: ~4ms
- Text symbol conversion: ~1ms

These benchmarks ensure the UI remains responsive during frequent conversions.

## License

Same as the main project - ISC License
