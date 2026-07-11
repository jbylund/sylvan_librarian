"""Assign card images to placeholder clusters.

The codebook (scripts/placeholder_centroids_v1.json) is built offline by
build_placeholder_codebook.py. Assignment is border-gated: the card's border class
(white / black / borderless-art) is read from the outer pixel ring of a downscaled
thumbnail, then the nearest centroid *within that border class* wins. The gate is
required — border pixels are a small share of the total distance, so ungated
nearest-centroid can put a white-border card on a black-border placeholder.

See docs/issues/clustered-placeholder-lqip.md for the design.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image

if TYPE_CHECKING:
    from os import PathLike

DEFAULT_CODEBOOK_PATH = Path(__file__).resolve().parent / "placeholder_centroids_v1.json"


class PlaceholderCodebook:
    """Frozen centroid set plus the logic to assign an image to its nearest cluster."""

    def __init__(self, path: PathLike[str] | str = DEFAULT_CODEBOOK_PATH) -> None:
        """Load a codebook artifact produced by build_placeholder_codebook.py."""
        raw = json.loads(Path(path).read_text())
        self.version: int = raw["version"]
        self.thumb_w: int = raw["thumb_w"]
        self.thumb_h: int = raw["thumb_h"]
        self.border_white_min: float = raw["border_white_min"]
        self.border_black_max: float = raw["border_black_max"]
        self.fallbacks: dict[str, int] = raw["fallbacks"]
        self.cluster_ids = np.array([c["id"] for c in raw["clusters"]], dtype=np.int64)
        self.borders: list[str] = [c["border"] for c in raw["clusters"]]
        self.centroids = np.stack(
            [
                np.frombuffer(base64.b64decode(c["centroid_u8_b64"]), dtype=np.uint8).astype(np.float32) / 255.0
                for c in raw["clusters"]
            ]
        )

    def thumb_vector(self, image: Image.Image) -> np.ndarray:
        """Downscale to the codebook's thumbnail size and flatten to [0, 1] RGB."""
        small = image.convert("RGB").resize((self.thumb_w, self.thumb_h), Image.LANCZOS)
        return np.asarray(small, dtype=np.float32).reshape(-1) / 255.0

    def border_class(self, vec: np.ndarray) -> str:
        """Classify the border from the mean luminance of the outer pixel ring."""
        arr = vec.reshape(self.thumb_h, self.thumb_w, 3)
        ring = np.concatenate([arr[0], arr[-1], arr[1:-1, 0], arr[1:-1, -1]])
        lum = float(ring.mean())
        if lum >= self.border_white_min:
            return "white"
        if lum <= self.border_black_max:
            return "black"
        return "art"

    def assign_vector(self, vec: np.ndarray) -> int:
        """Nearest centroid within the vector's border class; returns the cluster id."""
        border = self.border_class(vec)
        mask = np.array([b == border for b in self.borders])
        distances = ((self.centroids[mask] - vec) ** 2).sum(axis=1)
        return int(self.cluster_ids[mask][int(distances.argmin())])

    def assign_image(self, image: Image.Image) -> int:
        """Assign a full-size card image (any size) to its placeholder cluster."""
        return self.assign_vector(self.thumb_vector(image))

    def assign_file(self, path: PathLike[str] | str) -> int:
        """Assign an image file on disk to its placeholder cluster."""
        with Image.open(path) as image:
            return self.assign_image(image)

    def assign_bytes(self, data: bytes) -> int:
        """Assign an in-memory encoded image (e.g. a webp fetched from S3) to its cluster."""
        with Image.open(io.BytesIO(data)) as image:
            return self.assign_image(image)
