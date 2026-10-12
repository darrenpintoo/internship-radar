"""GitHub trackers adapter fixture tests (SPEC §5.5, §10).

Fixture provenance — all REAL responses captured live 2026-08-20 with
User-Agent "internship-radar/0.1 (fixture capture; ...)", then trimmed:

- github_simplify_listings.json: 6 items from
  raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/
  .github/scripts/listings.json (10.8 MB, 14,445 items live): 3 keepers
  (Summer 2027, active, visible), 1 active=false, 1 Summer-2026-only,
  1 is_visible=false.
- github_vansh_listings.json: 4 items from vanshb03/Summer2027-Internships
  dev listings.json (402 items live; `season` is a bare season word):
  2 Summer keepers, 1 Fall, 1 Summer active=false.
- github_zshah_jobs.json: 5 entries from zshah101/Automated-List-Of-Summer-
  2027-and-Fall-2026-Tech-Internships main data/jobs.json (dict keyed
  'source:company_slug:jobid', 481 entries live): 2 Summer-2027 open,
  1 'Not stated' open, 1 Fall-2026 open, 1 Summer-2027 is_open=false.
- github_nuft_sig.yaml / github_nuft_optiver.yaml: verbatim data/sig.yaml
  (8 role links) and data/optiver.yaml (5 role links) from the
  codeload.github.com/northwesternfintech/2027QuantInternships main tarball.
- github_speedyapply_readme.md: the three sentinel-delimited tables from
  speedyapply/2027-SWE-College-Jobs main README.md, trimmed to 8 real rows
  (sentinel spelling TABLE_FAANG/TABLE_QUANT/TABLE _START/_END verified).
"""

import hashlib
import io
import json
import tarfile
from pathlib import Path

from radar.adapters.base import RawItem
from radar.adapters.github_trackers import DEFAULT_REPOS, GithubTrackersAdapter

FIXTURES = Path(__file__).parent / "fixtures"


def _raw(mode: str, repo: str, payload) -> RawItem:
    return RawItem("github_trackers", repo, payload=payload, meta={"mode": mode, "repo": repo})


def _adapter() -> GithubTrackersAdapter:
    return GithubTrackersAdapter(http=None)


def _parse_json(mode: str, repo: str, fixture: str):
    payload = json.loads((FIXTURES / fixture).read_text())
    return _adapter().parse(_raw(mode, repo, payload))


# ---------------------------------------------------------------- simplify


def test_simplify_keeps_only_visible_active_summer_2027():
    postings = _parse_json(
        "simplify", "SimplifyJobs/Summer2027-Internships", "github_simplify_listings.json"
    )
    # Fixture holds 6 items; active=false, Summer-2026-only, and
    # is_visible=false rows must all drop.
    assert [p.company_name for p in postings] == [
        "Aquatic Capital Management",
        "Aquatic Capital Management",
        "Ellipsis Labs",
    ]


def test_simplify_field_mapping_and_hints():
    postings = _parse_json(
        "simplify", "SimplifyJobs/Summer2027-Internships", "github_simplify_listings.json"
    )
    p = postings[0]
    assert p.title == "Quantitative Researcher – Intern"
    assert p.source == "github_trackers"
    assert p.source_ref == "SimplifyJobs/Summer2027-Internships"
    assert p.source_key == "76c586e3-eb16-454a-a7d8-5431a785b342"  # tracker UUID
    assert p.location_raw == "London, UK; Chicago, IL"  # list joined with '; '
    assert p.posted_at == "2026-04-01T22:12:27Z"  # epoch 1775081547 → UTC Z
    assert p.tracker_hints["sponsorship"] == "Other"
    assert p.tracker_hints["category"] == "Quant"
    assert p.tracker_hints["degrees"] == "Bachelor's, Master's, PhD"


def test_vansh_season_field_branch():
    postings = _parse_json(
        "simplify", "vanshb03/Summer2027-Internships", "github_vansh_listings.json"
    )
    # season='Fall' and active=false rows drop; bare season='Summer' keeps.
    assert [p.company_name for p in postings] == ["Point72", "Point72"]
    assert postings[0].posted_at == "2026-04-19T16:54:58Z"
    assert postings[0].tracker_hints["sponsorship"] == "Other"


