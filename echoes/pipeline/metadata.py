"""Title, description, chapters and tags.

YouTube enforces limits that are easy to discover the hard way, so they are
enforced here instead:

* title at most 100 characters, and ``<`` / ``>`` are rejected outright;
* description at most 5000 characters;
* tags at most 500 characters in total, counted across all tags;
* chapter markers only activate if the first is exactly ``0:00``, there are
  at least three, and each runs at least ten seconds.

The last one is the one that silently does nothing when it is wrong: an
almost-correct chapter list produces a video with no chapters and no error.

The description carries the source list and the image attributions. That is
not decoration -- several of the licences this system accepts require
attribution, and the description is where it is given.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..errors import Permanent
from ..logging import get_logger

log = get_logger(__name__)

TITLE_MAX = 100
DESCRIPTION_MAX = 5000
TAGS_TOTAL_MAX = 500
MIN_CHAPTERS = 3
MIN_CHAPTER_S = 10.0

PROMPT = """\
TASK: metadata
PAYLOAD: {payload}

Write the publishing metadata for a {minutes}-minute history documentary.

WORKING TITLE: {title}
CHAPTERS:
{chapters}

Rules:
- The title must describe the subject accurately. No clickbait, no
  superlatives, no "you won't believe", no false claims of exclusivity or
  accuracy. At most {title_max} characters.
- The description opens with two or three sentences of plain summary, then a
  sentence saying where the evidence comes from and that disputed points are
  marked as disputed in the narration.
- Do not promise completeness or definitiveness.
- Tags: 8 to 15 short topical terms.

