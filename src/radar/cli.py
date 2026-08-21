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
import io
import logging
import re
import sys
import tarfile
import tempfile
from pathlib import Path

from . import alerts, applied, db, digest, enrich, pipeline
from . import settings as settings_mod
from .adapters import github_trackers as trackers_mod
from .adapters.ashby import AshbyAdapter
from .adapters.github_trackers import GithubTrackersAdapter
from .adapters.greenhouse import GreenhouseAdapter
from .adapters.hn_whoishiring import SEARCH_URL, HnWhoIsHiringAdapter
from .adapters.icims import IcimsAdapter
from .adapters.lever import LeverAdapter
from .adapters.reddit_rss import DEFAULT_FEEDS, RedditRssAdapter
from .http import FixtureHttp, Http
from .models import EnrichmentOut, Fit, GradWindow

_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"


def _build_adapters(http, registry, limit_sources: list[str] | None):
    """All M1 adapters, driven by the registry (SPEC §5)."""
    wanted = set(limit_sources) if limit_sources else None

    def enabled(name: str) -> bool:
        return wanted is None or name in wanted

    adapters = []
    for name, cls in (
        ("greenhouse", GreenhouseAdapter),
        ("lever", LeverAdapter),
        ("ashby", AshbyAdapter),
        ("icims", IcimsAdapter),
    ):
        companies = settings_mod.companies_for(registry, name)
        if enabled(name) and companies:
            adapters.append(cls(http, companies))
    if enabled("github_trackers"):
        adapters.append(GithubTrackersAdapter(http))
    if enabled("hn"):
        adapters.append(HnWhoIsHiringAdapter(http))
    if enabled("reddit"):
        adapters.append(RedditRssAdapter(http))
    return adapters


def _enrich_and_alert(conn, settings, registry, new_ids, repost_ids, *, cap, suppress_alerts):
    """Post-poll stage shared by poll and bootstrap: re-enrich reposts, drain the
    tier-first queue up to `cap`, then flash alerts (SPEC §7, §8.6)."""
    llm_calls = 0
    try:
        if repost_ids:
            llm_calls += enrich.re_enrich(conn, settings, registry, repost_ids).calls
        stats = enrich.enrich_postings(conn, settings, registry, cap=max(cap - llm_calls, 0))
        llm_calls += stats.calls
        print(
            f"enrich: {stats.calls} call(s), {stats.enriched} enriched, "
            f"{stats.deferred} deferred, {stats.failed_final} failed, "
            f"{stats.skipped_cluster} cluster-mates skipped"
        )
    except RuntimeError as exc:  # no LLM key configured — postings stay queued
        print(f"enrich: skipped ({exc})", file=sys.stderr)
    sent = alerts.send_flash_alerts(
        conn, settings, registry, new_ids + repost_ids, suppress=suppress_alerts
    )
    if sent:
        print(f"flash alerts: {sent} sent")
    return llm_calls, sent


def _poll(settings, registry, limit_sources, *, cap, suppress_alerts, mode="poll"):
    conn = db.connect(settings.db_path)
    http = Http(conn, settings.owner_email)
    try:
        adapters = _build_adapters(http, registry, limit_sources)
        report, new_ids, repost_ids = pipeline.run_poll(
            conn, adapters, registry["aliases"], mode=mode
        )
        llm_calls, sent = _enrich_and_alert(
            conn, settings, registry, new_ids, repost_ids, cap=cap, suppress_alerts=suppress_alerts
        )
        if report.run_id is not None:
            db.update_run_counts(conn, report.run_id, llm_calls, sent)
        conn.commit()
    finally:
        http.close()
        conn.close()
    failed = len(report.sources_failed)
    print(
        f"{mode}: {report.sources_ok} source(s) ok, {failed} failure(s), "
        f"{report.new_postings} new posting(s), {len(repost_ids)} repost(s)"
    )
    for name, err in report.sources_failed.items():
        print(f"  FAILED {name}: {err}", file=sys.stderr)
    return report, adapters


def cmd_poll(args) -> int:
    settings = settings_mod.load_settings()
    registry = settings_mod.load_registry()
    report, adapters = _poll(
        settings,
        registry,
        args.limit_sources,
        cap=settings.max_llm_calls_per_run,
        suppress_alerts=False,
    )
    if report.sources_ok == 0 and adapters:
        return 1  # ALL sources failed = infrastructure failure
    return 0


