"""Topic discovery, normalisation and the originality guard.

The guard answers one question: would this documentary duplicate one we have
already made? It is deliberately conservative, because the cost of the two
mistakes is asymmetric. Rejecting a good topic costs one candidate out of a
batch. Accepting a near-duplicate costs ninety minutes of render time and
puts two nearly identical videos on a channel, which is the thing that makes
an automated channel look automated.

Similarity is computed three ways and the strongest signal wins:

* token overlap (Jaccard) catches reordering -- "The Siege of Constantinople"
  against "Constantinople: The Siege";
* sequence ratio catches near-identical phrasing;
* subject containment catches a subset -- "Roman Concrete" inside "Roman
  Concrete and the Harbour at Caesarea".

No single one of these catches all three cases, which is why all three run.
"""

from __future__ import annotations

import difflib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

from ..errors import DuplicateTopic, Permanent
from ..logging import get_logger

log = get_logger(__name__)

# Words that carry no distinguishing weight in a documentary title.
_STOPWORDS = frozenset("""
a an the of and or in on at to for from with without into over under between
their its his her our your this that these those was were is are be been being
how why what when where who which than then but as by if it
story history documentary full complete part episode ancient
""".split())

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")

# Order matters: the longest suffix that applies is stripped first, so
# "settlements" reaches "settlement" rather than stopping at "settlement" +
# a stray "s". Crude by design -- a real stemmer is a dependency and a
# behaviour change, and this only needs to make plural and gerund forms of
# the same word collide.
_SUFFIXES = ("ements", "ations", "ement", "ation", "ings", "ies", "ers",
             "ing", "ed", "es", "s")


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            base = word[: -len(suffix)]
            if suffix == "ies":
                return base + "y"
            return base
    return word


def normalize_title(title: str, *, stem: bool = False) -> str:
    """Fold a display title to its comparable form.

    ``stem=True`` additionally collapses plural and gerund forms. It is used
    for *comparison* only, never for the stored ``normalized_title``: the
    stored form is a uniqueness key an operator may have to read, and
    "settlement greenland norse" is legible where a stemmed variant is not.
    """
    folded = unicodedata.normalize("NFKD", title)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    folded = _PUNCT.sub(" ", folded.lower())
    words = [w for w in _SPACES.split(folded) if w and w not in _STOPWORDS]
    if stem:
        words = [_stem(w) for w in words]
    return " ".join(words)


def tokens(title: str) -> set[str]:
    return {w for w in normalize_title(title).split() if len(w) > 2}


def similarity(a: str, b: str) -> float:
    """0..1. The strongest of three signals; see the module docstring."""
    na, nb = normalize_title(a, stem=True), normalize_title(b, stem=True)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0

    ta, tb = set(na.split()), set(nb.split())
    jaccard = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    sequence = difflib.SequenceMatcher(None, na, nb).ratio()
    # Containment: how much of the smaller title is inside the larger.
    smaller, larger = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    containment = len(smaller & larger) / len(smaller) if smaller else 0.0

    return max(jaccard, sequence, containment)


@dataclass
class OriginalityVerdict:
    original: bool
    score: float
    closest_title: str | None
    closest_id: int | None

    def reason(self) -> str:
        if self.original:
            return f"closest existing topic scores {self.score:.2f}"
        return (
            f"too similar ({self.score:.2f}) to existing topic "
            f"{self.closest_id}: {self.closest_title!r}"
        )


def check_originality(
    title: str, existing: Iterable[dict[str, Any]], threshold: float
) -> OriginalityVerdict:
    best_score = 0.0
    best: dict[str, Any] | None = None
    for row in existing:
        score = similarity(title, row.get("title", ""))
        if score > best_score:
            best_score, best = score, row
    return OriginalityVerdict(
        original=best_score < threshold,
        score=best_score,
        closest_title=(best or {}).get("title"),
        closest_id=(best or {}).get("id"),
    )


SEMANTIC_PROMPT = """\
TASK: semantic_duplicate
PAYLOAD: {payload}

Would a viewer consider these two documentaries to cover the same subject?

PROPOSED: {proposed}

EXISTING:
{existing}

Two titles are the same subject if a viewer who watched one would feel the
other was a repeat -- including when they use different names for the same
people, places or period ("Viking" and "Norse", "Constantinople" and
"Byzantium", "the Great War" and "the First World War").

Different aspects of a broad subject are NOT duplicates: "Roman roads" and
"Roman naval logistics" are distinct documentaries.

Return JSON: {{"duplicate_of": <existing title or null>, "reason": str}}
"""


