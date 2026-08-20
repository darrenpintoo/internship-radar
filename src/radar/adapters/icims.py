"""iCIMS careers-host JSON API adapter (SPEC §5.9) — verified live 2026-08-20.

GET https://{host}/api/jobs?page={n}&limit=100 — response
{jobs: [{data: {...}}], totalCount} exactly as speced; paginate while
page*limit < totalCount. Live deviations from the spec noted against reality:
`state` can be entirely ABSENT from a job's data (not just null), and the
keyword-filtered endpoint returns pre-stripped plain-text descriptions while
the unfiltered one returns HTML — strip_html handles both.
"""

from __future__ import annotations

from .. import http as http_mod
from ..models import Posting
from ..normalize import iso_norm, strip_html
from .base import RawItem

_PAGE_LIMIT = 100


class IcimsAdapter:
    name = "icims"
    prefilter_policy = "full"

    def __init__(self, http, companies: list[tuple[str, str]]):
        """companies: [(display_name, careers_host)] from the registry."""
        self._http = http
        self._companies = companies

    def _url(self, host: str, page: int) -> str:
        return f"https://{host}/api/jobs?page={page}&limit={_PAGE_LIMIT}"

    def _fetch_all_pages(self, host: str) -> list[dict]:
        """Combined jobs list across pages. Only page 1 sends conditional
        headers: its 304 stands for the whole snapshot, and a stray 304 on a
        later page must never mask changes seen on page 1."""
        jobs: list[dict] = []
        page = 1
        while True:
            payload = self._http.get_json(self._url(host, page), conditional=(page == 1))
            jobs.extend(payload.get("jobs", []))
            if page * _PAGE_LIMIT >= int(payload.get("totalCount", 0)):
                return jobs
            page += 1

    def fetch(self) -> list[RawItem]:
        items: list[RawItem] = []
        for display, host in self._companies:
            try:
                jobs = self._fetch_all_pages(host)
                items.append(RawItem(self.name, host, payload=jobs, meta={"company": display}))
            except http_mod.NotModified:
                items.append(RawItem(self.name, host, not_modified=True, meta={"company": display}))
            except Exception as exc:  # one host's failure never kills the rest
                items.append(
                    RawItem(
                        self.name,
                        host,
                        error=f"{type(exc).__name__}: {exc}",
                        meta={"company": display},
                    )
                )
        return items

    def parse(self, raw: RawItem) -> list[Posting]:
        display = raw.meta.get("company", raw.source_ref)
        postings = []
        for job in raw.payload:
            data = job.get("data", {})
            location = ", ".join(
                str(v) for v in (data.get("city"), data.get("state"), data.get("country")) if v
            )
            parts = [strip_html(data[k]) for k in ("description", "qualifications") if data.get(k)]
            postings.append(
                Posting(
                    company_name=display,
                    title=data["title"],
                    url=data["apply_url"],
                    source=self.name,
                    source_ref=raw.source_ref,
                    source_key=str(data["req_id"]),
                    location_raw=location or None,
                    posted_at=iso_norm(data.get("posted_date")),
                    description="\n".join(parts) or None,
                )
            )
        return postings
