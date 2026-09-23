"""Measure a voice's real speaking rate instead of assuming one.

The script engine decides how many words a 90-minute documentary needs. If
that conversion is wrong, every documentary comes out the wrong length and
the error is only discovered after rendering -- an hour of wasted encoding.

Piper's rate is not ``1 / length_scale``. Measured on one voice:

    scale 1.0 -> 209 wpm      scale 1.4 -> 174 wpm
    scale 1.2 -> 190 wpm      scale 1.6 -> 158 wpm

which is linear in *duration*, not in rate, with a non-zero intercept --
sentence-final pauses do not stretch with the scale. So we fit
``duration = a * scale + b`` from two probes and solve for the scale that
hits the target. Two probes because that is the minimum a line needs, and
each costs about a second.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Protocol

from ...logging import get_logger

log = get_logger(__name__)

# Deliberately mixed: long clauses, a date, a proper noun and two sentence
# endings, so the measured rate reflects documentary prose rather than a
# single unbroken run-on.
REFERENCE = (
    "In the spring of the year 480 before the common era, the Persian army "
    "began its march west. The scale of the undertaking was without precedent "
    "in the ancient world. Herodotus, writing a generation later, gives a "
    "figure so large that no modern historian accepts it, and yet the "
    "impression it leaves is worth preserving."
)
REFERENCE_WORDS = len(REFERENCE.split())


class _Scalable(Protocol):
    def synthesize(self, text: str, out_path: Path) -> float: ...


def measure_wpm(provider: _Scalable) -> float:
    """Words per minute this provider actually produces, as configured."""
    with tempfile.TemporaryDirectory() as tmp:
        duration = provider.synthesize(REFERENCE, Path(tmp) / "calibration.wav")
    if duration <= 0:
        raise ValueError("calibration produced zero-length audio")
    return REFERENCE_WORDS / (duration / 60.0)


def solve_length_scale(
    make_provider, target_wpm: float, *, probes: tuple[float, float] = (1.0, 1.6),
    lo: float = 0.6, hi: float = 2.5,
) -> tuple[float, float]:
    """Find the ``length_scale`` that lands nearest ``target_wpm``.

    ``make_provider(scale)`` must return a provider configured at that scale.
    Returns ``(scale, predicted_wpm)``. The result is clamped: a target the
    voice cannot reach yields the nearest scale it can, not an extrapolation
    into a range where the fit was never measured.
    """
    target_duration = REFERENCE_WORDS / (target_wpm / 60.0)

    durations = []
    for scale in probes:
        with tempfile.TemporaryDirectory() as tmp:
            durations.append(make_provider(scale).synthesize(REFERENCE, Path(tmp) / "c.wav"))

    (s0, d0), (s1, d1) = zip(probes, durations)
    if abs(s1 - s0) < 1e-9 or abs(d1 - d0) < 1e-9:
        return probes[0], REFERENCE_WORDS / (d0 / 60.0)

    slope = (d1 - d0) / (s1 - s0)
    intercept = d0 - slope * s0
    scale = (target_duration - intercept) / slope
    scale = max(lo, min(hi, scale))
    predicted = REFERENCE_WORDS / ((slope * scale + intercept) / 60.0)

    log.info(
        "calibrated speaking rate",
        extra={"target_wpm": round(target_wpm, 1), "length_scale": round(scale, 3),
               "predicted_wpm": round(predicted, 1)},
    )
    return scale, predicted
