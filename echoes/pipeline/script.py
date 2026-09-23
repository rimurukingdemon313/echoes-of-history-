"""The script engine.

Two things make this harder than "ask a model for a documentary".

**Length is a measurement, not a hope.** A 90-minute documentary at 150 words
per minute is 13,500 words. No model produces that in one response, and one
that is asked to will either stop early or pad. So the script is built
chapter by chapter against a word budget, and the result is checked against
the budget before anything is narrated. Padding is not an acceptable way to
reach length: each chapter is given its own slice of the research package, so
running long means covering more evidence rather than restating the same
evidence at greater length.

**Every chapter is written from sources.** Facts are distributed across
chapters before generation, and a chapter is only asked to write from the
facts it was given. A chapter with no facts is not written -- it is dropped,
because its only possible content would be invention.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..errors import Permanent
from ..logging import get_logger
from .research import fact_view
from .topics import text_overlap

log = get_logger(__name__)

OUTLINE_PROMPT = """\
TASK: chapter_outline
PAYLOAD: {payload}

Plan a {minutes}-minute documentary titled "{title}".

It needs {count} chapters, each about {words_per_chapter} words of narration.
Structure it so a listener with no prior knowledge can follow it: establish
the setting and the state of the evidence before the narrative, and let the
consequences close it.

The available research covers these themes:
{themes}

Constraints:
- Chapters must be distinct. Do not plan two chapters that would cover the
  same ground.
- Do not plan a chapter the research cannot support.
- No cliffhangers, no teasers, no "but first". This is a calm documentary.

Return JSON: {{"chapters": [{{"heading": str, "focus": str,
"target_words": int}}]}}
"""

PROSE_PROMPT = """\
TASK: chapter_prose
PAYLOAD: {payload}

Write the narration for one chapter of the documentary "{title}".

CHAPTER: {heading}
FOCUS: {focus}
LENGTH: about {target_words} words. This is a floor as much as a ceiling --
a chapter materially shorter than this leaves the documentary short.

Write ONLY from the facts below. Each fact carries a confidence label; your
prose must preserve it:
  established     - state it plainly
  interpretation  - attribute it ("historians have argued", "the reading is")
  disputed        - say the sources disagree, and give both figures
  uncertain       - hedge it explicitly ("the evidence suggests", "one account")

FACTS:
{facts}
{conflicts}
Rules:
- Never invent a date, a name, a number or a quotation. If a detail is not in
  the facts, it does not go in the script.
- Never fabricate a quotation, even an unattributed one.
- Do not open with a rhetorical question or a teaser.
- Do not address the viewer, ask them to subscribe, or refer to the video.
- Write continuous prose for narration: no headings, no bullet points, no
  stage directions.
- Calm, measured register. This is listened to slowly.

