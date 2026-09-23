"""The error taxonomy.

The distinction that matters is :class:`Retryable` versus :class:`Permanent`.
The stage runner retries the first with backoff and gives up immediately on
the second, so putting an exception in the wrong class is how a system either
hammers a dead API or abandons a job that would have succeeded on the second
try.

:class:`AmbiguousOutcome` is neither. It means a side effect may or may not
have happened -- a YouTube upload whose response never arrived. Retrying it
risks a duplicate video on the channel, so the only recovery is to go and
look at the remote state.
"""

from __future__ import annotations


class EchoesError(Exception):
    """Base for everything this system raises deliberately."""


class Retryable(EchoesError):
    """A transient failure. The runner may try again after a backoff."""


class Permanent(EchoesError):
    """A failure that will not fix itself. Stop and report."""


class AmbiguousOutcome(EchoesError):
    """A write whose result is unknown. Never retried; always reconciled."""


class ConfigError(Permanent):
    """Configuration is missing or contradictory. Named field required."""


class ProviderUnavailable(Retryable):
    """An external provider failed in a way that may pass later."""


class RateLimited(Retryable):
    """A provider refused because of its own rate limit."""

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class QualityGateFailed(Permanent):
    """Quality control refused to let a video through. Never publish."""

    def __init__(self, message: str, failures: list[str] | None = None) -> None:
        super().__init__(message)
        self.failures = failures or []


class FactCheckFailed(Permanent):
    """Too many claims in the script could not be supported by sources."""

    def __init__(self, message: str, unsupported: list[str] | None = None) -> None:
        super().__init__(message)
        self.unsupported = unsupported or []


class DuplicateTopic(Permanent):
    """The proposed topic is too close to one already produced."""
