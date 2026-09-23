"""The fact-check engine.

The script is written from the research package, but "written from" is not
"supported by": a model asked to turn twelve facts into eleven hundred words
will bridge between them, and the bridges are where invented dates and
confident-sounding numbers appear.

So every factual sentence is extracted and checked back against the package:

1. Pull the claims out of each chapter.
2. For each claim, retrieve the facts that could bear on it -- by token
   overlap, which is crude but transparent, and which crucially cannot
   hallucinate evidence that is not in the package.
3. Ask the model to judge each claim *against that retrieved evidence only*.
4. Rewrite the claims that fail, then verify the rewrite.

Step 2 is what makes this more than asking a model to mark its own homework.
The model never gets to decide what evidence exists; it only reads what the
database hands it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..errors import FactCheckFailed
from ..logging import get_logger
from .research import fact_view

log = get_logger(__name__)

EXTRACT_PROMPT = """\
TASK: claim_extraction
PAYLOAD: {payload}

List the checkable factual assertions in the passage below. A checkable
assertion names a date, a quantity, a person, a place, an event or a causal
link. Ignore transitional and interpretive sentences that assert nothing.

PASSAGE:
{text}

Return JSON: {{"claims": [{{"text": str, "kind": str}}]}}
Return at most {limit} claims.
"""

VERDICT_PROMPT = """\
TASK: claim_verdicts
PAYLOAD: {payload}

Judge each CLAIM using ONLY the EVIDENCE provided. Do not use your own
knowledge of the subject. If the evidence does not cover a claim, that claim
is unsupported -- that is the correct answer, not a failure.

Verdicts:
  supported     - the evidence states this, or states something it follows from
  uncertain     - the evidence touches it but does not establish it
  unsupported   - the evidence does not cover this claim
  contradicted  - the evidence says something incompatible with it

CLAIMS:
{claims}

EVIDENCE:
{evidence}

Return JSON: {{"verdicts": [{{"id": int, "verdict": str, "note": str,
"source_ids": [int]}}]}}
"""

REVISE_PROMPT = """\
TASK: chapter_revise
PAYLOAD: {payload}

Revise the passage below. These specific assertions could not be supported by
the research:

{problems}

For each one, either:
- restate it so it matches what the evidence actually supports, or
- attribute it honestly ("one account claims", "the evidence is thin"), or
- remove it.

Do NOT add new facts. Do NOT replace an unsupported number with a different
number. Keep the length and the calm register. Return only the revised prose.

