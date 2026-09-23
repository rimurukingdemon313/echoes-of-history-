"""The Metropolitan Museum of Art Open Access collection.

Keyless. Its value is twofold: catalogued object records written by curators,
and a clear public-domain flag on the images. The flag is why this is also
registered as an image provider -- ``isPublicDomain`` is an assertion by the
holding museum, which is the strongest licence signal available for free.
"""

from __future__ import annotations

from ...errors import EchoesError
from ...logging import get_logger
from .. import http as http_util
from .base import QUALITY, SourceDoc

log = get_logger(__name__)

BASE = "https://collectionapi.metmuseum.org/public/collection/v1"


class MetMuseumProvider:
    name = "met"

    def __init__(self, contact_email: str | None = None) -> None:
        self._ua = http_util.user_agent(contact_email)

    def available(self) -> bool:
        return True

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self._ua, "Accept": "application/json"}

    def search(self, query: str, *, limit: int = 5) -> list[SourceDoc]:
        try:
            found = http_util.get_json(
                f"{BASE}/search",
                params={"q": query, "hasImages": "true"},
                headers=self._headers(),
            )
        except EchoesError as exc:
            log.warning("met search failed", extra={"error": str(exc)})
            return []

        ids = (found.get("objectIDs") or [])[:limit]
        docs: list[SourceDoc] = []
        for object_id in ids:
            try:
                obj = http_util.get_json(f"{BASE}/objects/{object_id}",
                                         headers=self._headers())
            except EchoesError:
                continue
            if not obj or not obj.get("objectID"):
                continue
            public_domain = bool(obj.get("isPublicDomain"))
            text_parts = [
                obj.get("title"), obj.get("culture"), obj.get("period"),
                obj.get("objectDate"), obj.get("medium"), obj.get("creditLine"),
            ]
            docs.append(
                SourceDoc(
                    url=obj.get("objectURL") or f"{BASE}/objects/{object_id}",
                    title=str(obj.get("title") or f"Object {object_id}"),
                    provider=self.name,
                    text=" -- ".join(str(p) for p in text_parts if p)[:4000],
                    source_type="museum_catalogue",
                    author=str(obj.get("artistDisplayName") or "") or None,
                    published=str(obj.get("objectDate") or "") or None,
                    licence="Public Domain (CC0)" if public_domain else "rights reserved",
                    quality=QUALITY["museum_catalogue"],
                    rationale="curatorial catalogue record",
                    extra={
                        "image_urls": [u for u in (obj.get("primaryImage"),
                                                   obj.get("primaryImageSmall")) if u]
                        if public_domain else [],
                        "public_domain": public_domain,
                    },
                )
            )
        return docs
