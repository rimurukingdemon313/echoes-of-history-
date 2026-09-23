"""The stage runner.

A production is a fixed sequence of stages. The runner's only job is to walk
that sequence and make interruption cheap:

* A stage that already has a ``SUCCEEDED`` row is skipped. A crash after
  rendering therefore resumes at the thumbnail, not at research -- which is
  the difference between losing a minute and losing an hour of encoding.
* A :class:`Retryable` failure is retried with exponential backoff and full
  jitter, bounded by ``RETRY_MAX_ATTEMPTS``.
* A :class:`Permanent` failure stops the job immediately. Retrying it would
  burn quota to reach the same answer.
* An :class:`AmbiguousOutcome` stops the job and marks it
  ``NEEDS_ATTENTION``. It is never retried: it means a side effect may
  already have happened, and the only safe move is to go and look.

The runner deliberately knows nothing about documentaries. Everything that
understands history lives in the stages.
"""

from __future__ import annotations

import random
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from ..clock import Clock
from ..config import Settings
from ..db import repo
from ..errors import (
    AmbiguousOutcome,
    EchoesError,
    Permanent,
    RateLimited,
    Retryable,
)
from ..logging import get_logger
from ..providers.notify.base import Level, NOTIFY_EVENTS
from ..providers.registry import Providers

log = get_logger(__name__)


@dataclass
class Context:
    """Everything a stage is allowed to reach."""

    settings: Settings
    providers: Providers
    clock: Clock
    job: dict[str, Any]
    topic: dict[str, Any]
    work_dir: Path
    # Outputs of earlier stages in this run, by stage name. Stages read from
    # the database rather than this where the value is durable; this carries
    # only what is cheap and in-flight.
    results: dict[str, Any] = field(default_factory=dict)

    @property
    def job_id(self) -> int:
        return int(self.job["id"])

    @property
    def topic_id(self) -> int:
        return int(self.topic["id"])

    @property
    def dry_run(self) -> bool:
        return bool(self.job["dry_run"])

    def stage_dir(self, name: str) -> Path:
        path = self.work_dir / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def log(self, stage: str):
        return get_logger(f"echoes.stage.{stage}",
                          job_id=self.job_id, topic_id=self.topic_id, stage=stage)


class Stage(Protocol):
    name: str

    def run(self, ctx: Context) -> dict[str, Any]:
        """Do the work. Return a small JSON-serialisable summary."""


@dataclass
class StageOutcome:
    stage: str
    status: str
    attempts: int
    duration_ms: int
    output: dict[str, Any] | None = None
    error: str | None = None


