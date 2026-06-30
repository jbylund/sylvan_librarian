# Security Best Practices for Sylvan Librarian

This document outlines security best practices for developing and maintaining the Sylvan Librarian application.

## Table of Contents

1. [Code Security](#code-security)
2. [Database Security](#database-security)
3. [API Security](#api-security)
4. [Frontend Security](#frontend-security)
5. [Dependency Management](#dependency-management)
6. [Infrastructure Security](#infrastructure-security)
7. [Security Testing](#security-testing)

---

## Code Security

### SQL Injection Prevention

**✅ DO:**
- Always use parameterized queries with `%(placeholder)s` syntax
- Validate user inputs before using them in queries
- Use type hints to enforce expected types

```python
# Good: Parameterized query
cursor.execute(
    "SELECT * FROM cards WHERE name = %(name)s",
    {"name": user_input}
)

# Good: Validated integer for SET commands
if not isinstance(timeout, int) or timeout < 0:
    raise ValueError("Invalid timeout")
cursor.execute(f"set statement_timeout = {timeout}")
```

**❌ DON'T:**
- Never use f-strings or string concatenation with user input
- Don't trust client-side validation alone

```python
# Bad: SQL injection vulnerability
cursor.execute(f"SELECT * FROM cards WHERE name = '{user_input}'")

# Bad: Unvalidated interpolation
cursor.execute(f"set timeout = {user_provided_value}")
```

---

### Cross-Site Scripting (XSS) Prevention

**✅ DO:**
- Escape all user-generated content before rendering
- Use `textContent` instead of `innerHTML` in JavaScript
- Implement Content Security Policy headers

```python
# Good: Proper HTML escaping
def escape_html(text: str) -> str:
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
```

```javascript
// Good: Safe DOM manipulation
element.textContent = userInput;

// Good: Escaping before insertion
element.innerHTML = escapeHtml(userInput);
```

**❌ DON'T:**
- Never insert raw user input into HTML
- Don't use `innerHTML` with untrusted content

```javascript
// Bad: XSS vulnerability
element.innerHTML = userInput;
```

---

### Input Validation

**✅ DO:**
- Validate all inputs server-side
- Use type hints and Pydantic models
- Implement whitelist validation where possible
- Sanitize inputs before processing

```python
# Good: Type-enforced validation
def search_cards(limit: int = 100) -> list:
    if limit < 1 or limit > 1000:
        raise ValueError("Limit must be between 1 and 1000")
    # ... rest of code
```

**❌ DON'T:**
- Don't rely solely on client-side validation
- Don't accept arbitrary file paths from users

---

### Secrets Management

**✅ DO:**
- Use environment variables for secrets
- Keep `.env` files out of version control
- Use separate credentials for dev/test/prod
- Rotate credentials regularly

```python
# Good: Environment variable
api_key = os.environ.get("API_KEY")

# Good: Test credentials with security exception
password = "testpass"  # noqa: S106
```

**❌ DON'T:**
- Never hardcode production credentials
- Don't commit secrets to git
- Don't log sensitive information

```python
# Bad: Hardcoded secret
api_key = "sk-1234567890abcdef"  # DON'T DO THIS
```

---

## Database Security

### Connection Security

**✅ DO:**
- Use connection pooling for efficiency
- Implement proper connection timeouts
- Use SSL/TLS for production connections
- Configure statement timeouts to prevent long-running queries

```python
# Good: Configured connection pool
pool = ConnectionPool(
    conninfo="host=db user=app password=$PG_PASSWORD",
    min_size=1,
    max_size=10,
    timeout=5.0
)
```

**❌ DON'T:**
- Don't use root/admin credentials for application
- Don't leave connections open indefinitely

---

### Query Safety

**✅ DO:**
- Use prepared statements via parameterized queries
- Validate data types before queries
- Implement query timeouts
- Log slow queries for monitoring

**❌ DON'T:**
- Don't construct queries with string concatenation
- Don't allow unbounded queries

---

## API Security

### HTTP Security Headers

The application implements the following security headers via `SecurityHeadersMiddleware`:

```python
{
    "Content-Security-Policy": "default-src 'self'; ...",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()"
}
```

**✅ DO:**
- Keep security headers updated
- Test CSP in report-only mode first
- Adjust CSP for legitimate external resources

**❌ DON'T:**
- Don't disable security headers
- Don't use overly permissive CSP policies

---

### CORS Configuration

**✅ DO:**
- Restrict origins to known domains in production
- Use environment-specific configurations
- Log unauthorized CORS requests

```python
# Good: Production CORS
allowed_origins = [
    "https://sylvan-librarian.com",
    "https://www.sylvan-librarian.com"
]
```

**❌ DON'T:**
- Don't use wildcard `*` in production
- Don't allow untrusted origins

```python
# Bad: Too permissive
Access-Control-Allow-Origin: *
```

---

### Rate Limiting (Future Enhancement)

**Recommended:**
- Implement per-IP rate limiting
- Use different limits for different endpoints
- Provide clear rate limit headers

---

## Frontend Security

### Client-Side Best Practices

**✅ DO:**
- Validate inputs client-side for UX (but also server-side for security)
- Use `textContent` for dynamic content
- Sanitize URLs before navigation
- Implement CSP-compliant inline scripts

**❌ DON'T:**
- Don't trust client-side validation alone
- Don't use `eval()` or `Function()` constructor
- Don't include sensitive data in client code

---

### Third-Party Resources

**✅ DO:**
- Use Subresource Integrity (SRI) for CDN resources
- Verify CDN SSL certificates
- Keep track of all external dependencies

```html
<!-- Good: SRI hash -->
<script 
  src="https://cdn.example.com/lib.js"
  integrity="sha384-..."
  crossorigin="anonymous">
</script>
```

---

## Dependency Management

### Python Dependencies

**✅ DO:**
- Run `pip-audit` regularly to check for vulnerabilities
- Keep dependencies up to date
- Pin versions in requirements files
- Review dependency licenses

```bash
# Check for vulnerabilities
python -m pip_audit

# Update dependencies (with caution)
pip install --upgrade package-name
```

**❌ DON'T:**
- Don't use unmaintained packages
- Don't install packages from untrusted sources

---

### JavaScript Dependencies

**✅ DO:**
- Run `npm audit` regularly
- Use `npm audit fix` for automatic fixes
- Review package.json for unused dependencies

```bash
# Check for vulnerabilities
npm audit

# Fix vulnerabilities
npm audit fix
```

---

## Infrastructure Security

### Docker Security

**✅ DO:**
- Run containers as non-root user
- Use official base images
- Scan images for vulnerabilities
- Keep images updated

```dockerfile
# Good: Non-root user
USER 1000:1000
```

**❌ DON'T:**
- Don't run containers as root
- Don't include secrets in images

---

### Environment Variables

**✅ DO:**
- Use `.env` files for local development
- Use secret management in production (e.g., AWS Secrets Manager)
- Validate required variables at startup

```python
# Good: Required variable check
if not os.environ.get("PGPASSWORD"):
    raise RuntimeError("PGPASSWORD environment variable required")
```

---

## Security Testing

### Automated Testing

**✅ DO:**
- Include security tests in test suite
- Run security scans in CI/CD pipeline
- Test authentication and authorization
- Validate input handling

```python
# Good: Security test
def test_sql_injection_prevention():
    result = search_cards("'; DROP TABLE cards; --")
    assert result == []  # Should not execute SQL injection
```

---

### Manual Testing

**✅ DO:**
- Test for common vulnerabilities (OWASP Top 10)
- Attempt SQL injection, XSS, CSRF
- Test with unexpected inputs
- Review error messages for information disclosure

---

### Security Scanning Tools

Recommended tools:
- **Static Analysis:** `ruff`, `bandit`, `pylint`
- **Dependency Scanning:** `pip-audit`, `npm audit`, Snyk
- **Container Scanning:** Trivy, Clair
- **DAST:** OWASP ZAP, Burp Suite

```bash
# Run security linters
python -m bandit -r api/

# Dependency scanning
python -m pip_audit
npm audit
```

---

## Security Checklist for Pull Requests

Before merging any PR, verify:

- [ ] All SQL queries use parameterized statements
- [ ] User inputs are validated server-side
- [ ] HTML output is properly escaped
- [ ] No hardcoded secrets in code
- [ ] Security headers remain configured
- [ ] CORS is properly restricted
- [ ] Dependencies scanned for vulnerabilities
- [ ] Tests cover security-critical paths
- [ ] Code passes security linting
- [ ] Changes reviewed for security implications

---

## Incident Response

If a security vulnerability is discovered:

1. **Assess Severity:** Determine impact and exploitability
2. **Develop Fix:** Create patch following secure coding practices
3. **Test Thoroughly:** Ensure fix resolves issue without breaking functionality
4. **Deploy Quickly:** Prioritize security fixes
5. **Document:** Update security audit report
6. **Learn:** Improve practices to prevent similar issues

---

## Resources

- [OWASP Top 10](https://owasp.org/www-project-top-ten/)
- [OWASP Cheat Sheet Series](https://cheatsheetseries.owasp.org/)
- [Python Security Best Practices](https://python.readthedocs.io/en/stable/library/security_warnings.html)
- [Falcon Security Guide](https://falcon.readthedocs.io/en/stable/api/middleware.html)
- [PostgreSQL Security](https://www.postgresql.org/docs/current/sql-syntax-lexical.html#SQL-SYNTAX-IDENTIFIERS)

---

## Contact

For security concerns or to report vulnerabilities, please contact the maintainers through GitHub issues or direct communication.

**Remember:** Security is everyone's responsibility. When in doubt, err on the side of caution.
