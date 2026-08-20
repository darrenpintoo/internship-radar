"""Identity upsert, clustering, repost + guarded closed detection (SPEC §6.3)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from . import db, normalize
from .models import Posting

FUZZY_THRESHOLD = 92  # token_sort_ratio — never token_set_ratio (SPEC §6.3)
_EMPTY_SNAPSHOT_MIN_ACTIVE = 5


@dataclass
class BoardStats:
    seen_keys: set[str] = field(default_factory=set)
    new_ids: list[int] = field(default_factory=list)
    repost_ids: list[int] = field(default_factory=list)
    updated: int = 0
    unchanged: int = 0
    dropped_prefilter: int = 0
    closed_ids: list[int] = field(default_factory=list)
    closure_skipped: str | None = None  # set when the empty-snapshot guard fired


def process_board(
    conn: sqlite3.Connection,
    postings: list[Posting],
    prefilter_policy: str,
    aliases: dict[str, str],
    now: str | None = None,
) -> BoardStats:
    """Store one board's post-parse snapshot.

    Identity = (source, source_ref, source_key). Existing rows are ALWAYS
    touched (presence is pre-prefilter — a prefilter change must never
    inactivate stored rows); the prefilter gates INSERTION only (SPEC §6.2/6.3).
    """
    now = now or db.now_utc()
    stats = BoardStats()
    for p in postings:
        stats.seen_keys.add(p.source_key)
        row = db.get_by_identity(conn, p.source, p.source_ref, p.source_key)
        description = normalize.truncate_description(p.description)
        if row is not None:
            was_active = bool(row["active"])
            changed = (
                row["title"] != p.title
                or row["location_raw"] != p.location_raw
                or row["description"] != description
            )
            if changed:
                c_norm = normalize.company_norm(p.company_name, aliases)
                t_norm = normalize.title_norm(p.title)
                db.update_posting_content(
                    conn,
                    row["id"],
                    p.title,
                    p.location_raw,
                    description,
                    normalize.content_hash(c_norm, t_norm),
                )
                stats.updated += 1
            else:
                stats.unchanged += 1
            db.touch_posting(conn, row["id"], now)
            if not was_active:
                db.mark_repost(conn, row["id"])
                stats.repost_ids.append(row["id"])
            continue

        if not normalize.keep(p.title, prefilter_policy):
            stats.dropped_prefilter += 1
            continue

        c_norm = normalize.company_norm(p.company_name, aliases)
        t_norm = normalize.title_norm(p.title)
        content_hash = normalize.content_hash(c_norm, t_norm)
        stored = p.model_copy(update={"description": description})
        new_id = db.insert_posting(conn, stored, content_hash, now)
        _assign_cluster(conn, new_id, c_norm, t_norm, content_hash, aliases)
        stats.new_ids.append(new_id)
    conn.commit()
    return stats


def _assign_cluster(
    conn: sqlite3.Connection,
    new_id: int,
    c_norm: str,
    t_norm: str,
    content_hash: str,
    aliases: dict[str, str],
) -> None:
    """Attach the new row to matching cluster(s); merge when it bridges two.

    Exact: same content_hash anywhere. Fuzzy: same company_norm AND
    token_sort_ratio(title_norm) > 92. Cluster id convention: root row's id;
    a matched row with NULL cluster_id gets backfilled to itself (SPEC §6.3).
    """
    matched_clusters: set[int] = set()
    backfill: list[int] = []
    for row in db.rows_for_clustering(conn):
        if row["id"] == new_id:
            continue
        is_match = row["content_hash"] == content_hash
        if not is_match:
            if normalize.company_norm(row["company_name"], aliases) != c_norm:
                continue
            score = fuzz.token_sort_ratio(t_norm, normalize.title_norm(row["title"]))
            is_match = score > FUZZY_THRESHOLD
        if is_match:
            if row["cluster_id"] is not None:
                matched_clusters.add(int(row["cluster_id"]))
            else:
                matched_clusters.add(int(row["id"]))
                backfill.append(int(row["id"]))
    if not matched_clusters:
        return
    keep = min(matched_clusters)
    # Merge any bridged clusters into the smallest root id (SPEC §6.3).
    for other in sorted(matched_clusters):
        if other != keep:
            db.merge_clusters(conn, keep, other)  # members pointing at `other`
            db.set_cluster(conn, other, keep)  # the old root row itself
    for row_id in backfill:  # matched rows that had cluster_id NULL
        db.set_cluster(conn, row_id, keep)
    db.set_cluster(conn, keep, keep)  # root always points at itself
    db.set_cluster(conn, new_id, keep)


def detect_closed(
    conn: sqlite3.Connection,
    source: str,
    source_ref: str,
    seen_keys: set[str],
    stats: BoardStats,
    now: str | None = None,
) -> None:
    """Two-strike closed detection with degenerate-snapshot guards (SPEC §6.3).

    Callers only invoke this for boards whose fetch+parse SUCCEEDED this run
    (a failed adapter must not close anything) and never on 304 snapshots.
    """
    now = now or db.now_utc()
    rows = db.board_rows(conn, source, source_ref)
    active_rows = [r for r in rows if r["active"]]
    absent = [r for r in active_rows if r["source_key"] not in seen_keys]
    if (
        len(active_rows) >= _EMPTY_SNAPSHOT_MIN_ACTIVE
        and len(absent) == len(active_rows)
        and active_rows
    ):
        # Empty/degenerate payload: refuse to close 100% of a populated board.
        stats.closure_skipped = f"snapshot would close all {len(active_rows)} active rows — refused"
        return
    first_strike = [r["id"] for r in absent if r["missing_since"] is None]
    second_strike = [r["id"] for r in absent if r["missing_since"] is not None]
    if first_strike:
        db.set_missing(conn, first_strike, now)
    if second_strike:
        db.close_postings(conn, second_strike)
        stats.closed_ids.extend(second_strike)
    conn.commit()