def notify(ctx: Context, level: Level, event: str, message: str) -> None:
    """Send an operator notification and record that it was attempted.

    Recording delivery separately from sending is what makes a silently
    broken notifier visible: the dashboard can show notifications that were
    generated but never delivered.
    """
    if event not in NOTIFY_EVENTS:
        return
    provider = ctx.providers.notify
    delivered = False
    try:
        delivered = provider.send(level, event, message)
    except Exception as exc:  # noqa: BLE001 - notification must never break a run
        log.warning("notifier raised", extra={"error": str(exc)})
    try:
        repo.record_notification(
            job_id=ctx.job_id, level=level.value, event=event,
            message=message, provider=provider.name, delivered=delivered,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not record notification", extra={"error": str(exc)})


def _backoff(attempt: int, *, base: float = 2.0, cap: float = 60.0,
             retry_after: float | None = None) -> float:
    if retry_after is not None:
        return min(retry_after, cap)
    return random.uniform(0, min(cap, base * (2 ** attempt)))


class PipelineRunner:
    def __init__(
        self, stages: Sequence[Stage], *, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        self._stages = list(stages)
        # Injectable so tests exercise the retry path without waiting.
        self._sleep = sleep

    @property
    def stage_names(self) -> list[str]:
        return [s.name for s in self._stages]

    def run(self, ctx: Context, *, only: str | None = None) -> list[StageOutcome]:
        """Run the pipeline for ``ctx``. Returns one outcome per stage attempted."""
        done = repo.completed_stages(ctx.job_id)
        outcomes: list[StageOutcome] = []
        repo.set_job_status(ctx.job_id, "RUNNING")

        if not done:
            notify(ctx, Level.INFO, "production_started",
                   f"Started: {ctx.topic['title']}")

        for stage in self._stages:
            if only and stage.name != only:
                continue
            if stage.name in done:
                slog = ctx.log(stage.name)
                slog.info("stage already complete, skipping")
                # Carry the recorded output forward so a resumed run sees the
                # same values the original produced.
                recorded = done[stage.name].get("output")
                if isinstance(recorded, dict):
                    ctx.results[stage.name] = recorded
                outcomes.append(StageOutcome(stage.name, "SKIPPED", 0, 0, recorded))
                continue

            outcome = self._run_stage(stage, ctx)
            outcomes.append(outcome)
            if outcome.status != "SUCCEEDED":
                return outcomes

        repo.set_job_status(ctx.job_id, "SUCCEEDED")
        notify(ctx, Level.INFO, "production_succeeded",
               f"Completed: {ctx.topic['title']}")
        return outcomes

    def _run_stage(self, stage: Stage, ctx: Context) -> StageOutcome:
        slog = ctx.log(stage.name)
        max_attempts = max(1, ctx.settings.retry_max_attempts)
        started_total = time.monotonic()

        for attempt in range(1, max_attempts + 1):
            run_id = repo.start_stage(ctx.job_id, stage.name, attempt)
            began = time.monotonic()
            try:
                slog.info("stage started", extra={"attempt": attempt})
                output = stage.run(ctx) or {}
                elapsed = int((time.monotonic() - began) * 1000)
                repo.finish_stage(run_id, "SUCCEEDED", output=output, duration_ms=elapsed)
                ctx.results[stage.name] = output
                slog.info("stage succeeded",
                          extra={"attempt": attempt, "duration_ms": elapsed,
                                 **{k: v for k, v in output.items()
                                    if isinstance(v, (int, float, str, bool))}})
                return StageOutcome(stage.name, "SUCCEEDED", attempt,
                                    int((time.monotonic() - started_total) * 1000), output)

            except AmbiguousOutcome as exc:
                # Never retried. A repeat could duplicate a side effect that
                # may already have landed.
                elapsed = int((time.monotonic() - began) * 1000)
                repo.finish_stage(run_id, "FAILED", error=str(exc), duration_ms=elapsed)
                self._record(ctx, stage.name, exc, retryable=False)
                repo.set_job_status(ctx.job_id, "NEEDS_ATTENTION",
                                    stage=stage.name, error=str(exc))
                slog.error("ambiguous outcome; job needs human attention")
                notify(ctx, Level.CRITICAL, "needs_attention",
                       f"{ctx.topic['title']}: {stage.name} outcome unknown. "
                       f"{exc}")
                return StageOutcome(stage.name, "AMBIGUOUS", attempt,
                                    int((time.monotonic() - started_total) * 1000),
                                    error=str(exc))

            except Retryable as exc:
                elapsed = int((time.monotonic() - began) * 1000)
                repo.finish_stage(run_id, "FAILED", error=str(exc), duration_ms=elapsed)
                self._record(ctx, stage.name, exc, retryable=True)
                if attempt >= max_attempts:
                    slog.error("stage exhausted retries", extra={"attempts": attempt})
                    return self._fail(ctx, stage.name, exc, attempt, started_total)
                delay = _backoff(
                    attempt,
                    retry_after=getattr(exc, "retry_after_s", None)
                    if isinstance(exc, RateLimited) else None,
                )
                slog.warning("stage failed, retrying",
                             extra={"attempt": attempt, "sleep_s": round(delay, 2),
                                    "error": str(exc)})
                self._sleep(delay)

            except Exception as exc:  # noqa: BLE001 - Permanent and anything unforeseen
                elapsed = int((time.monotonic() - began) * 1000)
                repo.finish_stage(run_id, "FAILED", error=str(exc), duration_ms=elapsed)
                self._record(ctx, stage.name, exc, retryable=False)
                if not isinstance(exc, EchoesError):
                    # An unexpected exception is a bug, not a condition. Keep
                    # the traceback: the message alone rarely locates it.
                    slog.error("stage raised an unexpected error",
                               extra={"traceback": traceback.format_exc()[-2000:]})
                return self._fail(ctx, stage.name, exc, attempt, started_total)

        return self._fail(ctx, stage.name, Permanent("retry loop exhausted"),
                          max_attempts, started_total)

    def _fail(self, ctx: Context, stage: str, exc: Exception, attempt: int,
              started_total: float) -> StageOutcome:
        repo.set_job_status(ctx.job_id, "FAILED", stage=stage, error=str(exc))
        repo.set_topic_status(ctx.topic_id, "FAILED", reason=f"{stage}: {exc}"[:500])
        notify(ctx, Level.CRITICAL, "production_failed",
               f"{ctx.topic['title']} failed at {stage}: {exc}")
        return StageOutcome(stage, "FAILED", attempt,
                            int((time.monotonic() - started_total) * 1000),
                            error=str(exc))

    @staticmethod
    def _record(ctx: Context, stage: str, exc: Exception, *, retryable: bool) -> None:
        try:
            repo.record_error(
                job_id=ctx.job_id, stage=stage, kind=type(exc).__name__,
                message=str(exc), retryable=retryable,
                context={"topic": ctx.topic.get("title")},
            )
        except Exception as err:  # noqa: BLE001
            log.warning("could not record error", extra={"error": str(err)})
