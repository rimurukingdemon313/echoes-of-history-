"""The HTTP surface.

This is what n8n drives and what the dashboard reads. Two rules shape the
authorisation model, both learned the hard way on systems like this:

**Authorisation splits by direction, not by endpoint.** Controls that can
only *reduce* activity -- pausing the scheduler, stopping production -- are
never locked. An operator who has lost their token, or is on a phone in a
hurry, must still be able to stop the machine. Controls that *start, widen or
resume* activity require the token.

**With no token configured, the second group is refused rather than left
open.** A deployment that forgot to set ``API_TOKEN`` gets a clear refusal
naming the variable, not an unauthenticated endpoint that starts uploads.

Long work never runs inside a request. ``/produce`` starts a background
thread and returns the job id immediately, because a 40-minute render cannot
live inside an HTTP request that any proxy will time out.
"""

from __future__ import annotations

import threading
from typing import Any

from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from ..clock import Clock
from ..config import Settings, load_settings
from ..db import migrate, pool, repo
from ..errors import DuplicateTopic, EchoesError, Permanent
from ..logging import configure, get_logger
from ..orchestrator import Orchestrator
from ..version import version_stamp
from .dashboard import DASHBOARD_HTML

log = get_logger(__name__)

_state: dict[str, Any] = {"orchestrator": None, "settings": None,
                          "worker": None, "startup_error": None}
_lock = threading.Lock()


def get_settings() -> Settings:
    settings = _state.get("settings")
    if settings is None:
        raise HTTPException(503, "service is still starting")
    return settings


def get_orchestrator() -> Orchestrator:
    orchestrator = _state.get("orchestrator")
    if orchestrator is None:
        raise HTTPException(
            503, _state.get("startup_error") or "service is still starting")
    return orchestrator


def require_token(
    authorization: str | None = Header(default=None),
    x_api_token: str | None = Header(default=None),
) -> None:
    """Guard for anything that starts, widens or resumes activity."""
    settings = get_settings()
    if not settings.api_token:
        raise HTTPException(
            503,
            "API_TOKEN is not configured. Endpoints that can start or widen "
            "activity are refused rather than left unauthenticated. Set "
            "API_TOKEN and restart.",
        )
    supplied = x_api_token
    if not supplied and authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:]
    if supplied != settings.api_token:
        raise HTTPException(401, "invalid or missing API token")


def _start() -> None:
    settings = load_settings()
    configure("INFO")
    _state["settings"] = settings
    try:
        settings.validate()
        pool.init_pool(settings.database_url)
        migrate.migrate()
        _state["orchestrator"] = Orchestrator(settings, Clock())
        log.info("service ready", extra={"dry_run": settings.dry_run,
                                         "publish_mode": settings.publish_mode})
    except Exception as exc:  # noqa: BLE001
        # A failed startup must not take down the health endpoint. An
        # operator needs to be able to see *why* it is broken, and a service
        # that goes dark on a bad configuration tells them nothing.
        _state["startup_error"] = str(exc)
        log.error("startup failed", extra={"error": str(exc)})


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _start()
    yield
    pool.close_pool()


