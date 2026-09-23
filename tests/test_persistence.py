"""Durable state. These run against a real PostgreSQL, not a stand-in.

The guarantees here are the ones that stop the system doing a thing twice --
producing the same documentary, or putting a second copy of a video on the
channel. A fake database would not exercise the partial unique index and the
ON CONFLICT clauses that actually enforce them.
"""

from __future__ import annotations

from datetime import datetime, timezone

import psycopg
import pytest

from echoes.db import repo

pytestmark = pytest.mark.db


def _topic(title="The Harbour at Caesarea", normalized="harbour caesarea"):
    return repo.create_topic(title=title, normalized_title=normalized,
                             tokens=normalized.split(), period="Roman")


def test_a_normalized_title_can_only_be_taken_once(db):
    assert _topic() is not None
    assert _topic(title="Caesarea, The Harbour At") is None


def test_the_same_request_twice_is_the_same_job(db):
    topic = _topic()
    first, created_first = repo.create_or_get_job(
        topic_id=topic["id"], idempotency_key="daily-2026-03-14", dry_run=True,
        publish_mode="private", version_stamp="v1")
    second, created_second = repo.create_or_get_job(
        topic_id=topic["id"], idempotency_key="daily-2026-03-14", dry_run=True,
        publish_mode="private", version_stamp="v1")
    assert created_first is True and created_second is False
    assert first["id"] == second["id"]


def test_a_stage_cannot_succeed_twice(db):
    """This index is what makes resumption safe rather than merely likely."""
    topic = _topic()
    job, _ = repo.create_or_get_job(topic_id=topic["id"], idempotency_key="k",
                                    dry_run=True, publish_mode="private",
                                    version_stamp="v1")
    run_id = repo.start_stage(job["id"], "research", 1)
    repo.finish_stage(run_id, "SUCCEEDED", output={"sources": 11})

    with pytest.raises(psycopg.errors.UniqueViolation):
        second = repo.start_stage(job["id"], "research", 2)
        repo.finish_stage(second, "SUCCEEDED", output={"sources": 99})


def test_a_failed_stage_may_be_retried(db):
    topic = _topic()
    job, _ = repo.create_or_get_job(topic_id=topic["id"], idempotency_key="k",
                                    dry_run=True, publish_mode="private",
                                    version_stamp="v1")
    repo.finish_stage(repo.start_stage(job["id"], "script", 1), "FAILED",
                      error="provider down")
    repo.finish_stage(repo.start_stage(job["id"], "script", 2), "SUCCEEDED",
                      output={"words": 13500})
    assert "script" in repo.completed_stages(job["id"])


def test_completed_stages_carry_their_output_forward(db):
    topic = _topic()
    job, _ = repo.create_or_get_job(topic_id=topic["id"], idempotency_key="k",
                                    dry_run=True, publish_mode="private",
                                    version_stamp="v1")
    repo.finish_stage(repo.start_stage(job["id"], "render", 1), "SUCCEEDED",
                      output={"minutes": 91.2})
    assert repo.completed_stages(job["id"])["render"]["output"]["minutes"] == 91.2


def test_an_upload_can_only_be_claimed_once(db):
    """The defence against a second copy of the video on the channel."""
    topic = _topic()
    job, _ = repo.create_or_get_job(topic_id=topic["id"], idempotency_key="k",
                                    dry_run=False, publish_mode="private",
                                    version_stamp="v1")
    video = repo.upsert_video(job_id=job["id"], topic_id=topic["id"], title="T",
                              description="D", tags=[], chapters_json=[],
                              duration_s=5400.0, video_path="/v.mp4",
                              thumbnail_path="/t.jpg", version_stamp="v1")
    first, reserved_first = repo.reserve_upload(
        job_id=job["id"], video_id=video["id"], idempotency_key="k:upload",
        privacy_status="private", publish_at=None, bytes_total=100)
    second, reserved_second = repo.reserve_upload(
        job_id=job["id"], video_id=video["id"], idempotency_key="k:upload",
        privacy_status="private", publish_at=None, bytes_total=100)
    assert reserved_first is True and reserved_second is False
    assert first["id"] == second["id"]


