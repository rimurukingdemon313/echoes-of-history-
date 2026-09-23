"""Operator notifications.

The design constraint is restraint. A system that notifies on every stage
trains its operator to ignore it, and the one message that mattered -- the
refresh token expired, nothing has published for three days -- arrives in a
stream of noise. Only the events in :data:`NOTIFY_EVENTS` are sent.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol


class Level(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


# Events worth a phone buzz. Everything else goes to the log and the
# dashboard, where it can be looked at deliberately.
NOTIFY_EVENTS = frozenset({
    "production_started",
    "production_succeeded",
    "upload_succeeded",
    "published",
    "production_failed",
    "auth_expired",
    "needs_attention",
    "quota_exhausted",
})


class NotifyProvider(Protocol):
    name: str

    def send(self, level: Level, event: str, message: str) -> bool:
        """Deliver. Returns False on failure; never raises."""

    def available(self) -> bool:
        ...


class NoopNotifier:
    """Records nothing, delivers nothing, fails never."""

    name = "noop"

    def send(self, level: Level, event: str, message: str) -> bool:
        return True

    def available(self) -> bool:
        return True
