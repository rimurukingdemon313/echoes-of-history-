"""Behaviour of the production stages, built on scenarios with known answers."""

from __future__ import annotations

import wave

import pytest

from echoes.pipeline import narration as narration_mod
from echoes.errors import FactCheckFailed
from echoes.pipeline.factcheck import ClaimCheck, retrieve_evidence
from echoes.pipeline.factcheck import run as factcheck_run
from echoes.pipeline.metadata import (
    build_chapters, chapters_valid, clean_title, limit_tags, timestamp,
    build_description, TAGS_TOTAL_MAX, TITLE_MAX, DESCRIPTION_MAX,
)
from echoes.pipeline.narration import ChapterTiming, gap_for, plan_chunks
from echoes.pipeline.research import detect_conflicts
from echoes.pipeline.script import plan_budget
from echoes.providers.tts.base import split_into_chunks
from echoes.providers.tts.silent import SilentProvider


# ----------------------------------------------------------------- research
def test_conflicting_figures_for_the_same_assertion_are_reported():
    facts = [
        {"statement": "The harbour at Caesarea was built by Herod between 22 and 10 BCE.",
         "entities": ["Caesarea"], "source_index": 0},
        {"statement": "The harbour at Caesarea was built by Herod beginning in 15 BCE.",
         "entities": ["Caesarea"], "source_index": 1},
    ]
    conflicts = detect_conflicts(facts)
    assert any(c["kind"] == "date" for c in conflicts)


def test_unrelated_numbers_about_one_entity_are_not_a_conflict():
    """A build date and an excavation date are both numbers; neither disagrees."""
    facts = [
        {"statement": "The harbour at Caesarea was built by Herod in 15 BCE.",
         "entities": ["Caesarea"], "source_index": 0},
        {"statement": "Excavations at Caesarea recovered timber formwork in the 1990 season.",
         "entities": ["Caesarea"], "source_index": 1},
    ]
    assert detect_conflicts(facts) == []


def test_one_source_disagreeing_with_itself_is_not_a_conflict():
    facts = [
        {"statement": "The wall at Dura was rebuilt in 250 CE.", "entities": ["Dura"],
         "source_index": 3},
        {"statement": "The wall at Dura was rebuilt in 256 CE.", "entities": ["Dura"],
         "source_index": 3},
    ]
    assert detect_conflicts(facts) == []


def test_a_grouped_quantity_is_not_read_as_a_year():
    facts = [
        {"statement": "The annual shipment to Rome exceeded 150,000 tonnes each year.",
         "entities": ["Rome"], "source_index": 0},
        {"statement": "The annual shipment to Rome exceeded 220,000 tonnes each year.",
         "entities": ["Rome"], "source_index": 1},
    ]
    conflicts = detect_conflicts(facts)
    assert [c["kind"] for c in conflicts] == ["quantity"]


# ------------------------------------------------------------------- script
@pytest.mark.parametrize("minutes", [3, 10, 60, 90, 120])
def test_word_budget_matches_the_requested_duration(minutes):
    total, count, per_chapter = plan_budget(minutes, 150.0)
    assert total == int(minutes * 150.0)
    # The planned chapters must actually add up to the budget, within one
    # chapter's rounding -- otherwise the documentary comes out short.
    assert abs(count * per_chapter - total) <= per_chapter


def test_long_documentaries_are_split_into_several_chapters():
    _, count, _ = plan_budget(90, 150.0)
    assert count >= 6


# ---------------------------------------------------------------- factcheck
def test_evidence_retrieval_finds_the_supporting_fact():
    facts = [
        {"statement": "The harbour at Caesarea was built by Herod.", "confidence": "established"},
        {"statement": "Cahokia's population peaked around 1100 CE.", "confidence": "interpretation"},
    ]
    got = retrieve_evidence(ClaimCheck("Caesarea's harbour was constructed under Herod.",
                                       "assertion", 0), facts)
    assert got == [0]


