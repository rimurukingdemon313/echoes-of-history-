"""Crossref: DOIs for peer-reviewed scholarship.

Crossref returns metadata and abstracts, not full text, which is exactly
what is wanted here. A documentary should not paraphrase a paywalled paper
it cannot read; citing that the scholarship exists, and what its abstract
claims, is honest and legal.
"""

from __future__ import annotations

import re

from ...errors import EchoesError
from ...logging import get_logger
from .. import http as http_util
from .base import QUALITY, SourceDoc

log = get_logger(__name__)

API = "https://api.crossref.org/works"
_TAGS = re.compile(r"<[^>]+>")


class CrossrefProvider:
    name = "crossref"

    def __init__(self, contact_email: str | None = None) -> None:
        self._email = contact_email
        self._ua = http_util.user_agent(contact_email)

    def available(self) -> bool:
        return True

    def search(self, query: str, *, limit: int = 5) -> list[SourceDoc]:
        params = {"query.bibliographic": query, "rows": limit,
                  "select": "DOI,title,abstract,author,issued,container-title,URL,type"}
        # Supplying a mailto puts the request in Crossref's "polite pool",
        # which is faster and less likely to be throttled.
        if self._email:
            params["mailto"] = self._email
        try:
            payload = http_util.get_json(
                API, params=params,
                headers={"User-Agent": self._ua, "Accept": "application/json"},
            )
        except EchoesError as exc:
            log.warning("crossref search failed", extra={"error": str(exc)})
            return []

        docs: list[SourceDoc] = []
        for item in (payload.get("message", {}).get("items") or [])[:limit]:
            titles = item.get("title") or []
            title = titles[0] if titles else "Untitled work"
            abstract = _TAGS.sub(" ", item.get("abstract") or "").strip()
            authors = ", ".join(
                " ".join(p for p in (a.get("given"), a.get("family")) if p)
                for a in (item.get("author") or [])[:4]
            )
            year = None
            issued = (item.get("issued") or {}).get("date-parts") or []
            if issued and issued[0]:
                year = str(issued[0][0])
            docs.append(
                SourceDoc(
                    url=item.get("URL") or f"https://doi.org/{item.get('DOI','')}",
                    title=str(title),
                    provider=self.name,
                    text=abstract[:6000],
                    source_type="peer_reviewed",
                    author=authors or None,
                    published=year,
                    licence="metadata: CC0 (Crossref); article rights vary",
                    quality=QUALITY["peer_reviewed"],
                    rationale="registered scholarly work with a DOI",
                    extra={"doi": item.get("DOI"),
                           "journal": (item.get("container-title") or [None])[0]},
                )
            )
        return docs
