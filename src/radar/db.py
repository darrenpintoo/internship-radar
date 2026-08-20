"""SQLite layer — ALL SQL lives here (SPEC §3).

Migrations run via PRAGMA user_version: MIGRATIONS[i] moves the schema to
version i+1. Every later schema change appends an entry; never edit a shipped
one. Default rollback journal (no WAL — jobs.db must stay a single committed
file), explicit close so the persist step never sees a sidecar.

Timestamps: UTC 'YYYY-MM-DDTHH:MM:SSZ' everywhere, written by now_utc().
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from .models import Posting, RunReport

_DDL_V1 = """
CREATE TABLE postings (
  id            INTEGER PRIMARY KEY,
  company_name  TEXT NOT NULL,
  title         TEXT NOT NULL,
  location_raw  TEXT,
  url           TEXT NOT NULL,
  source        TEXT NOT NULL,
  source_ref    TEXT NOT NULL DEFAULT '',
  source_key    TEXT NOT NULL,
  description   TEXT,
  posted_at     TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  active        INTEGER NOT NULL DEFAULT 1,
  missing_since TEXT,
  repost_count  INTEGER NOT NULL DEFAULT 0,
  content_hash  TEXT NOT NULL,
  cluster_id    INTEGER,
  digested_at   TEXT,
  alerted_at    TEXT,
  applied_at    TEXT,
  enrich_attempts INTEGER NOT NULL DEFAULT 0,
  tracker_hints TEXT
);
CREATE UNIQUE INDEX idx_post_identity ON postings(source, source_ref, source_key);
CREATE INDEX idx_post_hash ON postings(content_hash);
CREATE INDEX idx_post_cluster ON postings(cluster_id);

CREATE TABLE enrichments (
  posting_id    INTEGER PRIMARY KEY REFERENCES postings(id),
  category      TEXT,
  season        TEXT,
  grad_earliest TEXT,
  grad_latest   TEXT,
  workauth      TEXT,
  deadline      TEXT,
  fit_quant     INTEGER, fit_ai_infra INTEGER, fit_swe INTEGER, fit_robotics INTEGER,
  rationale     TEXT,
  red_flags     TEXT,
  model         TEXT NOT NULL,
  enriched_at   TEXT NOT NULL
);

CREATE TABLE runs (
  id            INTEGER PRIMARY KEY,
  mode          TEXT NOT NULL,
  started_at    TEXT NOT NULL, finished_at TEXT,
  sources_ok    INTEGER, sources_failed TEXT,
  new_postings  INTEGER, llm_calls INTEGER, flash_alerts INTEGER DEFAULT 0
);

CREATE TABLE digest_log (
  id            INTEGER PRIMARY KEY,
  sent_at       TEXT NOT NULL,
  posting_ids   TEXT NOT NULL,
  subject       TEXT NOT NULL,
  resend_id     TEXT
);

CREATE TABLE alert_log (
  id            INTEGER PRIMARY KEY,
  posting_id    INTEGER NOT NULL REFERENCES postings(id),
  sent_at       TEXT NOT NULL,
  reason        TEXT NOT NULL
);

