# Security Checklist for Pull Requests

Use this checklist when reviewing pull requests to ensure security standards are maintained.

## General Security Review

### Code Changes
- [ ] No hardcoded secrets, API keys, or passwords
- [ ] All user inputs are validated server-side
- [ ] Type hints are used for function parameters
- [ ] Error messages don't expose sensitive information
- [ ] Logging doesn't include sensitive data (passwords, tokens, etc.)

### Dependencies
- [ ] New dependencies have been security scanned
- [ ] Dependencies are from trusted sources (PyPI, npm)
- [ ] Dependencies are pinned to specific versions
- [ ] License compatibility reviewed for new dependencies

## Database Security

### SQL Queries
- [ ] All SQL queries use parameterized statements (`%(placeholder)s`)
- [ ] No string concatenation or f-strings with user input in queries
- [ ] Statement timeouts are validated as integers before use
- [ ] Database credentials are from environment variables

### Data Access
- [ ] Queries include appropriate WHERE clauses to limit access
- [ ] No unbounded queries that could return excessive data
- [ ] Sensitive data is properly masked/encrypted if stored

## API Security

### Input Validation
- [ ] All API parameters are validated for type and range
- [ ] Query string parameters are sanitized
- [ ] File uploads (if any) are restricted by type and size
- [ ] Path parameters don't allow directory traversal

### Authentication & Authorization
- [ ] Protected endpoints require authentication (if applicable)
- [ ] Authorization checks are performed server-side
- [ ] Session management is secure (if applicable)

### HTTP Security
- [ ] Security headers remain configured
- [ ] CORS restrictions are appropriate for endpoint
- [ ] Rate limiting considerations documented (if needed)
- [ ] HTTPS is enforced in production settings

## Frontend Security

### XSS Prevention
- [ ] User-generated content is properly escaped
- [ ] DOM manipulation uses `textContent` not `innerHTML`
- [ ] Any use of `innerHTML` includes proper sanitization
- [ ] CSP policy allows only necessary resources

### Client-Side Validation
- [ ] Client-side validation is for UX only
- [ ] Server-side validation is present for all inputs
- [ ] Form submissions are protected against CSRF (if applicable)

## Infrastructure Security

### Container/Deployment
- [ ] Containers run as non-root user
- [ ] No secrets in Dockerfile or docker-compose.yml
- [ ] Environment variables used for configuration
- [ ] Health checks don't expose sensitive information

### Configuration
- [ ] Sensitive config in environment variables
- [ ] `.env` file remains in `.gitignore`
- [ ] Production and development configs are separated
- [ ] Default credentials are changed

## Testing

### Security Tests
- [ ] Tests cover security-critical code paths
- [ ] Input validation is tested with edge cases
- [ ] SQL injection attempts are tested
- [ ] XSS attempts are tested (if frontend changes)

### Test Data
- [ ] Test secrets are marked with `# noqa: S106`
- [ ] Test data doesn't contain real sensitive information
- [ ] Test databases are isolated from production

## Automated Checks

### Linting & Scanning
- [ ] `ruff check` passes without security warnings
- [ ] `pip-audit` shows no vulnerabilities (for Python deps)
- [ ] `npm audit` shows no vulnerabilities (for JS deps)
- [ ] CodeQL security checks pass (if enabled)
- [ ] All tests pass: `pytest`

### Commands to Run
```bash
# Python security checks
python -m ruff check
python -m pip_audit
python -m pytest -v

# JavaScript security checks (if applicable)
npm audit

# Optional: Run bandit for deeper Python security analysis
python -m bandit -r api/
```

## Documentation

### Security Documentation
- [ ] Security implications of changes are documented
- [ ] README updated if security configuration changes
- [ ] API documentation reflects authentication requirements
- [ ] Breaking changes are clearly documented

## Specific Vulnerability Checks

### SQL Injection
- [ ] No dynamic SQL construction with user input
- [ ] All parameters properly escaped/parameterized
- [ ] Database ORM/library used correctly

### Cross-Site Scripting (XSS)
- [ ] HTML entities escaped in templates
- [ ] JavaScript properly escapes dynamic content
- [ ] CSP headers prevent inline script execution

### Cross-Site Request Forgery (CSRF)
- [ ] State-changing operations use POST/PUT/DELETE
- [ ] CSRF tokens used for forms (if applicable)
- [ ] SameSite cookie attribute set appropriately

### Clickjacking
- [ ] X-Frame-Options header configured
- [ ] CSP frame-ancestors directive set

### Directory Traversal
- [ ] File paths are validated and sanitized
- [ ] User input doesn't construct file paths
- [ ] File operations use safe path libraries

### Information Disclosure
- [ ] Stack traces not exposed in production
- [ ] Error messages don't reveal system details
- [ ] Debug mode disabled in production
- [ ] Logs don't contain sensitive data

## High-Risk Changes

If your PR includes any of these, extra scrutiny is required:

- [ ] Changes to authentication/authorization logic
- [ ] New database queries or schema changes
- [ ] Modifications to security middleware
- [ ] Changes to CORS or CSP configuration
- [ ] New third-party dependencies
- [ ] File upload/download functionality
- [ ] Payment processing or PII handling
- [ ] Cryptographic operations

## Final Review

- [ ] Code reviewed by at least one other developer
- [ ] All automated security checks pass
- [ ] Manual security testing performed for high-risk changes
- [ ] Security documentation updated if needed
- [ ] Changes follow existing security patterns in codebase

## Post-Merge Monitoring

After merging security-sensitive changes:

- [ ] Monitor error logs for unusual activity
- [ ] Watch for increased error rates
- [ ] Verify security headers in production
- [ ] Confirm CORS restrictions working as expected

---

## Severity Levels

Use these guidelines to assess security findings:

**Critical (🔴):** Immediate exploitation possible, high impact
- SQL injection vulnerabilities
- Remote code execution
- Authentication bypass

**High (🟠):** Exploitation likely, significant impact
- XSS vulnerabilities
- Unauthorized data access
- Weak authentication

**Medium (🟡):** Exploitation possible, moderate impact
- Information disclosure
- Missing security headers
- Improper CORS configuration

**Low (🟢):** Difficult to exploit, minimal impact
- Verbose error messages
- Missing input validation on low-risk fields

---

## Questions to Ask

When reviewing code, ask:

1. **Input:** Can I control this input as an attacker?
2. **Processing:** How is this input processed and validated?
3. **Output:** Where does this data end up?
4. **Privilege:** What privileges does this code run with?
5. **Trust:** What assumptions are being made about data sources?
6. **Failure:** What happens if this fails or receives unexpected input?

---

## Resources

- [Security Audit Report](./security_audit_report.md)
- [Security Best Practices](./security_best_practices.md)
- [OWASP Top 10](https://owasp.org/www-project-top-ten/)
- [CWE Top 25](https://cwe.mitre.org/top25/)

---

**Remember:** It's easier to build security in than to bolt it on later. When in doubt, ask for a security review!
