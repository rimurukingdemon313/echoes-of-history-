"""Data access for the production pipeline.

Functions here are deliberately shaped around the pipeline's needs rather
than around the tables. ``reserve_upload`` in particular is not a generic
insert: it is the single place that decides whether this job is allowed to
push bytes at YouTube, and it answers that by writing a row first.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable, Sequence

from . import pool

# ----------------------------------------------------------------- settings


def get_setting(key: str, default: Any = None) -> Any:
    row = pool.query_one("SELECT value FROM settings WHERE key = %s", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: Any) -> None:
    pool.execute(
        """INSERT INTO settings (key, value, updated_at)
           VALUES (%s, %s, now())
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                                           updated_at = now()""",
        (key, json.dumps(value)),
    )


# ------------------------------------------------------------------- topics


def create_topic(
    *,
    title: str,
    normalized_title: str,
    tokens: Iterable[str],
    subject: str | None = None,
    period: str | None = None,
    region: str | None = None,
    category: str | None = None,
    angle: str | None = None,
    priority: int = 100,
) -> dict[str, Any] | None:
    """Insert a topic. Returns None if the normalized title is already taken.

    The conflict is not an error: the topic engine proposes candidates in
    batches and a collision simply means that one is already known.
    """
    row = pool.query_one(
        """INSERT INTO topics
             (title, normalized_title, subject, period, region, category,
              angle, priority)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (normalized_title) DO NOTHING
           RETURNING *""",
        (title, normalized_title, subject, period, region, category, angle, priority),
    )
    if row is None:
        return None
    if tokens:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO topic_tokens (topic_id, token) VALUES (%s,%s)
                       ON CONFLICT DO NOTHING""",
                    [(row["id"], t) for t in set(tokens)],
                )
    return row


def get_topic(topic_id: int) -> dict[str, Any] | None:
    return pool.query_one("SELECT * FROM topics WHERE id = %s", (topic_id,))


def set_topic_status(topic_id: int, status: str, *, reason: str | None = None) -> None:
    pool.execute(
        "UPDATE topics SET status = %s, rejection_reason = COALESCE(%s, rejection_reason) WHERE id = %s",
        (status, reason, topic_id),
    )


def mark_topic_published(topic_id: int, url: str, when: datetime) -> None:
    pool.execute(
        "UPDATE topics SET status='PUBLISHED', youtube_url=%s, published_at=%s WHERE id=%s",
        (url, when, topic_id),
    )


def claim_next_topic() -> dict[str, Any] | None:
    """Take the highest-priority idle topic, atomically.

    ``FOR UPDATE SKIP LOCKED`` is what allows more than one worker without a
    second worker picking the same documentary: the row is locked and skipped
    rather than waited on.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT * FROM topics
                    WHERE status IN ('IDEA','RETRY')
                    ORDER BY priority ASC, created_at ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1"""
            )
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute("UPDATE topics SET status='RESEARCHING' WHERE id=%s", (row["id"],))
            return row


def existing_topic_fingerprints() -> list[dict[str, Any]]:
    """Every topic that has been produced or is being produced.

    The originality check compares against this, not against published videos
    only -- otherwise two jobs started on the same day could both pass.
    """
    return pool.query(
        """SELECT id, title, normalized_title, subject, period, region
             FROM topics
            WHERE status NOT IN ('REJECTED','FAILED')"""
    )


# --------------------------------------------------------------------- jobs


def create_or_get_job(
    *,
    topic_id: int,
    idempotency_key: str,
    dry_run: bool,
    publish_mode: str,
    version_stamp: str,
) -> tuple[dict[str, Any], bool]:
    """Return ``(job, created)``.

    A repeated request with the same key returns the original job untouched.
    This is what makes an n8n retry, a double-clicked button and a redelivered
    webhook all harmless.
    """
    row = pool.query_one(
        """INSERT INTO jobs (topic_id, idempotency_key, dry_run, publish_mode,
                             version_stamp, status)
           VALUES (%s,%s,%s,%s,%s,'PENDING')
           ON CONFLICT (idempotency_key) DO NOTHING
           RETURNING *""",
        (topic_id, idempotency_key, dry_run, publish_mode, version_stamp),
    )
    if row is not None:
        return row, True
    existing = pool.query_one(
        "SELECT * FROM jobs WHERE idempotency_key = %s", (idempotency_key,)
    )
    assert existing is not None
    return existing, False