CREATE TABLE kv (
  k             TEXT PRIMARY KEY,
  v             TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
"""

MIGRATIONS: list[str] = [_DDL_V1]


def now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i in range(version, len(MIGRATIONS)):
        conn.executescript(MIGRATIONS[i])
        conn.execute(f"PRAGMA user_version = {i + 1}")
    conn.commit()


# ---------------------------------------------------------------- kv


def kv_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
    return row["v"] if row else None


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv (k, v, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v = excluded.v, updated_at = excluded.updated_at",
        (key, value, now_utc()),
    )


# ---------------------------------------------------------------- postings


def get_by_identity(
    conn: sqlite3.Connection, source: str, source_ref: str, source_key: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM postings WHERE source = ? AND source_ref = ? AND source_key = ?",
        (source, source_ref, source_key),
    ).fetchone()


def insert_posting(conn: sqlite3.Connection, p: Posting, content_hash: str, now: str) -> int:
    cur = conn.execute(
        "INSERT INTO postings (company_name, title, location_raw, url, source, source_ref,"
        " source_key, description, posted_at, first_seen_at, last_seen_at, content_hash,"
        " tracker_hints)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            p.company_name,
            p.title,
            p.location_raw,
            p.url,
            p.source,
            p.source_ref,
            p.source_key,
            p.description,
            p.posted_at,
            now,
            now,
            content_hash,
            json.dumps(p.tracker_hints) if p.tracker_hints else None,
        ),
    )
    return int(cur.lastrowid)


def touch_posting(conn: sqlite3.Connection, posting_id: int, now: str) -> None:
    """Seen in this snapshot: refresh last_seen, reactivate, clear missing_since."""
    conn.execute(
        "UPDATE postings SET last_seen_at = ?, active = 1, missing_since = NULL WHERE id = ?",
        (now, posting_id),
    )


def update_posting_content(
    conn: sqlite3.Connection,
    posting_id: int,
    title: str,
    location_raw: str | None,
    description: str | None,
    content_hash: str,
) -> None:
    """An employer edit is an in-place update — never a second row (SPEC §6.3)."""
    conn.execute(
        "UPDATE postings SET title = ?, location_raw = ?, description = ?, content_hash = ?"
        " WHERE id = ?",
        (title, location_raw, description, content_hash, posting_id),
    )


def mark_repost(conn: sqlite3.Connection, posting_id: int) -> None:
    """active=0 identity reappeared: bump repost_count and clear digested_at so
    the cluster re-enters the next digest with a `reposted` badge (SPEC §6.3)."""
    conn.execute(
        "UPDATE postings SET repost_count = repost_count + 1, digested_at = NULL WHERE id = ?",
        (posting_id,),
    )


def rows_for_clustering(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, company_name, title, cluster_id, content_hash FROM postings"
    ).fetchall()


def set_cluster(conn: sqlite3.Connection, posting_id: int, cluster_id: int) -> None:
    conn.execute("UPDATE postings SET cluster_id = ? WHERE id = ?", (cluster_id, posting_id))


def merge_clusters(conn: sqlite3.Connection, keep: int, other: int) -> None:
    conn.execute("UPDATE postings SET cluster_id = ? WHERE cluster_id = ?", (keep, other))


def board_rows(conn: sqlite3.Connection, source: str, source_ref: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, source_key, active, missing_since FROM postings"
        " WHERE source = ? AND source_ref = ?",
        (source, source_ref),
    ).fetchall()


def set_missing(conn: sqlite3.Connection, ids: list[int], now: str) -> None:
    conn.executemany("UPDATE postings SET missing_since = ? WHERE id = ?", [(now, i) for i in ids])


def close_postings(conn: sqlite3.Connection, ids: list[int]) -> None:
    conn.executemany("UPDATE postings SET active = 0 WHERE id = ?", [(i,) for i in ids])


# ---------------------------------------------------------------- runs


def record_run(conn: sqlite3.Connection, report: RunReport) -> int:
    cur = conn.execute(
        "INSERT INTO runs (mode, started_at, finished_at, sources_ok, sources_failed,"
        " new_postings, llm_calls, flash_alerts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            report.mode,
            report.started_at,
            report.finished_at,
            report.sources_ok,
            json.dumps(report.sources_failed),
            report.new_postings,
            report.llm_calls,
            report.flash_alerts,
        ),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------- selection


def undigested_postings(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Rows whose cluster has no digested member (cluster-level selection, SPEC §8.1).

    Never-digested active=0 rows are included (rendered with an 'already closed'
    badge — never silently dropped).
    """
    return conn.execute(
        "SELECT p.* FROM postings p"
        " WHERE NOT EXISTS ("
        "   SELECT 1 FROM postings m WHERE COALESCE(m.cluster_id, m.id) ="
        "     COALESCE(p.cluster_id, p.id) AND m.digested_at IS NOT NULL)"
        " ORDER BY p.first_seen_at DESC"
    ).fetchall()