def create_app() -> FastAPI:
    app = FastAPI(title="Echoes of History", version=version_stamp(),
                  docs_url=None, redoc_url=None, lifespan=_lifespan)

    # ---------------------------------------------------------- health --
    @app.get("/healthz")
    def healthz() -> JSONResponse:
        ready = _state.get("orchestrator") is not None
        database = pool.healthy() if ready else False
        body = {
            "ok": ready and database,
            "ready": ready,
            "database": "up" if database else "down",
            "version": version_stamp(),
            "error": _state.get("startup_error"),
        }
        return JSONResponse(body, status_code=200 if body["ok"] else 503)

    @app.get("/status")
    def status(orchestrator: Orchestrator = Depends(get_orchestrator)) -> dict[str, Any]:
        return orchestrator.status()

    @app.get("/jobs")
    def jobs(limit: int = Query(20, ge=1, le=100),
             _: Orchestrator = Depends(get_orchestrator)) -> dict[str, Any]:
        return {"jobs": repo.recent_jobs(limit)}

    @app.get("/jobs/{job_id}")
    def job(job_id: int, _: Orchestrator = Depends(get_orchestrator)) -> dict[str, Any]:
        row = repo.get_job(job_id)
        if not row:
            raise HTTPException(404, f"job {job_id} not found")
        video = repo.get_video(job_id)
        return {
            "job": row,
            "stages": repo.stage_history(job_id),
            "video": video,
            "upload": repo.get_upload(job_id),
            "qc": (video or {}).get("qc_report"),
        }

    # ------------------------------------------------- start / widen ----
    @app.post("/produce", dependencies=[Depends(require_token)])
    def produce(body: dict[str, Any] = Body(default={}),
                orchestrator: Orchestrator = Depends(get_orchestrator)) -> dict[str, Any]:
        """Begin one documentary. Returns immediately; the work runs behind."""
        allowed, why = orchestrator.can_start()
        if not allowed:
            raise HTTPException(409, why)
        with _lock:
            worker = _state.get("worker")
            if worker is not None and worker.is_alive():
                raise HTTPException(409, "a production is already running")

            key = body.get("idempotency_key")
            topic_id = body.get("topic_id")
            dry_run = body.get("dry_run")
            publish_mode = body.get("publish_mode")
            result_box: dict[str, Any] = {}

            def work() -> None:
                try:
                    result = orchestrator.start(
                        topic_id=topic_id, idempotency_key=key,
                        dry_run=dry_run, publish_mode=publish_mode)
                    result_box.update(result.summary())
                except Exception as exc:  # noqa: BLE001
                    result_box["error"] = str(exc)
                    log.error("production failed", extra={"error": str(exc)})

            thread = threading.Thread(target=work, name="production", daemon=True)
            _state["worker"] = thread
            _state["result"] = result_box
            thread.start()
        return {"accepted": True, "idempotency_key": key,
                "poll": "/status and /jobs"}

    @app.post("/jobs/{job_id}/resume", dependencies=[Depends(require_token)])
    def resume(job_id: int,
               orchestrator: Orchestrator = Depends(get_orchestrator)) -> dict[str, Any]:
        with _lock:
            worker = _state.get("worker")
            if worker is not None and worker.is_alive():
                raise HTTPException(409, "a production is already running")
            thread = threading.Thread(
                target=lambda: orchestrator.resume(job_id),
                name=f"resume-{job_id}", daemon=True)
            _state["worker"] = thread
            thread.start()
        return {"accepted": True, "job_id": job_id}

    @app.post("/topics/replenish", dependencies=[Depends(require_token)])
    def replenish(count: int = Query(8, ge=1, le=40),
                  orchestrator: Orchestrator = Depends(get_orchestrator)) -> dict[str, Any]:
        try:
            return orchestrator.replenish_topics(want=count)
        except DuplicateTopic as exc:
            raise HTTPException(409, str(exc)) from exc
        except EchoesError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/uploads/reconcile", dependencies=[Depends(require_token)])
    def reconcile(orchestrator: Orchestrator = Depends(get_orchestrator)) -> dict[str, Any]:
        return {"resolved": orchestrator.reconcile_uploads()}

    @app.post("/scheduler/resume", dependencies=[Depends(require_token)])
    def scheduler_resume(_: Orchestrator = Depends(get_orchestrator)) -> dict[str, Any]:
        repo.set_setting("scheduler_paused", False)
        return {"scheduler_paused": False}

    # ------------------------------------------------------ reduce only --
    # Deliberately unauthenticated. Stopping the machine must work from any
    # device, including one whose operator has lost the token.
    @app.post("/scheduler/pause")
    def scheduler_pause() -> dict[str, Any]:
        repo.set_setting("scheduler_paused", True)
        log.warning("scheduler paused by request")
        return {"scheduler_paused": True}

    @app.post("/stop")
    def stop() -> dict[str, Any]:
        repo.set_setting("stop_requested", True)
        log.warning("stop requested; no new production will start")
        return {"stop_requested": True,
                "note": "a production already running will finish its current "
                        "stage and then stop at the next checkpoint"}

    @app.get("/paused")
    def paused() -> dict[str, Any]:
        return {"scheduler_paused": bool(repo.get_setting("scheduler_paused", False)),
                "stop_requested": bool(repo.get_setting("stop_requested", False))}

    # ------------------------------------------------------- dashboard --
    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        return HTMLResponse(DASHBOARD_HTML)

    return app


app = create_app()
