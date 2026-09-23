"""Locally generated plates, for dry runs with no network egress.

These are abstract colour fields, not photographs, and every candidate is
flagged ``synthetic=True``. :mod:`echoes.pipeline.qc` refuses to pass a video
built from synthetic visuals unless ``DRY_RUN`` is true, so this can never
quietly furnish a real upload.

They exist to make the render stage testable at full scale: the encoder does
not care whether it is panning across a Roman mosaic or a gradient, so
timings, concatenation, file sizes and audio sync are all exercised honestly.
"""

from __future__ import annotations

import colorsys
import hashlib
from pathlib import Path

from .base import ImageCandidate


class SyntheticImageProvider:
    name = "synthetic"

    def __init__(self, out_dir: Path, width: int = 2304, height: int = 1296) -> None:
        self._dir = Path(out_dir)
        self._w = width
        self._h = height

    def available(self) -> bool:
        try:
            import PIL  # noqa: F401
        except ImportError:
            return False
        return True

    def search(self, query: str, *, limit: int = 8) -> list[ImageCandidate]:
        return [self._make(f"{query}#{i}", i) for i in range(limit)]

    def _make(self, key: str, index: int) -> ImageCandidate:
        from PIL import Image, ImageDraw

        digest = hashlib.sha256(key.encode()).hexdigest()
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"synthetic-{digest[:16]}.jpg"

        if not path.exists():
            hue = int(digest[:2], 16) / 255.0
            image = Image.new("RGB", (self._w, self._h))
            draw = ImageDraw.Draw(image)
            for y in range(0, self._h, 6):
                t = y / self._h
                r, g, b = colorsys.hsv_to_rgb((hue + t * 0.12) % 1.0, 0.28, 0.22 + t * 0.5)
                draw.rectangle([0, y, self._w, y + 6],
                               fill=(int(r * 255), int(g * 255), int(b * 255)))
            for i in range(0, 24):
                seed = int(digest[i * 2 % 60: i * 2 % 60 + 2], 16)
                x = (seed * 97) % self._w
                y = (seed * 53) % self._h
                size = 60 + (seed % 200)
                r, g, b = colorsys.hsv_to_rgb((hue + 0.5) % 1.0, 0.2, 0.35)
                draw.ellipse([x, y, x + size, y + size],
                             outline=(int(r * 255), int(g * 255), int(b * 255)), width=3)
            image.save(path, quality=88)

        return ImageCandidate(
            url=path.as_uri(),
            title=f"Synthetic plate {index}",
            provider=self.name,
            licence="CC0 (generated locally)",
            attribution=None,
            creator="Echoes of History (generated)",
            source_page=None,
            width=self._w,
            height=self._h,
            synthetic=True,
            extra={"local_path": str(path)},
        )
