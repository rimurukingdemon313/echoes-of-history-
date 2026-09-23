"""The Library of Congress digital collections.

Free, keyless, and genuinely primary: catalogue records for digitised
photographs, maps, manuscripts and newspapers. Ranked high because the
holding institution vouches for the item's identity and date.
"""

from __future__ import annotations

from ...errors import Permanent
from ...logging import get_logger
from .. import http as http_util
from .base import QUALITY, SourceDoc

log = get_logger(__name__)

SEARCH = "https://www.loc.gov/search/"


class LibraryOfCongressProvider:
    name = "loc"

    def __init__(self, contact_email: str | None = None) -> None:
        self._ua = http_util.user_agent(contact_email)

    def available(self) -> bool:
        return True

    def search(self, query: str, *, limit: int = 5) -> list[SourceDoc]:
        try:
            payload = http_util.get_json(
                SEARCH,
                params={"q": query, "fo": "json", "c": limit, "at": "results"},
                headers={"User-Agent": self._ua, "Accept": "application/json"},
            )
        except Permanent as exc:
            log.warning("loc search rejected", extra={"error": str(exc)})
            return []

        docs: list[SourceDoc] = []
        for item in (payload.get("results") or [])[:limit]:
            url = item.get("id") or item.get("url")
            if not isinstance(url, str) or not url.startswith("http"):
                continue
            description = item.get("description")
            if isinstance(description, list):
                description = " ".join(str(d) for d in description)
            docs.append(
                SourceDoc(
                    url=url,
                    title=str(item.get("title") or "Untitled item"),
                    provider=self.name,
                    text=" ".join(
                        str(p) for p in (description, item.get("subject")) if p
                    )[:8000],
                    source_type="primary_archive",
                    published=_first(item.get("date")),
                    licence=str(item.get("rights") or "see item record"),
                    quality=QUALITY["primary_archive"],
                    rationale="catalogued holding of a national library",
                    extra={"image_urls": _images(item)},
                )
            )
        return docs


def _first(value: object) -> str | None:
    if isinstance(value, list):
        return str(value[0]) if value else None
    return str(value) if value else None


def _images(item: dict) -> list[str]:
    urls = item.get("image_url") or []
    if isinstance(urls, str):
        urls = [urls]
    return [u for u in urls if isinstance(u, str) and u.startswith("http")]
