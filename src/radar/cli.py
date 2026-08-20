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
import sys
import tarfile
import tempfile
from pathlib import Path

from . import db, digest, pipeline
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


def cmd_dry_run(_args) -> int:
    """Full pipeline, EVERY adapter, against recorded fixtures + a temp DB.
    Zero network, zero LLM (SPEC §10)."""
    registry = settings_mod.load_registry()
    http = FixtureHttp(_dry_run_fixtures())
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        conn = db.connect(tmp.name)
        adapters = [
            GreenhouseAdapter(
                http, [("Anthropic", "anthropic"), ("Hudson River Trading", "wehrtyou")]
            ),
            LeverAdapter(http, [("Palantir", "palantir")]),
            AshbyAdapter(http, [("Modal", "modal")]),
            IcimsAdapter(http, [("SIG", "careers.sig.com")]),
            GithubTrackersAdapter(http),
            HnWhoIsHiringAdapter(http),
            RedditRssAdapter(http),
        ]
        report, _ = pipeline.run_poll(conn, adapters, registry["aliases"], mode="dry-run")
        print(
            f"dry-run: {report.sources_ok} source(s) ok, "
            f"{len(report.sources_failed)} failure(s), {report.new_postings} new posting(s)"
        )
        for name, err in report.sources_failed.items():
            print(f"  FAILED {name}: {err}", file=sys.stderr)
        print(digest.render_text(conn))
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

    p_probe = sub.add_parser("probe", help="registry token prober (scripts/probe_ats.py)")
    p_probe.set_defaults(func=cmd_probe)

    for name, milestone in [
        ("digest", "M2"),
        ("bootstrap", "M2"),
        ("sync-applied", "M2"),
        ("mark-applied", "M2"),
        ("backfill", "M4"),
        ("export", "M4"),
    ]:
        p = sub.add_parser(name)
        p.set_defaults(func=_not_implemented(milestone))

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
