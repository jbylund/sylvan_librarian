"""Build the placeholder codebook: stratified k-means over card image thumbnails.

Produces the versioned artifact pair that must always ship together:

- scripts/placeholder_centroids_v{N}.json — centroid vectors + metadata,
  consumed by placeholder_assignment.py (and by copy_images_to_s3.py at import time)
- api/static/placeholders-v{N}.css — the blurred centroids as base64 webp data URIs,
  one class per cluster (.ph-<id>) plus per-color fallback aliases (.ph-fb-<color>)

Sampling pulls artwork-unique printings from the live API across era x color/type
facets, boosted with border:white / border:borderless queries (unique=artwork
otherwise favors black-border printings). Clustering runs per (color-group x
border-class) stratum so that perceptually loud distinctions the raw pixel distance
underweights — border color especially — are structurally enforced. Border class is
read from the outer pixel ring, not metadata. Within each stratum, k is chosen by an
elbow rule. See docs/issues/clustered-placeholder-lqip.md.

Rebuilding shuffles cluster ids: bump --version, regenerate both artifacts, run the
copy_images_to_s3.py assignment backfill, then swap the served CSS.

Usage:
    python scripts/build_placeholder_codebook.py --version 2 [--review-page out.html]

Requires: numpy, pillow, scikit-learn (requirements/placeholders.txt).
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
from sklearn.cluster import KMeans

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SEARCH_URL = "https://sylvan-librarian.com/search"
CDN_URL = "https://d1hot9ps2xugbc.cloudfront.net/img/{set_code}/{collector_number}/1/280.webp"

THUMB_W, THUMB_H = 28, 39  # keeps the 745:1041 card aspect ratio
BORDER_WHITE_MIN, BORDER_BLACK_MAX = 0.70, 0.20
MIN_STRATUM_SIZE = 12  # smaller (color, border) groups fold into (color, black)
ELBOW_IMPROVEMENT = 0.06  # stop adding clusters when inertia improves less than this
MAX_K = 6

ERAS = [
    "year<=1996",
    "year>=1997 year<=2002",
    "year>=2003 year<=2007",
    "year>=2008 year<=2014",
    "year>=2015 year<=2019",
    "year>=2020",
]
GOLD_PAIRS = ["wu", "ub", "br", "rg", "gw", "wb", "ur", "bg", "rw", "gu"]
# (color group, query fragment); assignment priority for overlaps = order here
FACETS = (
    [("land", "t:land"), ("artifact", "t:artifact")]
    + [("gold", f"c:{pair}") for pair in GOLD_PAIRS]
    + [(color, f"c={color}") for color in "wubrg"]
)
COLOR_GROUPS = ["w", "u", "b", "r", "g", "gold", "artifact", "land"]
BORDER_CLASSES = ["black", "white", "art"]


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


def sample_printings() -> dict[tuple[str, str], str]:
    """Return {(set_code, collector_number): color_group} for a diverse sample."""
    jobs: list[tuple[str, str]] = []
    for color_group, fragment in FACETS:
        jobs.extend((color_group, f"{era} {fragment}") for era in ERAS)
        # unique=artwork favors black-border printings; pull the rarer borders explicitly
        jobs.append((color_group, f"border:white {fragment}"))
        jobs.append((color_group, f"border:borderless {fragment}"))

    priority = {group: idx for idx, (group, _) in enumerate(FACETS)}
    labels: dict[tuple[str, str], str] = {}
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        for (color_group, _), cards in zip(jobs, pool.map(lambda j: fetch_query(j[1]), jobs), strict=False):
            for key in cards:
                if key not in labels or priority[color_group] < priority[labels[key]]:
                    labels[key] = color_group
    logger.info("%d queries -> %d unique artworks", len(jobs), len(labels))
    return labels


def download_thumb(cache_dir: Path, key: tuple[str, str]) -> Path | None:
    """Fetch a printing's 280px thumbnail into the cache dir; None on failure."""
    set_code, collector_number = key
    dest = cache_dir / f"{set_code}__{collector_number}.webp"
    if dest.exists():
        return dest
    url = CDN_URL.format(set_code=urllib.parse.quote(set_code), collector_number=urllib.parse.quote(collector_number))
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        dest.write_bytes(response.content)
    except requests.RequestException:
        return None
    return dest


def load_vector(path: Path) -> np.ndarray | None:
    """Decode and downscale one thumbnail to a flat [0, 1] RGB vector; None if unreadable."""
    try:
        with Image.open(path) as image:
            small = image.convert("RGB").resize((THUMB_W, THUMB_H), Image.LANCZOS)
    except OSError:
        return None
    return np.asarray(small, dtype=np.float32).reshape(-1) / 255.0


def border_class(vec: np.ndarray) -> str:
    """Classify the border (white/black/art) from the outer pixel ring's mean luminance."""
    arr = vec.reshape(THUMB_H, THUMB_W, 3)
    ring = np.concatenate([arr[0], arr[-1], arr[1:-1, 0], arr[1:-1, -1]])
    lum = float(ring.mean())
    if lum >= BORDER_WHITE_MIN:
        return "white"
    if lum <= BORDER_BLACK_MAX:
        return "black"
    return "art"


