"""Digest: selection, grouping, render, send (SPEC §8).

M0 ships the cluster-level selection and a plain-text render for dry-run.
HTML render + Resend send + grouping land in M2.
"""

from __future__ import annotations

import sqlite3

from . import db


def select_clusters(conn: sqlite3.Connection) -> list[list[sqlite3.Row]]:
    """Never-digested clusters, one list of member rows per cluster (SPEC §8.1)."""
    rows = db.undigested_postings(conn)
    clusters: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        key = row["cluster_id"] if row["cluster_id"] is not None else row["id"]
        clusters.setdefault(int(key), []).append(row)
    return list(clusters.values())


def representative(members: list[sqlite3.Row]) -> sqlite3.Row:
    """Prefer a direct-ATS member (richer description, canonical apply URL),
    else earliest first_seen_at (SPEC §8.1)."""
    direct = [m for m in members if m["source"] in ("greenhouse", "lever", "ashby", "icims")]
    pool = direct or list(members)
    return sorted(pool, key=lambda m: m["first_seen_at"])[0]


def render_text(conn: sqlite3.Connection) -> str:
    """Plain-text would-be digest for dry-run / local dev (SPEC §10)."""
    lines = []
    clusters = select_clusters(conn)
    lines.append(f"would-be digest: {len(clusters)} cluster(s)")
    for members in clusters:
        rep = representative(members)
        badges = []
        if rep["repost_count"]:
            badges.append("reposted")
        if not rep["active"]:
            badges.append("already closed")
        sources = sorted({m["source"] for m in members})
        locations = "; ".join(sorted({m["location_raw"] for m in members if m["location_raw"]}))
        suffix = f"  [{', '.join(badges)}]" if badges else ""
        lines.append(
            f"- {rep['company_name']} — {rep['title']}{suffix}\n"
            f"    {locations or 'location n/a'} · {'/'.join(sources)}\n"
            f"    {rep['url']}"
        )
    return "\n".join(lines)
