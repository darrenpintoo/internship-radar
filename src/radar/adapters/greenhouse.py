"""Greenhouse boards API adapter (SPEC §5.2) — verified live 2026-08-20.

GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
Response {jobs: [...], meta: {total}} — full board, no pagination.
posted_at maps from first_published (NOT updated_at, which moves on any edit
and would resurface year-old postings as new). content arrives HTML-entity-
escaped: unescape before HTML→text.
"""

from __future__ import annotations

import re

from .. import http as http_mod
from ..models import Posting
from ..normalize import INCLUDE_RE, iso_norm, strip_html
from .base import RawItem

# Boards that time out at the default 30s (verified: Anduril needed >20s).
LARGE_BOARDS = {"spacex", "andurilindustries", "rocketlab"}

# Some boards (Jane Street, verified live 2026-08-20: 50 campus roles) strip
# 'Intern' from public titles and carry the campus flag only in the Greenhouse
# 'Employment Type' metadata field ('Summer Internship', 'Winter Co-Op', ...).
# Folding it into the title keeps the §6.2 prefilter and the digest honest.
_EMP_INTERN_RE = re.compile(r"intern|co[-\s]?op", re.I)


def _employment_type(job: dict) -> str | None:
    for m in job.get("metadata") or []:
        if (m.get("name") or "").strip().lower() == "employment type" and m.get("value"):
            return str(m["value"])
    return None


class GreenhouseAdapter:
    name = "greenhouse"
    prefilter_policy = "full"

    def __init__(self, http, companies: list[tuple[str, str]]):
        """companies: [(display_name, board_token)] from the registry."""
        self._http = http
        self._companies = companies

    def _url(self, token: str) -> str:
        return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"

    def fetch(self) -> list[RawItem]:
        items: list[RawItem] = []
        for display, token in self._companies:
            timeout = (
                http_mod.LARGE_BOARD_TIMEOUT if token in LARGE_BOARDS else http_mod.DEFAULT_TIMEOUT
            )
            try:
                payload = self._http.get_json(self._url(token), timeout=timeout)
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

    def parse(self, raw: RawItem) -> list[Posting]:
        display = raw.meta.get("company", raw.source_ref)
        postings = []
        for job in raw.payload.get("jobs", []):
            content = job.get("content") or ""
            title = job["title"]
            emp_type = _employment_type(job)
            if emp_type and _EMP_INTERN_RE.search(emp_type) and not INCLUDE_RE.search(title):
                title = f"{title} — {emp_type}"
            postings.append(
                Posting(
                    company_name=display,
                    title=title,
                    url=job["absolute_url"],
                    source=self.name,
                    source_ref=raw.source_ref,
                    source_key=str(job["id"]),  # int in the payload — cast (SPEC §5.2)
                    location_raw=(job.get("location") or {}).get("name"),
                    posted_at=iso_norm(job.get("first_published")),
                    description=strip_html(content) if content else None,
                )
            )
        return postings
