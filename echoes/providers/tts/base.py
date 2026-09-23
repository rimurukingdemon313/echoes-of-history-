"""The narration boundary.

A provider turns one chunk of text into one audio file and reports how long
it is. Chunking, stitching, retry and loudness are the pipeline's job, not
the provider's, so a new provider is a single small class.

Chunking lives outside for a reason beyond tidiness: chunk boundaries must
line up with chapter boundaries, because the chapter timings that become
YouTube chapter markers are measured from the rendered audio.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    text: str
    chapter_id: int | None
    chapter_ordinal: int


class TTSProvider(Protocol):
    name: str
    voice: str
    sample_rate: int

    def synthesize(self, text: str, out_path: Path) -> float:
        """Write audio for ``text`` to ``out_path``; return duration in seconds."""

    def available(self) -> bool:
        ...


_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def split_into_chunks(text: str, max_chars: int) -> list[str]:
    """Split on sentence boundaries, never mid-sentence.

    A chunk cut mid-sentence produces an audible swallow at the join, because
    the synthesiser drops the prosodic contour it was building. Very long
    sentences are left over-length rather than broken: a slightly large chunk
    is better than a damaged one.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    sentences = [s.strip() for s in _SENTENCE.split(text.strip()) if s.strip()]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if not current:
            current = sentence
        elif len(current) + 1 + len(sentence) <= max_chars:
            current = f"{current} {sentence}"
        else:
            chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return chunks