def semantic_duplicate(
    llm, proposed: str, existing: list[dict[str, Any]], *, limit: int = 25
) -> tuple[bool, str]:
    """Second gate: catch synonym duplicates the lexical score cannot see.

    Lexical similarity scores "Viking Settlement of Greenland" against "Norse
    Greenland Settlements" at well under any usable threshold, because the
    words genuinely differ. Only something that knows the two words name the
    same people can catch it.

    A failure here is not fatal. The lexical gate has already run; if the
    model is unreachable we accept its verdict as "not a duplicate" and say
    so in the log, rather than blocking production on an advisory check.
    """
    if not existing:
        return False, "no existing topics"
    titles = [str(r.get("title", "")) for r in existing[:limit] if r.get("title")]
    if not titles:
        return False, "no existing titles"

    prompt = SEMANTIC_PROMPT.format(
        payload=json.dumps({"proposed": proposed, "existing": titles},
                           default=str),
        proposed=proposed,
        existing="\n".join(f"- {t}" for t in titles),
    )
    try:
        response = llm.generate_json(prompt)
    except Exception as exc:  # noqa: BLE001 - advisory check, never fatal
        log.warning("semantic duplicate check unavailable",
                    extra={"error": str(exc)})
        return False, "semantic check unavailable"

    match = (response or {}).get("duplicate_of")
    if isinstance(match, str) and match.strip():
        return True, f"model judged it a duplicate of {match!r}: " \
                     f"{(response or {}).get('reason', '')}"
    return False, "model found no duplicate"


def text_overlap(a: str, b: str, *, shingle: int = 8) -> float:
    """Fraction of ``a``'s word-shingles that also appear in ``b``.

    Used on finished scripts rather than titles. Two documentaries can have
    entirely different titles and still reuse the same opening paragraph,
    which is exactly the "repeated openings" failure that makes a channel
    feel machine-made.
    """
    wa, wb = a.split(), b.split()
    if len(wa) < shingle or len(wb) < shingle:
        return 0.0
    sa = {" ".join(wa[i:i + shingle]) for i in range(len(wa) - shingle + 1)}
    sb = {" ".join(wb[i:i + shingle]) for i in range(len(wb) - shingle + 1)}
    if not sa:
        return 0.0
    return len(sa & sb) / len(sa)


DISCOVERY_PROMPT = """\
TASK: topic_candidates
PAYLOAD: {payload}

You are a commissioning editor for a long-form history documentary channel.
Propose {count} candidate subjects for a {minutes}-minute documentary.

Requirements:
- Each must be substantial enough to sustain {minutes} minutes of narration
  grounded in real evidence.
- Prefer subjects with surviving material or documentary record.
- Avoid subjects already covered in the EXCLUDE list.
- Avoid active political conflict, religious polemic, and contested
  contemporary history.
{extra}

Return JSON: {{"candidates": [{{"title": str, "subject": str, "period": str,
"region": str, "category": str, "angle": str, "rationale": str}}]}}
"""


def discover(
    llm, *, count: int, minutes: int, existing: list[dict[str, Any]],
    preferred_eras: list[str], banned: list[str],
) -> list[dict[str, Any]]:
    """Ask the model for candidate subjects. Returns raw, unvalidated rows."""
    extra_lines = []
    if preferred_eras:
        extra_lines.append(f"- Prefer these eras or regions: {', '.join(preferred_eras)}.")
    if banned:
        extra_lines.append(f"- Do not propose anything involving: {', '.join(banned)}.")

    payload = {
        "count": count,
        "minutes": minutes,
        "exclude": [r.get("title") for r in existing][:120],
        "preferred_eras": preferred_eras,
        "banned": banned,
    }
    prompt = DISCOVERY_PROMPT.format(
        payload=json.dumps(payload, default=str), count=count, minutes=minutes,
        extra="\n".join(extra_lines),
    )
    response = llm.generate_json(prompt)
    candidates = (response or {}).get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise Permanent("topic discovery returned no candidates")
    out = []
    for row in candidates:
        if not isinstance(row, dict) or not row.get("title"):
            continue
        out.append(row)
    if not out:
        raise Permanent("topic discovery returned no usable candidates")
    return out


def is_banned(title: str, banned: list[str]) -> bool:
    lowered = title.lower()
    return any(b.strip().lower() in lowered for b in banned if b.strip())


def select(
    candidates: list[dict[str, Any]], existing: list[dict[str, Any]],
    *, threshold: float, banned: list[str],
) -> tuple[dict[str, Any], OriginalityVerdict]:
    """Pick the first candidate that is neither banned nor a near-duplicate."""
    rejections: list[str] = []
    for candidate in candidates:
        title = str(candidate["title"])
        if is_banned(title, banned):
            rejections.append(f"{title!r}: matches a banned topic")
            continue
        verdict = check_originality(title, existing, threshold)
        if not verdict.original:
            rejections.append(f"{title!r}: {verdict.reason()}")
            continue
        return candidate, verdict
    raise DuplicateTopic(
        "every candidate was rejected:\n  " + "\n  ".join(rejections[:10])
    )
