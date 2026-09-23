"""Thumbnail generation.

Generates several concepts and picks between them on a measurable criterion
rather than an aesthetic one: whether the title text will actually be legible
at the size a phone renders it. Most thumbnails fail not because they are
ugly but because white text landed on a bright patch of the image.

So each concept is scored on the measured contrast between the text and the
pixels directly behind it, and the winner is the one that reads. The choice
is deterministic, which matters because an unattended system must not produce
a different thumbnail each time it retries.

No misleading imagery: the plate comes from the documentary's own visuals,
so the thumbnail shows something the viewer will actually see.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..errors import Permanent
from ..logging import get_logger

log = get_logger(__name__)

WIDTH, HEIGHT = 1280, 720

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def find_font() -> str | None:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return path
    found = sorted(Path("/usr/share/fonts").rglob("*Bold.ttf"))
    return str(found[0]) if found else None


@dataclass
class Concept:
    name: str
    path: Path
    contrast: float
    # Where the text sits, as a fraction of height. Recorded so a review can
    # see why a concept won.
    band_top: float
    band_height: float


MAX_LINES = 3


def _wrap(text: str, draw, font, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines[:MAX_LINES]


def _luminance_of_band(image, top: int, height: int) -> float:
    """Mean perceived brightness of the strip the text will sit on."""
    band = image.crop((0, top, image.width, min(top + height, image.height)))
    grey = band.convert("L")
    pixels = list(grey.getdata())
    return sum(pixels) / max(len(pixels), 1) / 255.0


# Words a title must never end on. Cutting "...Laurion and Athenian Power"
# to fit produced "LAURION AND ATHENIAN", which reads as an error rather than
# an abbreviation.
_DANGLING = frozenset(
    "and or of the a an to in on for with at by from as its their".split()
)


def _trim_dangling(text: str) -> str:
    words = text.split()
    while words and words[-1].lower().strip(",;:") in _DANGLING:
        words.pop()
    return " ".join(words).rstrip(" ,;:-")


def title_for_thumbnail(title: str, limit: int = 56) -> str:
    """Shorten a title to what fits, without leaving it hanging.

    Three rules, in order of preference:

    1. If it fits, use it whole.
    2. If it has a natural break -- a colon or a dash -- keep the part before
       it, which is almost always the subject.
    3. Otherwise cut at a word boundary and drop any trailing connective, so
       the result reads as a short title rather than a severed sentence.
    """
    cleaned = re.sub(r"\s*\|.*$", "", title).strip()
    cleaned = re.sub(r"\s*[:\u2013\u2014-]\s*A Full Documentary.*$", "",
                     cleaned, flags=re.IGNORECASE).strip()
    if len(cleaned) <= limit:
        return cleaned

    head = re.split(r"\s*[:\u2013\u2014]\s*", cleaned)[0].strip()
    if head and len(head) <= limit:
        return _trim_dangling(head)

    out = ""
    for word in cleaned.split():
        if len(f"{out} {word}".strip()) > limit:
            break
        out = f"{out} {word}".strip()
    return _trim_dangling(out) or cleaned[:limit]


def _render_concept(
    plate: Path, out_path: Path, text: str, *, name: str, band_top_frac: float,
    band_height_frac: float, font_path: str, scrim: float,
) -> Concept:
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

    with Image.open(plate) as source:
        base = ImageOps.fit(source.convert("RGB"), (WIDTH, HEIGHT),
                            method=Image.LANCZOS, centering=(0.5, 0.4))

    # A gentle contrast lift and a slight desaturation give a consistent
    # channel look and stop archive scans reading as washed out.
    base = ImageEnhance.Color(base).enhance(0.82)
    base = ImageEnhance.Contrast(base).enhance(1.12)

    measure = ImageDraw.Draw(base)
    margin = 72
    max_width = WIDTH - margin * 2

    # Lay the type out FIRST, then size the scrim to what was actually laid
    # out. Fixing the scrim height in advance left a three-line title
    # spilling above its darkened band, where it sat on bare photograph.
    size = 74
    font = ImageFont.truetype(font_path, size)
    words = len(text.split())
    lines = _wrap(text.upper(), measure, font, max_width)
    while size > 38 and sum(len(l.split()) for l in lines) < words:
        size -= 5
        font = ImageFont.truetype(font_path, size)
        lines = _wrap(text.upper(), measure, font, max_width)

    line_height = int(size * 1.18)
    block_height = line_height * len(lines)

    requested_top = int(HEIGHT * band_top_frac)
    requested_height = int(HEIGHT * band_height_frac)
    # The band must cover the text with breathing room, wherever the layout
    # asked for it to sit, and must stay inside the frame.
    band_height = max(requested_height, block_height + int(size * 0.7))
    band_top = max(0, min(requested_top, HEIGHT - band_height))
    if band_top + band_height > HEIGHT:
        band_height = HEIGHT - band_top

    region = base.crop((0, band_top, WIDTH, band_top + band_height))
    region = region.filter(ImageFilter.GaussianBlur(14))
    region = ImageEnhance.Brightness(region).enhance(scrim)
    base.paste(region, (0, band_top))

    draw = ImageDraw.Draw(base)
    y = band_top + (band_height - block_height) // 2
    for line in lines:
        width = draw.textlength(line, font=font)
        x = (WIDTH - width) / 2
        # Outline first, then the fill: legible even where the scrim is thin.
        for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (-2, -2), (2, 2)):
            draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0))
        draw.text((x, y), line, font=font, fill=(245, 241, 232))
        y += line_height

    # A thin rule under the block: a cheap, consistent channel signature.
    rule_y = min(y + 6, HEIGHT - 12)
    draw.rectangle([WIDTH // 2 - 90, rule_y, WIDTH // 2 + 90, rule_y + 3],
                   fill=(196, 160, 96))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    base.save(out_path, quality=90, optimize=True)

    luminance = _luminance_of_band(base, band_top, block_height or band_height)
    # Text is near-white, so contrast against the band is 1 - its brightness.
    return Concept(name, out_path, 1.0 - luminance, band_top / HEIGHT,
                   band_height / HEIGHT)


def generate(
    plates: Sequence[Path], title: str, out_dir: Path, *, keep_all: bool = False
) -> Concept:
    """Render concepts and return the most legible one.

    Ties are broken by concept order, so the same inputs always produce the
    same thumbnail. An unattended retry must not quietly change the artwork.
    """
    usable = [Path(p) for p in plates if Path(p).exists()]
    if not usable:
        raise Permanent("no plates available to build a thumbnail from")

    font_path = find_font()
    if not font_path:
        raise Permanent(
            "no TrueType font found for thumbnail text; install "
            "fonts-liberation or fonts-dejavu in the image"
        )

    text = title_for_thumbnail(title)
    # Three positions across two plates: a caption band, a lower third, and a
    # centred block. One of them nearly always lands on a dark region.
    layouts = (
        ("lower-third", 0.58, 0.30, 0.42),
        ("caption-top", 0.06, 0.28, 0.40),
        ("centre-block", 0.34, 0.32, 0.38),
    )
    plate_choices = usable[: min(2, len(usable))]

    concepts: list[Concept] = []
    for plate_index, plate in enumerate(plate_choices):
        for name, top, height, scrim in layouts:
            path = out_dir / f"thumb-{plate_index}-{name}.jpg"
            try:
                concepts.append(_render_concept(
                    plate, path, text, name=f"{name}-{plate_index}",
                    band_top_frac=top, band_height_frac=height,
                    font_path=font_path, scrim=scrim,
                ))
            except Exception as exc:  # noqa: BLE001
                log.warning("thumbnail concept failed",
                            extra={"concept": name, "error": str(exc)})

    if not concepts:
        raise Permanent("every thumbnail concept failed to render")

    best = max(concepts, key=lambda c: (round(c.contrast, 3), -concepts.index(c)))
    final = out_dir / "thumbnail.jpg"
    final.write_bytes(best.path.read_bytes())

    if not keep_all:
        for concept in concepts:
            if concept.path != best.path:
                concept.path.unlink(missing_ok=True)

    log.info("thumbnail selected",
             extra={"concept": best.name, "contrast": round(best.contrast, 3),
                    "considered": len(concepts)})
    return Concept(best.name, final, best.contrast, best.band_top, best.band_height)