def cmd_bootstrap(_args) -> int:
    """First full poll + enrich the ENTIRE backlog at the bootstrap cap, flash
    alerts suppressed. Digests then drain the ranked backlog tier-first over the
    next 2-3 mornings (SPEC §11 M2, owner decision D2)."""
    settings = settings_mod.load_settings()
    registry = settings_mod.load_registry()
    report, adapters = _poll(
        settings,
        registry,
        None,
        cap=settings.bootstrap_max_llm_calls,
        suppress_alerts=True,
        mode="bootstrap",
    )
    conn = db.connect(settings.db_path)
    pending = len(digest.select_clusters(conn))
    conn.close()
    print(f"bootstrap: {pending} cluster(s) queued for the next digests")
    return 0 if (report.sources_ok or not adapters) else 1


def _sync_applied(conn, settings, registry) -> None:
    try:
        stats = applied.sync_applied(conn, settings, registry)
    except applied.SyncError as exc:
        print(f"sync-applied: FAILED ({exc})", file=sys.stderr)
        return
    if stats.skipped:
        print(f"sync-applied: skipped ({stats.skipped})")
        return
    print(f"sync-applied: {stats.matched} matched, {len(stats.unmatched)} sheet rows unmatched")
    for line in stats.unmatched[:20]:
        print(f"  unmatched: {line}")


def cmd_digest(args) -> int:
    settings = settings_mod.load_settings()
    registry = settings_mod.load_registry()
    conn = db.connect(settings.db_path)
    try:
        _sync_applied(conn, settings, registry)
        return digest.run_digest(conn, settings, registry, send=not args.no_send)
    finally:
        conn.commit()
        conn.close()


def cmd_sync_applied(_args) -> int:
    settings = settings_mod.load_settings()
    registry = settings_mod.load_registry()
    conn = db.connect(settings.db_path)
    try:
        _sync_applied(conn, settings, registry)
    finally:
        conn.commit()
        conn.close()
    return 0


def cmd_mark_applied(args) -> int:
    settings = settings_mod.load_settings()
    conn = db.connect(settings.db_path)
    try:
        n = applied.mark_applied(conn, args.url)
        conn.commit()
    finally:
        conn.close()
    print(f"mark-applied: {n} posting(s) marked")
    return 0 if n else 1


# ---------------------------------------------------------------- dry-run


class FakeEnrichClient:
    """Deterministic keyword scorer so dry-run exercises grouping with zero LLM."""

    model_name = "fake-dry-run"

    def parse(self, system: str, user: str) -> EnrichmentOut:
        text = user.lower()
        fit = Fit(quant=20, ai_infra=20, swe=55, robotics=15)
        category = "swe"
        if re.search(r"quant|trading|trader", text):
            fit, category = Fit(quant=92, ai_infra=30, swe=60, robotics=10), "quant"
        elif re.search(r"robot|perception|autonom", text):
            fit, category = Fit(quant=10, ai_infra=45, swe=55, robotics=90), "robotics"
        elif re.search(r"\bml\b|machine learning|gpu|inference|infra", text):
            fit, category = Fit(quant=25, ai_infra=85, swe=60, robotics=20), "ai_infra"
        season = "summer_2027" if "2027" in text else "unclear"
        return EnrichmentOut(
            category=category,
            season=season,
            grad_window=GradWindow(earliest="2027-12", latest="2028-12"),
            workauth="unspecified",
            deadline=None,
            fit=fit,
            rationale="dry-run keyword scorer — not an LLM judgment",
            red_flags=[],
        )


