"""The shared HTTP client.

Reads retry with bounded exponential backoff and full jitter. Writes do not,
and the distinction is enforced by having separate functions rather than a
flag, because a flag defaulting the wrong way is how a retried write becomes
a second video on a channel.

Jitter matters more than it looks: without it, a provider that rate-limits a
batch of parallel image fetches gets all of them back simultaneously on the
retry, and rate-limits them again.
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, Mapping

import httpx

from ..errors import AmbiguousOutcome, Permanent, ProviderUnavailable, RateLimited
from ..logging import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)
USER_AGENT_FALLBACK = "EchoesOfHistory/1.0 (documentary research bot)"

# Retried: the server said "later" or the connection failed mid-flight.
_RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def user_agent(contact_email: str | None) -> str:
    """Wikimedia and Crossref both ask for a contact address in the UA.

    Supplying one is the difference between the polite pool and being
    throttled, so the absence of CONTACT_EMAIL is worth a warning.
    """
    if contact_email:
        return f"EchoesOfHistory/1.0 ({contact_email})"
    return USER_AGENT_FALLBACK


def _sleep_for(attempt: int, *, base: float, cap: float, retry_after: float | None) -> float:
    if retry_after is not None:
        return min(retry_after, cap)
    # Full jitter: uniform in [0, min(cap, base * 2**attempt)].
    ceiling = min(cap, base * (2 ** attempt))
    return random.uniform(0, ceiling)


def get_json(
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    max_attempts: int = 4,
    base_delay: float = 0.5,
    max_delay: float = 20.0,
    client: httpx.Client | None = None,
) -> Any:
    """GET returning parsed JSON, with backoff. Safe to retry: it is a read."""
    return _read(
        "GET", url, params=params, headers=headers, timeout=timeout,
        max_attempts=max_attempts, base_delay=base_delay, max_delay=max_delay,
        client=client, parse=lambda r: r.json(),
    )


def get_bytes(
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    max_attempts: int = 4,
    max_bytes: int = 40 * 1024 * 1024,
    client: httpx.Client | None = None,
) -> bytes:
    """GET returning raw bytes, with a ceiling.

    The ceiling is not paranoia: an archive can serve a 400 MB TIFF behind a
    thumbnail-looking URL, and downloading it would exhaust the container's
    disk allowance mid-production.
    """

    def parse(r: httpx.Response) -> bytes:
        data = r.content
        if len(data) > max_bytes:
            raise Permanent(
                f"response from {url} is {len(data)} bytes, over the "
                f"{max_bytes} limit"
            )
        return data

    return _read(
        "GET", url, params=params, headers=headers, timeout=timeout,
        max_attempts=max_attempts, base_delay=0.5, max_delay=20.0,
        client=client, parse=parse,
    )


def _read(
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | None,
    headers: Mapping[str, str] | None,
    timeout: httpx.Timeout | None,
    max_attempts: int,
    base_delay: float,
    max_delay: float,
    client: httpx.Client | None,
    parse: Callable[[httpx.Response], Any],
) -> Any:
    last: Exception | None = None
    owned = client is None
    http = client or httpx.Client(timeout=timeout or DEFAULT_TIMEOUT, follow_redirects=True)
    try:
        for attempt in range(max_attempts):
            try:
                response = http.request(method, url, params=params, headers=dict(headers or {}))
                if response.status_code in _RETRY_STATUS:
                    retry_after = _retry_after(response)
                    if response.status_code == 429:
                        last = RateLimited(f"{url} rate limited", retry_after)
                    else:
                        last = ProviderUnavailable(
                            f"{url} returned {response.status_code}"
                        )
                    if attempt == max_attempts - 1:
                        break
                    delay = _sleep_for(attempt, base=base_delay, cap=max_delay,
                                       retry_after=retry_after)
                    log.warning(
                        "retrying read",
                        extra={"url": url, "status": response.status_code,
                               "attempt": attempt + 1, "sleep_s": round(delay, 2)},
                    )
                    time.sleep(delay)
                    continue
                if response.status_code >= 400:
                    # 4xx other than the ones above will not change on a retry.
                    raise Permanent(f"{url} returned {response.status_code}")
                return parse(response)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = ProviderUnavailable(f"{url}: {exc}")
                if attempt == max_attempts - 1:
                    break
                delay = _sleep_for(attempt, base=base_delay, cap=max_delay, retry_after=None)
                log.warning("retrying read after transport error",
                            extra={"url": url, "attempt": attempt + 1,
                                   "sleep_s": round(delay, 2)})
                time.sleep(delay)
        raise last or ProviderUnavailable(f"{url} failed")
    finally:
        if owned:
            http.close()


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def post_json_once(
    url: str,
    *,
    json_body: Any,
    headers: Mapping[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    client: httpx.Client | None = None,
) -> Any:
    """POST exactly once.

    A transport failure here raises :class:`AmbiguousOutcome`, not a retryable
    error, because we cannot tell whether the server processed the request.
    The caller must reconcile against remote state rather than send again.
    """
    owned = client is None
    http = client or httpx.Client(timeout=timeout or DEFAULT_TIMEOUT, follow_redirects=True)
    try:
        try:
            response = http.post(url, json=json_body, headers=dict(headers or {}))
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise AmbiguousOutcome(
                f"POST {url} did not return a response: {exc}. The request may "
                f"have been processed; query remote state before retrying."
            ) from exc
        if response.status_code == 429:
            raise RateLimited(f"{url} rate limited", _retry_after(response))
        if response.status_code >= 500:
            raise ProviderUnavailable(f"{url} returned {response.status_code}")
        if response.status_code >= 400:
            raise Permanent(f"{url} returned {response.status_code}: {response.text[:400]}")
        return response.json()
    finally:
        if owned:
            http.close()