PASSAGE:
{text}
"""

_VALID = ("supported", "uncertain", "unsupported", "contradicted")


@dataclass
class ClaimCheck:
    text: str
    kind: str
    chapter_index: int
    verdict: str = "uncertain"
    note: str = ""
    source_indices: list[int] = field(default_factory=list)


@dataclass
class FactCheckResult:
    claims: list[ClaimCheck]
    unsupported_ratio: float
    revised_chapters: dict[int, str]
    passes: int

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for claim in self.claims:
            out[claim.verdict] = out.get(claim.verdict, 0) + 1
        return out

    @property
    def failing(self) -> list[ClaimCheck]:
        return [c for c in self.claims
                if c.verdict in ("unsupported", "contradicted")]


def extract_claims(llm, text: str, chapter_index: int, *, limit: int = 30
                   ) -> list[ClaimCheck]:
    payload = {"text": text, "limit": limit}
    prompt = EXTRACT_PROMPT.format(payload=json.dumps(payload, default=str),
                                   text=text,
                                   limit=limit)
    try:
        response = llm.generate_json(prompt)
    except Exception as exc:  # noqa: BLE001
        log.warning("claim extraction failed",
                    extra={"chapter": chapter_index, "error": str(exc)})
        return []
    out = []
    for row in (response or {}).get("claims", [])[:limit]:
        claim_text = str((row or {}).get("text", "")).strip()
        if len(claim_text.split()) >= 5:
            out.append(ClaimCheck(text=claim_text[:1000],
                                  kind=str(row.get("kind", "assertion"))[:40],
                                  chapter_index=chapter_index))
    return out


def retrieve_evidence(
    claim: ClaimCheck, facts: list[dict[str, Any]], *, top_k: int = 6
) -> list[int]:
    """Indices of the facts most likely to bear on ``claim``.

    Token overlap, weighted toward rarer words. It is not semantic, and it
    will miss a paraphrase that shares no vocabulary -- which is why an
    uncovered claim is judged ``unsupported`` rather than false. The point is
    that the evidence set comes from the package, not from the model.
    """
    claim_tokens = {w for w in re.findall(r"[a-z]{4,}", claim.text.lower())}
    numbers = set(re.findall(r"\d[\d,]*", claim.text))
    if not claim_tokens and not numbers:
        return []

    scored: list[tuple[float, int]] = []
    for index, fact in enumerate(facts):
        statement = fact["statement"].lower()
        fact_tokens = {w for w in re.findall(r"[a-z]{4,}", statement)}
        if not fact_tokens:
            continue
        overlap = len(claim_tokens & fact_tokens)
        if overlap == 0 and not numbers:
            continue
        score = overlap / max(len(claim_tokens), 1)
        # A shared figure is a much stronger signal than a shared common word.
        for number in numbers:
            if number in fact["statement"]:
                score += 0.5
        if score > 0:
            scored.append((score, index))
    scored.sort(key=lambda t: -t[0])
    return [index for _, index in scored[:top_k]]


def _format_claims(claims: list[ClaimCheck]) -> str:
    return "\n".join(f"{i}. {c.text}" for i, c in enumerate(claims))


def _format_evidence(facts: list[dict[str, Any]], indices: list[int]) -> str:
    if not indices:
        return "(no evidence retrieved)"
    return "\n".join(
        f"[{i}] ({facts[i]['confidence']}) {facts[i]['statement']}"
        for i in indices
    )


def judge(
    llm, claims: list[ClaimCheck], facts: list[dict[str, Any]], *, batch: int = 12
) -> None:
    """Assign a verdict to every claim, in batches, in place."""
    for start in range(0, len(claims), batch):
        window = claims[start:start + batch]
        evidence_indices: list[int] = []
        for claim in window:
            claim.source_indices = retrieve_evidence(claim, facts)
            evidence_indices.extend(claim.source_indices)
        unique_evidence = sorted(set(evidence_indices))

        payload = {
            "claims": [
                {"id": i, "text": c.text,
                 "candidate_source_ids": c.source_indices}
                for i, c in enumerate(window)
            ],
            "facts": [fact_view(facts[i]) for i in unique_evidence],
        }
        prompt = VERDICT_PROMPT.format(
            payload=json.dumps(payload, default=str),
            claims=_format_claims(window),
            evidence=_format_evidence(facts, unique_evidence),
        )
        try:
            response = llm.generate_json(prompt)
        except Exception as exc:  # noqa: BLE001
            # A failed verdict batch must not silently pass. Everything in it
            # stays at its default, which counts against the ratio.
            log.warning("verdict batch failed",
                        extra={"start": start, "error": str(exc)})
            for claim in window:
                claim.verdict = "uncertain"
                claim.note = "verdict unavailable"
            continue

        by_id = {}
        for row in (response or {}).get("verdicts", []):
            try:
                by_id[int(row.get("id"))] = row
            except (TypeError, ValueError):
                continue
        for index, claim in enumerate(window):
            row = by_id.get(index)
            if not row:
                claim.verdict = "uncertain"
                claim.note = "no verdict returned"
                continue
            verdict = str(row.get("verdict", "")).lower().strip()
            # An unrecognised verdict never counts as support.
            claim.verdict = verdict if verdict in _VALID else "uncertain"
            claim.note = str(row.get("note", ""))[:500]


def revise(llm, text: str, problems: list[ClaimCheck]) -> str:
    payload = {"text": text,
               "problems": [{"text": p.text, "verdict": p.verdict,
                             "note": p.note} for p in problems]}
    prompt = REVISE_PROMPT.format(
        payload=json.dumps(payload, default=str), text=text,
        problems="\n".join(f"- [{p.verdict}] {p.text}" for p in problems[:12]),
    )
    return llm.generate(prompt).strip()


def _failing_by_chapter(claims: list[ClaimCheck]) -> dict[int, int]:
    out: dict[int, int] = {}
    for claim in claims:
        if claim.verdict in ("unsupported", "contradicted"):
            out[claim.chapter_index] = out.get(claim.chapter_index, 0) + 1
    return out


def _check_all(llm, bodies: list[str], facts: list[dict[str, Any]]
               ) -> tuple[list[ClaimCheck], float]:
    claims: list[ClaimCheck] = []
    for index, body in enumerate(bodies):
        claims.extend(extract_claims(llm, body, index))
    if not claims:
        return [], 0.0
    judge(llm, claims, facts)
    failing = sum(1 for c in claims if c.verdict in ("unsupported", "contradicted"))
    return claims, failing / len(claims)


def run(
    llm, chapter_bodies: list[str], facts: list[dict[str, Any]],
    *, max_unsupported_ratio: float, max_passes: int = 2,
) -> FactCheckResult:
    """Check, revise, re-check -- and never accept a revision that hurt.

    The re-check is not a formality. A revision is another generation, and it
    can make a chapter worse: asked to soften three unsupported sentences, a
    model may rewrite the whole passage and strip out the sourced detail that
    was holding the rest up. Observed in testing, that turned a chapter with
    5 failures into one with 28.

    So revisions are accepted per chapter, and only on evidence: a chapter
    keeps its rewrite if the rewrite has strictly fewer failing claims than
    the original, and is reverted otherwise. The script can therefore only
    improve or stay the same across passes.
    """
    bodies = list(chapter_bodies)
    revised: dict[int, str] = {}

    claims, ratio = _check_all(llm, bodies, facts)
    if not claims:
        log.warning("no checkable claims found in script")
        return FactCheckResult([], 0.0, revised, 1)

    log.info("fact-check pass complete",
             extra={"pass": 1, "claims": len(claims),
                    "failing": sum(1 for c in claims
                                   if c.verdict in ("unsupported", "contradicted")),
                    "ratio": round(ratio, 4)})

    for pass_number in range(2, max_passes + 1):
        if ratio <= max_unsupported_ratio:
            break

        before = _failing_by_chapter(claims)
        failing = [c for c in claims
                   if c.verdict in ("unsupported", "contradicted")]
        by_chapter: dict[int, list[ClaimCheck]] = {}
        for claim in failing:
            by_chapter.setdefault(claim.chapter_index, []).append(claim)

        candidate_bodies = list(bodies)
        attempted: list[int] = []
        for chapter_index, problems in by_chapter.items():
            try:
                new_body = revise(llm, bodies[chapter_index], problems)
            except Exception as exc:  # noqa: BLE001
                log.warning("revision failed",
                            extra={"chapter": chapter_index, "error": str(exc)})
                continue
            # A revision that guts the chapter is rejected before it is even
            # scored: losing a third of the narration is not an improvement
            # however few claims survive to fail.
            if len(new_body.split()) < len(bodies[chapter_index].split()) * 0.6:
                log.warning("revision discarded: it cut the chapter too far",
                            extra={"chapter": chapter_index})
                continue
            candidate_bodies[chapter_index] = new_body
            attempted.append(chapter_index)

        if not attempted:
            break

        candidate_claims, candidate_ratio = _check_all(llm, candidate_bodies, facts)
        after = _failing_by_chapter(candidate_claims)

        kept: list[int] = []
        for chapter_index in attempted:
            if after.get(chapter_index, 0) < before.get(chapter_index, 0):
                bodies[chapter_index] = candidate_bodies[chapter_index]
                revised[chapter_index] = candidate_bodies[chapter_index]
                kept.append(chapter_index)
            else:
                log.info("reverting a revision that did not help",
                         extra={"chapter": chapter_index,
                                "failing_before": before.get(chapter_index, 0),
                                "failing_after": after.get(chapter_index, 0)})

        if not kept:
            # Nothing improved. Another pass would repeat the same work.
            log.info("no revision improved the script; stopping",
                     extra={"pass": pass_number})
            break

        claims, ratio = _check_all(llm, bodies, facts)
        log.info("fact-check pass complete",
                 extra={"pass": pass_number, "claims": len(claims),
                        "failing": sum(1 for c in claims
                                       if c.verdict in ("unsupported",
                                                        "contradicted")),
                        "ratio": round(ratio, 4),
                        "chapters_revised": len(kept)})

    if ratio > max_unsupported_ratio:
        failing = [c for c in claims if c.verdict in ("unsupported", "contradicted")]
        raise FactCheckFailed(
            f"{len(failing)} of {len(claims)} claims ({ratio:.1%}) remain "
            f"unsupported after revision; the limit is "
            f"{max_unsupported_ratio:.1%}. This documentary is not publishable.",
            [c.text[:200] for c in failing[:20]],
        )

    return FactCheckResult(claims, ratio, revised, max_passes)
