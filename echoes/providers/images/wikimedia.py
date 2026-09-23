"""Wikimedia Commons.

The single richest free source of historical imagery. ``extmetadata`` carries
the licence and the required attribution, and both are read from there rather
than assumed: a file being on Commons does not make it public domain.
"""

from __future__ import annotations

import re

from ...errors import EchoesError
from ...logging import get_logger
from .. import http as http_util
from .base import ImageCandidate, acceptable_licence

log = get_logger(__name__)

API = "https://commons.wikimedia.org/w/api.php"
_TAGS = re.compile(r"<[^>]+>")


class WikimediaCommonsProvider:
    name = "wikimedia"

    def __init__(self, contact_email: str | None = None, plate_width: int = 2304) -> None:
        self._ua = http_util.user_agent(contact_email)
        self._width = plate_width

    def available(self) -> bool:
        return True

    def search(self, query: str, *, limit: int = 8) -> list[ImageCandidate]:
        try:
            payload = http_util.get_json(
                API,
                params={
                    "action": "query", "format": "json",
                    "generator": "search", "gsrsearch": f"{query} filetype:bitmap",
                    "gsrnamespace": 6, "gsrlimit": limit * 2,
                    "prop": "imageinfo",
                    "iiprop": "url|size|extmetadata",
                    "iiurlwidth": self._width,
                },
                headers={"User-Agent": self._ua, "Accept": "application/json"},
            )
        except EchoesError as exc:
            log.warning("commons search failed", extra={"error": str(exc)})
            return []

        out: list[ImageCandidate] = []
        pages = (payload.get("query", {}).get("pages") or {}).values()
        for page in pages:
            info = (page.get("imageinfo") or [{}])[0]
            meta = info.get("extmetadata") or {}
            licence = _meta(meta, "LicenseShortName") or _meta(meta, "UsageTerms")
            if not acceptable_licence(licence):
                continue
            url = info.get("thumburl") or info.get("url")
            if not url:
                continue
            out.append(
                ImageCandidate(
                    url=url,
                    title=str(page.get("title", "")).removeprefix("File:"),
                    provider=self.name,
                    licence=licence or "unknown",
                    attribution=_meta(meta, "Attribution"),
                    creator=_TAGS.sub("", _meta(meta, "Artist") or "").strip() or None,
                    source_page=info.get("descriptionurl"),
                    width=info.get("thumbwidth") or info.get("width"),
                    height=info.get("thumbheight") or info.get("height"),
                )
            )
            if len(out) >= limit:
                break
        return out


def _meta(meta: dict, key: str) -> str | None:
    entry = meta.get(key)
    if isinstance(entry, dict):
        value = _TAGS.sub("", str(entry.get("value", ""))).strip()
        return value or None
    return None
