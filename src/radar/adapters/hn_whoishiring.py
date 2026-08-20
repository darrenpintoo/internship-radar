"""HN who-is-hiring adapter (SPEC §5.6) — verified live 2026-08-20.

Find the newest "Ask HN: Who is hiring?" story via Algolia search (never
hits[0] blindly — "Who wants to be hired?" is posted the same second and ties
on created_at), then fetch its comments and keep top-level ones that mention
internships. ~0-2 relevant postings/month; kept because it's nearly free.
"""

from __future__ import annotations

import re

from .. import http as http_mod
from ..models import Posting
from ..normalize import iso_norm, strip_html
from .base import RawItem

SEARCH_URL = (
    "https://hn.algolia.com/api/v1/search_by_date"
    "?tags=story,author_whoishiring&query=who%20is%20hiring"
)

_HIRING_TITLE_RE = re.compile(r"^Ask HN: Who is hiring\?", re.I)
# Strict word-boundary match: bare /intern/i false-matched 21/25 comments in
# the live Aug 2026 thread (internal/internet/international).
_INTERN_RE = re.compile(r"\bintern(s|ship)?\b|\bco[- ]?op\b", re.I)
# HN comment HTML separates paragraphs with opening <p> tags only (no </p>),
# which strip_html would flatten to spaces — convert to newlines first so
# "first non-empty line" means the comment's actual header line.
_P_OPEN_RE = re.compile(r"(?i)<p\b[^>]*>")


class HnWhoIsHiringAdapter:
    name = "hn"
    prefilter_policy = "excludes_only"

    def __init__(self, http):
        self._http = http

    def fetch(self) -> list[RawItem]:
        try:
            search = self._http.get_json(SEARCH_URL)
        except http_mod.NotModified:
            return [RawItem(self.name, "", not_modified=True)]
        except Exception as exc:
            return [RawItem(self.name, "", error=f"{type(exc).__name__}: {exc}")]
        hits = [h for h in search.get("hits", []) if _HIRING_TITLE_RE.match(h.get("title") or "")]
        if not hits:
            return [RawItem(self.name, "", error="no 'Ask HN: Who is hiring?' story in results")]
        story_id = str(max(hits, key=lambda h: h.get("created_at") or "")["objectID"])
        try:
            payload = self._http.get_json(f"https://hn.algolia.com/api/v1/items/{story_id}")
        except http_mod.NotModified:
            return [RawItem(self.name, story_id, not_modified=True)]
        except Exception as exc:
            return [RawItem(self.name, story_id, error=f"{type(exc).__name__}: {exc}")]
        return [RawItem(self.name, story_id, payload=payload)]

    def parse(self, raw: RawItem) -> list[Posting]:
        story_id = str(raw.payload.get("id") or raw.source_ref)
        postings = []
        for child in raw.payload.get("children", []):  # top-level only (SPEC §5.6)
            html = child.get("text") or ""  # deleted comments carry null text
            if not html:
                continue
            text = strip_html(_P_OPEN_RE.sub("\n", html))
            if not text or not _INTERN_RE.search(text):
                continue
            first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
            # A constant fallback title would collapse dedup hashes across months.
            title = first_line[:120] or "See posting"
            company = first_line.split("|", 1)[0].strip() or title
            postings.append(
                Posting(
                    company_name=company,
                    title=title,
                    url=f"https://news.ycombinator.com/item?id={child['id']}",
                    source=self.name,
                    source_ref=story_id,
                    source_key=str(child["id"]),
                    description=text,
                    posted_at=iso_norm(child.get("created_at")),
                )
            )
        return postings
