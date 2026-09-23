"""The research boundary.

A research provider answers one question: given a topic, what documents
exist, and what do they say. It returns text plus provenance -- never a
conclusion. Judging, reconciling and fact-checking happen in the pipeline,
where the decisions are visible and testable.

Source quality is assigned here rather than by the language model, because a
model asked to rate its own sources will rate them all highly. The ranking is
a fixed property of the institution: a national library's catalogue record
outranks a general encyclopaedia, which outranks an open web page.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class SourceDoc:
    url: str
    title: str
    provider: str
    text: str = ""
    source_type: str = "reference"
    author: str | None = None
    published: str | None = None
    licence: str | None = None
    quality: float = 0.5
    rationale: str = ""
    extra: dict = field(default_factory=dict)


class ResearchProvider(Protocol):
    name: str

    def search(self, query: str, *, limit: int = 5) -> list[SourceDoc]:
        """Return documents relevant to ``query``. Never raises on empty."""

    def available(self) -> bool:
        ...


# Fixed, auditable quality tiers. Raising one of these is a deliberate
# editorial decision, not a tuning knob.
QUALITY = {
    "primary_archive": 0.95,   # digitised primary material held by an institution
    "museum_catalogue": 0.85,  # catalogued object records
    "peer_reviewed": 0.90,     # journal articles with a DOI
    "national_library": 0.85,
    "encyclopaedia": 0.60,     # useful for orientation, weak as sole support
    "open_web": 0.35,
}
