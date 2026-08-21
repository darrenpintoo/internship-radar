"""Applied-state sync from the owner's Applications Sheet (SPEC §14 item 1).

One-way read: POST {token, action:'list'} to the Apps Script web app, match each
row to a radar cluster, stamp applied_at on every member and store the sheet
Status in kv. Radar never writes to the Sheet.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from rapidfuzz import fuzz

from radar import db
from radar.normalize import company_norm, title_norm

TITLE_THRESHOLD = 92
_DROP_PARAMS = {"ref", "source", "src", "gh_src", "lever-source"}


class SyncError(Exception):
    """Sheet unreachable or returned ok=false; the caller reports, never crashes."""


@dataclass
class SyncStats:
    matched: int = 0
    unmatched: list[str] = field(default_factory=list)
    skipped: str | None = None


def canonical_url(u: str) -> str:
    parts = urlsplit(u.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not (k.lower().startswith("utm_") or k.lower() in _DROP_PARAMS)
    ]
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def _cluster_key(row: sqlite3.Row) -> int:
    return row["cluster_id"] if row["cluster_id"] is not None else row["id"]


def _apply(conn: sqlite3.Connection, cluster_key: int, applied_at: str, status: str) -> int:
    members = db.cluster_members(conn, cluster_key)
    db.set_applied(conn, [m["id"] for m in members], applied_at)
    db.kv_set(conn, f"applied_status:{cluster_key}", status)
    return len(members)


def _fetch_rows(url: str, token: str, transport: httpx.BaseTransport | None) -> list[dict]:
    try:
        with httpx.Client(follow_redirects=True, timeout=30, transport=transport) as client:
            resp = client.post(url, json={"token": token, "action": "list"})
    except httpx.HTTPError as e:
        raise SyncError(f"sheet request failed: {e}") from e
    if resp.status_code // 100 != 2:
        raise SyncError(f"sheet returned HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError as e:
        raise SyncError("sheet returned non-JSON body") from e
    if not isinstance(body, dict) or not body.get("ok"):
        raise SyncError(
            f"sheet returned ok=false: {body.get('error') if isinstance(body, dict) else body!r}"
        )
    return list(body.get("rows") or [])


def sync_applied(
    conn: sqlite3.Connection,
    settings,
    registry: dict,
    *,
    transport: httpx.BaseTransport | None = None,
    now: str | None = None,
) -> SyncStats:
    if not settings.sheet_webapp_url or not settings.sheet_token:
        return SyncStats(skipped="not configured")
    rows = _fetch_rows(settings.sheet_webapp_url, settings.sheet_token, transport)
    now = now or db.now_utc()
    aliases = registry.get("aliases", {})

    postings = db.postings_for_matching(conn)
    by_url = {canonical_url(p["url"]): p for p in postings if p["url"]}
    by_company: dict[str, list[sqlite3.Row]] = {}
    for p in postings:
        by_company.setdefault(company_norm(p["company_name"], aliases), []).append(p)

    stats = SyncStats()
    for row in rows:
        company = str(row.get("company") or "").strip()
        title = str(row.get("title") or "").strip()
        link = str(row.get("link") or "").strip()
        hit = by_url.get(canonical_url(link)) if link else None
        if hit is None and company and title:
            t = title_norm(title)
            best = max(
                (
                    (fuzz.token_sort_ratio(t, title_norm(p["title"])), p)
                    for p in by_company.get(company_norm(company, aliases), [])
                ),
                key=lambda x: x[0],
                default=(0, None),
            )
            if best[0] > TITLE_THRESHOLD:
                hit = best[1]
        if hit is None:
            stats.unmatched.append(f"{company} — {title}")
            continue
        applied_at = str(row.get("date") or "").strip() or now
        status = str(row.get("status") or "").strip() or "Applied"
        _apply(conn, _cluster_key(hit), applied_at, status)
        stats.matched += 1
    conn.commit()
    return stats


def mark_applied(conn: sqlite3.Connection, url: str, now: str | None = None) -> int:
    target = canonical_url(url)
    hit = next(
        (
            p
            for p in db.postings_for_matching(conn)
            if p["url"] and canonical_url(p["url"]) == target
        ),
        None,
    )
    if hit is None:
        return 0
    n = _apply(conn, _cluster_key(hit), now or db.now_utc(), "Applied")
    conn.commit()
    return n
