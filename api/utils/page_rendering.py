"""Assemble the two server-rendered HTML pages from their templates.

Both pages are built from a template plus shared fragments, with critical CSS inlined and asset URLs
given a content-hash query string so a deploy invalidates a stale browser cache without renaming
files. Building is cached per distinct input, since the result depends only on the critical CSS and
the site name — not on the request.

Hashes are computed once at import. A file that changes underneath a running process therefore keeps
serving its old query string, which is correct: the process is also still serving the old bytes.
"""

from __future__ import annotations

import hashlib
import pathlib

import cachebox
import falcon
import minify_html
import orjson
from cachebox import LRUCache

from api.utils.caching import cached

# api/utils/page_rendering.py -> api/static. Anchored on the package directory rather than counting
# parents from __file__, so moving this module within the package does not silently repoint it.
STATIC_DIR = pathlib.Path(__file__).resolve().parents[1] / "static"
_FRAGMENTS_DIR = STATIC_DIR / "fragments"
_INDEX_HTML_PATH = STATIC_DIR / "index.html"
_CARD_HTML_PATH = STATIC_DIR / "card.html"

# Placeholder written into index.html/card.html wherever the site name belongs, so the substitution
# below can't accidentally match unrelated copy that happens to contain "MTG Search".
SITE_NAME_PLACEHOLDER = "%%%SITENAME%%%"

# Markup identical across index.html and card.html — read once at import time and spliced into each
# template's own placeholder comment (<!-- FAVICON --> etc.) by build_base_html / build_card_html.
# Fragments live in fragments/ rather than static/ directly since they are not complete documents and
# are never served on their own (only files with a route entry are reachable over HTTP).
_FAVICON_HTML = (_FRAGMENTS_DIR / "favicon.html").read_text()
_PRECONNECTS_HTML = (_FRAGMENTS_DIR / "preconnects.html").read_text()
_FONTS_HTML = (_FRAGMENTS_DIR / "fonts.html").read_text()
_CSS_HTML = (_FRAGMENTS_DIR / "css.html").read_text()
_FOOTER_HTML = (_FRAGMENTS_DIR / "footer.html").read_text()


@cachebox.cached(cache={})
def read_static_bytes(filename: str) -> bytes:
    """Read a static file's raw bytes, once per process.

    The files under STATIC_DIR never change while a process is running (a deploy replaces them
    underneath a fresh process, same as the content-hash comment on `_static_hash` above), so a
    per-request disk read buys nothing -- callers serving binary assets (favicon.ico,
    social-preview.webp) use this directly instead of re-reading on every hit.

    Deliberately `cachebox.cached` (unconditional, like `pyparsing_based.get_parse_expr`), not the
    settings-aware `cached` used below for `build_base_html`/`build_card_html`: that wrapper falls
    through to an uncached call whenever `settings.enable_cache` is off (the test-suite default),
    which is the right behavior for a query-result cache but would defeat the point here -- these
    bytes are immutable for the life of the process regardless of that setting.
    """
    return (STATIC_DIR / filename).read_bytes()


@cachebox.cached(cache={})
def _read_static_text(filename: str) -> str:
    """Read a static file's text, once per process. See `read_static_bytes` for why."""
    return (STATIC_DIR / filename).read_text()


def serve_static_file(*, filename: str, falcon_response: falcon.Response) -> None:
    """Serve a static file to the Falcon response.

    Args:
    ----
        filename (str): The file to serve.
        falcon_response (falcon.Response): The Falcon response to write to.

    """
    try:
        falcon_response.text = _read_static_text(filename)
    except FileNotFoundError:
        falcon_response.status = falcon.HTTP_404
        falcon_response.text = f"File not found: {filename}"
    except PermissionError:
        falcon_response.status = falcon.HTTP_403
        falcon_response.text = f"Permission denied: {filename}"
    except OSError as e:
        falcon_response.status = falcon.HTTP_500
        falcon_response.text = f"Error reading file {filename}: {e}"


def _static_hash(filename: str) -> str | None:
    """Return a short content hash for a static file, or None if it is not built yet.

    Args:
        filename: Name of the file under STATIC_DIR.

    Returns:
        The first 12 hex characters of its sha256, or None. app.min.js is generated, so a checkout
        that has not run the minifier legitimately has no hash for it.
    """
    try:
        return hashlib.sha256((STATIC_DIR / filename).read_bytes()).hexdigest()[:12]
    except FileNotFoundError:
        return None