def get_job(job_id: int) -> dict[str, Any] | None:
    return pool.query_one("SELECT * FROM jobs WHERE id = %s", (job_id,))


def set_job_status(
    job_id: int, status: str, *, stage: str | None = None, error: str | None = None
) -> None:
    finished = status in ("SUCCEEDED", "FAILED", "CANCELLED")
    pool.execute(
        """UPDATE jobs
              SET status = %s,
                  current_stage = COALESCE(%s, current_stage),
                  error = %s,
                  finished_at = CASE WHEN %s THEN now() ELSE finished_at END
            WHERE id = %s""",
        (status, stage, error, finished, job_id),
    )


def running_job_count() -> int:
    row = pool.query_one("SELECT count(*) AS n FROM jobs WHERE status='RUNNING'")
    return int(row["n"]) if row else 0


def recent_jobs(limit: int = 20) -> list[dict[str, Any]]:
    return pool.query(
        """SELECT j.*, t.title AS topic_title
             FROM jobs j JOIN topics t ON t.id = j.topic_id
            ORDER BY j.started_at DESC LIMIT %s""",
        (limit,),
    )


# --------------------------------------------------------------- stage runs


def completed_stages(job_id: int) -> dict[str, dict[str, Any]]:
    rows = pool.query(
        "SELECT stage, output, duration_ms FROM stage_runs WHERE job_id=%s AND status='SUCCEEDED'",
        (job_id,),
    )
    return {r["stage"]: r for r in rows}


def start_stage(job_id: int, stage: str, attempt: int) -> int:
    row = pool.query_one(
        """INSERT INTO stage_runs (job_id, stage, attempt, status)
           VALUES (%s,%s,%s,'RUNNING') RETURNING id""",
        (job_id, stage, attempt),
    )
    assert row is not None
    pool.execute("UPDATE jobs SET current_stage=%s WHERE id=%s", (stage, job_id))
    return int(row["id"])


def finish_stage(
    run_id: int,
    status: str,
    *,
    output: dict[str, Any] | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    pool.execute(
        """UPDATE stage_runs
              SET status=%s, output=%s, error=%s, duration_ms=%s, finished_at=now()
            WHERE id=%s""",
        (status, json.dumps(output) if output is not None else None, error, duration_ms, run_id),
    )


def stage_history(job_id: int) -> list[dict[str, Any]]:
    return pool.query(
        "SELECT * FROM stage_runs WHERE job_id=%s ORDER BY started_at", (job_id,)
    )


# ----------------------------------------------------------------- research


def upsert_source(
    *,
    url: str,
    title: str | None,
    provider: str,
    source_type: str | None = None,
    author: str | None = None,
    published: str | None = None,
    licence: str | None = None,
    quality: float = 0.5,
    rationale: str | None = None,
    content_hash: str | None = None,
) -> dict[str, Any]:
    row = pool.query_one(
        """INSERT INTO sources (url,title,provider,source_type,author,published,
                                licence,quality,rationale,content_hash)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (url) DO UPDATE
               SET title=COALESCE(EXCLUDED.title, sources.title),
                   quality=EXCLUDED.quality,
                   retrieved_at=now()
           RETURNING *""",
        (url, title, provider, source_type, author, published, licence, quality,
         rationale, content_hash),
    )
    assert row is not None
    return row


def create_research_package(
    *, job_id: int, topic_id: int, summary: str, source_count: int,
    conflicts: list[dict[str, Any]]
) -> dict[str, Any]:
    row = pool.query_one(
        """INSERT INTO research_packages (job_id, topic_id, summary, source_count, conflicts)
           VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (job_id) DO UPDATE
             SET summary=EXCLUDED.summary, source_count=EXCLUDED.source_count,
                 conflicts=EXCLUDED.conflicts
           RETURNING *""",
        (job_id, topic_id, summary, source_count, json.dumps(conflicts)),
    )
    assert row is not None
    return row


def add_research_facts(package_id: int, facts: Sequence[dict[str, Any]]) -> int:
    if not facts:
        return 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO research_facts
                     (package_id, source_id, statement, excerpt, confidence, entities)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                [
                    (
                        package_id,
                        f.get("source_id"),
                        f["statement"],
                        f.get("excerpt"),
                        f.get("confidence", "uncertain"),
                        json.dumps(f.get("entities", [])),
                    )
                    for f in facts
                ],
            )
    return len(facts)


