"""Failure handling and the final gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from echoes.errors import AmbiguousOutcome, Permanent, ProviderUnavailable, RateLimited
from echoes.db import repo
from echoes.pipeline import qc
from echoes.pipeline.runner import Context, PipelineRunner
from echoes.providers.registry import build as build_providers


# ------------------------------------------------------------------- runner
class _Stage:
    def __init__(self, name, behaviour):
        self.name = name
        self._behaviour = behaviour
        self.calls = 0

    def run(self, ctx):
        self.calls += 1
        result = self._behaviour(self.calls)
        return result if isinstance(result, dict) else {}


def _context(settings, tmp_path, job, topic, clock):
    return Context(settings=settings, providers=build_providers(settings),
                   clock=clock, job=job, topic=topic, work_dir=tmp_path)


@pytest.fixture
def job_and_topic(db):
    topic = repo.create_topic(title="A Subject", normalized_title="a subject",
                              tokens=["subject"])
    job, _ = repo.create_or_get_job(topic_id=topic["id"], idempotency_key="k",
                                    dry_run=True, publish_mode="private",
                                    version_stamp="v1")
    return job, topic


pytestmark = pytest.mark.db


def test_a_transient_failure_is_retried_then_succeeds(settings, tmp_path, clock,
                                                      job_and_topic):
    job, topic = job_and_topic

    def behaviour(call):
        if call < 3:
            raise ProviderUnavailable("archive is down")
        return {"ok": True}

    stage = _Stage("research", behaviour)
    runner = PipelineRunner([stage], sleep=lambda _s: None)
    outcomes = runner.run(_context(settings.with_(retry_max_attempts=4),
                                   tmp_path, job, topic, clock))
    assert stage.calls == 3
    assert outcomes[0].status == "SUCCEEDED"


def test_retries_are_bounded(settings, tmp_path, clock, job_and_topic):
    job, topic = job_and_topic
    stage = _Stage("research", lambda _c: (_ for _ in ()).throw(
        ProviderUnavailable("still down")))
    runner = PipelineRunner([stage], sleep=lambda _s: None)
    outcomes = runner.run(_context(settings.with_(retry_max_attempts=3),
                                   tmp_path, job, topic, clock))
    assert stage.calls == 3
    assert outcomes[0].status == "FAILED"
    assert repo.get_job(job["id"])["status"] == "FAILED"


def test_a_permanent_failure_is_not_retried(settings, tmp_path, clock,
                                            job_and_topic):
    """Retrying a permanent error burns quota to reach the same answer."""
    job, topic = job_and_topic
    stage = _Stage("script", lambda _c: (_ for _ in ()).throw(
        Permanent("the research package is empty")))
    runner = PipelineRunner([stage], sleep=lambda _s: None)
    outcomes = runner.run(_context(settings.with_(retry_max_attempts=5),
                                   tmp_path, job, topic, clock))
    assert stage.calls == 1
    assert outcomes[0].status == "FAILED"


def test_an_ambiguous_outcome_is_never_retried(settings, tmp_path, clock,
                                               job_and_topic):
    """A repeat could duplicate a side effect that may already have landed."""
    job, topic = job_and_topic
    stage = _Stage("upload", lambda _c: (_ for _ in ()).throw(
        AmbiguousOutcome("connection lost mid-upload")))
    runner = PipelineRunner([stage], sleep=lambda _s: None)
    outcomes = runner.run(_context(settings.with_(retry_max_attempts=5),
                                   tmp_path, job, topic, clock))
    assert stage.calls == 1
    assert outcomes[0].status == "AMBIGUOUS"
    assert repo.get_job(job["id"])["status"] == "NEEDS_ATTENTION"


def test_a_rate_limit_is_retried(settings, tmp_path, clock, job_and_topic):
    job, topic = job_and_topic
    slept: list[float] = []

    def behaviour(call):
        if call == 1:
            raise RateLimited("quota", retry_after_s=2.0)
        return {}

    stage = _Stage("script", behaviour)
    runner = PipelineRunner([stage], sleep=slept.append)
    runner.run(_context(settings, tmp_path, job, topic, clock))
    assert stage.calls == 2
    assert slept and slept[0] <= 2.0, "Retry-After must cap the backoff"


def test_a_later_stage_does_not_run_after_a_failure(settings, tmp_path, clock,
                                                    job_and_topic):
    job, topic = job_and_topic
    first = _Stage("research", lambda _c: (_ for _ in ()).throw(Permanent("no")))
    second = _Stage("script", lambda _c: {})
    runner = PipelineRunner([first, second], sleep=lambda _s: None)
    runner.run(_context(settings, tmp_path, job, topic, clock))
    assert second.calls == 0


def test_a_resumed_run_skips_what_already_succeeded(settings, tmp_path, clock,
                                                    job_and_topic):
    job, topic = job_and_topic
    research = _Stage("research", lambda _c: {"sources": 12})
    script = _Stage("script", lambda _c: {"words": 13500})
    runner = PipelineRunner([research, script], sleep=lambda _s: None)
    runner.run(_context(settings, tmp_path, job, topic, clock))
    assert (research.calls, script.calls) == (1, 1)

    # Second run: everything is already done, so nothing is recomputed.
    outcomes = runner.run(_context(settings, tmp_path, job, topic, clock))
    assert (research.calls, script.calls) == (1, 1)
    assert [o.status for o in outcomes] == ["SKIPPED", "SKIPPED"]
    assert outcomes[0].output["sources"] == 12


# ----------------------------------------------------------------------- qc
def _qc(settings, **overrides):
    base = dict(
        settings=settings, video_path=None, thumbnail_path=None,
        narration_duration_s=settings.duration.target_minutes * 60.0,
        chapters=[{"start_s": 0.0, "duration_s": 600.0, "title": "A",
                   "timestamp": "0:00"},
                  {"start_s": 600.0, "duration_s": 600.0, "title": "B",
                   "timestamp": "10:00"},
                  {"start_s": 1200.0, "duration_s": 600.0, "title": "C",
                   "timestamp": "20:00"}],
        chapter_bodies=[" ".join(["word"] * (settings.duration.target_words() // 2)),
                        " ".join(["other"] * (settings.duration.target_words() // 2))],
        title="A Real Title", description="x" * 400, tags=["history"],
        sources=[{"url": f"https://example.org/{i}", "provider": "loc"}
                 for i in range(10)],
        visual_assets=[{"licence": "CC0", "provider": "wikimedia",
                        "perceptual_hash": f"{i:016x}"} for i in range(20)],
        unsupported_ratio=0.01, script_generator="gemini", dry_run=True,
    )
    base.update(overrides)
    return qc.run(**base)


def _blocked(report):
    return {c.name for c in report.blocking_failures}


def test_an_offline_script_cannot_pass_on_a_live_run(settings):
    """Recorded as a column, so it cannot be laundered by editing the prose."""
    assert "script_not_synthetic" not in _blocked(
        _qc(settings, script_generator="offline", dry_run=True))
    assert "script_not_synthetic" in _blocked(
        _qc(settings, script_generator="offline", dry_run=False))
    assert "script_not_synthetic" not in _blocked(
        _qc(settings, script_generator="gemini", dry_run=False))
    # Fails closed: an unidentified author blocks a live run too.
    assert "script_not_synthetic" in _blocked(
        _qc(settings, script_generator="unknown", dry_run=False))


def test_synthetic_plates_cannot_pass_on_a_live_run(settings):
    assets = [{"licence": "CC0", "provider": "synthetic",
               "perceptual_hash": f"{i:016x}"} for i in range(20)]
    assert "visuals_not_synthetic" not in _blocked(_qc(settings, visual_assets=assets))
    assert "visuals_not_synthetic" in _blocked(
        _qc(settings, visual_assets=assets, dry_run=False))


def test_fixture_sources_cannot_pass_on_a_live_run(settings):
    sources = [{"url": f"https://fixtures.invalid/{i}", "provider": "fixtures"}
               for i in range(10)]
    assert "sources_not_fixtures" in _blocked(
        _qc(settings, sources=sources, dry_run=False))


def test_an_unlicensed_image_blocks_publication(settings):
    assets = [{"licence": "", "provider": "wikimedia", "perceptual_hash": "0"}]
    assert "visuals_licensed" in _blocked(_qc(settings, visual_assets=assets))


def test_too_few_sources_blocks_publication(settings):
    assert "sources_present" in _blocked(
        _qc(settings, sources=[{"url": "https://example.org/1", "provider": "loc"}]))


def test_too_many_unsupported_claims_block_publication(settings):
    assert "factcheck_within_limit" in _blocked(_qc(settings, unsupported_ratio=0.4))


def test_a_documentary_shorter_than_the_floor_is_blocked(settings):
    report = _qc(settings, narration_duration_s=10 * 60.0)
    assert "narration_duration_in_range" in _blocked(report)


def test_a_missing_render_blocks_publication(settings):
    assert "video_readable" in _blocked(_qc(settings, video_path=None))


def test_broken_chapter_markers_block_but_absent_ones_do_not(settings):
    broken = [{"start_s": 30.0, "duration_s": 600.0, "title": "A", "timestamp": "0:30"},
              {"start_s": 5.0, "duration_s": 600.0, "title": "B", "timestamp": "0:05"},
              {"start_s": 900.0, "duration_s": 600.0, "title": "C", "timestamp": "15:00"}]
    assert "chapters_valid" in _blocked(_qc(settings, chapters=broken))
    assert "chapters_present" not in _blocked(_qc(settings, chapters=[]))


def test_a_clean_documentary_passes(settings, tmp_path):
    """Everything editorial is clean; only the absent artefacts block.

    The two that remain are exactly right: this fixture has no rendered file
    and no thumbnail, and quality control must refuse to publish without
    either of them.
    """
    report = _qc(settings)
    assert _blocked(report) == {"video_readable", "thumbnail_exists"}


def test_a_complete_documentary_passes_every_check(settings, tmp_path):
    """With a real render and thumbnail on disk, nothing blocks."""
    from echoes.media import ffmpeg
    from echoes.providers.images.synthetic import SyntheticImageProvider
    from echoes.providers.tts.silent import SilentProvider
    if not ffmpeg.have_ffmpeg():
        pytest.skip("ffmpeg is not installed")

    minutes = settings.duration.target_minutes
    plate = Path(SyntheticImageProvider(tmp_path / "p", settings.render.plate_width,
                                        settings.render.plate_height)
                 .search("x", limit=1)[0].extra["local_path"])

    # A short stand-in for the picture track: quality control probes the file
    # for resolution and streams, and compares its length against the
    # narration it was told about, so both are made to agree.
    picture = tmp_path / "pic.mp4"
    ffmpeg.render_segment(plate, picture, duration_s=4.0,
                          width=settings.render.width, height=settings.render.height,
                          fps=settings.render.fps, preset="ultrafast", crf=30)
    raw = tmp_path / "n.wav"
    SilentProvider(150.0).synthesize(" ".join(["word"] * 10), raw)
    audio = tmp_path / "a.m4a"
    ffmpeg.normalise_loudness(raw, audio)
    final = tmp_path / "final.mp4"
    ffmpeg.mux(picture, audio, final)

    thumbnail = tmp_path / "t.jpg"
    from PIL import Image
    Image.new("RGB", (1280, 720), (20, 20, 20)).save(thumbnail, quality=85)

    report = _qc(
        settings.with_(duration=settings.duration.__class__(
            target_minutes=minutes, min_minutes=0, max_minutes=minutes,
            words_per_minute=settings.duration.words_per_minute)),
        video_path=final, thumbnail_path=thumbnail,
        narration_duration_s=ffmpeg.probe(final).duration_s,
    )
    assert _blocked(report) == set(), report.summary()
    assert report.passed
