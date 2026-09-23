"""Adapters turning research results into image candidates.

Kept apart from the research providers so those stay about text and
provenance, and apart from the image providers so those stay about a single
upstream API.
"""

from __future__ import annotations

from .images.base import ImageCandidate, acceptable_licence


def met_candidates(provider, query: str, limit: int) -> list[ImageCandidate]:
    out: list[ImageCandidate] = []
    for doc in provider.search(query, limit=limit):
        if not doc.extra.get("public_domain"):
            continue
        for url in doc.extra.get("image_urls", []):
            out.append(
                ImageCandidate(
                    url=url, title=doc.title, provider="met",
                    licence="Public Domain (CC0)", creator=doc.author,
                    source_page=doc.url,
                )
            )
            break
        if len(out) >= limit:
            break
    return out


def loc_candidates(provider, query: str, limit: int) -> list[ImageCandidate]:
    out: list[ImageCandidate] = []
    for doc in provider.search(query, limit=limit):
        licence = doc.licence or ""
        # The LoC rights field is free text and often points to a statement
        # page rather than naming a licence. Anything we cannot positively
        # recognise is skipped -- an image is not worth a rights dispute.
        if not acceptable_licence(licence):
            continue
        for url in doc.extra.get("image_urls", []):
            out.append(
                ImageCandidate(
                    url=url if url.startswith("http") else f"https:{url}",
                    title=doc.title, provider="loc", licence=licence,
                    source_page=doc.url,
                )
            )
            break
        if len(out) >= limit:
            break
    return out