# Feed the cache-busting ?v= query strings. Computed once at import, so a file replaced underneath a
# running process keeps its old hash — which is correct, since the process serves the old bytes too.
_STYLES_CSS_HASH = _static_hash("styles.css")
_APP_MIN_JS_HASH = _static_hash("app.min.js")
_APP_JS_HASH = _static_hash("app.js")
_CARD_JS_HASH = _static_hash("card.js")


def _app_script_url() -> str:
    """The URL index.html's script tag should load, given what this checkout actually has.

    app.min.js is a build artifact (gitignored, produced by a Makefile rule), while index.html
    references it by name. An image built from a checkout that never ran the minifier therefore
    shipped a script tag for a file the route table could not serve, and the page loaded with no
    JavaScript at all. When there is no minified file, point at the committed app.js instead.
    """
    if _APP_MIN_JS_HASH:
        return f"/static/app.min.js?v={_APP_MIN_JS_HASH}"
    if _APP_JS_HASH:
        return f"/static/app.js?v={_APP_JS_HASH}"
    return "/static/app.js"


def _inject_shared_fragments(html: str) -> str:
    """Splice the shared head/footer fragments into their placeholder comments.

    Must run before the CRITICAL_CSS/asset-hash substitutions below: the CSS fragment carries its
    own inner <!-- CRITICAL_CSS --> placeholder, which only exists in `html` after this replace.
    """
    html = html.replace("<!-- FAVICON -->", _FAVICON_HTML)
    html = html.replace("<!-- PRECONNECTS -->", _PRECONNECTS_HTML)
    html = html.replace("<!-- FONTS -->", _FONTS_HTML)
    html = html.replace("<!-- CSS -->", _CSS_HTML)
    return html.replace("<!-- FOOTER -->", _FOOTER_HTML)


# Flip to False to disable HTML minification (e.g. while debugging a minifier-induced issue).
_MINIFY_HTML_ENABLED = True


def _minify_html(html: str) -> str:
    """Minify HTML to shave a bit more off the page weight on top of gzip/brotli/zstd compression.

    keep_comments=True is required: `build_base_html`'s cached output still carries per-request
    placeholders (SERVER_SIDE_RESULTS, SERVER_SIDE_EMBEDDED_DATA) substituted by `search()` after
    this function returns, and those are plain HTML comments that must survive intact.
    """
    if not _MINIFY_HTML_ENABLED:
        return html
    return minify_html.minify(html, minify_js=True, minify_css=True, keep_comments=True)


@cached(cache=LRUCache(maxsize=16))
def build_base_html(critical_css: str, site_name: str) -> str:
    """Read index.html and inject critical CSS and site name. Cached per (critical_css, site_name) pair."""
    html = _INDEX_HTML_PATH.read_text()
    html = _inject_shared_fragments(html)
    html = html.replace("<!-- CRITICAL_CSS -->", critical_css)
    if _STYLES_CSS_HASH:
        html = html.replace("/static/styles.css", f"/static/styles.css?v={_STYLES_CSS_HASH}")
    html = html.replace("/static/app.min.js", _app_script_url())
    return _minify_html(html.replace(SITE_NAME_PLACEHOLDER, site_name))


@cached(cache=LRUCache(maxsize=4))
def build_card_html(critical_css: str) -> str:
    """Read card.html and inject critical CSS and versioned asset URLs."""
    html = _CARD_HTML_PATH.read_text()
    html = _inject_shared_fragments(html)
    html = html.replace("<!-- CRITICAL_CSS -->", critical_css)
    if _STYLES_CSS_HASH:
        html = html.replace("/static/styles.css", f"/static/styles.css?v={_STYLES_CSS_HASH}")
    if _CARD_JS_HASH:
        html = html.replace("/static/card.js", f"/static/card.js?v={_CARD_JS_HASH}")
    return _minify_html(html)


def serialize_embedded_json(data: object) -> str:
    r"""Serialize data to JSON safe for inlining inside an HTML script tag.

    Replaces literal `<` with `\\u003c` so strings containing `</script>` or `<!--`
    cannot prematurely close the script context in HTML parsers.

    Args:
        data: The JSON-serializable Python data structure.

    Returns:
        JSON string safe for embedding inside an inline HTML <script> block.
    """
    return orjson.dumps(data).decode("utf-8").replace("<", "\\u003c")
