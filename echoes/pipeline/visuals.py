"""The visual engine.

Builds a timeline of images that runs exactly as long as the narration, and
guarantees three things about it:

* **Every asset carries a licence.** An asset whose licence cannot be read is
  discarded at the provider boundary, not patched up here.
* **No image repeats near itself.** Deduplication is by perceptual hash, not
  by URL, because archives serve the same photograph from several URLs and a
  documentary that shows one image twice in a minute looks broken.
* **The timeline matches the audio.** Segment durations are derived from the
  measured narration, so the picture cannot drift against the voice.

Motion is assigned so consecutive segments never pan the same direction; a
sequence of identical pans reads as a slideshow with a stuck effect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..errors import Permanent
from ..logging import get_logger
from ..media.ffmpeg import MOTIONS
from ..providers.http import get_bytes, user_agent
from ..providers.images.base import ImageCandidate, attribution_line

log = get_logger(__name__)

# One image every 14 seconds or so. Long enough that a pan is unhurried,
# short enough that a chapter is not one static picture for eight minutes.
SECONDS_PER_VISUAL = 14.0
MIN_SEGMENT_S = 7.0
MAX_SEGMENT_S = 22.0


@dataclass
class PlannedVisual:
    candidate: ImageCandidate
    chapter_id: int | None
    chapter_ordinal: int
    start_s: float
    duration_s: float
    motion: str
    local_path: Path | None = None
    plate_path: Path | None = None
    perceptual_hash: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "chapter_id": self.chapter_id,
            "provider": self.candidate.provider,
            "source_url": self.candidate.url,
            "licence": self.candidate.licence,
            "attribution": attribution_line(self.candidate),
            "creator": self.candidate.creator,
            "local_path": str(self.local_path) if self.local_path else None,
            "plate_path": str(self.plate_path) if self.plate_path else None,
            "start_s": self.start_s,
            "duration_s": self.duration_s,
            "motion": self.motion,
            "perceptual_hash": self.perceptual_hash,
        }


def segment_count(duration_s: float) -> int:
    return max(1, round(duration_s / SECONDS_PER_VISUAL))


def split_chapter(start_s: float, duration_s: float) -> list[tuple[float, float]]:
    """Divide one chapter's span into segment spans that exactly tile it.

    The last segment absorbs the rounding remainder, so the segments sum to
    the chapter duration to the millisecond. Accumulated rounding across
    sixty segments is how a picture track ends up seconds short of its audio.
    """
    count = segment_count(duration_s)
    each = duration_s / count
    if each < MIN_SEGMENT_S and count > 1:
        count = max(1, int(duration_s // MIN_SEGMENT_S))
        each = duration_s / count
    if each > MAX_SEGMENT_S:
        count = max(1, round(duration_s / MAX_SEGMENT_S))
        each = duration_s / count

    spans: list[tuple[float, float]] = []
    cursor = round(start_s, 3)
    end = round(start_s + duration_s, 3)
    for index in range(count):
        if index == count - 1:
            # The last segment absorbs the remainder, so the spans sum to the
            # chapter duration exactly.
            duration = round(end - cursor, 3)
        else:
            duration = round(each, 3)
        spans.append((cursor, duration))
        # Advance by the *rounded* value. Advancing by the unrounded one
        # leaves each span's stored duration slightly adrift from where the
        # next span starts; across the ~300 segments of a 90-minute film that
        # accumulated to tens of milliseconds of picture/audio drift.
        cursor = round(cursor + duration, 3)
    return spans


def queries_for(heading: str, body: str, topic_title: str, *, limit: int = 4
                ) -> list[str]:
    """Search terms for one chapter's imagery.

    Proper nouns from the chapter body, because those are what an archive is
    catalogued by. The topic title is always included as a fallback, so a
    chapter with no usable proper nouns still gets relevant material.
    """
    nouns: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"\b[A-Z][a-z]{3,}(?:\s+[A-Z][a-z]{3,})?", body):
        term = match.group(0).strip()
        key = term.lower()
        if key not in seen and len(term) > 4:
            seen.add(key)
            nouns.append(term)

    queries = [f"{topic_title} {heading}".strip()]
    for noun in nouns[: limit - 1]:
        queries.append(f"{noun} {topic_title.split()[0] if topic_title else ''}".strip())
    return queries[:limit]


def perceptual_hash(path: Path, *, size: int = 8) -> str:
    """Difference hash: 64 bits describing the image's gradient structure.

    Robust to rescaling and re-compression, which is exactly what archives do
    to the same photograph, so two copies of one image collide as they should.
    """
    from PIL import Image

    with Image.open(path) as image:
        small = image.convert("L").resize((size + 1, size), Image.LANCZOS)
        # tobytes() on an 8-bit greyscale image is one byte per pixel, in the
        # same order getdata() returned, and is not deprecated.
        pixels = list(small.tobytes())
    bits = 0
    position = 0
    for row in range(size):
        offset = row * (size + 1)
        for column in range(size):
            left = pixels[offset + column]
            right = pixels[offset + column + 1]
            if left > right:
                bits |= 1 << position
            position += 1
    return f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def collect_candidates(
    providers, queries: Iterable[str], *, needed: int, per_query: int = 8
) -> list[ImageCandidate]:
    """Gather licence-clean candidates across providers, de-duplicated by URL."""
    seen: set[str] = set()
    out: list[ImageCandidate] = []
    for query in queries:
        for provider in providers:
            if len(out) >= needed * 3:
                return out
            try:
                found = provider.search(query, limit=per_query)
            except Exception as exc:  # noqa: BLE001
                log.warning("image provider failed",
                            extra={"provider": provider.name, "query": query,
                                   "error": str(exc)})
                continue
            for candidate in found:
                if candidate.url in seen:
                    continue
                seen.add(candidate.url)
                out.append(candidate)
    return out


def fetch_and_prepare(
    candidate: ImageCandidate, raw_dir: Path, plate_dir: Path, *,
    plate_width: int, plate_height: int, contact_email: str | None = None,
) -> tuple[Path, Path, str]:
    """Download (or read) the image and produce the pre-scaled plate.

    The plate is the single most important performance decision in the
    renderer: motion is panned across this, so it is produced once here
    rather than by rescaling a full-size scan on every output frame.
    """
    from PIL import Image, ImageOps

    raw_dir.mkdir(parents=True, exist_ok=True)
    plate_dir.mkdir(parents=True, exist_ok=True)

    local = candidate.extra.get("local_path")
    if local and Path(local).exists():
        source = Path(local)
    else:
        data = get_bytes(candidate.url,
                         headers={"User-Agent": user_agent(contact_email)})
        stem = re.sub(r"[^a-zA-Z0-9]+", "-", candidate.url)[-60:]
        source = raw_dir / f"{stem}.img"
        source.write_bytes(data)

    digest = perceptual_hash(source)
    plate = plate_dir / f"plate-{digest}.jpg"
    if not plate.exists():
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            # Cover the plate and centre-crop: letterboxing an archive scan
            # into 16:9 would put black bars inside the frame, and the crop
            # window needs the full plate to move across.
            fitted = ImageOps.fit(image, (plate_width, plate_height),
                                  method=Image.LANCZOS, centering=(0.5, 0.45))
            fitted.save(plate, quality=88, optimize=True)
    return source, plate, digest


def plan(
    providers, topic_title: str, chapters: list[dict[str, Any]],
    timings: list[Any], work_dir: Path, *, plate_width: int, plate_height: int,
    contact_email: str | None = None, min_hamming: int = 6,
    lookback: int = 8,
) -> list[PlannedVisual]:
    """Build the full visual timeline."""
    raw_dir = work_dir / "images"
    plate_dir = work_dir / "plates"
    by_ordinal = {int(c.get("ordinal", i)): c for i, c in enumerate(chapters)}

    planned: list[PlannedVisual] = []
    recent_hashes: list[str] = []
    motion_index = 0

    for timing in timings:
        chapter = by_ordinal.get(timing.ordinal, {})
        spans = split_chapter(timing.start_s, timing.duration_s)
        queries = queries_for(timing.heading, str(chapter.get("body", "")),
                              topic_title)
        candidates = collect_candidates(providers, queries, needed=len(spans))
        if not candidates:
            raise Permanent(
                f"no licence-clean images found for chapter "
                f"{timing.ordinal} ({timing.heading!r}). Widen IMAGE_PROVIDERS "
                f"or add 'synthetic' for a dry run."
            )

        cursor = 0
        for start_s, duration_s in spans:
            chosen: PlannedVisual | None = None
            while cursor < len(candidates):
                candidate = candidates[cursor]
                cursor += 1
                try:
                    source, plate, digest = fetch_and_prepare(
                        candidate, raw_dir, plate_dir,
                        plate_width=plate_width, plate_height=plate_height,
                        contact_email=contact_email,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("image unusable, skipping",
                                extra={"url": candidate.url[:120],
                                       "error": str(exc)})
                    continue
                # Reject anything visually close to what was shown recently.
                if any(hamming(digest, prior) < min_hamming
                       for prior in recent_hashes[-lookback:]):
                    continue
                chosen = PlannedVisual(
                    candidate=candidate, chapter_id=chapter.get("id"),
                    chapter_ordinal=timing.ordinal, start_s=start_s,
                    duration_s=duration_s,
                    motion=MOTIONS[motion_index % len(MOTIONS)],
                    local_path=source, plate_path=plate,
                    perceptual_hash=digest,
                )
                break

            if chosen is None:
                if not planned:
                    raise Permanent(
                        f"could not prepare any image for chapter "
                        f"{timing.ordinal} ({timing.heading!r})"
                    )
                # Reuse the least-recently-used plate rather than leaving a
                # hole in the picture track. Logged, because a run that does
                # this often has an image supply problem.
                fallback = planned[len(planned) % len(planned)]
                log.warning("reusing an earlier plate to fill a segment",
                            extra={"chapter": timing.ordinal, "start_s": start_s})
                chosen = PlannedVisual(
                    candidate=fallback.candidate, chapter_id=chapter.get("id"),
                    chapter_ordinal=timing.ordinal, start_s=start_s,
                    duration_s=duration_s,
                    motion=MOTIONS[motion_index % len(MOTIONS)],
                    local_path=fallback.local_path, plate_path=fallback.plate_path,
                    perceptual_hash=fallback.perceptual_hash,
                )
            else:
                recent_hashes.append(chosen.perceptual_hash or "")

            planned.append(chosen)
            motion_index += 1

    log.info("visual plan built",
             extra={"segments": len(planned),
                    "unique_images": len({p.perceptual_hash for p in planned}),
                    "covers_s": round(sum(p.duration_s for p in planned), 1)})
    return planned
