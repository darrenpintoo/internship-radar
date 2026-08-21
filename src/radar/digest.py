"""Digest: selection, grouping, render, send (SPEC §8, §7 grad flag).

Unit of selection is the cluster (COALESCE(cluster_id, id)). Send-first
ordering: digested_at + digest_log are written only after Resend returns 2xx —
prefer a duplicate digest over a lost posting (SPEC §8.5).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import db, mail
from .settings import tier_for, tier_index, tier_rank

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"
DIRECT_ATS = ("greenhouse", "lever", "ashby", "icims")
TRACK_CATEGORIES = frozenset({"quant", "ai_infra", "swe", "robotics"})
GRAD_CANDIDATES = ("2027-12", "2028-05", "2028-12")
OTHER_CAP = 40
CLOSING_HORIZON_DAYS = 14
STALE_DAYS = 14  # SPEC §3 safety window: warn if undigested rows age past it

_SOURCE_SHORT = {
    "greenhouse": "gh",
    "lever": "lever",
    "ashby": "ashby",
    "icims": "icims",
    "github_trackers": "tracker",
    "hn_whoishiring": "hn",
    "reddit_rss": "reddit",
    "workday": "wd",
}


@dataclass
class Digest:
    subject: str
    html: str
    text: str
    rendered_member_ids: list[int]
    strong: list[dict[str, Any]]
    worth: list[dict[str, Any]]
    closing: list[dict[str, Any]]
    other: list[dict[str, Any]]
    omitted_count: int
    sources_failed: dict[str, str]
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not (self.strong or self.worth or self.other or self.closing)


# ---------------------------------------------------------------- selection


def select_clusters(conn: sqlite3.Connection) -> list[list[sqlite3.Row]]:
    """Never-digested clusters, one list of member rows per cluster (SPEC §8.1)."""
    rows = db.undigested_postings(conn)
    clusters: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        clusters.setdefault(_cluster_key(row), []).append(row)
    return list(clusters.values())


def representative(members: list[sqlite3.Row]) -> sqlite3.Row:
    """Prefer a direct-ATS member (richer description, canonical apply URL),
    else earliest first_seen_at (SPEC §8.1)."""
    direct = [m for m in members if m["source"] in DIRECT_ATS]
    pool = direct or list(members)
    return sorted(pool, key=lambda m: m["first_seen_at"])[0]


def _cluster_key(row: sqlite3.Row) -> int:
    return int(row["cluster_id"] if row["cluster_id"] is not None else row["id"])


def grad_flag(earliest: str | None, latest: str | None) -> str | None:
    """Candidate grad dates ∩ [earliest, latest] ('YYYY-MM', null = unbounded) (SPEC §7)."""
    if earliest is None and latest is None:
        return None
    matching = [
        d
        for d in GRAD_CANDIDATES
        if (earliest is None or d >= earliest) and (latest is None or d <= latest)
    ]
    if len(matching) == len(GRAD_CANDIDATES):
        return "✓ any grad date"
    if matching:
        return "⚠ use " + ", ".join(matching)
    return "✗ outside window"


def _max_fit(enr: sqlite3.Row | None) -> int:
    if enr is None:
        return -1
    fits = [enr["fit_quant"], enr["fit_ai_infra"], enr["fit_swe"], enr["fit_robotics"]]
    if any(f is None for f in fits):
        return -1
    return max(int(f) for f in fits)


def _pick_enrichment(conn: sqlite3.Connection, members: list[sqlite3.Row]) -> sqlite3.Row | None:
    """Any member's enrichment serves the cluster; prefer one with scores."""
    enrs = db.enrichments_for(conn, [int(m["id"]) for m in members])
    if not enrs:
        return None
    return max(enrs.values(), key=_max_fit)


def _build_row(
    conn: sqlite3.Connection,
    members: list[sqlite3.Row],
    registry: dict,
    tiers: dict[str, str],
    applied_status: str | None,
) -> dict[str, Any]:
    rep = representative(members)
    enr = _pick_enrichment(conn, members)
    max_fit = _max_fit(enr)
    enriched = max_fit >= 0
    tier = tier_for(rep["company_name"], registry, tiers)
    locations: list[str] = []
    for m in members:
        for loc in (m["location_raw"] or "").split("; "):
            loc = loc.strip()
            if loc and loc not in locations:
                locations.append(loc)
    sources = sorted({m["source"] for m in members})
    season = enr["season"] if enriched else None
    red_flags: list[str] = []
    if enr is not None and enr["red_flags"]:
        try:
            red_flags = list(json.loads(enr["red_flags"]))
        except ValueError:
            red_flags = []
    return {
        "cluster_key": _cluster_key(rep),
        "member_ids": [int(m["id"]) for m in members],
        "company": rep["company_name"],
        "title": rep["title"],
        "url": rep["url"],
        "locations": locations,
        "sources": sources,
        "source_badges": [_SOURCE_SHORT.get(s, s) for s in sources],
        "tier": tier,
        "enriched": enriched,
        "max_fit": max_fit,
        "category": enr["category"] if enriched else None,
        "season": season,
        "season_chip": (
            None
            if season in (None, "summer_2027")
            else ("off-season" if season == "other" else "season?")
        ),
        "grad_flag": grad_flag(enr["grad_earliest"], enr["grad_latest"]) if enriched else None,
        "workauth": (enr["workauth"] if enriched and enr["workauth"] != "unspecified" else None),
        "deadline": enr["deadline"] if enriched else None,
        "rationale": enr["rationale"] if enriched else None,
        "red_flags": red_flags,
        "reposted": any(int(m["repost_count"] or 0) > 0 for m in members),
        "closed": not any(int(m["active"]) for m in members),
        "alerted": any(m["alerted_at"] for m in members),
        "applied": any(m["applied_at"] for m in members),
        "applied_status": applied_status,
        "first_seen_at": min(m["first_seen_at"] for m in members),
    }


