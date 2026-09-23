"""Narration.

The failure this module is built around: a 90-minute documentary is roughly
120 synthesis calls, and losing the whole production because call 97 failed
is unacceptable. So every chunk is a database row. A resumed run synthesises
only the chunks that are missing, and a chunk that fails is retried on its
own rather than taking the documentary with it.

Chapter boundaries are chunk boundaries. That is not for tidiness: the
chapter timings that become YouTube chapter markers are measured from the
rendered audio, so a chunk that straddles two chapters would make those
markers unattributable.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import Permanent, Retryable
from ..logging import get_logger
from ..media import ffmpeg
from ..providers.tts.base import Chunk, split_into_chunks

log = get_logger(__name__)


@dataclass
class ChapterTiming:
    chapter_id: int | None
    ordinal: int
    heading: str
    start_s: float
    duration_s: float


@dataclass
class NarrationResult:
    audio_path: Path
    duration_s: float
    timings: list[ChapterTiming]
    chunks_synthesised: int
    chunks_reused: int
    measured_wpm: float


def plan_chunks(chapters: list[dict[str, Any]], max_chars: int) -> list[Chunk]:
    """Split every chapter into synthesis-sized pieces, in order."""
    out: list[Chunk] = []
    ordinal = 0
    for chapter in chapters:
        body = str(chapter.get("body") or "").strip()
        if not body:
            continue
        for piece in split_into_chunks(body, max_chars):
            out.append(Chunk(ordinal=ordinal, text=piece,
                             chapter_id=chapter.get("id"),
                             chapter_ordinal=int(chapter.get("ordinal", 0))))
            ordinal += 1
    if not out:
        raise Permanent("the script produced no narratable text")
    return out


def chunk_hash(text: str, voice: str, length_scale: float) -> str:
    """Identity of a synthesised chunk.

    The voice and the rate are part of it, so changing either invalidates the
    cached audio instead of stitching two different voices into one file.
    """
    key = f"{voice}|{length_scale:.4f}|{text}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def synthesise_chunk(
    tts, chunk: Chunk, out_path: Path, *, attempts: int = 3,
    sleep=time.sleep,
) -> float:
    """Synthesise one chunk, retrying on provider failure.

    Retrying here is safe in a way that retrying a write is not: synthesis
    has no external side effect, so a repeat costs only time.
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            duration = tts.synthesize(chunk.text, out_path)
            if duration <= 0:
                raise Retryable("synthesis produced zero-length audio")
            return duration
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < attempts:
                delay = min(8.0, 0.5 * (2 ** attempt))
                log.warning("chunk synthesis failed, retrying",
                            extra={"ordinal": chunk.ordinal, "attempt": attempt,
                                   "error": str(exc)})
                sleep(delay)
    raise Retryable(
        f"chunk {chunk.ordinal} failed after {attempts} attempts: {last}"
    )


def gap_for(previous: Chunk | None, current: Chunk) -> float:
    """Silence to insert before ``current``.

    A chapter change gets a longer pause than a paragraph break. Without it
    the narration runs continuously across a subject change, which is the
    single thing that makes long-form narration exhausting to listen to.
    """
    if previous is None:
        return 0.0
    if previous.chapter_ordinal != current.chapter_ordinal:
        return 1.6
    return 0.45


def run(
    tts, chapters: list[dict[str, Any]], work_dir: Path, *, max_chars: int,
    voice: str, length_scale: float, sample_rate: int = 48000,
    existing: dict[int, dict[str, Any]] | None = None,
    on_chunk=None, sleep=time.sleep,
) -> NarrationResult:
    """Synthesise the whole documentary and measure what came out.

    ``existing`` maps chunk ordinal to a previously completed row, which is
    how a resumed run skips work it already did.
    """
    chunks = plan_chunks(chapters, max_chars)
    existing = existing or {}
    parts_dir = work_dir / "chunks"
    parts_dir.mkdir(parents=True, exist_ok=True)

    pieces: list[Path] = []
    # (chapter_ordinal, chapter_id, heading) -> accumulated seconds
    timeline: list[tuple[Chunk, float, float]] = []
    cursor = 0.0
    synthesised = reused = 0
    previous: Chunk | None = None

    for chunk in chunks:
        digest = chunk_hash(chunk.text, voice, length_scale)
        path = parts_dir / f"chunk-{chunk.ordinal:05d}-{digest[:12]}.wav"

        gap = gap_for(previous, chunk)
        if gap > 0:
            pad = parts_dir / f"gap-{chunk.ordinal:05d}.wav"
            if not pad.exists():
                ffmpeg.silence(pad, gap, sample_rate=sample_rate)
            pieces.append(pad)
            cursor += gap

        prior = existing.get(chunk.ordinal)
        if (prior and prior.get("text_hash") == digest and prior.get("path")
                and Path(prior["path"]).exists() and prior.get("duration_s")):
            duration = float(prior["duration_s"])
            path = Path(prior["path"])
            reused += 1
        else:
            duration = synthesise_chunk(tts, chunk, path, sleep=sleep)
            synthesised += 1
            if on_chunk:
                on_chunk(chunk, path, duration, digest)

        pieces.append(path)
        timeline.append((chunk, cursor, duration))
        cursor += duration
        previous = chunk

    narration_path = work_dir / "narration.wav"
    total = ffmpeg.concat_audio(pieces, narration_path, sample_rate=sample_rate)

    timings = _chapter_timings(chapters, timeline, total)
    words = sum(len(c.text.split()) for c in chunks)
    measured_wpm = words / (total / 60.0) if total > 0 else 0.0

    log.info("narration complete",
             extra={"duration_s": round(total, 1),
                    "minutes": round(total / 60.0, 1),
                    "synthesised": synthesised, "reused": reused,
                    "measured_wpm": round(measured_wpm, 1)})

    return NarrationResult(narration_path, total, timings, synthesised,
                           reused, measured_wpm)


def _chapter_timings(
    chapters: list[dict[str, Any]], timeline: list[tuple[Chunk, float, float]],
    total: float,
) -> list[ChapterTiming]:
    """Collapse chunk timings into one span per chapter.

    Measured from the audio, never estimated from word counts -- the chapter
    markers must point at the moment the chapter actually begins.
    """
    spans: dict[int, list[float]] = {}
    for chunk, start, duration in timeline:
        entry = spans.setdefault(chunk.chapter_ordinal, [start, start + duration])
        entry[0] = min(entry[0], start)
        entry[1] = max(entry[1], start + duration)

    out: list[ChapterTiming] = []
    for chapter in chapters:
        ordinal = int(chapter.get("ordinal", 0))
        span = spans.get(ordinal)
        if span is None:
            continue
        start, end = span
        out.append(ChapterTiming(
            chapter_id=chapter.get("id"), ordinal=ordinal,
            heading=str(chapter.get("heading", f"Part {ordinal + 1}")),
            start_s=round(start, 3),
            duration_s=round(min(end, total) - start, 3),
        ))
    out.sort(key=lambda t: t.start_s)
    return out