def research_facts(package_id: int) -> list[dict[str, Any]]:
    return pool.query(
        """SELECT rf.*, s.url AS source_url, s.title AS source_title,
                  s.quality AS source_quality
             FROM research_facts rf
             LEFT JOIN sources s ON s.id = rf.source_id
            WHERE rf.package_id = %s
            ORDER BY rf.id""",
        (package_id,),
    )


def research_package_for_job(job_id: int) -> dict[str, Any] | None:
    return pool.query_one("SELECT * FROM research_packages WHERE job_id=%s", (job_id,))


# ------------------------------------------------------------------ scripts


def create_script(*, job_id: int, topic_id: int, version: int,
                  generator: str = "unknown") -> dict[str, Any]:
    row = pool.query_one(
        """INSERT INTO scripts (job_id, topic_id, version, generator)
           VALUES (%s,%s,%s,%s)
           ON CONFLICT (job_id, version) DO UPDATE
             SET version=EXCLUDED.version, generator=EXCLUDED.generator
           RETURNING *""",
        (job_id, topic_id, version, generator),
    )
    assert row is not None
    return row


def replace_chapters(script_id: int, chapters: Sequence[dict[str, Any]]) -> None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM script_chapters WHERE script_id=%s", (script_id,))
            cur.executemany(
                """INSERT INTO script_chapters
                     (script_id, ordinal, heading, body, word_count)
                   VALUES (%s,%s,%s,%s,%s)""",
                [
                    (script_id, i, c["heading"], c["body"], len(c["body"].split()))
                    for i, c in enumerate(chapters)
                ],
            )
            cur.execute(
                """UPDATE scripts SET word_count = (
                       SELECT COALESCE(sum(word_count),0) FROM script_chapters
                        WHERE script_id=%s)
                    WHERE id=%s""",
                (script_id, script_id),
            )


def set_script_duration(script_id: int, estimated_s: float, status: str) -> None:
    pool.execute(
        "UPDATE scripts SET estimated_s=%s, status=%s WHERE id=%s",
        (estimated_s, status, script_id),
    )


def get_script(job_id: int) -> dict[str, Any] | None:
    return pool.query_one(
        "SELECT * FROM scripts WHERE job_id=%s ORDER BY version DESC LIMIT 1", (job_id,)
    )


def chapters(script_id: int) -> list[dict[str, Any]]:
    return pool.query(
        "SELECT * FROM script_chapters WHERE script_id=%s ORDER BY ordinal", (script_id,)
    )


def set_chapter_timing(chapter_id: int, start_s: float, duration_s: float) -> None:
    pool.execute(
        "UPDATE script_chapters SET start_s=%s, duration_s=%s WHERE id=%s",
        (start_s, duration_s, chapter_id),
    )


# ------------------------------------------------------------------- claims


def replace_claims(script_id: int, items: Sequence[dict[str, Any]]) -> list[int]:
    ids: list[int] = []
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM claims WHERE script_id=%s", (script_id,))
            for c in items:
                cur.execute(
                    """INSERT INTO claims (script_id, chapter_id, text, kind, verdict, note)
                       VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (script_id, c.get("chapter_id"), c["text"], c.get("kind"),
                     c.get("verdict", "uncertain"), c.get("note")),
                )
                got = cur.fetchone()
                assert got is not None
                ids.append(int(got["id"]))
    return ids


def set_claim_verdict(
    claim_id: int, verdict: str, *, note: str | None = None,
    source_ids: Sequence[int] = (), strength: float = 0.6
) -> None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE claims SET verdict=%s, note=%s, resolved=TRUE WHERE id=%s",
                (verdict, note, claim_id),
            )
            for sid in source_ids:
                cur.execute(
                    """INSERT INTO claim_sources (claim_id, source_id, strength)
                       VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (claim_id, sid, strength),
                )


