"""The duplicate guard. Its job is to stop the channel repeating itself."""

from __future__ import annotations

import pytest

from echoes.errors import DuplicateTopic
from echoes.pipeline.topics import (
    check_originality, normalize_title, select, similarity, text_overlap,
)


def test_normalisation_strips_what_does_not_distinguish():
    assert normalize_title("The Siege of Constantinople: A Full Documentary") == \
        "siege constantinople"


@pytest.mark.parametrize("a,b", [
    ("The Siege of Constantinople", "Constantinople: The Siege"),      # reordered
    ("The Fall of Carthage", "The Fall of Carthage"),                   # identical
    ("Roman Concrete", "Roman Concrete and the Harbour at Caesarea"),   # contained
    ("Roman Trading Routes", "Roman Trade Route"),                      # inflection
])
def test_near_duplicates_are_caught(a, b):
    assert similarity(a, b) >= 0.72


@pytest.mark.parametrize("a,b", [
    ("The Antikythera Mechanism", "Cahokia: City on the Mississippi"),
    ("Roman Roads", "Roman Naval Logistics"),
])
def test_genuinely_different_subjects_are_allowed(a, b):
    assert similarity(a, b) < 0.72


def test_guard_reports_what_it_collided_with():
    existing = [{"id": 7, "title": "The Siege of Constantinople"}]
    verdict = check_originality("Constantinople: The Siege", existing, 0.72)
    assert verdict.original is False
    assert verdict.closest_id == 7
    assert "7" in verdict.reason()


def test_selection_skips_duplicates_and_takes_the_first_original():
    existing = [{"id": 1, "title": "Roman Concrete"}]
    chosen, verdict = select(
        [{"title": "Roman Concrete and Harbours"}, {"title": "The Salt Roads of the Sahara"}],
        existing, threshold=0.72, banned=[],
    )
    assert chosen["title"] == "The Salt Roads of the Sahara"
    assert verdict.original


def test_banned_subjects_are_refused_even_when_original():
    with pytest.raises(DuplicateTopic):
        select([{"title": "The Politics of Modern Elections"}], [],
               threshold=0.72, banned=["modern elections"])


def test_all_duplicates_raises_rather_than_producing_a_repeat():
    existing = [{"id": 1, "title": "The Fall of Carthage"}]
    with pytest.raises(DuplicateTopic):
        select([{"title": "The Fall of Carthage"}], existing,
               threshold=0.72, banned=[])


def test_reused_prose_is_detected_across_scripts():
    """Different titles, same opening: the failure that looks machine-made."""
    opening = ("the record begins as it so often does in fragments and the gaps "
               "matter as much as the evidence that survives them here")
    assert text_overlap(opening, opening) == 1.0
    assert text_overlap(opening, "an entirely unrelated passage about other matters") == 0.0