def test_one_youtube_video_id_cannot_be_recorded_against_two_uploads(db):
    topic = _topic()
    job, _ = repo.create_or_get_job(topic_id=topic["id"], idempotency_key="k",
                                    dry_run=False, publish_mode="private",
                                    version_stamp="v1")
    video = repo.upsert_video(job_id=job["id"], topic_id=topic["id"], title="T",
                              description="D", tags=[], chapters_json=[],
                              duration_s=1.0, video_path="/v.mp4",
                              thumbnail_path=None, version_stamp="v1")
    a, _ = repo.reserve_upload(job_id=job["id"], video_id=video["id"],
                               idempotency_key="a", privacy_status="private",
                               publish_at=None, bytes_total=1)
    b, _ = repo.reserve_upload(job_id=job["id"], video_id=video["id"],
                               idempotency_key="b", privacy_status="private",
                               publish_at=None, bytes_total=1)
    repo.set_upload_status(a["id"], "SUCCEEDED", youtube_video_id="abc123")
    with pytest.raises(psycopg.errors.UniqueViolation):
        repo.set_upload_status(b["id"], "SUCCEEDED", youtube_video_id="abc123")


def test_uploads_in_doubt_are_findable(db):
    topic = _topic()
    job, _ = repo.create_or_get_job(topic_id=topic["id"], idempotency_key="k",
                                    dry_run=False, publish_mode="private",
                                    version_stamp="v1")
    video = repo.upsert_video(job_id=job["id"], topic_id=topic["id"], title="T",
                              description="D", tags=[], chapters_json=[],
                              duration_s=1.0, video_path="/v.mp4",
                              thumbnail_path=None, version_stamp="v1")
    upload, _ = repo.reserve_upload(job_id=job["id"], video_id=video["id"],
                                    idempotency_key="k:upload",
                                    privacy_status="private", publish_at=None,
                                    bytes_total=10)
    repo.set_upload_status(upload["id"], "IN_FLIGHT", upload_url="https://u/1")
    in_doubt = repo.uploads_in_doubt()
    assert [r["id"] for r in in_doubt] == [upload["id"]]

    repo.set_upload_status(upload["id"], "SUCCEEDED", youtube_video_id="xyz")
    assert repo.uploads_in_doubt() == []


def test_claiming_a_topic_takes_it_out_of_the_pool(db):
    _topic(title="A", normalized="a-one")
    _topic(title="B", normalized="b-two")
    first = repo.claim_next_topic()
    second = repo.claim_next_topic()
    assert first["id"] != second["id"]
    assert repo.claim_next_topic() is None


def test_audio_chunks_record_progress_for_resumption(db):
    topic = _topic()
    job, _ = repo.create_or_get_job(topic_id=topic["id"], idempotency_key="k",
                                    dry_run=True, publish_mode="private",
                                    version_stamp="v1")
    script = repo.create_script(job_id=job["id"], topic_id=topic["id"], version=1)
    audio = repo.create_audio_job(job_id=job["id"], script_id=script["id"],
                                  provider="piper", voice="v", chunk_total=3)
    row = repo.upsert_audio_chunk(audio_job_id=audio["id"], ordinal=0,
                                  chapter_id=None, text_hash="h0")
    repo.complete_audio_chunk(row["id"], "/tmp/a.wav", 12.5)
    chunks = repo.audio_chunks(audio["id"])
    assert len(chunks) == 1 and chunks[0]["status"] == "DONE"
    # Re-planning the same chunk must not create a duplicate row.
    repo.upsert_audio_chunk(audio_job_id=audio["id"], ordinal=0,
                            chapter_id=None, text_hash="h0")
    assert len(repo.audio_chunks(audio["id"])) == 1


def test_settings_round_trip(db):
    repo.set_setting("measured_wpm:v:1.713", 133.4)
    assert repo.get_setting("measured_wpm:v:1.713") == 133.4
    repo.set_setting("measured_wpm:v:1.713", 141.0)
    assert repo.get_setting("measured_wpm:v:1.713") == 141.0
    assert repo.get_setting("absent", "fallback") == "fallback"
