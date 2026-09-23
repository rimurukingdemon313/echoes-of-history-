"""The visual boundary.

Every candidate carries a licence string and, where the licence demands it,
an attribution line. :func:`acceptable_licence` is the gate: a candidate whose
licence cannot be recognised is dropped, never used "probably fine". An
unlicensed image on a monetised channel is a copyright strike, and a strike
costs more than an empty slot in a timeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ImageCandidate:
    url: str
    title: str
    provider: str
    licence: str
    attribution: str | None = None
    creator: str | None = None
    source_page: str | None = None
    width: int | None = None
    height: int | None = None
    synthetic: bool = False
    extra: dict = field(default_factory=dict)


class ImageProvider(Protocol):
    name: str

    def search(self, query: str, *, limit: int = 8) -> list[ImageCandidate]:
        ...

    def available(self) -> bool:
        ...


# Recognised free licences. Anything not matching is refused.
_ALLOWED = (
    re.compile(r"\bcc0\b", re.I),
    re.compile(r"public\s*domain", re.I),
    re.compile(r"\bpd(-|\s)", re.I),
    re.compile(r"\bcc[\s-]?by(?:[\s-]?sa)?(?:[\s-]?\d(?:\.\d)?)?\b", re.I),
)
# Explicit refusals, checked first: "CC BY-NC" contains "CC BY".
_REFUSED = (
    re.compile(r"\bnc\b|non[\s-]?commercial", re.I),
    re.compile(r"\bnd\b|no[\s-]?deriv", re.I),
    re.compile(r"fair\s*use|rights\s*reserved|all\s*rights", re.I),
)


def acceptable_licence(licence: str | None) -> bool:
    """True only for licences that permit commercial reuse with attribution.

    Non-commercial and no-derivatives licences are refused even though the
    channel may not be monetised yet: a video is edited and re-uploaded over
    its life, and a licence that forbids that is a trap set for later.
    """
    if not licence:
        return False
    for pattern in _REFUSED:
        if pattern.search(licence):
            return False
    return any(pattern.search(licence) for pattern in _ALLOWED)


def attribution_line(candidate: ImageCandidate) -> str:
    """The credit line for the video description."""
    parts = [candidate.title.strip() or "Untitled"]
    if candidate.creator:
        parts.append(f"by {candidate.creator}")
    parts.append(f"({candidate.licence})")
    if candidate.source_page:
        parts.append(f"- {candidate.source_page}")
    return " ".join(parts)