def pick_k(matrix: np.ndarray) -> KMeans:
    """Elbow rule: stop when one more cluster improves inertia by < ELBOW_IMPROVEMENT."""
    hi = min(MAX_K, max(1, len(matrix) // 40))
    previous = KMeans(n_clusters=1, n_init=1, random_state=42).fit(matrix)
    for k in range(2, hi + 1):
        model = KMeans(n_clusters=k, n_init=4, random_state=42).fit(matrix)
        if (previous.inertia_ - model.inertia_) / previous.inertia_ < ELBOW_IMPROVEMENT:
            return previous
        previous = model
    return previous


def cluster_strata(groups: dict[tuple[str, str], list[np.ndarray]]) -> list[dict]:
    """K-means each (color group, border) stratum; returns flat clusters with global ids."""
    clusters: list[dict] = []
    for color_group in COLOR_GROUPS:
        for border in BORDER_CLASSES:
            vectors = groups.get((color_group, border))
            if not vectors:
                continue
            matrix = np.array(vectors)
            model = pick_k(matrix)
            sizes = np.bincount(model.predict(matrix), minlength=len(model.cluster_centers_))
            for centroid, size in sorted(zip(model.cluster_centers_, sizes, strict=False), key=lambda pair: -pair[1]):
                clusters.append(
                    {
                        "id": len(clusters),
                        "stratum": f"{color_group}/{border}",
                        "color_group": color_group,
                        "border": border,
                        "n": int(size),
                        "centroid_u8_b64": base64.b64encode(
                            (centroid.clip(0, 1) * 255).round().astype(np.uint8).tobytes()
                        ).decode(),
                    }
                )
            logger.info("%s/%s n=%d k=%d", color_group, border, len(matrix), len(model.cluster_centers_))
    return clusters


def centroid_data_uri(cluster: dict) -> str:
    """Encode a cluster centroid as a slightly blurred webp data URI."""
    arr = np.frombuffer(base64.b64decode(cluster["centroid_u8_b64"]), dtype=np.uint8)
    image = Image.fromarray(arr.reshape(THUMB_H, THUMB_W, 3))
    # slight pre-blur softens upscale artifacts; the browser's smooth scaling does the rest
    image = image.filter(ImageFilter.GaussianBlur(0.6))
    buffer = io.BytesIO()
    image.save(buffer, "WEBP", quality=40, method=6)
    return "data:image/webp;base64," + base64.b64encode(buffer.getvalue()).decode()


def emit_css(clusters: list[dict], fallbacks: dict[str, int], path: Path) -> None:
    """Write the placeholder stylesheet: one class per cluster plus ph-fb-* fallback aliases."""
    rules = ['[class^="ph-"],[class*=" ph-"]{background-size:cover;background-repeat:no-repeat;background-position:center}']
    for cluster in clusters:
        selectors = [f".ph-{cluster['id']}"]
        selectors.extend(f".ph-fb-{color}" for color, fid in fallbacks.items() if fid == cluster["id"])
        rules.append(f"{','.join(selectors)}{{background-image:url({centroid_data_uri(cluster)})}}")
    path.write_text("\n".join(rules) + "\n")
    logger.info("wrote %s (%d bytes)", path, path.stat().st_size)


def main() -> None:
    """Sample, cluster, and emit the codebook JSON + CSS (+ optional review page)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", type=int, required=True, help="codebook version to emit")
    parser.add_argument("--cache-dir", type=Path, default=None, help="thumbnail cache (default: temp dir)")
    parser.add_argument("--review-page", type=Path, default=None, help="optional HTML page of centroids to eyeball")
    args = parser.parse_args()
    cache_dir = args.cache_dir or Path(tempfile.mkdtemp(prefix="placeholder_thumbs_"))
    cache_dir.mkdir(parents=True, exist_ok=True)

    labels = sample_printings()
    groups: dict[tuple[str, str], list[np.ndarray]] = {}
    with concurrent.futures.ThreadPoolExecutor(16) as pool:
        paths = pool.map(lambda key: download_thumb(cache_dir, key), sorted(labels))
        for key, path in zip(sorted(labels), paths, strict=False):
            vec = load_vector(path) if path else None
            if vec is not None:
                groups.setdefault((labels[key], border_class(vec)), []).append(vec)
    for group_key in [g for g, members in groups.items() if len(members) < MIN_STRATUM_SIZE]:
        groups.setdefault((group_key[0], "black"), []).extend(groups.pop(group_key))

    clusters = cluster_strata(groups)
    fallbacks = {}
    for cluster in clusters:  # largest black-border cluster per color group (already size-sorted)
        if cluster["border"] == "black" and cluster["color_group"] not in fallbacks:
            fallbacks[cluster["color_group"]] = cluster["id"]

    codebook = {
        "version": args.version,
        "thumb_w": THUMB_W,
        "thumb_h": THUMB_H,
        "border_white_min": BORDER_WHITE_MIN,
        "border_black_max": BORDER_BLACK_MAX,
        "built_from": f"{len(labels)} artwork-unique printings sampled from the live API",
        "fallbacks": fallbacks,
        "clusters": clusters,
    }
    codebook_path = REPO_ROOT / "scripts" / f"placeholder_centroids_v{args.version}.json"
    codebook_path.write_text(json.dumps(codebook, indent=1))
    logger.info("wrote %s (%d clusters)", codebook_path, len(clusters))

    emit_css(clusters, fallbacks, REPO_ROOT / "api" / "static" / f"placeholders-v{args.version}.css")

    if args.review_page:
        rows = "".join(
            f'<div><img src="{centroid_data_uri(c)}" style="width:140px;aspect-ratio:745/1041">'
            f"<br>{c['id']} {c['stratum']} n={c['n']}</div>"
            for c in clusters
        )
        args.review_page.write_text(
            "<!doctype html><meta charset=utf-8><body style='display:flex;flex-wrap:wrap;"
            f"gap:12px;background:#222;color:#ccc;font-family:sans-serif'>{rows}"
        )
        logger.info("wrote %s", args.review_page)


if __name__ == "__main__":
    main()
