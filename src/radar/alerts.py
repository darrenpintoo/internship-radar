"""Flash alerts (SPEC §8.6).

tier-S or max fit >= 90, never-alerted clusters only, capped by FLASH_MAX_PER_DAY
(counted via alert_log), suppressed during bootstrap. A SendError on one alert
never aborts the poll run.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import db, mail
from .settings import Settings, tier_for, tier_index

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates"
FIT_THRESHOLD = 90
REASON_LABELS = {"tier_s": "tier-S company", "fit": "fit ≥ 90"}


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
    )


def max_fit(e: sqlite3.Row | None) -> int | None:
    if e is None:
        return None
    vals = [e["fit_quant"], e["fit_ai_infra"], e["fit_swe"], e["fit_robotics"]]
    if any(v is None for v in vals):
        return None
    return max(int(v) for v in vals)


def cluster_enrichment(conn: sqlite3.Connection, members: list[sqlite3.Row]) -> sqlite3.Row | None:
    """Any scored member's enrichment (one representative is enriched per cluster)."""
    found = db.enrichments_for(conn, [int(m["id"]) for m in members])
    for m in members:
        e = found.get(int(m["id"]))
        if e is not None and max_fit(e) is not None:
            return e
    return None


def render_flash(
    row: sqlite3.Row,
    members: list[sqlite3.Row],
    enrichment: sqlite3.Row | None,
    tier: str | None,
    reason: str,
) -> str:
    return (
        _env()
        .get_template("flash.html.j2")
        .render(
            title=row["title"],
            url=row["url"],
            company=row["company_name"],
            location="; ".join(sorted({m["location_raw"] for m in members if m["location_raw"]})),
            tier=tier,
            sources=sorted({m["source"] for m in members}),
            enrichment=enrichment,
            reason_label=REASON_LABELS[reason],
        )
    )


def send_flash_alerts(
    conn: sqlite3.Connection,
    settings: Settings,
    registry: dict,
    posting_ids: list[int],
    *,
    suppress: bool = False,
    transport: httpx.BaseTransport | None = None,
    now: str | None = None,
) -> int:
    """Returns number of alerts sent. `suppress` = bootstrap (SPEC §8.6)."""
    if suppress or not settings.send_flash or not posting_ids:
        return 0
    now = now or db.now_utc()
    remaining = settings.flash_max_per_day - db.alerts_sent_on(conn, now[:10])
    if remaining <= 0:
        log.info("flash: daily cap reached, skipping %d candidates", len(posting_ids))
        return 0

    tiers = tier_index(registry)
    seen_clusters: set[int] = set()
    sent = 0
    for row in db.alert_candidates(conn, posting_ids):
        if sent >= remaining:
            log.info("flash: daily cap reached mid-run; remainder rolls into the digest")
            break
        key = int(row["cluster_id"] if row["cluster_id"] is not None else row["id"])
        if key in seen_clusters:
            continue
        seen_clusters.add(key)

        members = db.cluster_members(conn, key)
        tier = tier_for(row["company_name"], registry, tiers)
        enrichment = cluster_enrichment(conn, members)
        fit = max_fit(enrichment)
        if tier == "S":
            reason = "tier_s"
        elif fit is not None and fit >= FIT_THRESHOLD:
            reason = "fit"
        else:
            continue

        pid = int(row["id"])
        subject = f"Radar ⚡ {row['company_name']} — {row['title']}"
        html = render_flash(row, members, enrichment, tier, reason)
        try:
            mail.send_email(
                settings.resend_api_key,
                settings.digest_to,
                subject,
                html,
                idempotency_key=f"flash-{pid}",
                transport=transport,
            )
        except mail.SendError as exc:
            log.warning("flash: send failed for posting %d: %s", pid, exc)
            continue
        db.mark_alerted(conn, [int(m["id"]) for m in members], now)
        db.insert_alert_log(conn, pid, now, reason)
        conn.commit()
        sent += 1
    return sent
