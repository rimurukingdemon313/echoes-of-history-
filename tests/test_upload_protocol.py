"""The resumable upload protocol, against a server that behaves like YouTube.

This is the riskiest code in the system: a write with no client-supplied
idempotency key, where the wrong recovery puts a second video on a real
channel. It cannot be exercised against YouTube in a test, so these drive a
local server implementing the parts of the protocol the client depends on:

- a session is created and its URL returned in ``Location``;
- ``Content-Range: bytes */TOTAL`` asks what the server holds, answering 308
  with a ``Range`` header, or 200 with the resource when it is already done;
- chunks are acknowledged with 308 and a ``Range``;
- an expired session answers 404.

The behaviour under test is not "does it upload". It is "when the connection
drops, does it ever send the file twice".
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from echoes.errors import AmbiguousOutcome, ConfigError, Permanent
from echoes.pipeline.upload import YouTubeClient

STATE: dict = {}


class _FakeYouTube(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def _json(self, code: int, body: dict, headers: dict | None = None):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path.startswith("/token"):
            return self._json(200, {"access_token": "fake-access-token"})
        if self.path.startswith("/upload"):
            STATE["sessions"] = STATE.get("sessions", 0) + 1
            STATE["received"] = 0
            return self._json(200, {}, {"Location":
                                        f"http://{self.headers['Host']}/session/1"})
        return self._json(404, {})

    def do_PUT(self):
        if not self.path.startswith("/session/"):
            return self._json(404, {})
        if STATE.get("expired"):
            return self._json(404, {"error": "session gone"})

        content_range = self.headers.get("Content-Range", "")
        total = STATE["total"]

        # A query: "what do you already hold?"
        if content_range.startswith("bytes */"):
            STATE["queries"] = STATE.get("queries", 0) + 1
            if STATE["received"] >= total:
                return self._json(200, {"id": "VIDEO123"})
            if STATE["received"] == 0:
                self.send_response(308)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(308)
            self.send_header("Range", f"bytes=0-{STATE['received'] - 1}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        STATE["bytes_seen"] = STATE.get("bytes_seen", 0) + len(body)
        STATE["chunks"] = STATE.get("chunks", 0) + 1
        STATE["received"] += len(body)

        if STATE["received"] >= total:
            return self._json(200, {"id": "VIDEO123"})
        self.send_response(308)
        self.send_header("Range", f"bytes=0-{STATE['received'] - 1}")
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def server(monkeypatch):
    STATE.clear()
    STATE.update({"received": 0, "total": 0})
    httpd = HTTPServer(("127.0.0.1", 0), _FakeYouTube)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    from echoes.pipeline import upload as upload_mod
    monkeypatch.setattr(upload_mod, "TOKEN_URL", f"{base}/token")
    monkeypatch.setattr(upload_mod, "UPLOAD_URL", f"{base}/upload")
    # A small chunk so a multi-chunk upload is exercised without a large file.
    monkeypatch.setattr(upload_mod, "CHUNK_BYTES", 4096)
    yield base
    httpd.shutdown()


@pytest.fixture
def client():
    return YouTubeClient("client-id", "client-secret", "refresh-token")


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"\x00" * 20000)
    STATE["total"] = path.stat().st_size
    return path


def test_credentials_are_required_by_name():
    with pytest.raises(ConfigError, match="YOUTUBE_CLIENT_ID"):
        YouTubeClient(None, "s", "r").access_token()


def test_a_full_upload_sends_the_file_exactly_once(server, client, video):
    session = client.start_session(
        title="T", description="D", tags=["a"], category_id="27", language="en",
        privacy_status="private", publish_at=None,
        size_bytes=video.stat().st_size)
    resource = client.upload(session, video, size_bytes=video.stat().st_size)
    assert resource["id"] == "VIDEO123"
    assert STATE["bytes_seen"] == video.stat().st_size
    assert STATE["sessions"] == 1


def test_an_interrupted_upload_resumes_without_resending(server, client, video):
    """The whole point: bytes already accepted are never sent again."""
    total = video.stat().st_size
    session = client.start_session(
        title="T", description="D", tags=[], category_id="27", language="en",
        privacy_status="private", publish_at=None, size_bytes=total)

    # Simulate a process that died after the server accepted 8192 bytes.
    STATE["received"] = 8192
    STATE["bytes_seen"] = 0

    state, received, resource = client.session_progress(session, total)
    assert state == "incomplete"
    assert received == 8192

    client.upload(session, video, size_bytes=total, start_at=received)
    # Only the remainder crossed the wire.
    assert STATE["bytes_seen"] == total - 8192


def test_a_completed_upload_is_recognised_not_repeated(server, client, video):
    """The failure this prevents: a second copy of the video on the channel."""
    total = video.stat().st_size
    session = client.start_session(
        title="T", description="D", tags=[], category_id="27", language="en",
        privacy_status="private", publish_at=None, size_bytes=total)

    # The upload finished, but the response never reached us.
    STATE["received"] = total
    STATE["bytes_seen"] = 0

    state, received, resource = client.session_progress(session, total)
    assert state == "complete"
    assert resource["id"] == "VIDEO123"
    assert STATE["bytes_seen"] == 0, "nothing may be re-sent"


def test_an_expired_session_is_reported_as_gone(server, client, video):
    total = video.stat().st_size
    session = client.start_session(
        title="T", description="D", tags=[], category_id="27", language="en",
        privacy_status="private", publish_at=None, size_bytes=total)
    STATE["expired"] = True
    state, _, _ = client.session_progress(session, total)
    assert state == "gone"


def test_a_lost_connection_raises_ambiguous_not_retryable(server, client, video,
                                                          monkeypatch):
    """A dropped connection must never be treated as 'try again'."""
    import httpx
    total = video.stat().st_size
    session = client.start_session(
        title="T", description="D", tags=[], category_id="27", language="en",
        privacy_status="private", publish_at=None, size_bytes=total)

    def explode(*args, **kwargs):
        raise httpx.ConnectError("connection reset")

    monkeypatch.setattr(httpx.Client, "put", explode)
    with pytest.raises(AmbiguousOutcome):
        client.upload(session, video, size_bytes=total)


def test_scheduling_requires_private(server, client):
    """public + publishAt does not schedule; it publishes immediately."""
    from datetime import datetime, timedelta, timezone
    when = datetime.now(timezone.utc) + timedelta(days=1)
    with pytest.raises(Permanent, match="privacyStatus=private"):
        client.start_session(title="T", description="D", tags=[],
                             category_id="27", language="en",
                             privacy_status="public", publish_at=when,
                             size_bytes=10)


def test_an_unknown_privacy_status_is_refused(server, client):
    with pytest.raises(Permanent, match="privacyStatus"):
        client.start_session(title="T", description="D", tags=[],
                             category_id="27", language="en",
                             privacy_status="everyone", publish_at=None,
                             size_bytes=10)


def test_a_scheduled_upload_sends_publish_at_as_rfc3339(server, client,
                                                        monkeypatch):
    from datetime import datetime, timezone
    captured = {}
    import httpx
    original = httpx.Client.post

    def capture(self, url, **kwargs):
        captured.update(kwargs.get("json") or {})
        return original(self, url, **kwargs)

    monkeypatch.setattr(httpx.Client, "post", capture)
    when = datetime(2026, 12, 25, 18, 0, tzinfo=timezone.utc)
    client.start_session(title="T", description="D", tags=[], category_id="27",
                         language="en", privacy_status="private",
                         publish_at=when, size_bytes=10)
    assert captured["status"]["publishAt"] == "2026-12-25T18:00:00Z"
    assert captured["status"]["privacyStatus"] == "private"


def test_a_revoked_refresh_token_is_permanent_not_retryable(monkeypatch, tmp_path):
    """invalid_grant needs a human. Retrying it looks like an outage and hides that."""
    import httpx

    class _Response:
        status_code = 400
        headers = {"content-type": "application/json"}

        @staticmethod
        def json():
            return {"error": "invalid_grant"}

    monkeypatch.setattr(httpx.Client, "post",
                        lambda self, url, **kw: _Response())
    with pytest.raises(Permanent, match="invalid_grant"):
        YouTubeClient("id", "secret", "revoked").access_token()


def test_a_token_endpoint_outage_is_retryable(monkeypatch):
    """A 5xx is the service being down, which is a different thing entirely."""
    import httpx
    from echoes.errors import ProviderUnavailable

    class _Response:
        status_code = 503
        headers = {}

    monkeypatch.setattr(httpx.Client, "post",
                        lambda self, url, **kw: _Response())
    with pytest.raises(ProviderUnavailable):
        YouTubeClient("id", "secret", "token").access_token()