def claim_verdict_counts(script_id: int) -> dict[str, int]:
    rows = pool.query(
        "SELECT verdict, count(*) AS n FROM claims WHERE script_id=%s GROUP BY verdict",
        (script_id,),
    )
    return {r["verdict"]: int(r["n"]) for r in rows}


def unsupported_claims(script_id: int) -> list[dict[str, Any]]:
    return pool.query(
        """SELECT * FROM claims
            WHERE script_id=%s AND verdict IN ('unsupported','contradicted')
            ORDER BY id""",
        (script_id,),
    )


# -------------------------------------------------------------------- audio


def create_audio_job(
    *, job_id: int, script_id: int, provider: str, voice: str, chunk_total: int
) -> dict[str, Any]:
    row = pool.query_one(
        """INSERT INTO audio_jobs (job_id, script_id, provider, voice, chunk_total)
           VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (job_id) DO UPDATE
             SET chunk_total=EXCLUDED.chunk_total, provider=EXCLUDED.provider,
                 voice=EXCLUDED.voice
           RETURNING *""",
        (job_id, script_id, provider, voice, chunk_total),
    )
    assert row is not None
    return row


def upsert_audio_chunk(
    *, audio_job_id: int, ordinal: int, chapter_id: int | None, text_hash: str
) -> dict[str, Any]:
    row = pool.query_one(
        """INSERT INTO audio_chunks (audio_job_id, ordinal, chapter_id, text_hash)
           VALUES (%s,%s,%s,%s)
           ON CONFLICT (audio_job_id, ordinal) DO UPDATE
             SET chapter_id=EXCLUDED.chapter_id
           RETURNING *""",
        (audio_job_id, ordinal, chapter_id, text_hash),
    )
    assert row is not None
    return row


def complete_audio_chunk(chunk_id: int, path: str, duration_s: float) -> None:
    pool.execute(
        "UPDATE audio_chunks SET path=%s, duration_s=%s, status='DONE' WHERE id=%s",
        (path, duration_s, chunk_id),
    )


def fail_audio_chunk(chunk_id: int) -> None:
    pool.execute(
        "UPDATE audio_chunks SET status='FAILED', attempts=attempts+1 WHERE id=%s",
        (chunk_id,),
    )


def audio_chunks(audio_job_id: int) -> list[dict[str, Any]]:
    return pool.query(
        "SELECT * FROM audio_chunks WHERE audio_job_id=%s ORDER BY ordinal",
        (audio_job_id,),
    )


def finish_audio_job(audio_job_id: int, duration_s: float, storage_key: str) -> None:
    pool.execute(
        """UPDATE audio_jobs SET duration_s=%s, storage_key=%s, status='DONE',
               chunk_done=(SELECT count(*) FROM audio_chunks
                            WHERE audio_job_id=%s AND status='DONE')
            WHERE id=%s""",
        (duration_s, storage_key, audio_job_id, audio_job_id),
    )


def get_audio_job(job_id: int) -> dict[str, Any] | None:
    return pool.query_one("SELECT * FROM audio_jobs WHERE job_id=%s", (job_id,))


# ------------------------------------------------------------------ visuals


