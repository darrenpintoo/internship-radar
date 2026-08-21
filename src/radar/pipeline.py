"""Orchestrates fetch → parse → prefilter → dedup → store, run bookkeeping (SPEC §2)."""

from __future__ import annotations

import sqlite3

from . import db, dedup
from .adapters.base import SourceAdapter
from .models import RunReport


def run_poll(
    conn: sqlite3.Connection,
    adapters: list[SourceAdapter],
    aliases: dict[str, str],
    mode: str = "poll",
) -> tuple[RunReport, list[int]]:
    """Poll all adapters. Returns (report, new_posting_ids).

    Per-adapter/per-board failures are caught and recorded — one failure never
    kills the run (SPEC §5.1), and a failed or 304 board skips closed detection
    (SPEC §6.3). Exit-code policy lives in cli.py: this function only reports.
    """
    report = RunReport(mode=mode, started_at=db.now_utc())
    all_new: list[int] = []

    for adapter in adapters:
        try:
            raws = adapter.fetch()
        except Exception as exc:
            report.sources_failed[adapter.name] = f"{type(exc).__name__}: {exc}"
            continue

        adapter_ok = False
        for raw in raws:
            board = f"{adapter.name}:{raw.source_ref}" if raw.source_ref else adapter.name
            if raw.error:
                report.sources_failed[board] = raw.error
                continue
            if raw.not_modified:
                adapter_ok = True  # snapshot unchanged — skip the diff entirely
                continue
            try:
                postings = adapter.parse(raw)
            except Exception as exc:
                report.sources_failed[board] = f"parse: {type(exc).__name__}: {exc}"
                continue
            adapter_ok = True
            stats = dedup.process_board(conn, postings, adapter.prefilter_policy, aliases)
            dedup.detect_closed(conn, adapter.name, raw.source_ref, stats.seen_keys, stats)
            if stats.closure_skipped:
                report.sources_failed[f"{board} (closure-guard)"] = stats.closure_skipped
            report.new_postings += len(stats.new_ids)
            all_new.extend(stats.new_ids)
            all_new.extend(stats.repost_ids)

        if adapter_ok:
            report.sources_ok += 1

    report.finished_at = db.now_utc()
    report.run_id = db.record_run(conn, report)
    conn.commit()
    return report, all_new
