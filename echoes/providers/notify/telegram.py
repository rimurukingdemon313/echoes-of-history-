"""Telegram. Free, no infrastructure, reaches a phone."""

from __future__ import annotations

import httpx

from ...logging import get_logger
from .base import Level

log = get_logger(__name__)

_EMOJI = {Level.INFO: "✓", Level.WARNING: "⚠", Level.CRITICAL: "✗"}


class TelegramNotifier:
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str, timeout_s: float = 15.0) -> None:
        self._token = bot_token
        self._chat = chat_id
        self._timeout = timeout_s

    def available(self) -> bool:
        return bool(self._token and self._chat)

    def send(self, level: Level, event: str, message: str) -> bool:
        """Deliver one message.

        Never raises: a notifier that can take down a production run by
        failing is worse than one that silently misses a message. The failure
        is logged and recorded in the notifications table, so a missing
        delivery is still visible on the dashboard.
        """
        if not self.available():
            return False
        text = f"{_EMOJI.get(level, '')} *{event}*\n{message}"[:4000]
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(
                    f"https://api.telegram.org/bot{self._token}/sendMessage",
                    json={"chat_id": self._chat, "text": text,
                          "parse_mode": "Markdown",
                          "disable_web_page_preview": True},
                )
            if response.status_code >= 400:
                log.warning("telegram delivery refused",
                            extra={"status": response.status_code})
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram delivery failed", extra={"error": str(exc)})
            return False