def replace_visual_assets(job_id: int, assets: Sequence[dict[str, Any]]) -> None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM visual_assets WHERE job_id=%s", (job_id,))
            cur.executemany(
                """INSERT INTO visual_assets
                     (job_id, chapter_id, ordinal, provider, source_url, licence,
                      attribution, creator, local_path, plate_path, start_s,
                      duration_s, motion, perceptual_hash)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [
                    (
                        job_id, a.get("chapter_id"), i, a["provider"],
                        a.get("source_url"), a["licence"], a.get("attribution"),
                        a.get("creator"), a.get("local_path"), a.get("plate_path"),
                        a.get("start_s"), a.get("duration_s"), a.get("motion"),
                        a.get("perceptual_hash"),
                    )
                    for i, a in enumerate(assets)
                ],
            )


def visual_assets(job_id: int) -> list[dict[str, Any]]:
    return pool.query(
        "SELECT * FROM visual_assets WHERE job_id=%s ORDER BY ordinal", (job_id,)
    )


# ------------------------------------------------------------------- render


def create_render_job(
    *, job_id: int, width: int, height: int, fps: int, segment_total: int
) -> dict[str, Any]:
    row = pool.query_one(
        """INSERT INTO render_jobs (job_id, width, height, fps, segment_total)
           VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (job_id) DO UPDATE SET segment_total=EXCLUDED.segment_total
           RETURNING *""",
        (job_id, width, height, fps, segment_total),
    )
    assert row is not None
    return row


def upsert_render_segment(
    *, render_job_id: int, ordinal: int, asset_id: int | None, duration_s: float
) -> dict[str, Any]:
    row = pool.query_one(
        """INSERT INTO render_segments (render_job_id, ordinal, asset_id, duration_s)
           VALUES (%s,%s,%s,%s)
           ON CONFLICT (render_job_id, ordinal) DO UPDATE
             SET duration_s=EXCLUDED.duration_s, asset_id=EXCLUDED.asset_id
           RETURNING *""",
        (render_job_id, ordinal, asset_id, duration_s),
    )
    assert row is not None
    return row


def complete_render_segment(segment_id: int, path: str) -> None:
    pool.execute(
        "UPDATE render_segments SET path=%s, status='DONE' WHERE id=%s",
        (path, segment_id),
    )


def render_segments(render_job_id: int) -> list[dict[str, Any]]:
    return pool.query(
        "SELECT * FROM render_segments WHERE render_job_id=%s ORDER BY ordinal",
        (render_job_id,),
    )


def finish_render_job(
    render_job_id: int, *, duration_s: float, size_bytes: int, storage_key: str
) -> None:
    pool.execute(
        """UPDATE render_jobs
              SET status='DONE', duration_s=%s, bytes=%s, storage_key=%s,
                  finished_at=now(),
                  segment_done=(SELECT count(*) FROM render_segments
                                 WHERE render_job_id=%s AND status='DONE')
            WHERE id=%s""",
        (duration_s, size_bytes, storage_key, render_job_id, render_job_id),
    )


def get_render_job(job_id: int) -> dict[str, Any] | None:
    return pool.query_one("SELECT * FROM render_jobs WHERE job_id=%s", (job_id,))


# ------------------------------------------------------------------- videos


def upsert_video(
    *, job_id: int, topic_id: int, title: str, description: str,
    tags: list[str], chapters_json: list[dict[str, Any]], duration_s: float | None,
    video_path: str | None, thumbnail_path: str | None, version_stamp: str,
) -> dict[str, Any]:
    row = pool.query_one(
        """INSERT INTO videos (job_id, topic_id, title, description, tags, chapters,
                               duration_s, video_path, thumbnail_path, version_stamp)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (job_id) DO UPDATE
             SET title=EXCLUDED.title, description=EXCLUDED.description,
                 tags=EXCLUDED.tags, chapters=EXCLUDED.chapters,
                 duration_s=EXCLUDED.duration_s, video_path=EXCLUDED.video_path,
                 thumbnail_path=EXCLUDED.thumbnail_path
           RETURNING *""",
        (job_id, topic_id, title, description, json.dumps(tags),
         json.dumps(chapters_json), duration_s, video_path, thumbnail_path,
         version_stamp),
    )
    assert row is not None
    return row


def set_qc_report(video_id: int, report: dict[str, Any]) -> None:
    pool.execute("UPDATE videos SET qc_report=%s WHERE id=%s",
                 (json.dumps(report), video_id))


def get_video(job_id: int) -> dict[str, Any] | None:
    return pool.query_one("SELECT * FROM videos WHERE job_id=%s", (job_id,))


# ------------------------------------------------------------------ uploads


def reserve_upload(
    *, job_id: int, video_id: int, idempotency_key: str, privacy_status: str,
    publish_at: datetime | None, bytes_total: int | None,
) -> tuple[dict[str, Any], bool]:
    """Claim the right to upload. Returns ``(row, reserved_now)``.

    The row is written *before* any bytes are sent. If this process dies
    mid-upload, the surviving row says an attempt was in flight and the
    recovery path queries YouTube for the result instead of uploading a
    second copy.
    """
    row = pool.query_one(
        """INSERT INTO youtube_uploads
             (job_id, video_id, idempotency_key, privacy_status, publish_at,
              bytes_total, status)
           VALUES (%s,%s,%s,%s,%s,%s,'PENDING')
           ON CONFLICT (idempotency_key) DO NOTHING
           RETURNING *""",
        (job_id, video_id, idempotency_key, privacy_status, publish_at, bytes_total),
    )
    if row is not None:
        return row, True
    existing = pool.query_one(
        "SELECT * FROM youtube_uploads WHERE idempotency_key=%s", (idempotency_key,)
    )
    assert existing is not None
    return existing, False


def set_upload_status(
    upload_id: int, status: str, *, youtube_video_id: str | None = None,
    upload_url: str | None = None, bytes_sent: int | None = None,
    error: str | None = None,
) -> None:
    pool.execute(
        """UPDATE youtube_uploads
              SET status=%s,
                  youtube_video_id=COALESCE(%s, youtube_video_id),
                  upload_url=COALESCE(%s, upload_url),
                  bytes_sent=COALESCE(%s, bytes_sent),
                  error=%s,
                  completed_at=CASE WHEN %s IN ('SUCCEEDED','FAILED','SKIPPED_DRY_RUN')
                                    THEN now() ELSE completed_at END
            WHERE id=%s""",
        (status, youtube_video_id, upload_url, bytes_sent, error, status, upload_id),
    )


def get_upload(job_id: int) -> dict[str, Any] | None:
    return pool.query_one(
        "SELECT * FROM youtube_uploads WHERE job_id=%s ORDER BY id DESC LIMIT 1",
        (job_id,),
    )


def uploads_in_doubt() -> list[dict[str, Any]]:
    """Uploads whose outcome is unknown. These need reconciliation, not retry."""
    return pool.query(
        "SELECT * FROM youtube_uploads WHERE status IN ('IN_FLIGHT','AMBIGUOUS')"
    )


# ------------------------------------------------------------ observability


def record_error(
    *, job_id: int | None, stage: str | None, kind: str, message: str,
    retryable: bool, context: dict[str, Any] | None = None,
) -> None:
    pool.execute(
        """INSERT INTO errors (job_id, stage, kind, message, retryable, context)
           VALUES (%s,%s,%s,%s,%s,%s)""",
        (job_id, stage, kind, message[:4000], retryable,
         json.dumps(context or {})),
    )


def record_notification(
    *, job_id: int | None, level: str, event: str, message: str,
    provider: str | None, delivered: bool,
) -> None:
    pool.execute(
        """INSERT INTO notifications (job_id, level, event, message, provider, delivered)
           VALUES (%s,%s,%s,%s,%s,%s)""",
        (job_id, level, event, message[:4000], provider, delivered),
    )


def record_workflow_run(
    *, job_id: int | None, source: str, external_id: str | None,
    payload: dict[str, Any] | None,
) -> None:
    pool.execute(
        """INSERT INTO workflow_runs (job_id, source, external_id, payload)
           VALUES (%s,%s,%s,%s)""",
        (job_id, source, external_id, json.dumps(payload or {})),
    )


def counters() -> dict[str, int]:
    row = pool.query_one(
        """SELECT
             (SELECT count(*) FROM jobs WHERE status='SUCCEEDED')      AS succeeded,
             (SELECT count(*) FROM jobs WHERE status='FAILED')         AS failed,
             (SELECT count(*) FROM jobs WHERE status='RUNNING')        AS running,
             (SELECT count(*) FROM topics WHERE status='PUBLISHED')    AS published,
             (SELECT count(*) FROM topics WHERE status='IDEA')         AS ideas,
             (SELECT count(*) FROM youtube_uploads
               WHERE status='SUCCEEDED')                               AS uploads"""
    )
    return {k: int(v) for k, v in (row or {}).items()}
