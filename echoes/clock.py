"""Time, in one place.

Nothing in this system calls ``datetime.now()`` directly. Every module that
needs the time takes a :class:`Clock`, so a test can pin it and a scheduler
can be reasoned about without waiting for real hours to pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


class Clock:
    """The real clock. UTC internally, always timezone-aware."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def local(self, tz: str) -> datetime:
        return self.now().astimezone(ZoneInfo(tz))


@dataclass
class FrozenClock(Clock):
    """A clock that does not move unless a test moves it."""

    at: datetime

    def __post_init__(self) -> None:
        if self.at.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")

    def now(self) -> datetime:
        return self.at

    def advance(self, **kw: float) -> "FrozenClock":
        self.at = self.at + timedelta(**kw)
        return self
