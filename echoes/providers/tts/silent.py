"""A TTS provider that produces correctly-timed silence.

Used when a dry run needs realistic timings on a machine with no voice model
installed. It computes duration from word count at the configured speaking
rate, so every downstream stage -- chapter markers, segment durations, the
render timeline, quality control -- sees the numbers it would see in
production. It writes silence, so the result is never mistaken for narration.
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path


class SilentProvider:
    name = "silent"

    def __init__(self, words_per_minute: float = 150.0, sample_rate: int = 22050) -> None:
        self.voice = "silent"
        self.sample_rate = sample_rate
        self._wpm = words_per_minute

    def available(self) -> bool:
        return True

    def synthesize(self, text: str, out_path: Path) -> float:
        words = max(len(text.split()), 1)
        duration = words / (self._wpm / 60.0)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        frames = int(duration * self.sample_rate)
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            wf.writeframes(struct.pack("<h", 0) * frames)
        return duration
