"""Job orchestration: choosing what to make, and running the pipeline.

This is the layer n8n and the scheduler talk to. It owns three decisions that
must not live inside a stage:

* whether the system is *allowed* to start new work (database reachable,
  concurrency budget free);
* which topic to produce, and whether that topic is original;
* how a job is identified, so the same request twice is the same job.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .clock import Clock
from .config import Settings
from .db import pool, repo
from .errors import ConfigError, DuplicateTopic, Permanent
from .logging import get_logger
from .pipeline import topics as topic_engine
from .pipeline.runner import Context, PipelineRunner, StageOutcome
from .pipeline.stages import ALL_STAGES
from .providers.registry import Providers, build as build_providers
from .version import version_stamp

log = get_logger(__name__)


@dataclass
class StartResult:
    job_id: int
    topic_id: int
    topic_title: str
    created: bool
    outcomes: list[StageOutcome]

    @property
    def succeeded(self) -> bool:
        return all(o.status in ("SUCCEEDED", "SKIPPED") for o in self.outcomes)

    def summary(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id, "topic": self.topic_title,
            "created": self.created, "succeeded": self.succeeded,
            "stages": [{"stage": o.stage, "status": o.status,
                        "attempts": o.attempts, "ms": o.duration_ms,
                        "error": o.error} for o in self.outcomes],
        }


class Orchestrator:
    def __init__(self, settings: Settings, clock: Clock | None = None,
                 providers: Providers | None = None) -> None:
        settings.validate()
        self.settings = settings
        self.clock = clock or Clock()
        self.providers = providers or build_providers(settings)
        self.runner = PipelineRunner(ALL_STAGES)

    # ------------------------------------------------------------ guards
    def can_start(self) -> tuple[bool, str]:
        """Whether it is safe to begin a new production.

        Fails closed. An unreachable database means the system cannot record
        what it is doing, and a production it cannot record is one it may
        repeat -- so it stands aside rather than proceeding hopefully.
        """
        if not pool.healthy():
            return False, "the database is unreachable; no new work will start"
        running = repo.running_job_count()
        if running >= self.settings.max_concurrent_jobs:
            return False, (f"{running} job(s) already running, limit is "
                           f"{self.settings.max_concurrent_jobs}")
        return True, "ok"

    # ------------------------------------------------------------ topics
    def replenish_topics(self, *, want: int = 8) -> dict[str, Any]:
        """Add new candidate topics, rejecting duplicates.

        Run ahead of production rather than during it, so a documentary is
        never blocked waiting for the topic engine.
        """
        existing = repo.existing_topic_fingerprints()
        candidates = topic_engine.discover(
            self.providers.llm, count=want * 2,
            minutes=self.settings.duration.target_minutes,
            existing=existing,
            preferred_eras=self.settings.preferred_eras,
            banned=self.settings.banned_topics,
        )

        added: list[str] = []
        rejected: list[dict[str, str]] = []
        for candidate in candidates:
            if len(added) >= want:
                break
            title = str(candidate.get("title", "")).strip()
            if not title:
                continue
            if topic_engine.is_banned(title, self.settings.banned_topics):
                rejected.append({"title": title, "why": "banned"})
                continue
            verdict = topic_engine.check_originality(
                title, existing, self.settings.similarity_threshold)
            if not verdict.original:
                rejected.append({"title": title, "why": verdict.reason()})
                continue
            duplicate, why = topic_engine.semantic_duplicate(
                self.providers.llm, title, existing)
            if duplicate:
                rejected.append({"title": title, "why": why})
                continue

            row = repo.create_topic(
                title=title,
                normalized_title=topic_engine.normalize_title(title),
                tokens=topic_engine.tokens(title),
                subject=candidate.get("subject"), period=candidate.get("period"),
                region=candidate.get("region"), category=candidate.get("category"),
                angle=candidate.get("angle"),
            )
            if row is None:
                rejected.append({"title": title, "why": "normalized title taken"})
                continue
            added.append(title)
            existing.append({"id": row["id"], "title": title})

        log.info("topic pool replenished",
                 extra={"added": len(added), "rejected": len(rejected)})
        return {"added": added, "rejected": rejected}

    def ensure_topic_available(self) -> dict[str, Any]:
        row = repo.claim_next_topic()
        if row is not None:
            return row
        self.replenish_topics()
        row = repo.claim_next_topic()
        if row is None:
            raise DuplicateTopic(
                "no original topic is available: every candidate the topic "
                "engine proposed duplicates something already produced. Add "
                "PREFERRED_ERAS, or widen the subject range."
            )
        return row

    # -------------------------------------------------------------- jobs
    def start(
        self, *, topic_id: int | None = None, idempotency_key: str | None = None,
        dry_run: bool | None = None, publish_mode: str | None = None,
    ) -> StartResult:
        allowed, why = self.can_start()
        if not allowed:
            raise Permanent(f"refusing to start: {why}")

        topic = repo.get_topic(topic_id) if topic_id else self.ensure_topic_available()
        if topic is None:
            raise Permanent(f"topic {topic_id} does not exist")

        key = idempotency_key or f"auto-{self.clock.now():%Y%m%d}-{uuid.uuid4().hex[:8]}"
        job, created = repo.create_or_get_job(
            topic_id=int(topic["id"]), idempotency_key=key,
            dry_run=self.settings.dry_run if dry_run is None else dry_run,
            publish_mode=publish_mode or self.settings.publish_mode,
            version_stamp=version_stamp(),
        )
        repo.record_workflow_run(job_id=int(job["id"]), source="orchestrator",
                                 external_id=key, payload={"topic_id": topic["id"]})
        return self._run(job, topic, created)

    def resume(self, job_id: int) -> StartResult:
        """Continue a job from the first stage that has not succeeded."""
        job = repo.get_job(job_id)
        if job is None:
            raise Permanent(f"job {job_id} does not exist")
        topic = repo.get_topic(int(job["topic_id"]))
        if topic is None:
            raise Permanent(f"job {job_id} points at a topic that is gone")
        done = repo.completed_stages(job_id)
        log.info("resuming job", extra={"job_id": job_id,
                                        "completed_stages": sorted(done)})
        return self._run(job, topic, created=False)

    def _run(self, job: dict[str, Any], topic: dict[str, Any],
             created: bool) -> StartResult:
        work_dir = self.settings.work_dir / f"job-{job['id']}"
        work_dir.mkdir(parents=True, exist_ok=True)
        ctx = Context(settings=self.settings, providers=self.providers,
                      clock=self.clock, job=job, topic=topic, work_dir=work_dir)
        outcomes = self.runner.run(ctx)
        return StartResult(int(job["id"]), int(topic["id"]),
                           str(topic["title"]), created, outcomes)

    # -------------------------------------------------------- reconciling
    def reconcile_uploads(self) -> list[dict[str, Any]]:
        """Resolve uploads whose outcome was never established.

        This is the recovery path for the one genuinely dangerous failure:
        a process that died mid-upload. It asks YouTube what it holds rather
        than sending anything, so it can never create a duplicate.
        """
        from .pipeline.upload import YouTubeClient

        out: list[dict[str, Any]] = []
        pending = repo.uploads_in_doubt()
        if not pending:
            return out

        client = YouTubeClient(self.settings.youtube_client_id,
                               self.settings.youtube_client_secret,
                               self.settings.youtube_refresh_token)
        if not client.configured():
            log.warning("cannot reconcile uploads without YouTube credentials",
                        extra={"in_doubt": len(pending)})
            return [{"upload_id": int(r["id"]), "state": "unresolved",
                     "why": "no credentials"} for r in pending]

        for row in pending:
            upload_id = int(row["id"])
            session_url = row.get("upload_url")
            total = int(row.get("bytes_total") or 0)
            if not session_url or not total:
                out.append({"upload_id": upload_id, "state": "unresolved",
                            "why": "no session recorded"})
                continue
            try:
                state, received, resource = client.session_progress(session_url, total)
            except Exception as exc:  # noqa: BLE001
                out.append({"upload_id": upload_id, "state": "error",
                            "why": str(exc)})
                continue

            if state == "complete" and resource and resource.get("id"):
                repo.set_upload_status(upload_id, "SUCCEEDED",
                                       youtube_video_id=resource["id"],
                                       bytes_sent=total)
                out.append({"upload_id": upload_id, "state": "was_complete",
                            "youtube_video_id": resource["id"]})
            elif state == "incomplete":
                repo.set_upload_status(upload_id, "PENDING", bytes_sent=received)
                out.append({"upload_id": upload_id, "state": "resumable",
                            "bytes_received": received})
            else:
                repo.set_upload_status(upload_id, "FAILED",
                                       error="upload session expired")
                out.append({"upload_id": upload_id, "state": "expired"})

        log.info("upload reconciliation complete", extra={"resolved": len(out)})
        return out

    # ------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        healthy = pool.healthy()
        counters = repo.counters() if healthy else {}
        jobs = repo.recent_jobs(5) if healthy else []
        current = next((j for j in jobs if j["status"] == "RUNNING"), None)
        return {
            "database": "up" if healthy else "down",
            "dry_run": self.settings.dry_run,
            "publish_mode": self.settings.publish_mode,
            "scheduler_enabled": self.settings.scheduler_enabled,
            "timezone": self.settings.timezone,
            "providers": self.providers.describe(),
            "version_stamp": version_stamp(),
            "counters": counters,
            "current_job": {
                "id": current["id"], "topic": current["topic_title"],
                "stage": current["current_stage"],
            } if current else None,
            "recent": [
                {"id": j["id"], "topic": j["topic_title"], "status": j["status"],
                 "stage": j["current_stage"],
                 "started": j["started_at"].isoformat() if j.get("started_at") else None}
                for j in jobs
            ],
            "uploads_in_doubt": len(repo.uploads_in_doubt()) if healthy else None,
        }