def test_a_claim_outside_the_research_retrieves_nothing():
    """No evidence retrieved is what makes a claim unsupported rather than false."""
    facts = [{"statement": "The harbour at Caesarea was built by Herod.",
              "confidence": "established"}]
    got = retrieve_evidence(ClaimCheck("The Antarctic ice sheet formed 34 million years ago.",
                                       "assertion", 0), facts)
    assert got == []


class _WorseningLLM:
    """A model whose 'revision' destroys the sourced content of a chapter.

    Exactly the observed failure: asked to soften three sentences, it returns
    generic prose with the evidence stripped out.
    """

    name = "worsening"

    def __init__(self):
        self.revisions = 0

    def available(self):
        return True

    def generate(self, prompt, **kw):
        self.revisions += 1
        return " ".join(["Something unverifiable happened at some point."] * 40)

    def generate_json(self, prompt, **kw):
        if "TASK: claim_extraction" in prompt:
            text = prompt.split("PASSAGE:\n", 1)[1].split("\nReturn JSON", 1)[0]
            sentences = [s.strip() for s in text.split(".") if len(s.split()) >= 5]
            return {"claims": [{"text": s, "kind": "assertion"} for s in sentences[:20]]}
        # Anything mentioning Herod is supported; the filler never is.
        claims = []
        for row in prompt.split("CLAIMS:\n", 1)[1].split("\nEVIDENCE", 1)[0].splitlines():
            claims.append(row)
        out = []
        for index, row in enumerate(claims):
            supported = "Herod" in row
            out.append({"id": index,
                        "verdict": "supported" if supported else "unsupported",
                        "note": "", "source_ids": []})
        return {"verdicts": out}


def test_a_revision_that_makes_a_chapter_worse_is_reverted():
    """The script may only improve across passes, never regress."""
    good = (". ".join(["The harbour was built by Herod in 15 BCE"] * 6)
            + ". Something unverifiable happened at some point.")
    llm = _WorseningLLM()
    facts = [{"statement": "The harbour was built by Herod in 15 BCE.",
              "confidence": "established", "entities": ["Herod"]}]

    # A threshold of zero forces a revision attempt: pass 1 has one failing
    # claim, so the run must try to fix it.
    try:
        result = factcheck_run(llm, [good], facts, max_unsupported_ratio=0.0,
                               max_passes=2)
        revised = result.revised_chapters
    except FactCheckFailed:
        # Refusing the script is also correct here -- what must not happen is
        # the worse rewrite being carried forward.
        revised = {}

    assert llm.revisions >= 1, "the revision path must have been exercised"
    assert revised == {}, "a revision with more failures must never be kept"


# --------------------------------------------------------------- narration
def test_chunks_never_cut_a_sentence_in_half():
    text = " ".join(f"This is sentence number {i} in the narration." for i in range(80))
    chunks = split_into_chunks(text, 400)
    assert all(c.rstrip().endswith(".") for c in chunks)
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")


def test_chunks_respect_chapter_boundaries():
    chapters = [
        {"id": 1, "ordinal": 0, "heading": "A", "body": "One sentence here. Another one here."},
        {"id": 2, "ordinal": 1, "heading": "B", "body": "Third sentence here. Fourth one here."},
    ]
    chunks = plan_chunks(chapters, 400)
    assert {c.chapter_ordinal for c in chunks} == {0, 1}
    assert all(c.chapter_id in (1, 2) for c in chunks)


def test_a_chapter_change_gets_a_longer_pause_than_a_paragraph():
    chapters = [
        {"id": 1, "ordinal": 0, "heading": "A", "body": "One. Two."},
        {"id": 2, "ordinal": 1, "heading": "B", "body": "Three. Four."},
    ]
    chunks = plan_chunks(chapters, 20)
    same = [c for c in chunks if c.chapter_ordinal == 0]
    other = [c for c in chunks if c.chapter_ordinal == 1][0]
    assert gap_for(same[0], other) > gap_for(same[0], same[-1])


