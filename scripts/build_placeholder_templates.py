"""Build the placeholder template artifact pair (grayscale frame templates + CSS).

Produces the versioned artifacts that must ship together:

- scripts/placeholder_templates_v{N}.json — per-bucket grayscale template luminance +
  geometry, consumed by placeholder_measurement.py (in copy_images_to_s3.py at
  image-processing time)
- api/static/placeholders-v{N}.css — the templates as webp data URIs plus per-bucket
  default colors and geometry; one class per bucket (.ph-<bucket>) and per-color
  fallback aliases (.ph-fb-<group>) for printings with no measured colors yet

Buckets are (frame generation x color group), a pure function of card metadata (see
placeholder_measurement.bucket_for) — identifiers are stable by construction, so
rebuilding only refreshes template images and default colors, never meaning.

Sampling pulls artwork-unique printings from the live API. Frame generation comes
from `frame:` queries (Scryfall's per-printing frame field — release year mislabels
retro-frame reprints), white-border and borderless corpora from `border:` queries.
Each bucket's members are averaged into a normalized grayscale template with a
transparent art window; the CSS composites it over per-card colors with
background-blend-mode: multiply.

Usage:
    python scripts/build_placeholder_templates.py --version 1 [--cache-dir DIR]

Requires: numpy, pillow (requirements/placeholders.txt).
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import io
import json
import logging
import tempfile
import urllib.parse
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageFilter

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SEARCH_URL = "https://sylvan-librarian.com/search"
CDN_URL = "https://d1hot9ps2xugbc.cloudfront.net/img/{set_code}/{collector_number}/1/280.webp"

TPL_W, TPL_H = 112, 157  # template resolution; keeps the 745:1041 card aspect
BORDER_FRAC = 0.05  # outer ring excluded from tint solving / normalization
MIN_BUCKET_SIZE = 15
LUM_WEIGHTS = np.array([0.299, 0.587, 0.114])

# art window rects (x0, y0, x1, y1) as fractions, per frame generation
ART_RECTS = {
    "old": (0.10, 0.105, 0.90, 0.525),
    "old-wb": (0.10, 0.105, 0.90, 0.525),
    "modern": (0.075, 0.11, 0.925, 0.545),
    "m15": (0.06, 0.105, 0.94, 0.56),
    "fullart": (0.045, 0.045, 0.955, 0.82),
}
GEN_ORDER = ["old", "old-wb", "modern", "m15", "fullart"]
COLOR_ORDER = ["w", "u", "b", "r", "g", "gold", "artifact", "land"]
BORDER_COLORS = {"old-wb": "e8e0d0"}
DEFAULT_BORDER_COLOR = "0d0d0d"
# fallback aliases point at the m15 bucket of each color group (most printings today)
FALLBACK_GEN = "m15"

GOLD_PAIRS = ["wu", "ub", "br", "rg", "gw", "wb", "ur", "bg", "rw", "gu"]
FACETS = (
    [("land", "t:land"), ("artifact", "t:artifact")]
    + [("gold", f"c:{pair}") for pair in GOLD_PAIRS]
    + [(color, f"c={color}") for color in "wubrg"]
)
# frame: value -> generation; queried per facet. border: queries label wb/fullart.
FRAME_QUERIES = {"1993": "old", "1997": "old", "2003": "modern", "2015": "m15"}


def fetch_query(query: str) -> list[tuple[str, str]]:
    """Run one search against the live API; returns (set_code, collector_number) pairs."""
    try:
        response = requests.get(
            SEARCH_URL, params={"q": query, "unique": "artwork"}, headers={"Accept": "application/json"}, timeout=30
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        logger.exception("query failed: %r", query)
        return []
    return [(card["set_code"], card["collector_number"]) for card in payload.get("cards", [])]


def sample_corpus() -> dict[str, tuple[str, str]]:
    """{image key: (color group, frame generation)} for a diverse artwork-unique sample."""
    jobs: list[tuple[str, str, str]] = []  # (color, gen, query); later jobs override earlier
    for color, fragment in FACETS:
        for frame, gen in FRAME_QUERIES.items():
            jobs.append((color, gen, f"frame:{frame} {fragment}"))
        jobs.append((color, "old-wb", f"border:white {fragment}"))
        jobs.append((color, "fullart", f"border:borderless {fragment}"))

    labels: dict[str, tuple[str, str]] = {}
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        for (color, gen, _), cards in zip(jobs, pool.map(lambda j: fetch_query(j[2]), jobs), strict=True):
            for set_code, collector_number in cards:
                labels[f"{set_code}__{collector_number}"] = (color, gen)
    logger.info("%d queries -> %d unique artworks", len(jobs), len(labels))
    return labels


def download_thumb(cache_dir: Path, key: str) -> Path | None:
    """Fetch a printing's 280px thumbnail into the cache dir; None on failure."""
    dest = cache_dir / f"{key}.webp"
    if dest.exists():
        return dest
    set_code, collector_number = key.split("__", 1)
    url = CDN_URL.format(set_code=urllib.parse.quote(set_code), collector_number=urllib.parse.quote(collector_number))
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        dest.write_bytes(response.content)
    except requests.RequestException:
        return None
    return dest


