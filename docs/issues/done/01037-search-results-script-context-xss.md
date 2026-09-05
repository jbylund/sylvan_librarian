# Search Results Can Break Out of the Inline Script

**Severity: high. Found 2026-08-25. Status: fixed in #1037 (commit 49830944, 2026-08-26) — literal `<`
in the embedded search-results JSON is escaped to `<` in `serialize_embedded_json`
(`api/utils/page_rendering.py`), covered by `api/tests/test_page_rendering.py`. Verified during
2026-08-26 audit.**

The homepage embeds the complete search response directly into an executable `<script>` block:

```python
search_results_json = orjson.dumps(search_results).decode("utf-8")
embedded_data = f"""// Server-side embedded search results
      window.EMBEDDED_SEARCH_RESULTS = {search_results_json};
      """
```

That is safe as JavaScript string serialization but not as HTML serialization. HTML parsing recognizes
`</script>` even when it appears inside a JavaScript string. `orjson` emits `<` literally, and the
search response includes the original query, so an anonymous caller can supply a valid quoted search
term containing a script-closing sequence. The parser accepts that query and the homepage reflects it
into the script block.

This is an origin-level reflected XSS. The site currently has little user state, but the impact is
not limited to visual defacement: an administrator who has authenticated with HTTP Basic Auth may
have credentials cached for same-origin requests, allowing injected script to call privileged admin
routes. The current CSP permits inline script, so it does not contain the injection.

Affected code:

- [`api/api_resource.py`](../../api/api_resource.py), homepage result embedding
- [`api/static/index.html`](../../api/static/index.html), executable placeholder context
- [`api/middlewares/security_headers.py`](../../api/middlewares/security_headers.py), `script-src
  'unsafe-inline'`

## Fix

Do not put raw JSON inside an executable script context. The smallest safe fix is to replace every
literal `<` in the serialized JSON with `\u003c` before insertion. The preferred structural fix is:

1. Embed the payload in a non-executable `<script type="application/json" id="embedded-results">`.
2. Escape `<` in that payload as defense in depth.
3. Parse `textContent` from the external application script.
4. Remove the inline assignment and, once the remaining inline scripts are migrated, remove
   `'unsafe-inline'` from `script-src`.

Using `textContent` alone does not make an unescaped `</script>` safe; the HTML parser closes a
`type="application/json"` element the same way. The JSON still needs HTML-safe serialization.

## Acceptance criteria

- A search whose original query contains a script-closing sequence is returned as inert data.
- The serialized payload contains no literal `<`.
- Browser tests verify that the payload cannot create an element or execute script.
- Existing server-side result hydration still works with empty and non-empty result sets.
- A regression test covers both the serializer and the complete homepage response.

## Deployment consequence

This is an application defect; Caddy, nginx, TLS, and rate limiting do not fix it. Public internet
exposure should remain unsupported until the fix is deployed.