def _group_of(row: dict[str, Any]) -> str:
    fit = row["max_fit"]
    if fit >= 75:
        return "strong"
    if row["tier"] == "S" and (
        not row["enriched"] or row["category"] in TRACK_CATEGORIES or fit >= 50
    ):
        return "strong"
    if fit >= 50:
        return "worth"
    return "other"


def _sort_key(row: dict[str, Any]) -> tuple:
    return (-row["max_fit"], tier_rank(row["tier"]), row["first_seen_at"])


# ---------------------------------------------------------------- build


def build_digest(
    conn: sqlite3.Connection,
    settings: Any,
    registry: dict,
    today: str | None = None,
) -> Digest:
    today = today or date.today().isoformat()
    today_d = date.fromisoformat(today)
    max_rows = int(getattr(settings, "digest_max_rows", 150) or 150)
    tiers = tier_index(registry)

    stale_cutoff = (today_d - timedelta(days=STALE_DAYS)).isoformat()
    rows: list[dict[str, Any]] = []
    for members in select_clusters(conn):
        key = _cluster_key(members[0])
        status = db.kv_get(conn, f"applied_status:{key}")
        if status == "Rejected":
            continue
        row = _build_row(conn, members, registry, tiers, status)
        if row["first_seen_at"][:10] < stale_cutoff:
            log.warning(
                "undigested cluster %s (%s — %s) older than %d days",
                key,
                row["company"],
                row["title"],
                STALE_DAYS,
            )
        rows.append(row)
    rows.sort(key=_sort_key)

    strong = [r for r in rows if _group_of(r) == "strong"]
    worth = [r for r in rows if _group_of(r) == "worth"]
    other_all = [r for r in rows if _group_of(r) == "other"]
    other_cap = max(0, min(OTHER_CAP, max_rows - len(strong) - len(worth)))
    other = other_all[:other_cap]
    omitted = len(other_all) - len(other)

    horizon = (today_d + timedelta(days=CLOSING_HORIZON_DAYS)).isoformat()
    closing: list[dict[str, Any]] = []
    seen_closing: set[int] = set()
    for p in db.closing_soon(conn, today, horizon):
        key = _cluster_key(p)
        if key in seen_closing or db.kv_get(conn, f"applied_status:{key}") == "Rejected":
            continue
        seen_closing.add(key)
        members = db.cluster_members(conn, key)
        crow = _build_row(conn, members, registry, tiers, None)
        crow["deadline"] = p["deadline"]
        closing.append(crow)
    closing.sort(key=lambda r: (r["deadline"], _sort_key(r)))

    rendered_ids: list[int] = []
    for r in strong + worth + other:
        rendered_ids.extend(r["member_ids"])

    run = db.latest_run(conn, "poll")
    sources_failed: dict[str, str] = {}
    run_info: dict[str, Any] | None = None
    if run is not None:
        try:
            sources_failed = dict(json.loads(run["sources_failed"] or "{}"))
        except ValueError:
            sources_failed = {}
        run_info = {
            "finished_at": run["finished_at"] or run["started_at"],
            "sources_ok": int(run["sources_ok"] or 0),
            "sources_failed": sources_failed,
            "new_postings": int(run["new_postings"] or 0),
            "llm_calls": int(run["llm_calls"] or 0),
            "flash_alerts": int(run["flash_alerts"] or 0),
        }

    n_new = len(strong) + len(worth) + len(other)
    counts = {
        "new": n_new,
        "strong": len(strong),
        "worth": len(worth),
        "other": len(other),
        "closing": len(closing),
        "omitted": omitted,
    }
    subject = _subject(n_new, len(strong), today_d, len(sources_failed))
    generated_at = db.now_utc()
    ctx = {
        "subject": subject,
        "today": today,
        "strong": strong,
        "worth": worth,
        "closing": closing,
        "other": other,
        "omitted_count": omitted,
        "counts": counts,
        "run": run_info,
        "generated_at": generated_at,
    }
    return Digest(
        subject=subject,
        html=_env().get_template("digest.html.j2").render(**ctx),
        text=_render_plain(ctx),
        rendered_member_ids=rendered_ids,
        strong=strong,
        worth=worth,
        closing=closing,
        other=other,
        omitted_count=omitted,
        sources_failed=sources_failed,
        counts=counts,
    )