# ---------------------------------------------------------------- zshah

ZSHAH_REPO = "zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships"


def test_zshah_season_and_open_filter():
    postings = _parse_json("zshah", ZSHAH_REPO, "github_zshah_jobs.json")
    # Fall 2026 and is_open=false entries drop; 'Not stated' keeps.
    assert {p.company_name for p in postings} == {"Deepgram", "Ellipsis Labs", "Centerfield"}


def test_zshah_dict_key_is_source_key_and_posted_at_normalized():
    postings = _parse_json("zshah", ZSHAH_REPO, "github_zshah_jobs.json")
    by_company = {p.company_name: p for p in postings}
    p = by_company["Deepgram"]
    assert p.source_key == "ashby:deepgram:dc8693b5-72ce-4ca3-ab15-9c8434d35da1"
    assert p.posted_at == "2026-07-17T16:00:27Z"  # ISO-with-offset → UTC Z
    assert p.location_raw
    assert p.url.startswith("http")
    assert p.tracker_hints.get("category")


def test_zshah_mirror_shape_fallback():
    payload = json.loads((FIXTURES / "github_zshah_jobs.json").read_text())
    mirror = {"jobs": [dict(item, id=key) for key, item in payload.items()]}
    postings = _adapter().parse(_raw("zshah", ZSHAH_REPO, mirror))
    assert {p.source_key for p in postings} == {
        "ashby:deepgram:dc8693b5-72ce-4ca3-ab15-9c8434d35da1",
        "ashby:ellipsislabs:02136b22-35b1-4b3d-8bef-567c3380a849",
        "ashby:centerfield:1d7eacc1-37f7-478c-9b0a-fa7974f1a9e4",
    }


# ---------------------------------------------------------------- nuft_yaml


