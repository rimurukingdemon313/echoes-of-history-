"""The research engine.

Produces a *research package*: a set of sources with provenance, a set of
extracted facts each pointing at the source it came from, and an explicit
list of conflicts where sources disagree.

The rule that shapes everything here: the language model is used to *read*
sources, never as a source. It is asked "what does this passage assert", not
"what happened". A fact with no ``source_id`` cannot enter the package, which
is what makes the later "is this claim supported" question answerable by a
database query rather than by an opinion.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ..errors import Permanent, Retryable
from ..logging import get_logger
from ..providers.research.base import SourceDoc

log = get_logger(__name__)

EXTRACT_PROMPT = """\
TASK: fact_extraction
PAYLOAD: {payload}

Read the SOURCE passage below and list the factual assertions it makes about
the subject. Do not add anything the passage does not say. Do not use your own
knowledge of the subject.

For each assertion, classify confidence as the SOURCE presents it:
  established     - stated plainly as fact
  interpretation  - presented as a scholarly reading or argument
  disputed        - the source notes disagreement
  uncertain       - hedged, approximate, or attributed to a single account

SUBJECT: {subject}

SOURCE ({provider}, quality {quality}): {title}
---
{text}
---

Return JSON: {{"facts": [{{"statement": str, "excerpt": str,
"confidence": str, "entities": [str]}}]}}
Return at most {limit} facts. If the passage says nothing about the subject,
return {{"facts": []}}.
"""

_VALID_CONFIDENCE = ("established", "interpretation", "disputed", "uncertain")


def fact_view(fact: dict[str, Any]) -> dict[str, Any]:
    """The fields of a fact that belong in a prompt.

    Facts read back from the database carry row metadata -- ids, timestamps,
    the source join -- none of which a model should see and some of which is
    not JSON-serialisable. Projecting here keeps prompts small and stops a
    schema change from silently leaking columns into a prompt.
    """
    return {
        "statement": fact.get("statement", ""),
        "confidence": fact.get("confidence", "uncertain"),
        "entities": list(fact.get("entities") or [])[:12],
    }


@dataclass
class ResearchResult:
    sources: list[SourceDoc]
    facts: list[dict[str, Any]]
    conflicts: list[dict[str, Any]]
    summary: str

    @property
    def source_count(self) -> int:
        return len(self.sources)


def build_queries(topic: dict[str, Any], *, extra: int = 4) -> list[str]:
    """Turn one topic into several searches.

    A single query against a single provider returns a single point of view.
    Varying the angle -- context, evidence, daily life, consequence -- is what
    produces a package broad enough to write ninety minutes from.
    """
    title = str(topic.get("title") or "").strip()
    subject = str(topic.get("subject") or title).strip()
    region = str(topic.get("region") or "").strip()
    period = str(topic.get("period") or "").strip()

    queries = [title]
    if subject and subject.lower() != title.lower():
        queries.append(subject)

    angles = [
        f"{subject} archaeology evidence",
        f"{subject} history {period}".strip(),
        f"{subject} {region} society economy".strip(),
        f"{subject} primary sources chronicle",
        f"{subject} scholarly debate interpretation",
        f"{subject} daily life material culture",
    ]
    for angle in angles[:extra]:
        cleaned = re.sub(r"\s+", " ", angle).strip()
        if cleaned and cleaned not in queries:
            queries.append(cleaned)
    return queries


def gather(providers, queries: list[str], *, per_query: int = 4) -> list[SourceDoc]:
    """Run every query against every provider, de-duplicated by URL.

    A provider that fails is logged and skipped rather than failing the
    stage: losing one of four archives narrows a package, losing all four is
    what the caller's minimum-source check is for.
    """
    seen: dict[str, SourceDoc] = {}
    failures: list[str] = []

    for provider in providers:
        got = 0
        for query in queries:
            try:
                docs = provider.search(query, limit=per_query)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{provider.name}: {exc}")
                log.warning("research provider failed",
                            extra={"provider": provider.name, "query": query,
                                   "error": str(exc)})
                continue
            for doc in docs:
                if doc.url not in seen and doc.text.strip():
                    seen[doc.url] = doc
                    got += 1
        log.info("provider gathered sources",
                 extra={"provider": provider.name, "sources": got})

    if not seen and failures:
        raise Retryable(
            "every research provider failed: " + "; ".join(failures[:4])
        )
    # Best sources first, so a truncated extraction budget spends itself on
    # the archives rather than the encyclopaedia.
    return sorted(seen.values(), key=lambda d: (-d.quality, d.url))


def extract_facts(
    llm, subject: str, docs: list[SourceDoc], *, per_source: int = 8,
    max_chars: int = 6000,
) -> list[dict[str, Any]]:
    """Ask the model what each source asserts. Facts carry their source index."""
    facts: list[dict[str, Any]] = []
    for index, doc in enumerate(docs):
        text = doc.text.strip()
        if len(text) < 120:
            continue
        payload = {
            "subject": subject, "provider": doc.provider,
            "quality": doc.quality, "title": doc.title,
            "text": text[:max_chars], "limit": per_source,
        }
        prompt = EXTRACT_PROMPT.format(
            payload=json.dumps(payload, default=str), subject=subject,
            provider=doc.provider,
            quality=doc.quality, title=doc.title, text=text[:max_chars],
            limit=per_source,
        )
        try:
            response = llm.generate_json(prompt)
        except Exception as exc:  # noqa: BLE001
            log.warning("fact extraction failed for source",
                        extra={"url": doc.url, "error": str(exc)})
            continue

        for row in (response or {}).get("facts", [])[:per_source]:
            statement = str((row or {}).get("statement", "")).strip()
            if len(statement) < 20:
                continue
            confidence = str(row.get("confidence", "uncertain")).lower()
            if confidence not in _VALID_CONFIDENCE:
                # An unrecognised label is downgraded, never guessed upward.
                confidence = "uncertain"
            facts.append({
                "statement": statement[:2000],
                "excerpt": str(row.get("excerpt", ""))[:1000] or None,
                "confidence": confidence,
                "entities": [str(e) for e in (row.get("entities") or [])][:12],
                "source_index": index,
            })
    return facts


# Quantities are matched first and removed, because a year pattern that
# allows a bare number will read "150,000 tonnes" as the year 150.
_NUMBER = re.compile(r"\b\d{1,3}(?:,\d{3})+\b|\b\d{5,}\b")
# A year is either explicitly marked with an era, or a bare 3-4 digit number
# once the quantities are gone.
_YEAR_ERA = re.compile(r"\b(\d{1,4})\s*(?:BCE|BC|AD|CE)\b", re.IGNORECASE)
_YEAR_BARE = re.compile(r"\b(\d{3,4})\b")


def _content_tokens(statement: str) -> set[str]:
    """Words that carry the assertion, with the figures removed.

    Figures are stripped because they are the thing being compared: two
    statements that differ *only* in their number must look identical here,
    or the overlap test rejects exactly the pair we want to catch.
    """
    stripped = _NUMBER.sub(" ", statement)
    stripped = _YEAR_ERA.sub(" ", stripped)
    stripped = _YEAR_BARE.sub(" ", stripped)
    return {w for w in re.findall(r"[a-z]{4,}", stripped.lower())}


def _figures(statement: str) -> tuple[set[int], set[str]]:
    """Split a statement's figures into years and quantities.

    Quantities are taken out of the text before years are looked for, so a
    grouped number cannot be mistaken for a date.
    """
    numbers = {m.group(0).replace(",", "") for m in _NUMBER.finditer(statement)}
    remainder = _NUMBER.sub(" ", statement)

    years = {int(m.group(1)) for m in _YEAR_ERA.finditer(remainder)}
    if not years:
        # No era marker anywhere: fall back to bare 3-4 digit numbers, which
        # in historical prose are overwhelmingly dates.
        years = {int(m.group(1)) for m in _YEAR_BARE.finditer(remainder)}
    return years, numbers


def detect_conflicts(
    facts: list[dict[str, Any]], *, overlap_threshold: float = 0.55
) -> list[dict[str, Any]]:
    """Find sources that disagree on a figure for the *same* assertion.

    An earlier version compared every number mentioned about an entity, which
    reported the date a site was excavated as contradicting the date it was
    built. Both are numbers "about Caesarea"; neither disagrees with the
    other.

    So a conflict now requires two things: the statements must come from
    different sources, and they must overlap strongly once their numbers are
    removed -- meaning they assert the same thing and differ in the figure.
    That is the only case a narrator can honestly describe as sources
    disagreeing.

    This is deliberately narrow. Disagreements expressed in prose rather than
    in figures are not caught here; reading for those is the fact-checker's
    job.
    """
    by_entity: dict[str, list[dict[str, Any]]] = {}
    for fact in facts:
        for entity in fact.get("entities", []):
            key = str(entity).strip().lower()
            if len(key) > 3:
                by_entity.setdefault(key, []).append(fact)

    conflicts: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for entity, group in by_entity.items():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                left, right = group[i], group[j]
                if left["source_index"] == right["source_index"]:
                    continue

                lt, rt = _content_tokens(left["statement"]), _content_tokens(right["statement"])
                if not lt or not rt:
                    continue
                overlap = len(lt & rt) / len(lt | rt)
                if overlap < overlap_threshold:
                    continue

                ly, ln = _figures(left["statement"])
                ry, rn = _figures(right["statement"])

                for kind, lv, rv in (("date", ly, ry), ("quantity", ln, rn)):
                    if not lv or not rv or lv == rv:
                        continue
                    key = (entity, kind)
                    if key in seen:
                        continue
                    seen.add(key)
                    conflicts.append({
                        "entity": entity,
                        "kind": kind,
                        "overlap": round(overlap, 2),
                        "values": sorted(str(v) for v in (lv | rv))[:8],
                        "statements": [left["statement"][:300], right["statement"][:300]],
                        "source_indices": sorted({left["source_index"],
                                                  right["source_index"]}),
                    })
    return conflicts[:40]


def summarise(facts: list[dict[str, Any]], docs: list[SourceDoc]) -> str:
    counts: dict[str, int] = {}
    for fact in facts:
        counts[fact["confidence"]] = counts.get(fact["confidence"], 0) + 1
    providers: dict[str, int] = {}
    for doc in docs:
        providers[doc.provider] = providers.get(doc.provider, 0) + 1
    return (
        f"{len(docs)} sources ("
        + ", ".join(f"{k}: {v}" for k, v in sorted(providers.items()))
        + f"); {len(facts)} facts ("
        + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
        + ")"
    )


def run(
    llm, providers, topic: dict[str, Any], *, min_sources: int, per_query: int = 4
) -> ResearchResult:
    subject = str(topic.get("subject") or topic.get("title") or "")
    queries = build_queries(topic)
    docs = gather(providers, queries, per_query=per_query)

    if len(docs) < min_sources:
        raise Permanent(
            f"research found only {len(docs)} usable sources for "
            f"{topic.get('title')!r}; MIN_SOURCES_PER_DOCUMENTARY is "
            f"{min_sources}. Broaden RESEARCH_PROVIDERS or pick another topic "
            f"-- a documentary built on fewer sources cannot be fact-checked."
        )

    facts = extract_facts(llm, subject, docs)
    if not facts:
        raise Permanent(
            f"no facts could be extracted from {len(docs)} sources; the "
            f"sources may be catalogue stubs with no prose"
        )
    conflicts = detect_conflicts(facts)
    log.info("research complete",
             extra={"sources": len(docs), "facts": len(facts),
                    "conflicts": len(conflicts)})
    return ResearchResult(docs, facts, conflicts, summarise(facts, docs))