def _nuft_fixture_tarball() -> bytes:
    """In-memory tar.gz of the two recorded NUFT yaml fixtures, shaped like the
    codeload archive the adapter fetches (members under {repo}-main/data/)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for fixture, member in (
            ("github_nuft_sig.yaml", "2027QuantInternships-main/data/sig.yaml"),
            ("github_nuft_optiver.yaml", "2027QuantInternships-main/data/optiver.yaml"),
        ):
            data = (_FIXTURES / fixture).read_bytes()
            info = tarfile.TarInfo(member)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _dry_run_fixtures() -> dict:
    repos = {cfg["repo"]: cfg for cfg in trackers_mod.DEFAULT_REPOS}
    return {
        "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs?content=true": _FIXTURES
        / "greenhouse_anthropic.json",
        "https://boards-api.greenhouse.io/v1/boards/wehrtyou/jobs?content=true": _FIXTURES
        / "greenhouse_wehrtyou.json",
        "https://boards-api.greenhouse.io/v1/boards/janestreet/jobs?content=true": _FIXTURES
        / "greenhouse_janestreet.json",
        "https://api.lever.co/v0/postings/palantir?mode=json": _FIXTURES / "lever_palantir.json",
        "https://api.ashbyhq.com/posting-api/job-board/modal": _FIXTURES / "ashby_modal.json",
        "https://careers.sig.com/api/jobs?page=1&limit=100": _FIXTURES / "icims_sig.json",
        SEARCH_URL: _FIXTURES / "hn_search.json",
        "https://hn.algolia.com/api/v1/items/49156683": _FIXTURES / "hn_items_49156683.json",
        DEFAULT_FEEDS[0]: _FIXTURES / "reddit_csmajors_new.rss",
        repos["SimplifyJobs/Summer2027-Internships"]["url"]: _FIXTURES
        / "github_simplify_listings.json",
        repos["vanshb03/Summer2027-Internships"]["url"]: _FIXTURES / "github_vansh_listings.json",
        repos["zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships"][
            "url"
        ]: _FIXTURES / "github_zshah_jobs.json",
        repos["northwesternfintech/2027QuantInternships"]["url"]: _nuft_fixture_tarball(),
        repos["speedyapply/2027-SWE-College-Jobs"]["url"]: _FIXTURES
        / "github_speedyapply_readme.md",
        repos["speedyapply/2027-AI-College-Jobs"]["url"]: _FIXTURES
        / "github_speedyapply_readme.md",
    }


def cmd_dry_run(args) -> int:
    """Full pipeline, EVERY adapter, against recorded fixtures + a temp DB, with a
    keyword-scoring fake enricher. Zero network, zero LLM (SPEC §10)."""
    settings = settings_mod.load_settings()
    registry = settings_mod.load_registry()
    http = FixtureHttp(_dry_run_fixtures())
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        conn = db.connect(tmp.name)
        adapters = [
            GreenhouseAdapter(
                http,
                [
                    ("Anthropic", "anthropic"),
                    ("Hudson River Trading", "wehrtyou"),
                    ("Jane Street", "janestreet"),
                ],
            ),
            LeverAdapter(http, [("Palantir", "palantir")]),
            AshbyAdapter(http, [("Modal", "modal")]),
            IcimsAdapter(http, [("SIG", "careers.sig.com")]),
            GithubTrackersAdapter(http),
            HnWhoIsHiringAdapter(http),
            RedditRssAdapter(http),
        ]
        report, _new, _reposts = pipeline.run_poll(
            conn, adapters, registry["aliases"], mode="dry-run"
        )
        stats = enrich.enrich_postings(
            conn, settings, registry, cap=10_000, client=FakeEnrichClient()
        )
        print(
            f"dry-run: {report.sources_ok} source(s) ok, "
            f"{len(report.sources_failed)} failure(s), {report.new_postings} new posting(s), "
            f"{stats.enriched} enriched (fake scorer)"
        )
        for name, err in report.sources_failed.items():
            print(f"  FAILED {name}: {err}", file=sys.stderr)
        print(digest.render_text(conn, settings, registry))
        if args.html:
            d = digest.build_digest(conn, settings, registry)
            Path(args.html).write_text(d.html, encoding="utf-8")
            print(f"wrote HTML digest preview to {args.html}")
        conn.close()
    return 0


def _not_implemented(milestone: str):
    def handler(_args) -> int:
        print(f"not implemented until {milestone} (SPEC §11)", file=sys.stderr)
        return 2

    return handler


def cmd_probe(_args) -> int:
    print(
        "probe is a standalone script: uv run python scripts/probe_ats.py [--dry-run]",
        file=sys.stderr,
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="radar", description="internship-radar (SPEC.md)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_poll = sub.add_parser("poll", help="poll all sources, store, enrich, flash-alert")
    p_poll.add_argument(
        "--limit-sources",
        nargs="+",
        help="only run these adapters (e.g. --limit-sources greenhouse)",
    )
    p_poll.set_defaults(func=cmd_poll)

    p_digest = sub.add_parser("digest", help="sync applied-state, render + send the digest")
    p_digest.add_argument("--no-send", action="store_true", help="print the text digest only")
    p_digest.set_defaults(func=cmd_digest)

    p_boot = sub.add_parser("bootstrap", help="first full poll + enrich the whole backlog")
    p_boot.set_defaults(func=cmd_bootstrap)

    p_sync = sub.add_parser("sync-applied", help="pull applied-state from the Applied sheet")
    p_sync.set_defaults(func=cmd_sync_applied)

    p_mark = sub.add_parser("mark-applied", help="mark one posting (by URL) as applied")
    p_mark.add_argument("url")
    p_mark.set_defaults(func=cmd_mark_applied)

    p_dry = sub.add_parser("dry-run", help="fixtures + temp DB, fake enricher, no network")
    p_dry.add_argument("--html", help="also write the HTML digest preview to this path")
    p_dry.set_defaults(func=cmd_dry_run)

    p_probe = sub.add_parser("probe", help="registry token prober (scripts/probe_ats.py)")
    p_probe.set_defaults(func=cmd_probe)

    for name, milestone in [("backfill", "M4"), ("export", "M4")]:
        p = sub.add_parser(name)
        p.set_defaults(func=_not_implemented(milestone))

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