def _nuft_tarball() -> bytes:
    """In-memory tar.gz of the two recorded fixture yamls — no network."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in ("sig", "optiver"):
            data = (FIXTURES / f"github_nuft_{name}.yaml").read_bytes()
            info = tarfile.TarInfo(f"2027QuantInternships-main/data/{name}.yaml")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_nuft_one_posting_per_role_link():
    raw = _raw("nuft_yaml", "northwesternfintech/2027QuantInternships", _nuft_tarball())
    postings = _adapter().parse(raw)
    assert len(postings) == 13  # sig.yaml has 8 links, optiver.yaml has 5
    sig = [p for p in postings if p.company_name == "SIG"]
    assert len(sig) == 8
    assert all(p.location_raw == "Bala Cynwyd, Chicago, NYC" for p in sig)
    assert all(p.tracker_hints == {"category": "Quant"} for p in postings)
    labeled = {p.title for p in sig}
    assert "QT Internship - Chicago" in labeled  # label appended when present
    unlabeled = [p for p in postings if p.company_name == "Optiver" and "QT" in p.title]
    assert unlabeled[0].title == "QT Internship"  # no label → bare role title


def test_nuft_source_key_is_sha256_of_url():
    raw = _raw("nuft_yaml", "northwesternfintech/2027QuantInternships", _nuft_tarball())
    postings = _adapter().parse(raw)
    for p in postings:
        assert p.source_key == hashlib.sha256(p.url.encode()).hexdigest()
    again = _adapter().parse(
        _raw("nuft_yaml", "northwesternfintech/2027QuantInternships", _nuft_tarball())
    )
    assert [p.source_key for p in again] == [p.source_key for p in postings]  # stable
    assert postings[0].posted_at is None  # NUFT yamls carry no dates


# ---------------------------------------------------------------- speedyapply


def _speedy_postings():
    text = (FIXTURES / "github_speedyapply_readme.md").read_text()
    return _adapter().parse(_raw("speedyapply", "speedyapply/2027-SWE-College-Jobs", text))


def test_speedyapply_sentinel_slicing_and_row_count():
    postings = _speedy_postings()
    assert len(postings) == 8  # 3 FAANG + 3 QUANT + 2 TABLE rows in the fixture
    assert {p.company_name for p in postings} >= {"Microsoft", "NVIDIA", "Citadel Securities"}


def test_speedyapply_url_is_direct_employer_apply_link():
    by_company = {p.company_name: p for p in _speedy_postings()}
    ms = by_company["Microsoft"]
    # The apply anchor (LAST href in the row), not the company-site anchor.
    assert ms.url == "https://apply.careers.microsoft.com/careers/job/1970393556962891"
    assert ms.title == "AI Software Engineering Intern"
    assert ms.location_raw == "Washington, DC +1"
    assert ms.source_key == hashlib.sha256(ms.url.encode()).hexdigest()
    assert ms.posted_at is None  # Age column is relative — first_seen_at carries
    belv = by_company["Belvedere Trading"]
    assert belv.url.startswith("https://jobs.lever.co/belvederetrading/")


# ---------------------------------------------------------------- fetch


def test_fetch_isolates_per_repo_failures():
    class BoomHttp:
        def get_json(self, url, **kwargs):
            raise RuntimeError("boom")

        def get(self, url, **kwargs):
            raise RuntimeError("boom")

    items = GithubTrackersAdapter(http=BoomHttp()).fetch()
    assert len(items) == len(DEFAULT_REPOS)  # one RawItem per repo config, none lost
    assert all(item.error for item in items)
    assert {item.meta["mode"] for item in items} == {
        "simplify",
        "zshah",
        "nuft_yaml",
        "speedyapply",
        "zapply_md",
    }


def test_every_mode_emits_nonempty_source_keys():
    all_postings = (
        _parse_json(
            "simplify", "SimplifyJobs/Summer2027-Internships", "github_simplify_listings.json"
        )
        + _parse_json("zshah", ZSHAH_REPO, "github_zshah_jobs.json")
        + _adapter().parse(
            _raw("nuft_yaml", "northwesternfintech/2027QuantInternships", _nuft_tarball())
        )
        + _speedy_postings()
    )
    assert all_postings
    assert all(p.source_key for p in all_postings)
    assert all(p.source == "github_trackers" for p in all_postings)


def test_zapply_underclassmen_markdown_links():
    """Recorded 2026-08-23: | [Name](url) | Status | Year | Note | rows."""
    from radar.adapters.base import RawItem
    from radar.adapters.github_trackers import GithubTrackersAdapter

    payload = (FIXTURES / "github_zapply_underclass.md").read_text()
    raw = RawItem(
        "github_trackers",
        "zapplyjobs/underclassmen-internships",
        payload=payload,
        meta={"mode": "zapply_md", "repo": "zapplyjobs/underclassmen-internships"},
    )
    postings = GithubTrackersAdapter(http=None).parse(raw)
    assert len(postings) >= 5
    by_title = {p.title: p for p in postings}
    ms = next(p for t, p in by_title.items() if "Microsoft Explore" in t)
    assert ms.url.startswith("https://careers.microsoft.com")
    assert "Freshman" in (ms.tracker_hints.get("eligibility") or "") or "Freshman" in ms.description
    assert all(p.source_key for p in postings)


def test_newgrad_target_uses_newgrad_repos_and_skips_season_filter():
    from radar.adapters.github_trackers import NEWGRAD_REPOS

    a = GithubTrackersAdapter(http=None, target="newgrad")
    assert a._repos is NEWGRAD_REPOS
    assert all("New-Grad" in c["repo"] or "NEW_GRAD" in c["url"] for c in NEWGRAD_REPOS)
    # season_filter False keeps items with no Summer-2027 term.
    item = {
        "company_name": "Acme",
        "title": "Software Engineer, New Grad",
        "url": "https://x.co/1",
        "id": "u1",
        "locations": ["NYC"],
        "active": True,
        "is_visible": True,
    }
    raw = _raw("simplify", "SimplifyJobs/New-Grad-Positions", [item])
    raw.meta["season_filter"] = False
    assert len(a.parse(raw)) == 1
    raw.meta["season_filter"] = True
    assert len(a.parse(raw)) == 0  # internship mode would drop it
