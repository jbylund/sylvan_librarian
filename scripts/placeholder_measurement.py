"""Measure card image placeholder data: bucket + three tint colors per printing.

The placeholder scheme (docs/issues/clustered-placeholder-lqip.md): a small vocabulary
of shared grayscale frame *templates* — one per (frame generation x color group) bucket,
built offline by build_placeholder_templates.py — plus three measured colors per
printing (frame tint left/right, art color) that the client composites in CSS.

The bucket is a pure function of card metadata (Scryfall frame / border_color /
full_art / colors / type line), so bucket identifiers are stable by construction.
Only the three colors are measured from pixels. The frame tint is the least-squares
solve of `template x tint ~= card` over the frame region (the card "divided by" the
template, so the template's own shading doesn't bias the tint), measured separately
over the left and right sides with the transition band excluded — two-color frames
(dual lands, hybrid) get a gradient; mono cards measure the same color twice.

The stored/served value is a single string: "<bucket> <frameL> <frameR> <art>"
with bare lowercase hex colors, e.g. "modern-r 8a3b2f 6a4a3f 334455".
"""

from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image

if TYPE_CHECKING:
    from os import PathLike

DEFAULT_TEMPLATES_PATH = Path(__file__).resolve().parent / "placeholder_templates_v1.json"

# Matches the stored value format; renderers apply the same shape check.
PLACEHOLDER_VALUE_RE = re.compile(r"^([a-z0-9-]+) ([0-9a-f]{6}) ([0-9a-f]{6}) ([0-9a-f]{6})$")

_FRAME_GROUPS = {"1993": "old", "1997": "old", "2003": "modern"}


def color_group(type_line: str | None, colors: list[str] | None) -> str:
    """Color group for bucketing and fallback classes: land/artifact/gold/w/u/b/r/g.

    Type checks precede color (artifact lands read as lands); colorless non-artifact
    cards (devoid, Eldrazi) use the artifact group — the gray frame is the closest look.
    """
    type_line = type_line or ""
    if "Land" in type_line:
        return "land"
    if "Artifact" in type_line:
        return "artifact"
    colors = colors or []
    if len(colors) > 1:
        return "gold"
    if len(colors) == 1:
        return colors[0].lower()
    return "artifact"


def frame_group(frame: str | None, border_color: str | None, full_art: bool) -> str:
    """Frame-generation group: fullart / old-wb / old / modern / m15.

    Driven by Scryfall's per-printing `frame` field — never release year (retro-frame
    reprints are new releases with old frames). White borders get their own group
    regardless of frame year; their template carries the light border.
    """
    if full_art or border_color == "borderless":
        return "fullart"
    if border_color == "white":
        return "old-wb"
    return _FRAME_GROUPS.get(frame or "", "m15")


def bucket_for(meta: dict) -> str:
    """Placeholder bucket for a card's metadata dict.

    Expects keys: frame, border_color, full_art, colors, type_line (Scryfall blob
    field names; missing keys degrade to the most common groups).
    """
    gen = frame_group(meta.get("frame"), meta.get("border_color"), bool(meta.get("full_art")))
    return f"{gen}-{color_group(meta.get('type_line'), meta.get('colors'))}"


class PlaceholderTemplates:
    """The committed template artifact plus the color-measurement logic."""

    def __init__(self, path: PathLike[str] | str = DEFAULT_TEMPLATES_PATH) -> None:
        """Load a template artifact produced by build_placeholder_templates.py."""
        raw = json.loads(Path(path).read_text())
        self.version: int = raw["version"]
        self.tpl_w: int = raw["tpl_w"]
        self.tpl_h: int = raw["tpl_h"]
        self.border_frac: float = raw["border_frac"]
        self.art_rects: dict[str, list[float]] = raw["art_rects"]
        self.templates: dict[str, np.ndarray] = {
            name: np.frombuffer(base64.b64decode(b["template_lum_b64"]), dtype=np.uint8)
            .reshape(self.tpl_h, self.tpl_w)
            .astype(np.float32)
            for name, b in raw["buckets"].items()
        }
        self._mask_cache: dict[str, tuple] = {}

    def _masks(self, gen: str) -> tuple:
        """(art_mask, left_mask, right_mask) for a frame generation's art rect."""
        if gen not in self._mask_cache:
            x0, y0, x1, y1 = self.art_rects[gen]
            art = np.zeros((self.tpl_h, self.tpl_w), dtype=bool)
            art[round(y0 * self.tpl_h) : round(y1 * self.tpl_h), round(x0 * self.tpl_w) : round(x1 * self.tpl_w)] = True
            inner = np.zeros((self.tpl_h, self.tpl_w), dtype=bool)
            bx, by = round(self.border_frac * self.tpl_w), round(self.border_frac * self.tpl_h)
            inner[by:-by, bx:-bx] = True
            tint = inner & ~art
            cols = np.arange(self.tpl_w)[None, :].repeat(self.tpl_h, axis=0)
            self._mask_cache[gen] = (art, tint & (cols < 0.42 * self.tpl_w), tint & (cols > 0.58 * self.tpl_w))
        return self._mask_cache[gen]

    @staticmethod
    def _solve_tint(card: np.ndarray, template_lum: np.ndarray, mask: np.ndarray) -> str:
        """Least-squares tint so that (template/255) * tint ~= card over the mask."""
        t = template_lum[mask] / 255.0
        c = card[mask]
        tint = (t[:, None] * c).sum(axis=0) / max(float((t * t).sum()), 1e-6)
        r, g, b = np.clip(tint, 0, 255).round().astype(int)
        return f"{r:02x}{g:02x}{b:02x}"

    def _art_color(self, card: np.ndarray, gen: str) -> str:
        """Mean color of the art window, inset 12% to avoid frame bleed."""
        x0, y0, x1, y1 = self.art_rects[gen]
        dx, dy = (x1 - x0) * 0.12, (y1 - y0) * 0.12
        region = card[
            round((y0 + dy) * self.tpl_h) : round((y1 - dy) * self.tpl_h),
            round((x0 + dx) * self.tpl_w) : round((x1 - dx) * self.tpl_w),
        ]
        r, g, b = region.reshape(-1, 3).mean(axis=0).round().astype(int)
        return f"{r:02x}{g:02x}{b:02x}"

    def measure_image(self, image: Image.Image, bucket: str) -> str | None:
        """The stored placeholder value for a card image, or None for unknown buckets."""
        if bucket not in self.templates:
            return None
        gen = bucket.rsplit("-", 1)[0]
        card = np.asarray(image.convert("RGB").resize((self.tpl_w, self.tpl_h), Image.LANCZOS), dtype=np.float32)
        template_lum = self.templates[bucket]
        _, left_mask, right_mask = self._masks(gen)
        frame_left = self._solve_tint(card, template_lum, left_mask)
        frame_right = self._solve_tint(card, template_lum, right_mask)
        return f"{bucket} {frame_left} {frame_right} {self._art_color(card, gen)}"

    def measure_file(self, path: PathLike[str] | str, meta: dict) -> str | None:
        """Measure an image file using the bucket derived from the card's metadata."""
        with Image.open(path) as image:
            return self.measure_image(image, bucket_for(meta))

    def measure_bytes(self, data: bytes, meta: dict) -> str | None:
        """Measure an in-memory encoded image (e.g. a webp fetched from S3)."""
        with Image.open(io.BytesIO(data)) as image:
            return self.measure_image(image, bucket_for(meta))