Return JSON: {{"title": str, "description": str, "tags": [str],
"thumbnail_text": str}}
"""

_BANNED_TITLE = (
    re.compile(r"you won'?t believe", re.I),
    re.compile(r"\bshocking\b", re.I),
    re.compile(r"most accurate|definitive account|the real truth", re.I),
    re.compile(r"they don'?t want you", re.I),
    re.compile(r"\bgone wrong\b", re.I),
)


@dataclass
class Metadata:
    title: str
    description: str
    tags: list[str]
    chapters: list[dict[str, Any]] = field(default_factory=list)
    category_id: str = "27"
    language: str = "en"
    thumbnail_text: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "title": self.title, "description": self.description,
            "tags": self.tags, "chapters": self.chapters,
        }


def timestamp(seconds: float) -> str:
    """YouTube chapter timestamp. Hours only when there are hours."""
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def build_chapters(timings: Sequence[Any]) -> list[dict[str, Any]]:
    """Turn measured chapter spans into markers YouTube will accept.

    Chapters shorter than ten seconds are merged into the previous one rather
    than dropped, because dropping one would leave a gap and YouTube requires
    the list to be contiguous from 0:00.
    """
    rows: list[dict[str, Any]] = []
    for timing in sorted(timings, key=lambda t: t.start_s):
        heading = str(timing.heading).strip() or f"Part {len(rows) + 1}"
        start = float(timing.start_s)
        duration = float(timing.duration_s)
        if rows and duration < MIN_CHAPTER_S:
            rows[-1]["duration_s"] += duration
            continue
        rows.append({"start_s": start, "duration_s": duration, "title": heading})

    if not rows:
        return []
    # The first marker must be exactly zero or none of them activate.
    rows[0]["start_s"] = 0.0
    for row in rows:
        row["timestamp"] = timestamp(row["start_s"])

    # A chapter list YouTube will not accept is worse than none: it renders
    # as a wall of timestamps in the description with no chapter bar. A short
    # documentary legitimately has too few sections to qualify, so drop the
    # list rather than treat it as a failure.
    ok, why = chapters_valid(rows)
    if not ok:
        log.info("not publishing chapter markers", extra={"reason": why})
        return []
    return rows


def chapters_valid(rows: Sequence[dict[str, Any]]) -> tuple[bool, str]:
    if len(rows) < MIN_CHAPTERS:
        return False, f"need at least {MIN_CHAPTERS} chapters, have {len(rows)}"
    if abs(float(rows[0]["start_s"])) > 0.001:
        return False, "the first chapter must start at 0:00"
    for index in range(1, len(rows)):
        if float(rows[index]["start_s"]) <= float(rows[index - 1]["start_s"]):
            return False, f"chapter {index} does not start after chapter {index - 1}"
    for index, row in enumerate(rows):
        if float(row["duration_s"]) < MIN_CHAPTER_S:
            return False, f"chapter {index} is shorter than {MIN_CHAPTER_S:.0f}s"
    return True, "ok"


def clean_title(raw: str, fallback: str) -> str:
    title = re.sub(r"\s+", " ", str(raw or "")).strip().strip('"')
    # YouTube rejects angle brackets in titles outright.
    title = title.replace("<", "").replace(">", "")
    if not title:
        title = fallback
    for pattern in _BANNED_TITLE:
        if pattern.search(title):
            log.warning("model proposed a clickbait title; using the topic title",
                        extra={"proposed": title[:120]})
            title = fallback
            break
    if len(title) > TITLE_MAX:
        cut = title[:TITLE_MAX]
        # Prefer a word boundary over a hard truncation mid-word.
        if " " in cut[-20:]:
            cut = cut[:cut.rfind(" ")]
        title = cut.rstrip(" ,;:-—")
    return title


def limit_tags(tags: Sequence[str], total_max: int = TAGS_TOTAL_MAX) -> list[str]:
    """Trim tags to YouTube's combined-length budget.

    The limit applies to the total across all tags, not to each one, and the
    API rejects the whole request when it is exceeded.
    """
    out: list[str] = []
    used = 0
    seen: set[str] = set()
    for tag in tags:
        cleaned = re.sub(r"[^\w\s\-]", "", str(tag)).strip()
        if not cleaned or cleaned.lower() in seen:
            continue
        # Commas separate tags in the API's accounting, so each costs +1.
        cost = len(cleaned) + 1
        if used + cost > total_max:
            continue
        seen.add(cleaned.lower())
        out.append(cleaned)
        used += cost
    return out


def build_description(
    summary: str, chapters: Sequence[dict[str, Any]],
    sources: Sequence[dict[str, Any]], attributions: Sequence[str],
    *, channel: str,
) -> str:
    """Assemble the description, trimming from the least essential end.

    Order matters: summary, then chapters, then sources, then image credits.
    If the 5000-character limit bites, credits are truncated with a note
    rather than chapters being lost -- chapters are functional, and a
    truncation notice keeps the attribution honest about being incomplete.
    """
    parts: list[str] = [summary.strip(), ""]

    if chapters:
        parts.append("Chapters")
        for row in chapters:
            parts.append(f"{row['timestamp']} {row['title']}")
        parts.append("")

    if sources:
        parts.append("Principal sources")
        for source in sources[:14]:
            title = str(source.get("title") or "Untitled")[:90]
            url = str(source.get("url") or "")
            parts.append(f"- {title} - {url}" if url else f"- {title}")
        if len(sources) > 14:
            parts.append(f"- and {len(sources) - 14} further sources, listed in "
                         f"the research package for this documentary")
        parts.append("")

    if attributions:
        parts.append("Image credits")
        for line in attributions[:25]:
            parts.append(f"- {line[:180]}")
        if len(attributions) > 25:
            # Several accepted licences require attribution, so an omission
            # must be stated rather than left to look like a complete list.
            parts.append(
                f"- and {len(attributions) - 25} further images; the complete "
                f"per-image credit list is posted in the pinned comment."
            )
        parts.append("")

    parts.append(
        "Where the historical evidence is disputed, the narration says so. "
        "Corrections are welcome."
    )
    parts.append(f"— {channel}")

    text = "\n".join(parts).strip()
    if len(text) <= DESCRIPTION_MAX:
        return text

    # Rebuild without credits, then note the omission.
    trimmed = [p for p in parts]
    if "Image credits" in trimmed:
        start = trimmed.index("Image credits")
        end = start + 1
        while end < len(trimmed) and trimmed[end].startswith("- "):
            end += 1
        trimmed = trimmed[:start] + [
            "Image credits: full per-image attribution is listed in the "
            "pinned comment (the description limit does not fit it)."
        ] + trimmed[end:]
    text = "\n".join(trimmed).strip()
    return text[:DESCRIPTION_MAX].rstrip()


def generate(
    llm, topic_title: str, chapters: Sequence[dict[str, Any]],
    sources: Sequence[dict[str, Any]], attributions: Sequence[str],
    *, minutes: float, channel: str, category_id: str = "27",
    language: str = "en",
) -> Metadata:
    payload = {
        "title": topic_title, "minutes": round(minutes, 1),
        "chapters": [{"heading": c["title"]} for c in chapters],
        "title_max": TITLE_MAX,
    }
    prompt = PROMPT.format(
        payload=json.dumps(payload, default=str), minutes=round(minutes),
        title=topic_title,
        chapters="\n".join(f"- {c['title']}" for c in chapters),
        title_max=TITLE_MAX,
    )
    try:
        response = llm.generate_json(prompt) or {}
    except Exception as exc:  # noqa: BLE001
        # Metadata is required, but a model outage should not destroy a
        # finished render. Fall back to the topic title and a plain summary.
        log.warning("metadata generation failed; using fallbacks",
                    extra={"error": str(exc)})
        response = {}

    title = clean_title(response.get("title"), topic_title)
    summary = str(response.get("description") or
                  f"A {round(minutes)}-minute documentary on {topic_title}.")
    tags = limit_tags(response.get("tags") or ["history", "documentary"])
    description = build_description(summary, chapters, sources, attributions,
                                    channel=channel)

    if not title:
        raise Permanent("could not produce a title")

    log.info("metadata built",
             extra={"title_len": len(title), "description_len": len(description),
                    "tags": len(tags), "chapters": len(chapters)})
    return Metadata(title=title, description=description, tags=tags,
                    chapters=list(chapters), category_id=category_id,
                    language=language,
                    thumbnail_text=str(response.get("thumbnail_text") or title))
