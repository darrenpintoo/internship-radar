"""Lever postings API adapter (SPEC §5.3) — verified live 2026-08-20.

GET https://api.lever.co/v0/postings/{token}?mode=json — response is a
top-level JSON array. createdAt is epoch MILLISECONDS. descriptionPlain
carries only the opening prose: the bullet `lists` sections (where grad-year /
work-auth eligibility lives) and additionalPlain are separate fields and must
be assembled into the description — the pipeline truncates at store time.
"""

from __future__ import annotations

from .. import http as http_mod
from ..models import Posting
from ..normalize import iso_from_epoch_ms, strip_html
from .base import RawItem


class LeverAdapter:
    name = "lever"
    prefilter_policy = "full"

    def __init__(self, http, companies: list[tuple[str, str]]):
        """companies: [(display_name, board_token)] from the registry."""
        self._http = http
        self._companies = companies

    def _url(self, token: str) -> str:
        return f"https://api.lever.co/v0/postings/{token}?mode=json"

    def fetch(self) -> list[RawItem]:
        items: list[RawItem] = []
        for display, token in self._companies:
            try:
                payload = self._http.get_json(self._url(token))
                items.append(RawItem(self.name, token, payload=payload, meta={"company": display}))
            except http_mod.NotModified:
                items.append(
                    RawItem(self.name, token, not_modified=True, meta={"company": display})
                )
            except Exception as exc:  # one board's failure never kills the rest
                items.append(
                    RawItem(
                        self.name,
                        token,
                        error=f"{type(exc).__name__}: {exc}",
                        meta={"company": display},
                    )
                )
        return items

    @staticmethod
    def _location(categories: dict) -> str | None:
        primary = categories.get("location")
        extras = [loc for loc in categories.get("allLocations") or [] if loc and loc != primary]
        parts = [p for p in [primary, *extras] if p]
        return "; ".join(parts) if parts else None

    @staticmethod
    def _description(job: dict) -> str | None:
        parts = [job.get("descriptionPlain") or ""]
        for section in job.get("lists") or []:
            heading = (section.get("text") or "").strip()
            if heading:
                parts.append(heading)
            content = section.get("content") or ""
            if content:
                parts.append(strip_html(content))
        parts.append(job.get("additionalPlain") or "")
        joined = "\n".join(p for p in parts if p.strip())
        return joined or None

    def parse(self, raw: RawItem) -> list[Posting]:
        display = raw.meta.get("company", raw.source_ref)
        postings = []
        for job in raw.payload:  # top-level JSON array (SPEC §5.3)
            postings.append(
                Posting(
                    company_name=display,
                    title=job["text"],
                    url=job["hostedUrl"],
                    source=self.name,
                    source_ref=raw.source_ref,
                    source_key=job["id"],  # UUID string
                    location_raw=self._location(job.get("categories") or {}),
                    posted_at=iso_from_epoch_ms(job["createdAt"]) if job.get("createdAt") else None,
                    description=self._description(job),
                )
            )
        return postings