def test_chapter_timings_are_measured_from_the_audio(tmp_path):
    chapters = [
        {"id": 1, "ordinal": 0, "heading": "A",
         "body": " ".join(["word"] * 150)},
        {"id": 2, "ordinal": 1, "heading": "B",
         "body": " ".join(["word"] * 150)},
    ]
    result = narration_mod.run(
        SilentProvider(150.0), chapters, tmp_path, max_chars=2000,
        voice="silent", length_scale=1.0,
    )
    assert len(result.timings) == 2
    first, second = result.timings
    assert first.start_s == 0.0
    # The second chapter starts after the first plus the chapter gap.
    assert second.start_s >= first.start_s + first.duration_s
    with wave.open(str(result.audio_path)) as wf:
        actual = wf.getnframes() / wf.getframerate()
    assert abs(actual - result.duration_s) < 0.2


# ---------------------------------------------------------------- metadata
@pytest.mark.parametrize("seconds,expected", [
    (0, "0:00"), (59, "0:59"), (60, "1:00"), (3600, "1:00:00"), (5432.7, "1:30:33"),
])
def test_chapter_timestamps(seconds, expected):
    assert timestamp(seconds) == expected


def test_a_short_chapter_is_merged_not_dropped():
    """YouTube requires contiguous markers; dropping one would leave a gap."""
    timings = [
        ChapterTiming(1, 0, "A", 0.0, 600.0),
        ChapterTiming(2, 1, "B", 600.0, 700.0),
        ChapterTiming(3, 2, "Blip", 1300.0, 4.0),
        ChapterTiming(4, 3, "C", 1304.0, 800.0),
    ]
    rows = build_chapters(timings)
    assert len(rows) == 3
    assert chapters_valid(rows) == (True, "ok")


def test_too_few_sections_publishes_no_markers_rather_than_broken_ones():
    rows = build_chapters([ChapterTiming(1, 0, "A", 0.0, 600.0),
                           ChapterTiming(2, 1, "B", 600.0, 600.0)])
    assert rows == []


def test_first_marker_is_always_zero():
    rows = build_chapters([ChapterTiming(i, i, f"C{i}", 12.0 + i * 600.0, 600.0)
                           for i in range(4)])
    assert rows[0]["start_s"] == 0.0
    assert rows[0]["timestamp"] == "0:00"


@pytest.mark.parametrize("proposed", [
    "You Won't Believe The Shocking Truth",
    "The Most Accurate History Ever Told",
    "They Don't Want You To Know This",
])
def test_clickbait_titles_are_replaced_with_the_subject(proposed):
    assert clean_title(proposed, "Roman Harbours") == "Roman Harbours"


def test_titles_are_cut_at_a_word_boundary():
    title = clean_title("The " + "Extraordinary " * 20 + "History", "fallback")
    assert len(title) <= TITLE_MAX
    assert not title.endswith("Extraordin")


def test_tags_stay_within_the_combined_budget():
    tags = limit_tags([f"tag-number-{i}" for i in range(200)])
    assert sum(len(t) + 1 for t in tags) <= TAGS_TOTAL_MAX


def test_omitted_image_credits_are_disclosed():
    """Several accepted licences require attribution; silence would be a lie."""
    rows = build_chapters([ChapterTiming(i, i, f"C{i}", i * 600.0, 600.0)
                           for i in range(4)])
    description = build_description(
        "Summary.", rows,
        [{"title": f"S{i}", "url": f"https://example.org/{i}"} for i in range(30)],
        [f"Image {i} by Someone (CC BY-SA 4.0)" for i in range(200)],
        channel="Echoes of History",
    )
    assert "further images" in description
    assert "further sources" in description
    assert len(description) <= DESCRIPTION_MAX
    assert "0:00 C0" in description
