"""The HTTP surface, driven over real requests.

The rule under test is the one that is easy to state and easy to get wrong:
controls that can only *reduce* activity are never locked, and controls that
start, resume or widen activity are refused outright when no token is
configured rather than left open.

These go through the ASGI stack, so they exercise the dependency wiring and
the status codes a proxy will actually see, not a claim about the source.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.db

TEST_DB = os.environ.get("ECHOES_TEST_DATABASE_URL")


@pytest.fixture
def client(db, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATABASE_URL", TEST_DB or "")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("API_TOKEN", "secret-token")
    monkeypatch.setenv("LLM_PROVIDER", "offline")
    monkeypatch.setenv("TTS_PROVIDER", "silent")
    monkeypatch.setenv("RESEARCH_PROVIDERS", "fixtures")
    monkeypatch.setenv("IMAGE_PROVIDERS", "synthetic")
    monkeypatch.setenv("DRY_RUN", "true")

    from echoes.api import app as app_module
    app_module._state.update({"orchestrator": None, "settings": None,
                              "worker": None, "startup_error": None})
    with TestClient(app_module.create_app()) as test_client:
        yield test_client


@pytest.fixture
def untokened_client(db, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATABASE_URL", TEST_DB or "")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "offline")
    monkeypatch.setenv("TTS_PROVIDER", "silent")
    monkeypatch.setenv("RESEARCH_PROVIDERS", "fixtures")
    monkeypatch.setenv("IMAGE_PROVIDERS", "synthetic")

    from echoes.api import app as app_module
    app_module._state.update({"orchestrator": None, "settings": None,
                              "worker": None, "startup_error": None})
    with TestClient(app_module.create_app()) as test_client:
        yield test_client


AUTHED = ("/produce", "/topics/replenish", "/uploads/reconcile",
          "/scheduler/resume")


def test_health_and_status_are_open(client):
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["ok"] is True

    status = client.get("/status")
    assert status.status_code == 200
    assert status.json()["database"] == "up"
    assert status.json()["dry_run"] is True


def test_the_dashboard_is_served(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "Echoes of History" in page.text
    # Presentation only: no control that could start or widen activity.
    assert "/produce" not in page.text


@pytest.mark.parametrize("path", AUTHED)
def test_activity_starting_endpoints_reject_an_absent_token(client, path):
    assert client.post(path).status_code == 401


@pytest.mark.parametrize("path", AUTHED)
def test_activity_starting_endpoints_reject_a_wrong_token(client, path):
    response = client.post(path, headers={"X-API-Token": "not-the-token"})
    assert response.status_code == 401


@pytest.mark.parametrize("path", ["/scheduler/pause", "/stop"])
def test_reducing_controls_never_require_a_token(client, path):
    """An operator must be able to stop the system having lost everything."""
    assert client.post(path).status_code == 200


def test_pause_and_stop_are_recorded_and_readable(client):
    assert client.get("/paused").json() == {"scheduler_paused": False,
                                            "stop_requested": False}
    client.post("/scheduler/pause")
    client.post("/stop")
    assert client.get("/paused").json() == {"scheduler_paused": True,
                                            "stop_requested": True}


def test_resume_requires_the_token_but_pause_does_not(client):
    client.post("/scheduler/pause")
    assert client.post("/scheduler/resume").status_code == 401
    ok = client.post("/scheduler/resume", headers={"X-API-Token": "secret-token"})
    assert ok.status_code == 200
    assert client.get("/paused").json()["scheduler_paused"] is False


def test_a_bearer_token_is_accepted(client):
    response = client.post("/topics/replenish?count=1",
                           headers={"Authorization": "Bearer secret-token"})
    assert response.status_code == 200


@pytest.mark.parametrize("path", AUTHED)
def test_with_no_token_configured_those_endpoints_are_refused_not_open(
        untokened_client, path):
    """The failure mode this prevents: a forgotten variable leaving /produce open."""
    response = untokened_client.post(path)
    assert response.status_code == 503
    assert "API_TOKEN" in response.json()["detail"]


def test_reducing_controls_still_work_with_no_token_configured(untokened_client):
    assert untokened_client.post("/stop").status_code == 200
    assert untokened_client.post("/scheduler/pause").status_code == 200


def test_an_unknown_job_is_a_404(client):
    assert client.get("/jobs/999999").status_code == 404


def test_topics_can_be_replenished_and_appear_in_status(client):
    before = client.get("/status").json()["counters"]["ideas"]
    response = client.post("/topics/replenish?count=3",
                           headers={"X-API-Token": "secret-token"})
    assert response.status_code == 200
    assert len(response.json()["added"]) >= 1
    after = client.get("/status").json()["counters"]["ideas"]
    assert after > before
