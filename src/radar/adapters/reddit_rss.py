"""Reddit RSS adapter (SPEC §5.7) — verified live 2026-08-20; fail-soft.

Anonymous RSS is throttled to ~1 request/minute and datacenter IPs (GitHub
Actions) are usually 403'd outright — a failed feed yields RawItem(error=...)
and the run moves on. Feeds are spaced >= 60s apart via min_interval because
the anonymous rate-limit bucket empties after ONE request.
"""

from __future__ import annotations

import re

import feedparser

from .. import http as http_mod
from ..models import Posting
from ..normalize import iso_norm, strip_html
from .base import RawItem

DEFAULT_FEEDS = ["https://www.reddit.com/r/csMajors/new.rss"]

# No 'new grad' arm — owner graduates 2028+ (SPEC §5.7).
TITLE_RE = re.compile(r"\bintern|co[- ]?op|2027|summer analyst", re.I)
_SUBREDDIT_RE = re.compile(r"/r/([^/]+)/")


class RedditRssAdapter:
    name = "reddit"
    prefilter_policy = "excludes_only"
    # Reddit 403s datacenter IPs intermittently (SPEC §5.7): failures are
    # recorded for the footer but never trip the digest's "[k sources down]" prefix.
    best_effort = True

    def __init__(self, http, feeds: list[str] | None = None):
        self._http = http
        self._feeds = feeds if feeds is not None else DEFAULT_FEEDS

    def fetch(self) -> list[RawItem]:
        items: list[RawItem] = []
        for url in self._feeds:
            try:
                resp = self._http.get(url, min_interval=60.0, conditional=True)
                items.append(RawItem(self.name, url, payload=resp.text))
            except http_mod.NotModified:
                items.append(RawItem(self.name, url, not_modified=True))
            except Exception as exc:  # expected from CI — fail soft (SPEC §5.7)
                items.append(RawItem(self.name, url, error=f"{type(exc).__name__}: {exc}"))
        return items

    def parse(self, raw: RawItem) -> list[Posting]:
        match = _SUBREDDIT_RE.search(raw.source_ref)
        subreddit = match.group(1) if match else raw.source_ref
        feed = feedparser.parse(raw.payload)
        postings = []
        for entry in feed.entries:
            if not TITLE_RE.search(entry.title):
                continue
            summary = entry.get("summary") or ""
            if not summary and entry.get("content"):
                summary = entry.content[0].value
            postings.append(
                Posting(
                    # Company extraction from RSS titles is unreliable — use the
                    # subreddit as a neutral placeholder and let LLM enrichment
                    # classify (this source leans on enrichment, SPEC §5.7).
                    company_name=f"r/{subreddit}",
                    title=entry.title,
                    url=entry.link,  # comments permalink; no outbound parsing in v1
                    source=self.name,
                    source_ref=subreddit,
                    source_key=entry.id,  # 't3_xxx'
                    description=strip_html(summary) if summary else None,
                    posted_at=iso_norm(entry.get("published")),
                )
            )
        return postings