def _subject(n_new: int, n_strong: int, d: date, n_failed: int) -> str:
    when = f"{d:%a %b} {d.day}"
    body = (
        f"Radar: 0 new — {when}"
        if n_new == 0
        else f"Radar: {n_new} new ({n_strong} strong) — {when}"
    )
    if n_failed:
        body = f"Radar [{n_failed} sources down]: " + body.removeprefix("Radar: ")
    return body


_ENV: Environment | None = None


def _env() -> Environment:
    global _ENV
    if _ENV is None:
        _ENV = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=select_autoescape(default=True, default_for_string=True),
            trim_blocks=True,
            lstrip_blocks=True,
        )
    return _ENV


# ---------------------------------------------------------------- text render


def _row_text(r: dict[str, Any]) -> str:
    bits: list[str] = []
    if r["locations"]:
        bits.append("; ".join(r["locations"]))
    if r["category"]:
        bits.append(r["category"])
    if r["season_chip"]:
        bits.append(r["season_chip"])
    if r["grad_flag"]:
        bits.append(r["grad_flag"])
    if r["workauth"]:
        bits.append(r["workauth"])
    if r["deadline"]:
        bits.append(f"deadline {r['deadline']}")
    badges = [
        name
        for flag, name in (
            (r["reposted"], "reposted"),
            (r["closed"], "already closed"),
            (r["alerted"], "⚡ alerted"),
            (r["applied"], "applied"),
        )
        if flag
    ]
    bits.extend(badges)
    bits.append("/".join(r["sources"]))
    fit = f" [{r['max_fit']}]" if r["enriched"] else ""
    tail = r["rationale"] if r["enriched"] else "pending scoring"
    return (
        f"- {r['company']} — {r['title']}{fit}\n    {' · '.join(bits)}\n    {tail}\n    {r['url']}"
    )


def _render_plain(ctx: dict[str, Any]) -> str:
    lines = [ctx["subject"], ""]
    for label, key in (
        ("Strong fits", "strong"),
        ("Worth a look", "worth"),
        ("Closing soon", "closing"),
        ("Everything else new", "other"),
    ):
        rows = ctx[key]
        if not rows:
            continue
        lines.append(f"== {label} ({len(rows)}) ==")
        lines.extend(_row_text(r) for r in rows)
        if key == "other" and ctx["omitted_count"]:
            lines.append(f"  … and {ctx['omitted_count']} more — they roll into tomorrow")
        lines.append("")
    if ctx["counts"]["new"] == 0 and not ctx["closing"]:
        lines.append("Nothing new today.")
        lines.append("")
    run = ctx["run"]
    if run:
        failed = ", ".join(run["sources_failed"]) or "none"
        lines.append(
            f"last poll {run['finished_at']}: {run['sources_ok']} sources ok, "
            f"{len(run['sources_failed'])} failed ({failed}), "
            f"{run['new_postings']} new postings, {run['llm_calls']} llm calls, "
            f"{run['flash_alerts']} flash alerts"
        )
    else:
        lines.append("no poll run recorded yet")
    lines.append(f"generated {ctx['generated_at']}")
    return "\n".join(lines)


def render_text(
    conn: sqlite3.Connection, settings: Any = None, registry: dict | None = None
) -> str:
    """Plain-text would-be digest for dry-run / local dev (SPEC §10). Reads nothing
    but the DB; marks nothing."""
    if registry is None:
        from .settings import load_registry

        registry = load_registry()
    d = build_digest(conn, settings, registry)
    return f"would-be digest: {d.counts['new']} cluster(s)\n{d.text}"


# ---------------------------------------------------------------- send


def run_digest(
    conn: sqlite3.Connection,
    settings: Any,
    registry: dict,
    *,
    send: bool = True,
    transport=None,
    today: str | None = None,
) -> int:
    """Build → send → only then mark digested (SPEC §8.5). Returns an exit code."""
    today = today or date.today().isoformat()
    d = build_digest(conn, settings, registry, today=today)
    if d.is_empty and not settings.send_empty:
        log.info("digest empty and SEND_EMPTY=false — not sending")
        return 0
    if not send:
        print(d.text)
        return 0
    try:
        resend_id = mail.send_email(
            settings.resend_api_key,
            settings.digest_to,
            d.subject,
            d.html,
            idempotency_key=f"digest-{today}",
            transport=transport,
        )
    except mail.SendError as exc:
        log.error("digest send failed: %s", exc)
        return 1
    now = db.now_utc()
    db.mark_digested(conn, d.rendered_member_ids, now)
    db.insert_digest_log(conn, now, d.rendered_member_ids, d.subject, resend_id)
    conn.commit()
    log.info("digest sent: %s (%d rows, resend id %s)", d.subject, d.counts["new"], resend_id)
    return 0