Write the chapter now.
"""


@dataclass
class Chapter:
    heading: str
    focus: str
    target_words: int
    body: str = ""
    fact_indices: list[int] = field(default_factory=list)

    @property
    def word_count(self) -> int:
        return len(self.body.split())


@dataclass
class ScriptResult:
    chapters: list[Chapter]
    word_count: int
    estimated_s: float
    shortfall_words: int

    @property
    def estimated_minutes(self) -> float:
        return self.estimated_s / 60.0


def plan_budget(target_minutes: float, wpm: float, *, chapter_minutes: float = 7.5
                ) -> tuple[int, int, int]:
    """Return ``(total_words, chapter_count, words_per_chapter)``.

    Chapters of roughly seven or eight minutes are short enough that a model
    can hold one in a single response without truncating, and long enough
    that the documentary does not read as a list.

    The floors below scale with the target rather than being constants. An
    earlier version hard-coded "at least 6 chapters of at least 400 words",
    which for a short target produced a script several times longer than
    asked for -- the budget has to be able to shrink, or the only length the
    system can actually hit is the one it was tuned for.
    """
    total_words = max(1, int(target_minutes * wpm))
    count = max(1, round(target_minutes / chapter_minutes))
    # Prefer more chapters on a long film, but never more than the word
    # budget can fill at a sensible chapter length.
    if total_words >= 3000:
        count = max(6, count)
    per_chapter = total_words // count
    floor = min(400, total_words)
    if per_chapter < floor:
        count = max(1, total_words // floor)
        per_chapter = total_words // count
    return total_words, count, per_chapter


def _theme_summary(facts: list[dict[str, Any]], limit: int = 30) -> str:
    entities: dict[str, int] = {}
    for fact in facts:
        for entity in fact.get("entities", []):
            key = str(entity).strip()
            if len(key) > 3:
                entities[key] = entities.get(key, 0) + 1
    ranked = sorted(entities.items(), key=lambda kv: -kv[1])[:limit]
    return ", ".join(name for name, _ in ranked) or "(no named entities extracted)"


def outline(
    llm, title: str, facts: list[dict[str, Any]], *, minutes: int, wpm: float
) -> list[Chapter]:
    total_words, count, per_chapter = plan_budget(minutes, wpm)
    payload = {
        "title": title, "minutes": minutes, "chapter_count": count,
        "words_per_chapter": per_chapter, "total_words": total_words,
        "themes": _theme_summary(facts),
    }
    prompt = OUTLINE_PROMPT.format(
        payload=json.dumps(payload, default=str), minutes=minutes, title=title,
        count=count,
        words_per_chapter=per_chapter, themes=_theme_summary(facts),
    )
    response = llm.generate_json(prompt)
    rows = (response or {}).get("chapters")
    if not isinstance(rows, list) or not rows:
        raise Permanent("outline generation returned no chapters")

    chapters: list[Chapter] = []
    seen: set[str] = set()
    for row in rows:
        heading = str((row or {}).get("heading", "")).strip()
        if not heading:
            continue
        key = heading.lower()
        if key in seen:
            # A duplicated heading means two chapters covering one subject.
            continue
        seen.add(key)
        chapters.append(Chapter(
            heading=heading,
            focus=str(row.get("focus", "")).strip(),
            target_words=int(row.get("target_words") or per_chapter),
        ))
    if not chapters:
        raise Permanent("outline generation produced no usable chapters")
    return chapters


def _relevance(chapter: Chapter, fact: dict[str, Any]) -> float:
    haystack = f"{chapter.heading} {chapter.focus}".lower()
    words = {w for w in re.findall(r"[a-z]{4,}", haystack)}
    if not words:
        return 0.0
    text = (fact["statement"] + " " + " ".join(fact.get("entities", []))).lower()
    fact_words = {w for w in re.findall(r"[a-z]{4,}", text)}
    if not fact_words:
        return 0.0
    return len(words & fact_words) / len(words | fact_words)


def assign_facts(chapters: list[Chapter], facts: list[dict[str, Any]],
                 *, min_per_chapter: int = 3) -> None:
    """Distribute facts across chapters, by relevance, without duplication.

    Every fact is placed exactly once. A fact used in two chapters is the
    mechanism by which a documentary repeats itself, and repetition is the
    failure mode this system is most at risk of, because it is the easiest
    way to reach a word count.
    """
    if not facts:
        return
    scored: list[tuple[float, int, int]] = []
    for fact_index, fact in enumerate(facts):
        for chapter_index, chapter in enumerate(chapters):
            scored.append((_relevance(chapter, fact), fact_index, chapter_index))
    scored.sort(key=lambda t: -t[0])

    placed: set[int] = set()
    for score, fact_index, chapter_index in scored:
        if fact_index in placed:
            continue
        chapter = chapters[chapter_index]
        # Leave room: a chapter that hoovers up every fact starves the rest.
        if len(chapter.fact_indices) >= max(min_per_chapter * 3, 12):
            continue
        chapter.fact_indices.append(fact_index)
        placed.add(fact_index)

    # Round-robin whatever relevance could not place, so no fact is wasted.
    leftovers = [i for i in range(len(facts)) if i not in placed]
    for position, fact_index in enumerate(leftovers):
        chapters[position % len(chapters)].fact_indices.append(fact_index)


def _format_facts(facts: list[dict[str, Any]], indices: list[int]) -> str:
    lines = []
    for n, index in enumerate(indices, start=1):
        fact = facts[index]
        lines.append(f"{n}. [{fact['confidence']}] {fact['statement']}")
    return "\n".join(lines)


def _format_conflicts(conflicts: list[dict[str, Any]], chapter: Chapter) -> str:
    relevant = [
        c for c in conflicts
        if str(c.get("entity", "")).lower() in
        f"{chapter.heading} {chapter.focus}".lower()
    ]
    if not relevant:
        return ""
    lines = ["", "SOURCES DISAGREE on the following. Say so explicitly:"]
    for conflict in relevant[:6]:
        lines.append(
            f"- {conflict['entity']}: {conflict['kind']} given variously as "
            f"{', '.join(str(v) for v in conflict['values'])}"
        )
    return "\n".join(lines) + "\n"


def write_chapter(
    llm, title: str, chapter: Chapter, facts: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
) -> str:
    selected = [facts[i] for i in chapter.fact_indices]
    payload = {
        "title": title, "heading": chapter.heading, "focus": chapter.focus,
        "target_words": chapter.target_words,
        "facts": [fact_view(f) for f in selected],
    }
    prompt = PROSE_PROMPT.format(
        payload=json.dumps(payload, default=str), title=title,
        heading=chapter.heading,
        focus=chapter.focus, target_words=chapter.target_words,
        facts=_format_facts(facts, chapter.fact_indices),
        conflicts=_format_conflicts(conflicts, chapter),
    )
    return llm.generate(prompt, max_tokens=None).strip()


def generate(
    llm, title: str, facts: list[dict[str, Any]], conflicts: list[dict[str, Any]],
    *, minutes: int, wpm: float, min_words: int,
) -> ScriptResult:
    chapters = outline(llm, title, facts, minutes=minutes, wpm=wpm)
    assign_facts(chapters, facts)

    written: list[Chapter] = []
    for chapter in chapters:
        if not chapter.fact_indices:
            # Nothing to write it from. Dropping it is the honest move; the
            # alternative is a chapter of invention.
            log.warning("dropping chapter with no supporting facts",
                        extra={"heading": chapter.heading})
            continue
        chapter.body = write_chapter(llm, title, chapter, facts, conflicts)
        if chapter.word_count < 50:
            log.warning("dropping chapter that came back nearly empty",
                        extra={"heading": chapter.heading,
                               "words": chapter.word_count})
            continue
        written.append(chapter)
        log.info("chapter written",
                 extra={"heading": chapter.heading, "words": chapter.word_count,
                        "target": chapter.target_words})

    if not written:
        raise Permanent("no chapters could be written from the research package")

    total = sum(c.word_count for c in written)
    return ScriptResult(
        chapters=written,
        word_count=total,
        estimated_s=total / (wpm / 60.0),
        shortfall_words=max(0, min_words - total),
    )


def internal_repetition(chapters: list[Chapter], *, shingle: int = 8) -> float:
    """Highest overlap between any two chapters of this script.

    A high value means the documentary restates itself, which is the specific
    failure that padding to a word count produces.
    """
    worst = 0.0
    for i in range(len(chapters)):
        for j in range(i + 1, len(chapters)):
            worst = max(worst, text_overlap(chapters[i].body, chapters[j].body,
                                            shingle=shingle))
    return worst


_FORBIDDEN = (
    (re.compile(r'"[^"]{40,}"'), "long quoted passage (possible fabricated quotation)"),
    (re.compile(r"\b(subscribe|like and share|hit the bell|comment below)\b", re.I),
     "channel-promotion phrasing"),
    (re.compile(r"\b(in this video|in today's video|welcome back)\b", re.I),
     "video self-reference"),
    (re.compile(r"\b(you won't believe|shocking truth|they don't want you to know)\b", re.I),
     "clickbait phrasing"),
)


def style_violations(text: str) -> list[str]:
    """Editorial rules that are cheap to check mechanically."""
    found = []
    for pattern, label in _FORBIDDEN:
        if pattern.search(text):
            found.append(label)
    return found
