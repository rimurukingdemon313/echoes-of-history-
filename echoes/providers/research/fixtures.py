"""Offline research source, for dry runs and tests.

Serves ``SourceDoc``s from a local JSON file, or -- when none is present --
synthesises plausible catalogue prose deterministically. It exists because
the pipeline must be exercisable end to end without network egress, and
because tests need source text that does not change between runs.

Every document it returns is marked ``provider="fixtures"``, and
:mod:`echoes.pipeline.qc` treats the presence of a fixtures source as a
blocking failure whenever ``DRY_RUN`` is false. A documentary must never be
published on the strength of invented sources.
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path

from ...logging import get_logger
from .base import SourceDoc

log = get_logger(__name__)

PROVIDER_NAME = "fixtures"

_TEMPLATES = (
    "The site was first surveyed systematically in {year}, and the excavation "
    "reports describe {feature} extending some {span} metres along the "
    "{direction} edge of the settlement.",
    "Material recovered from the {layer} layer includes {material}, which the "
    "excavators dated to the {century} century on stratigraphic grounds.",
    "A {document} preserved in the {archive} records a payment of {amount} "
    "units, apparently for the maintenance of {feature}.",
    "Analysis of {material} recovered in {year} indicates a source in the "
    "{direction} highlands, implying a trade route of at least {span} "
    "kilometres.",
    "The {century} century saw {feature} fall out of use; the reasons are "
    "debated, and no contemporary account explains the change.",
    "Estimates of the population at its height vary between {amount} and "
    "{amount2}, depending on which density figure is applied to the excavated "
    "area.",
    "{person} argued in a study of the region that {feature} served an "
    "administrative rather than a religious function, a reading that later "
    "work has only partly supported.",
)

_SLOTS = {
    "year": ["1887", "1923", "1964", "1971", "1990", "2004", "2016"],
    "feature": ["the enclosure wall", "the granary complex", "the harbour mole",
                "the terraced field system", "the cistern network",
                "the colonnaded street"],
    "span": ["40", "120", "260", "480", "900"],
    "direction": ["northern", "southern", "eastern", "western"],
    "layer": ["destruction", "occupation", "abandonment", "foundation"],
    "material": ["imported amphorae", "worked obsidian", "glazed ware",
                 "iron slag", "carbonised grain", "cedar timber"],
    "century": ["third", "fifth", "eighth", "eleventh", "fourteenth"],
    "document": ["tax register", "temple inventory", "letter", "land grant"],
    "archive": ["municipal archive", "monastic library", "state collection"],
    "amount": ["1,200", "4,500", "12,000", "38,000"],
    "amount2": ["2,400", "9,000", "21,000", "55,000"],
    "person": ["Halvorsen", "Nakamura", "Oyelaran", "Petrescu", "Silveira"],
}


class FixturesProvider:
    """Deterministic offline sources. Never valid for a real publish."""

    name = PROVIDER_NAME

    def __init__(self, fixture_path: Path | None = None, seed: int = 4711) -> None:
        self._path = Path(fixture_path) if fixture_path else None
        self._seed = seed
        self._loaded: list[dict] | None = None

    def available(self) -> bool:
        return True

    def _from_file(self, query: str, limit: int) -> list[SourceDoc]:
        if self._loaded is None:
            try:
                self._loaded = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                log.warning("could not read research fixtures",
                            extra={"path": str(self._path), "error": str(exc)})
                self._loaded = []
        docs = []
        for row in self._loaded[:limit]:
            docs.append(SourceDoc(
                url=row["url"], title=row.get("title", "Fixture"),
                provider=self.name, text=row.get("text", ""),
                source_type=row.get("source_type", "reference"),
                licence=row.get("licence", "fixture"),
                quality=float(row.get("quality", 0.5)),
                rationale="offline fixture; not a real source",
            ))
        return docs

    def search(self, query: str, *, limit: int = 5) -> list[SourceDoc]:
        if self._path and self._path.exists():
            return self._from_file(query, limit)

        docs: list[SourceDoc] = []
        for index in range(limit):
            digest = hashlib.sha256(f"{self._seed}:{query}:{index}".encode()).hexdigest()
            rng = random.Random(int(digest[:16], 16))
            sentences = []
            subject = query.split()[0] if query.split() else "the site"
            for template in rng.sample(_TEMPLATES, k=min(6, len(_TEMPLATES))):
                filled = template
                for slot, options in _SLOTS.items():
                    filled = filled.replace("{" + slot + "}", rng.choice(options))
                sentences.append(filled)
            sentences.insert(
                0,
                f"{query.title()} is discussed at length in the literature on "
                f"{subject}, where the surviving evidence is uneven."
            )
            docs.append(SourceDoc(
                url=f"https://fixtures.invalid/{digest[:20]}",
                title=f"{query.title()} - catalogue record {index + 1}",
                provider=self.name,
                text=" ".join(sentences),
                source_type="reference",
                licence="fixture (offline)",
                quality=0.55,
                rationale="offline fixture; not a real source",
            ))
        return docs
