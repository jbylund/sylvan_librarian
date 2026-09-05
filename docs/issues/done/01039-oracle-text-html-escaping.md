# Oracle Text Is Rendered as HTML Before Escaping

**Severity: medium. Found 2026-08-25. Status: fixed in #1039 (commit 01633312, 2026-08-26) — Single
Safe HTML Escaping Entry Point for mana and oracle text, covered by `api/tests/test_card_text_escaping.py`
and `api/static/app.test.js`. Verified during 2026-08-26 audit.**

The main search UI and no-JavaScript server renderer convert mana symbols and newlines in
`oracle_text`, then insert the result into HTML. They do not escape the original text first:

- [`api/static/app.js`](../../api/static/app.js): `formatOracleText()` calls
  `convertManaSymbols(oracleText)` and its result is used in `innerHTML` templates.
- [`api/noscript_helpers.py`](../../api/noscript_helpers.py): `format_oracle_text()` follows the same
  order and returns HTML.

[`api/static/card.js`](../../api/static/card.js) already shows the correct pattern:
`convertManaSymbols(escapeHtml(text))`.

Official Scryfall oracle text is not expected to contain arbitrary HTML, so this is not currently an
anonymous write-to-XSS path by itself. It becomes stored XSS if imported data is compromised,
custom data sources are added, an upstream response is poisoned, or an administrator imports a
hostile record. The affected content is shown to every visitor who renders the card.

## Fix

1. Escape the complete untrusted text first.
2. Replace only recognized mana-token text in the escaped representation with fixed, trusted markup.
3. Convert escaped newlines to `<br>`.
4. Escape unknown mana-token matches rather than returning their original text.
5. Use one tested semantic contract across `app.js`, `card.js`, and `noscript_helpers.py`.

Do not sanitize after creating mana-symbol markup; that either destroys the intended spans or
requires a complex HTML allowlist. Escaping first keeps the trusted markup boundary narrow.

This issue is distinct from the homepage script-context injection:

- [`security-search-results-script-context-xss.md`](./security-search-results-script-context-xss.md)

The two bugs need different serializers and regression tests.

## Acceptance criteria

- Card names, type lines, oracle text, mana costs, and external-link attributes have an explicit
  escaping rule at every `innerHTML`/HTML-string sink.
- Oracle text containing tags, event attributes, quotes, and malformed mana braces renders as text.
- Recognized mana symbols still render correctly in cards, modals, and no-JavaScript results.
- Browser and Python tests share hostile fixtures and assert equivalent safe output.
- The dedicated card page, search results, modal, and server-rendered fallback use the same ordering.

## Defense in depth

After inline application scripts are removed, tightening CSP to drop `script-src 'unsafe-inline'`
will reduce the impact of future HTML-injection mistakes. CSP is not a substitute for correct
escaping.
