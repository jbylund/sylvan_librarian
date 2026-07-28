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

import minify_html
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
_CARD_JS_HASH = _static_hash("card.js")


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
    if _APP_MIN_JS_HASH:
        html = html.replace("/static/app.min.js", f"/static/app.min.js?v={_APP_MIN_JS_HASH}")
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
