"""CLI: poll | digest | bootstrap | probe | dry-run | backfill | export |
sync-applied | mark-applied (SPEC §2.1).

Exit-code contract (SPEC §9.2): `poll` exits 0 whenever the run completed and
state was written — including with some sources failed (recorded in
runs.sources_failed); nonzero is reserved for infrastructure failure (DB
unwritable, config unparseable, ALL sources failed). `digest` exits nonzero
iff the send fails.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from . import db, digest, pipeline
from . import settings as settings_mod
from .adapters.greenhouse import GreenhouseAdapter
from .http import FixtureHttp, Http

_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"


def _build_adapters(http, registry, limit_sources: list[str] | None):
    """Adapters available as of M0. Grows through M1 (SPEC §11)."""
    adapters = []
    wanted = set(limit_sources) if limit_sources else None

    def enabled(name: str) -> bool:
        return wanted is None or name in wanted

    if enabled("greenhouse"):
        adapters.append(GreenhouseAdapter(http, settings_mod.companies_for(registry, "greenhouse")))
    return adapters


def cmd_poll(args) -> int:
    settings = settings_mod.load_settings()
    registry = settings_mod.load_registry()
    conn = db.connect(settings.db_path)
    http = Http(conn, settings.owner_email)
    try:
        adapters = _build_adapters(http, registry, args.limit_sources)
        report, _new_ids = pipeline.run_poll(conn, adapters, registry["aliases"])
    finally:
        http.close()
        conn.commit()
        conn.close()
    failed = len(report.sources_failed)
    print(
        f"poll: {report.sources_ok} source(s) ok, {failed} failure(s), "
        f"{report.new_postings} new posting(s)"
    )
    for name, err in report.sources_failed.items():
        print(f"  FAILED {name}: {err}", file=sys.stderr)
    if report.sources_ok == 0 and adapters:
        return 1  # ALL sources failed = infrastructure failure
    return 0


def cmd_dry_run(_args) -> int:
    """Full pipeline against fixtures + a temp DB. Zero network, zero LLM (SPEC §10)."""
    registry = settings_mod.load_registry()
    fixtures = {
        "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs?content=true": _FIXTURES
        / "greenhouse_anthropic.json",
        "https://boards-api.greenhouse.io/v1/boards/wehrtyou/jobs?content=true": _FIXTURES
        / "greenhouse_wehrtyou.json",
    }
    http = FixtureHttp(fixtures)
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        conn = db.connect(tmp.name)
        adapters = [
            GreenhouseAdapter(
                http,
                [("Anthropic", "anthropic"), ("Hudson River Trading", "wehrtyou")],
            )
        ]
        report, _ = pipeline.run_poll(conn, adapters, registry["aliases"], mode="dry-run")
        print(
            f"dry-run: {report.sources_ok} source(s) ok, "
            f"{len(report.sources_failed)} failure(s), {report.new_postings} new posting(s)"
        )
        print(digest.render_text(conn))
        conn.close()
    return 0


def _not_implemented(milestone: str):
    def handler(_args) -> int:
        print(f"not implemented until {milestone} (SPEC §11)", file=sys.stderr)
        return 2

    return handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="radar", description="internship-radar (SPEC.md)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_poll = sub.add_parser("poll", help="poll all sources, store new postings")
    p_poll.add_argument(
        "--limit-sources",
        nargs="+",
        help="only run these adapters (e.g. --limit-sources greenhouse)",
    )
    p_poll.set_defaults(func=cmd_poll)

    p_dry = sub.add_parser("dry-run", help="fixtures + temp DB, no network, no LLM")
    p_dry.set_defaults(func=cmd_dry_run)

    for name, milestone in [
        ("digest", "M2"),
        ("bootstrap", "M2"),
        ("sync-applied", "M2"),
        ("mark-applied", "M2"),
        ("probe", "M1"),
        ("backfill", "M4"),
        ("export", "M4"),
    ]:
        p = sub.add_parser(name)
        p.set_defaults(func=_not_implemented(milestone))

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