def load_arr(path: Path) -> np.ndarray | None:
    """Decode and downscale one thumbnail to template resolution; None if unreadable."""
    try:
        with Image.open(path) as image:
            small = image.convert("RGB").resize((TPL_W, TPL_H), Image.LANCZOS)
    except OSError:
        return None
    return np.asarray(small, dtype=np.float32)


def masks_for(gen: str) -> tuple:
    """(art_mask, tint_mask, left_mask, right_mask) for a generation's art rect."""
    x0, y0, x1, y1 = ART_RECTS[gen]
    art = np.zeros((TPL_H, TPL_W), dtype=bool)
    art[round(y0 * TPL_H) : round(y1 * TPL_H), round(x0 * TPL_W) : round(x1 * TPL_W)] = True
    inner = np.zeros((TPL_H, TPL_W), dtype=bool)
    bx, by = round(BORDER_FRAC * TPL_W), round(BORDER_FRAC * TPL_H)
    inner[by:-by, bx:-bx] = True
    tint = inner & ~art
    cols = np.arange(TPL_W)[None, :].repeat(TPL_H, axis=0)
    return art, tint, tint & (cols < 0.42 * TPL_W), tint & (cols > 0.58 * TPL_W)


def solve_tint(card: np.ndarray, template_lum: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Least-squares RGB tint so (template/255) * tint ~= card over the mask."""
    t = template_lum[mask] / 255.0
    c = card[mask]
    return np.clip((t[:, None] * c).sum(axis=0) / max(float((t * t).sum()), 1e-6), 0, 255)


def hexify(rgb: np.ndarray) -> str:
    """Bare lowercase hex for an RGB triple."""
    r, g, b = rgb.round().astype(int)
    return f"{r:02x}{g:02x}{b:02x}"


def template_data_uri(lum: np.ndarray, art_mask: np.ndarray) -> tuple[str, int]:
    """(data URI, byte size) for a grayscale template with a feathered art window."""
    alpha = np.full((TPL_H, TPL_W), 255, dtype=np.uint8)
    alpha[art_mask] = 0
    alpha_img = Image.fromarray(alpha).filter(ImageFilter.GaussianBlur(1.2))
    tpl_img = Image.merge("LA", (Image.fromarray(lum.astype(np.uint8)), alpha_img)).convert("RGBA")
    buffer = io.BytesIO()
    tpl_img.save(buffer, "WEBP", quality=55, method=6)
    return "data:image/webp;base64," + base64.b64encode(buffer.getvalue()).decode(), buffer.tell()


def emit_css(buckets: dict[str, dict], path: Path) -> None:
    """Write the placeholder stylesheet.

    Base rule + one class per bucket carrying the template data URI, layer geometry,
    categorical border color, and default (bucket-mean) tint colors, so a class with
    no inline style still renders a sensible generic placeholder. Fallback aliases
    .ph-fb-<group> share the FALLBACK_GEN bucket's rule.
    """
    rules = [
        '[class^="ph-"],[class*=" ph-"]{'
        "background-image:var(--tpl),linear-gradient(var(--art),var(--art)),"
        "linear-gradient(90deg,var(--frame-l) 0 40%,var(--frame-r) 60% 100%);"
        "background-repeat:no-repeat;background-blend-mode:multiply,normal,normal}"
    ]
    frame_size = (1 - 2 * BORDER_FRAC) * 100
    for name, bucket in buckets.items():
        gen, group = name.rsplit("-", 1)
        x0, y0, x1, y1 = ART_RECTS[gen]
        art_w, art_h = (x1 - x0) * 100, (y1 - y0) * 100
        pos_x = x0 / (1 - (x1 - x0)) * 100
        pos_y = y0 / (1 - (y1 - y0)) * 100
        selectors = [f".ph-{name}"]
        if gen == FALLBACK_GEN:
            selectors.append(f".ph-fb-{group}")
        frame_l, frame_r, art = bucket["defaults"]
        rules.append(
            f"{','.join(selectors)}{{--tpl:url({bucket['uri']});"
            f"--frame-l:#{frame_l};--frame-r:#{frame_r};--art:#{art};"
            f"background-color:#{BORDER_COLORS.get(gen, DEFAULT_BORDER_COLOR)};"
            f"background-size:100% 100%,{art_w:.1f}% {art_h:.1f}%,{frame_size:.1f}% {frame_size:.1f}%;"
            f"background-position:center,{pos_x:.1f}% {pos_y:.1f}%,center}}"
        )
    path.write_text("\n".join(rules) + "\n")
    logger.info("wrote %s (%d bytes)", path, path.stat().st_size)


def main() -> None:
    """Sample, average, and emit the template artifact JSON + CSS."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", type=int, required=True, help="artifact version to emit")
    parser.add_argument("--cache-dir", type=Path, default=None, help="thumbnail cache (default: temp dir)")
    args = parser.parse_args()
    cache_dir = args.cache_dir or Path(tempfile.mkdtemp(prefix="placeholder_thumbs_"))
    cache_dir.mkdir(parents=True, exist_ok=True)

    labels = sample_corpus()
    sums: dict[str, np.ndarray] = {}
    members: dict[str, list[Path]] = {}
    with concurrent.futures.ThreadPoolExecutor(16) as pool:
        paths = pool.map(lambda key: download_thumb(cache_dir, key), sorted(labels))
        for key, path in zip(sorted(labels), paths, strict=True):
            arr = load_arr(path) if path else None
            if arr is None:
                continue
            color, gen = labels[key]
            bucket = f"{gen}-{color}"
            sums[bucket] = arr if bucket not in sums else sums[bucket] + arr
            members.setdefault(bucket, []).append(path)

    buckets: dict[str, dict] = {}
    total_bytes = 0
    for gen in GEN_ORDER:
        for color in COLOR_ORDER:
            name = f"{gen}-{color}"
            paths = members.get(name, [])
            if len(paths) < MIN_BUCKET_SIZE:
                continue
            art_mask, tint_mask, left_mask, right_mask = masks_for(gen)
            lum = (sums[name] / len(paths)) @ LUM_WEIGHTS
            # normalize the tint region to ~235 so multiply(template, tint)
            # reconstructs a measured tint at roughly full strength
            lum = np.clip(lum * (235.0 / max(float(lum[tint_mask].mean()), 1.0)), 0, 255)

            # bucket-default colors: median of member measurements against the template
            samples = [load_arr(p) for p in paths[:: max(1, len(paths) // 60)]]
            measured = np.array(
                [
                    np.concatenate(
                        [
                            solve_tint(card, lum, left_mask),
                            solve_tint(card, lum, right_mask),
                            card[art_mask].mean(axis=0),
                        ]
                    )
                    for card in samples
                    if card is not None
                ]
            )
            med = np.median(measured, axis=0)
            defaults = [hexify(med[0:3]), hexify(med[3:6]), hexify(med[6:9])]

            uri, nbytes = template_data_uri(lum, art_mask)
            total_bytes += nbytes
            buckets[name] = {
                "uri": uri,
                "defaults": defaults,
                "template_lum_b64": base64.b64encode(lum.round().astype(np.uint8).tobytes()).decode(),
                "n": len(paths),
            }
            logger.info("%s n=%d defaults=%s (%d B)", name, len(paths), defaults, nbytes)

    artifact = {
        "version": args.version,
        "tpl_w": TPL_W,
        "tpl_h": TPL_H,
        "border_frac": BORDER_FRAC,
        "art_rects": {gen: list(rect) for gen, rect in ART_RECTS.items()},
        "built_from": f"{len(labels)} artwork-unique printings sampled from the live API",
        "buckets": {
            name: {"template_lum_b64": b["template_lum_b64"], "defaults": b["defaults"], "n": b["n"]} for name, b in buckets.items()
        },
    }
    artifact_path = REPO_ROOT / "scripts" / f"placeholder_templates_v{args.version}.json"
    artifact_path.write_text(json.dumps(artifact, indent=1))
    logger.info("wrote %s (%d buckets, %d B of template webp)", artifact_path, len(buckets), total_bytes)

    emit_css(buckets, REPO_ROOT / "api" / "static" / f"placeholders-v{args.version}.css")


if __name__ == "__main__":
    main()
