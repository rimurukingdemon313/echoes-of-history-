"""Wikipedia via the MediaWiki Action API.

Wikipedia is treated as an *orientation* source, not an authority: quality
0.60, and the fact-checker requires corroboration before a claim resting on
it alone is called supported. Its real value here is the reference list it
points at, which is why ``extra['references']`` is populated.
"""

from __future__ import annotations

from typing import Any

from ...logging import get_logger
from .. import http as http_util
from .base import QUALITY, SourceDoc

log = get_logger(__name__)

API = "https://en.wikipedia.org/w/api.php"


class WikipediaProvider:
    name = "wikipedia"

    def __init__(self, contact_email: str | None = None, lang: str = "en") -> None:
        self._ua = http_util.user_agent(contact_email)
        self._api = f"https://{lang}.wikipedia.org/w/api.php"

    def available(self) -> bool:
        return True

    def _headers(self) -> dict[str, str]:
        # Wikimedia asks every bot to identify itself and will throttle a
        # generic agent. This is a policy requirement, not a nicety.
        return {"User-Agent": self._ua, "Accept": "application/json"}

    def search(self, query: str, *, limit: int = 5) -> list[SourceDoc]:
        found = http_util.get_json(
            self._api,
            params={
                "action": "query", "format": "json", "list": "search",
                "srsearch": query, "srlimit": limit, "srprop": "snippet",
            },
            headers=self._headers(),
        )
        titles = [r["title"] for r in (found.get("query", {}).get("search") or [])]
        return [doc for t in titles if (doc := self.fetch(t)) is not None]

    def fetch(self, title: str) -> SourceDoc | None:
        payload = http_util.get_json(
            self._api,
            params={
                "action": "query", "format": "json", "prop": "extracts|info",
                "titles": title, "explaintext": 1, "inprop": "url",
                "redirects": 1,
            },
            headers=self._headers(),
        )
        pages: dict[str, Any] = (payload.get("query", {}).get("pages") or {})
        for page_id, page in pages.items():
            if page_id == "-1" or "missing" in page:
                continue
            text = (page.get("extract") or "").strip()
            if not text:
                continue
            return SourceDoc(
                url=page.get("fullurl") or f"https://en.wikipedia.org/wiki/{title}",
                title=page.get("title", title),
                provider=self.name,
                text=text,
                source_type="encyclopaedia",
                licence="CC BY-SA 4.0",
                quality=QUALITY["encyclopaedia"],
                rationale="general reference; requires corroboration",
            )
        return None
