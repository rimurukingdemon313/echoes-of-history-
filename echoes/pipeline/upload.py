"""YouTube upload.

The hazard this module exists to manage: an upload is a write with no
client-supplied idempotency key. If the connection drops after YouTube has
accepted the video but before the response arrives, retrying blindly puts a
second copy on the channel, and nothing in the API will tell you that
happened.

So uploads use the resumable protocol, and the session URL is persisted
*before any bytes are sent*. That turns the dangerous case into a safe one:
a ``PUT`` to an existing session with ``Content-Range: bytes */TOTAL`` asks
YouTube what it already has. The answer is either "I have N bytes" (308, with
a Range header) or "I already finished, here is the video" (200/201, with the
resource). Either way we learn the truth instead of guessing, and we never
send the file twice.

If even that cannot be established, the stage raises
:class:`AmbiguousOutcome`, which the runner refuses to retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx

from ..errors import (
    AmbiguousOutcome,
    ConfigError,
    Permanent,
    ProviderUnavailable,
    RateLimited,
)
from ..logging import get_logger

log = get_logger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMBNAIL_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

# 8 MiB. Large enough that a 90-minute film is a manageable number of
# requests, small enough that a dropped connection loses little work. Google
# requires resumable chunks to be a multiple of 256 KiB.
CHUNK_BYTES = 8 * 1024 * 1024
_PRIVACY = ("private", "unlisted", "public")


@dataclass
class UploadResult:
    youtube_video_id: str
    privacy_status: str
    publish_at: datetime | None
    bytes_sent: int
    resumed: bool = False


class YouTubeClient:
    def __init__(
        self, client_id: str | None, client_secret: str | None,
        refresh_token: str | None, *, timeout_s: float = 120.0,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token: str | None = None
        self._timeout = httpx.Timeout(connect=15.0, read=timeout_s,
                                      write=timeout_s, pool=15.0)

    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret and self._refresh_token)

    def _require(self) -> None:
        missing = [
            name for name, value in (
                ("YOUTUBE_CLIENT_ID", self._client_id),
                ("YOUTUBE_CLIENT_SECRET", self._client_secret),
                ("YOUTUBE_REFRESH_TOKEN", self._refresh_token),
            ) if not value
        ]
        if missing:
            raise ConfigError("YouTube upload needs " + ", ".join(missing))

    def access_token(self, *, force: bool = False) -> str:
        """Exchange the refresh token for an access token.

        A refresh token that has been revoked or expired returns 400 with
        ``invalid_grant``. That is a permanent failure needing a human to
        re-authorise, and it is reported as such rather than retried -- the
        distinction matters because retrying looks identical to being down.
        """
        self._require()
        if self._access_token and not force:
            return self._access_token
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(TOKEN_URL, data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                    "grant_type": "refresh_token",
                })
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ProviderUnavailable(f"token endpoint unreachable: {exc}") from exc

        if response.status_code == 400:
            body = response.json() if response.headers.get(
                "content-type", "").startswith("application/json") else {}
            if body.get("error") == "invalid_grant":
                raise Permanent(
                    "YOUTUBE_REFRESH_TOKEN is no longer valid (invalid_grant). "
                    "Re-authorise the channel and set a new refresh token; the "
                    "system cannot recover from this on its own."
                )
            raise Permanent(f"token refresh refused: {body.get('error', '400')}")
        if response.status_code >= 500:
            raise ProviderUnavailable(f"token endpoint returned {response.status_code}")
        if response.status_code >= 400:
            raise Permanent(f"token refresh failed: {response.status_code}")

        token = (response.json() or {}).get("access_token")
        if not token:
            raise Permanent("token refresh returned no access_token")
        self._access_token = token
        return token

    # ------------------------------------------------------------------
    def start_session(
        self, *, title: str, description: str, tags: list[str],
        category_id: str, language: str, privacy_status: str,
        publish_at: datetime | None, size_bytes: int,
        made_for_kids: bool = False,
    ) -> str:
        """Create a resumable upload session; return its URL.

        Scheduling requires ``privacyStatus`` to be ``private`` alongside
        ``publishAt``. Sending ``public`` with a future ``publishAt`` does not
        schedule the video -- it publishes it immediately.
        """
        if privacy_status not in _PRIVACY:
            raise Permanent(
                f"privacyStatus must be one of {_PRIVACY}, got {privacy_status!r}"
            )
        status: dict[str, Any] = {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": made_for_kids,
        }
        if publish_at is not None:
            if privacy_status != "private":
                raise Permanent(
                    "a scheduled publish requires privacyStatus=private; "
                    f"got {privacy_status!r}, which would publish immediately"
                )
            status["publishAt"] = publish_at.isoformat().replace("+00:00", "Z")

        body = {
            "snippet": {
                "title": title, "description": description, "tags": tags,
                "categoryId": category_id, "defaultLanguage": language,
                "defaultAudioLanguage": language,
            },
            "status": status,
        }
        headers = {
            "Authorization": f"Bearer {self.access_token()}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size_bytes),
            "X-Upload-Content-Type": "video/mp4",
        }
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(
                    UPLOAD_URL,
                    params={"uploadType": "resumable",
                            "part": "snippet,status"},
                    json=body, headers=headers,
                )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # No bytes were sent, so nothing can have been created. Safe to
            # surface as retryable.
            raise ProviderUnavailable(f"could not open upload session: {exc}") from exc

        if response.status_code == 401:
            self.access_token(force=True)
            raise ProviderUnavailable("access token rejected; refreshed, retry")
        if response.status_code == 403:
            raise self._quota_or_permission(response)
        if response.status_code >= 400:
            raise Permanent(
                f"upload session refused ({response.status_code}): "
                f"{response.text[:400]}"
            )

        location = response.headers.get("location")
        if not location:
            raise Permanent("upload session response carried no Location header")
        return location

    @staticmethod
    def _quota_or_permission(response: httpx.Response) -> Exception:
        text = response.text[:500]
        if "quota" in text.lower():
            return RateLimited(
                "YouTube upload quota exhausted for today. Uploads have their "
                "own daily bucket; this resets at midnight Pacific."
            )
        return Permanent(f"YouTube refused the request (403): {text}")

    def session_progress(self, session_url: str, size_bytes: int
                         ) -> tuple[str, int, dict[str, Any] | None]:
        """Ask YouTube what it already holds for this session.

        Returns ``(state, bytes_received, resource)`` where state is
        ``"incomplete"``, ``"complete"`` or ``"gone"``. This is the query that
        makes an interrupted upload recoverable without risking a duplicate.
        """
        headers = {
            "Authorization": f"Bearer {self.access_token()}",
            "Content-Length": "0",
            "Content-Range": f"bytes */{size_bytes}",
        }
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.put(session_url, headers=headers, content=b"")
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ProviderUnavailable(f"could not query upload session: {exc}") from exc

        if response.status_code in (200, 201):
            try:
                return "complete", size_bytes, response.json()
            except ValueError:
                return "complete", size_bytes, None
        if response.status_code == 308:
            received = 0
            rng = response.headers.get("range")
            if rng and "-" in rng:
                try:
                    received = int(rng.split("-")[-1]) + 1
                except ValueError:
                    received = 0
            return "incomplete", received, None
        if response.status_code in (404, 410):
            # The session expired. Resumable sessions are valid for about a
            # week; beyond that a fresh one is required.
            return "gone", 0, None
        raise ProviderUnavailable(
            f"unexpected session status {response.status_code}"
        )

    def upload(
        self, session_url: str, path: Path, *, size_bytes: int,
        start_at: int = 0, on_progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        """Send the file from ``start_at``. Returns the video resource."""
        sent = start_at
        with open(path, "rb") as handle:
            handle.seek(start_at)
            with httpx.Client(timeout=self._timeout) as client:
                while sent < size_bytes:
                    chunk = handle.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    last = sent + len(chunk) - 1
                    headers = {
                        "Authorization": f"Bearer {self.access_token()}",
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {sent}-{last}/{size_bytes}",
                    }
                    try:
                        response = client.put(session_url, headers=headers,
                                              content=chunk)
                    except (httpx.TimeoutException, httpx.TransportError) as exc:
                        # The bytes may or may not have landed. Do not resend
                        # blindly; ask the session what it holds.
                        raise AmbiguousOutcome(
                            f"upload connection lost at byte {sent} of "
                            f"{size_bytes}: {exc}. Query the session before "
                            f"sending anything further."
                        ) from exc

                    if response.status_code in (200, 201):
                        if on_progress:
                            on_progress(size_bytes, size_bytes)
                        try:
                            return response.json()
                        except ValueError as exc:
                            raise AmbiguousOutcome(
                                "YouTube accepted the upload but returned an "
                                "unreadable body; the video may exist"
                            ) from exc

                    if response.status_code == 308:
                        rng = response.headers.get("range")
                        if rng and "-" in rng:
                            try:
                                sent = int(rng.split("-")[-1]) + 1
                            except ValueError:
                                sent = last + 1
                        else:
                            sent = last + 1
                        if on_progress:
                            on_progress(sent, size_bytes)
                        continue

                    if response.status_code in (500, 502, 503, 504):
                        raise ProviderUnavailable(
                            f"YouTube returned {response.status_code} mid-upload; "
                            f"the session can be resumed"
                        )
                    if response.status_code == 403:
                        raise self._quota_or_permission(response)
                    raise Permanent(
                        f"upload rejected ({response.status_code}): "
                        f"{response.text[:300]}"
                    )

        raise AmbiguousOutcome(
            "upload loop ended without a completion response; query the session"
        )

    def set_thumbnail(self, video_id: str, path: Path) -> bool:
        """Attach the thumbnail. Failure is not fatal to a published video."""
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(
                    THUMBNAIL_URL, params={"videoId": video_id},
                    headers={"Authorization": f"Bearer {self.access_token()}",
                             "Content-Type": "image/jpeg"},
                    content=Path(path).read_bytes(),
                )
            if response.status_code < 300:
                return True
            log.warning("thumbnail rejected",
                        extra={"status": response.status_code,
                               "body": response.text[:200]})
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("thumbnail upload failed", extra={"error": str(exc)})
            return False

    def get_video(self, video_id: str) -> dict[str, Any] | None:
        """Read back a video. Used to confirm an ambiguous upload landed."""
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.get(
                    VIDEOS_URL,
                    params={"part": "status,snippet", "id": video_id},
                    headers={"Authorization": f"Bearer {self.access_token()}"},
                )
            if response.status_code >= 400:
                return None
            items = (response.json() or {}).get("items") or []
            return items[0] if items else None
        except Exception:  # noqa: BLE001
            return None
