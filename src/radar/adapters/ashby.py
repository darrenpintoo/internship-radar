"""Ashby posting API adapter (SPEC §5.4) — verified live 2026-08-20.

GET https://api.ashbyhq.com/posting-api/job-board/{token}
Response {jobs: [...], apiVersion: "1"} — descriptions included by default,
no query params needed. url maps from jobUrl (the posting page; applyUrl is
just jobUrl + '/application'). location is a PLAIN STRING, with any
secondaryLocations[].location appended '; '-joined.
"""

from __future__ import annotations

from .. import http as http_mod
from ..models import Posting
from ..normalize import iso_norm, strip_html
from .base import RawItem

# OpenAI's board is ~12 MB / 737 jobs — never assume small responses here.
LARGE_BOARDS = {"openai"}


class AshbyAdapter:
    name = "ashby"
    prefilter_policy = "full"

    def __init__(self, http, companies: list[tuple[str, str]]):
        """companies: [(display_name, board_token)] from the registry."""
        self._http = http
        self._companies = companies

    def _url(self, token: str) -> str:
        return f"https://api.ashbyhq.com/posting-api/job-board/{token}"

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
            locations = [job.get("location") or ""]
            locations += [
                sec["location"]
                for sec in job.get("secondaryLocations") or []
                if sec.get("location")
            ]
            description = job.get("descriptionPlain")
            if not description:
                html = job.get("descriptionHtml")
                description = strip_html(html) if html else None
            postings.append(
                Posting(
                    company_name=display,
                    title=job["title"],
                    url=job["jobUrl"],  # NOT applyUrl (= jobUrl + '/application')
                    source=self.name,
                    source_ref=raw.source_ref,
                    source_key=job["id"],  # UUID string
                    location_raw="; ".join(loc for loc in locations if loc) or None,
                    posted_at=iso_norm(job.get("publishedAt")),
                    description=description,
                )
            )
        return postings
