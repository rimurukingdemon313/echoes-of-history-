"""A deterministic stand-in for a language model.

This exists so the pipeline can be exercised end to end -- at full
13,500-word, 90-minute scale -- without a Gemini key and without network
egress. It is a **simulator, not a writer**. The prose it produces is
assembled from the research facts it is handed plus fixed connective
scaffolding; it is coherent enough to drive narration, timing, rendering and
quality control, and it is not a documentary anyone should publish.

``PUBLISH_MODE`` is irrelevant to that judgement, so the offline provider
marks every script it produces. :mod:`echoes.pipeline.qc` refuses to pass a
video whose script carries the marker unless ``DRY_RUN`` is true, which is
what stops a synthetic script reaching a real channel.

Prompts carry a ``TASK:`` header naming what is being asked. A real provider
ignores it as ordinary prompt text; this one dispatches on it.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from typing import Any

from ...errors import Permanent

SYNTHETIC_MARKER = "[[SYNTHETIC-OFFLINE-DRAFT]]"

_TASK = re.compile(r"^TASK:\s*([a-z_]+)\s*$", re.MULTILINE)
# Anchored to a single line. A DOTALL match would run greedily to the last
# brace in the prompt, which for these templates is the JSON *example* in the
# closing instruction, not the payload.
_PAYLOAD = re.compile(r"^PAYLOAD:[ \t]*(.+)$", re.MULTILINE)

_OPENERS = (
    "The record begins, as it so often does, in fragments.",
    "What survives of this period is uneven, and the gaps matter as much as the evidence.",
    "To understand what followed, it is worth pausing on the conditions that made it possible.",
    "The account that reaches us was written down long after the events it describes.",
    "Contemporary observers disagreed about almost everything except the outcome.",
)
_CONNECTIVES = (
    "What is less often noted is that",
    "The surviving evidence suggests that",
    "Set against that background,",
    "It is here that the sources begin to diverge, because",
    "The consequence, drawn out over the following decades, was that",
    "Modern scholarship has tended to read this differently:",
    "Archaeological work in the region has complicated the picture, since",
    "Seen from the other side of the frontier,",
)
_HEDGES = (
    "though the dating remains contested",
    "although no contemporary source confirms the detail",
    "a reading that rests on a single chronicle",
    "and the figure should be treated as an order of magnitude rather than a count",
    "on evidence that is suggestive rather than decisive",
)
_CLOSERS = (
    "What remains is less a conclusion than a set of questions the evidence cannot close.",
    "The period ends without a clean boundary; the changes it set in motion outlast it.",
    "It is a history assembled from partial testimony, and it should be held that way.",
)


class OfflineProvider:
    """Deterministic, offline, and honest about being synthetic."""

    name = "offline"

    def __init__(self, seed: int = 1453) -> None:
        self._seed = seed

    def available(self) -> bool:
        return True

    # -------------------------------------------------------------- helpers
    def _rng(self, key: str) -> random.Random:
        digest = hashlib.sha256(f"{self._seed}:{key}".encode()).hexdigest()
        return random.Random(int(digest[:16], 16))

    @staticmethod
    def _dispatch(prompt: str) -> tuple[str, dict[str, Any]]:
        match = _TASK.search(prompt)
        if not match:
            raise Permanent(
                "offline provider needs a 'TASK:' header naming the operation; "
                "this prompt has none"
            )
        payload: dict[str, Any] = {}
        pm = _PAYLOAD.search(prompt)
        if pm:
            try:
                payload = json.loads(pm.group(1))
            except json.JSONDecodeError as exc:
                raise Permanent(f"offline provider could not read PAYLOAD: {exc}") from exc
        return match.group(1), payload

    # ---------------------------------------------------------------- tasks
    def generate(
        self, prompt: str, *, system: str | None = None, heavy: bool = False,
        max_tokens: int | None = None, temperature: float | None = None,
    ) -> str:
        task, payload = self._dispatch(prompt)
        if task == "chapter_prose":
            return self._chapter_prose(payload)
        if task == "chapter_revise":
            return self._chapter_revise(payload)
        raise Permanent(f"offline provider has no text handler for task {task!r}")

    def generate_json(
        self, prompt: str, *, system: str | None = None, heavy: bool = False,
        max_tokens: int | None = None,
    ) -> Any:
        task, payload = self._dispatch(prompt)
        handler = {
            "topic_candidates": self._topic_candidates,
            "chapter_outline": self._chapter_outline,
            "claim_extraction": self._claim_extraction,
            "claim_verdicts": self._claim_verdicts,
            "metadata": self._metadata,
            "fact_extraction": self._fact_extraction,
            "semantic_duplicate": self._semantic_duplicate,
        }.get(task)
        if handler is None:
            raise Permanent(f"offline provider has no JSON handler for task {task!r}")
        return handler(payload)

    # ------------------------------------------------------------ handlers
    def _topic_candidates(self, payload: dict[str, Any]) -> Any:
        seeds = payload.get("seeds") or [
            ("The Grain Fleets of the Late Roman Mediterranean", "Roman", "Mediterranean"),
            ("The Silver Mines of Laurion and Athenian Power", "Classical", "Greece"),
            ("Cahokia: The City on the Mississippi", "Pre-Columbian", "North America"),
            ("The Library at Nineveh", "Assyrian", "Mesopotamia"),
            ("Monsoon Trade and the Swahili Coast", "Medieval", "East Africa"),
            ("The Iron Pillar of Delhi", "Gupta", "South Asia"),
            ("Greenland's Norse Settlements and Their Silence", "Medieval", "North Atlantic"),
            ("The Antikythera Mechanism", "Hellenistic", "Aegean"),
        ]
        out = []
        for title, period, region in seeds:
            out.append({
                "title": title,
                "subject": title.split(":")[0],
                "period": period,
                "region": region,
                "category": "archaeology",
                "angle": "material evidence and what it cannot tell us",
                "rationale": "offline seed candidate",
            })
        return {"candidates": out}

    def _chapter_outline(self, payload: dict[str, Any]) -> Any:
        title = payload.get("title", "An Untitled History")
        count = int(payload.get("chapter_count", 12))
        rng = self._rng(f"outline:{title}")
        shapes = [
            "The Landscape Before", "The Evidence We Have", "Origins and Argument",
            "The First Accounts", "Building the System", "Daily Life and Labour",
            "Trade, Coin and Distance", "The Turning Point", "War and Its Aftermath",
            "Administration and Decline", "What the Excavations Show",
            "Competing Interpretations", "The Long Consequence", "Silence and Survival",
            "Reading the Record Now",
        ]
        rng.shuffle(shapes)
        chapters = []
        for i in range(count):
            chapters.append({
                "heading": shapes[i % len(shapes)] if i else "Opening: A Question of Evidence",
                "focus": f"section {i + 1} of {count}",
                "target_words": int(payload.get("words_per_chapter", 1100)),
            })
        return {"chapters": chapters}

    def _chapter_prose(self, payload: dict[str, Any]) -> str:
        heading = payload.get("heading", "Untitled Section")
        facts = payload.get("facts") or []
        target = int(payload.get("target_words", 1100))
        rng = self._rng(f"prose:{heading}:{target}")

        sentences: list[str] = [rng.choice(_OPENERS)]
        fact_i = 0
        while len(" ".join(sentences).split()) < target:
            if facts and fact_i < len(facts) * 3:
                fact = facts[fact_i % len(facts)]
                statement = str(fact.get("statement", "")).rstrip(".")
                confidence = fact.get("confidence", "uncertain")
                fact_i += 1
                if not statement:
                    continue
                if confidence == "established":
                    sentences.append(f"{rng.choice(_CONNECTIVES)} {statement}.")
                elif confidence == "disputed":
                    sentences.append(
                        f"{rng.choice(_CONNECTIVES)} {statement} -- "
                        f"{rng.choice(_HEDGES)}."
                    )
                else:
                    sentences.append(
                        f"It has been argued that {statement}, {rng.choice(_HEDGES)}."
                    )
            else:
                sentences.append(
                    f"{rng.choice(_CONNECTIVES)} the picture assembled here rests on "
                    f"material that was never intended as a record, and reading it "
                    f"requires accepting how much has been lost."
                )
        sentences.append(rng.choice(_CLOSERS))
        return " ".join(sentences)

    def _chapter_revise(self, payload: dict[str, Any]) -> str:
        """Soften the flagged sentences, keeping everything else intact.

        A revision must be a *revision*. Regenerating the passage from
        scratch is what makes a second fact-check pass come back worse than
        the first, so this edits in place and leaves untouched prose alone.
        """
        text = str(payload.get("text", ""))
        problems = payload.get("problems") or []
        flagged = [str(p.get("text", "")).strip() for p in problems
                   if str(p.get("text", "")).strip()]

        sentences = re.split(r"(?<=[.!?])\s+", text)
        out = []
        for sentence in sentences:
            stripped = sentence.strip()
            if any(f[:60] and f[:60] in stripped for f in flagged):
                # Attribute rather than assert; no new facts, no new numbers.
                softened = stripped[0].lower() + stripped[1:] if stripped else stripped
                out.append(f"One account suggests that {softened}")
            else:
                out.append(sentence)
        return " ".join(out).strip()

    def _claim_extraction(self, payload: dict[str, Any]) -> Any:
        text = payload.get("text", "")
        claims = []
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            s = sentence.strip()
            # Only sentences carrying a number, a date or a capitalised name
            # are treated as factual assertions worth checking.
            if len(s.split()) >= 6 and re.search(r"\d|\b[A-Z][a-z]{3,}", s):
                claims.append({"text": s[:500], "kind": "assertion"})
            if len(claims) >= int(payload.get("limit", 40)):
                break
        return {"claims": claims}

    def _claim_verdicts(self, payload: dict[str, Any]) -> Any:
        claims = payload.get("claims") or []
        facts = " ".join(str(f.get("statement", "")).lower()
                         for f in (payload.get("facts") or []))
        out = []
        for claim in claims:
            text = str(claim.get("text", ""))
            # Overlap with the research package, computed the same way the
            # real fact-checker's evidence retrieval does: token containment.
            tokens = {w for w in re.findall(r"[a-z]{4,}", text.lower())}
            hit = sum(1 for t in tokens if t in facts)
            ratio = hit / max(len(tokens), 1)
            if ratio >= 0.5:
                verdict, note = "supported", "offline: strong token overlap with sources"
            elif ratio >= 0.25:
                verdict, note = "uncertain", "offline: partial overlap with sources"
            else:
                verdict, note = "unsupported", "offline: no overlap with sources"
            out.append({"id": claim.get("id"), "verdict": verdict, "note": note,
                        "source_ids": claim.get("candidate_source_ids", [])[:3]})
        return {"verdicts": out}

    def _fact_extraction(self, payload: dict[str, Any]) -> Any:
        """Turn source sentences into facts, without inventing any.

        Every statement returned is a sentence that was actually in the
        passage. That keeps the offline path honest about the one property
        that matters downstream: a fact must be traceable to its source.
        """
        text = str(payload.get("text", ""))
        limit = int(payload.get("limit", 8))
        rng = self._rng(f"facts:{payload.get('title','')}")
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text)
                     if len(s.split()) >= 8]
        facts = []
        for sentence in sentences[: limit * 2]:
            entities = re.findall(r"\b[A-Z][a-z]{3,}\b", sentence)[:6]
            confidence = rng.choices(
                ["established", "interpretation", "disputed", "uncertain"],
                weights=[6, 2, 1, 2],
            )[0]
            facts.append({
                "statement": sentence[:800],
                "excerpt": sentence[:300],
                "confidence": confidence,
                "entities": entities,
            })
            if len(facts) >= limit:
                break
        return {"facts": facts}

    def _semantic_duplicate(self, payload: dict[str, Any]) -> Any:
        """Offline stand-in: token containment, no synonym knowledge.

        This cannot catch "Viking" against "Norse" -- that is precisely what
        a real model is for. It is here so the dry run exercises the gate's
        plumbing, and it never claims a duplicate it cannot justify.
        """
        proposed = {w for w in re.findall(r"[a-z]{4,}", str(payload.get("proposed","")).lower())}
        for title in payload.get("existing", []):
            other = {w for w in re.findall(r"[a-z]{4,}", str(title).lower())}
            if proposed and other and len(proposed & other) / len(proposed | other) > 0.8:
                return {"duplicate_of": title, "reason": "offline: near-identical tokens"}
        return {"duplicate_of": None, "reason": "offline: no lexical duplicate"}

    def _metadata(self, payload: dict[str, Any]) -> Any:
        title = payload.get("title", "A History")
        chapters = payload.get("chapters") or []
        return {
            "title": f"{title} | A Full Documentary",
            "description": (
                f"A long-form documentary on {title}.\n\n"
                "This film is assembled from public archival material and cited "
                "sources. Where the evidence is disputed, the narration says so."
            ),
            "tags": ["history", "documentary", "ancient history", "archaeology"],
            "thumbnail_text": title.split(":")[0][:28],
            "chapter_titles": [c.get("heading", f"Part {i+1}")
                               for i, c in enumerate(chapters)],
        }
