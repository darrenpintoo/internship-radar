"""Applied-state sync tests (SPEC §14 item 1). Zero network: httpx.MockTransport."""

import json
from types import SimpleNamespace

import httpx
import pytest

from radar import applied, db, dedup
from radar.models import Posting

ALIASES = {"hrt": "hudson river trading"}
REGISTRY = {"companies": [], "aliases": ALIASES}
SETTINGS = SimpleNamespace(
    sheet_webapp_url="https://script.google.com/macros/s/x/exec", sheet_token="t"
)


def make_posting(**overrides) -> Posting:
    base = dict(
        company_name="Hudson River Trading",
        title="Software Engineering Internship (C++ or Python) – Summer 2027",
        url="https://boards.greenhouse.io/wehrtyou/jobs/1",
        source="greenhouse",
        source_ref="wehrtyou",
        source_key="1",
        location_raw="New York, NY",
        description="Build trading systems.",
    )
    base.update(overrides)
    return Posting(**base)


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    # 2-member cluster (greenhouse + tracker echo) plus a separate Jane Street posting.
    dedup.process_board(c, [make_posting()], "full", ALIASES)
    dedup.process_board(
        c,
        [
            make_posting(
                source="github_trackers",
                source_ref="simplify",
                source_key="uuid-1",
                url="https://example.com/apply",
                description=None,
            )
        ],
        "excludes_only",
        ALIASES,
    )
    dedup.process_board(
        c,
        [
            make_posting(
                company_name="Jane Street",
                title="Quantitative Trading Intern – Summer 2027",
                url="https://www.janestreet.com/join-jane-street/position/1234/",
                source_ref="janestreet",
                source_key="1234",
            )
        ],
        "full",
        ALIASES,
    )
    assert (
        c.execute("SELECT COUNT(DISTINCT COALESCE(cluster_id, id)) FROM postings").fetchone()[0]
        == 2
    )
    yield c
    c.close()


ROWS = [
    {
        "rowId": "a",
        "date": "2026-08-10",
        "company": "HRT",
        "title": "whatever",
        "status": "Phone screen",
        "link": "https://Boards.Greenhouse.io/wehrtyou/jobs/1/?utm_source=li&gh_src=abc#app",
    },
    {
        "rowId": "b",
        "date": "2026-08-12",
        "company": "Jane Street LLC",
        "title": "Quantitative Trading Intern (Summer 2027)",
        "status": "",
        "link": "https://janestreet.com/some/other/page",
    },
    {
        "rowId": "c",
        "date": "2026-08-13",
        "company": "Acme Corp",
        "title": "Backend Intern",
        "status": "Applied",
        "link": "https://acme.example/jobs/9",
    },
]


def transport_for(calls, body=None, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/exec"):
            # Apps Script redirects /exec POSTs to googleusercontent (followed as GET).
            calls.append(json.loads(request.content))
            return httpx.Response(
                302, headers={"location": "https://script.googleusercontent.com/echo"}
            )
        return httpx.Response(status, json=body if body is not None else {"ok": True, "rows": ROWS})

    return httpx.MockTransport(handler)


def applied_map(conn):
    return {r["url"]: r["applied_at"] for r in conn.execute("SELECT url, applied_at FROM postings")}


def test_sync_matches_url_fuzzy_and_reports_unmatched(conn):
    calls = []
    stats = applied.sync_applied(
        conn, SETTINGS, REGISTRY, transport=transport_for(calls), now="2026-08-21T00:00:00Z"
    )
    assert calls[0] == {"token": "t", "action": "list"}
    assert stats.skipped is None and stats.matched == 2
    assert stats.unmatched == ["Acme Corp — Backend Intern"]
    am = applied_map(conn)
    # url match stamps both cluster members with the sheet date
    assert am["https://boards.greenhouse.io/wehrtyou/jobs/1"] == "2026-08-10"
    assert am["https://example.com/apply"] == "2026-08-10"
    # fuzzy company+title match
    assert am["https://www.janestreet.com/join-jane-street/position/1234/"] == "2026-08-12"
    hrt_key = conn.execute(
        "SELECT COALESCE(cluster_id, id) FROM postings WHERE url LIKE '%greenhouse%'"
    ).fetchone()[0]
    js_key = conn.execute("SELECT id FROM postings WHERE url LIKE '%janestreet%'").fetchone()[0]
    assert db.kv_get(conn, f"applied_status:{hrt_key}") == "Phone screen"
    assert db.kv_get(conn, f"applied_status:{js_key}") == "Applied"  # blank status defaults


def test_not_configured_skips_without_request(conn):
    calls = []
    stats = applied.sync_applied(
        conn,
        SimpleNamespace(sheet_webapp_url="", sheet_token="t"),
        REGISTRY,
        transport=transport_for(calls),
    )
    assert stats == applied.SyncStats(0, [], "not configured")
    assert calls == []


def test_non_ok_response_raises(conn):
    with pytest.raises(applied.SyncError):
        applied.sync_applied(
            conn,
            SETTINGS,
            REGISTRY,
            transport=transport_for([], body={"ok": False, "error": "bad token"}),
        )
    with pytest.raises(applied.SyncError):
        applied.sync_applied(
            conn, SETTINGS, REGISTRY, transport=transport_for([], body={}, status=500)
        )
    assert all(v is None for v in applied_map(conn).values())


def test_mark_applied_by_url(conn):
    n = applied.mark_applied(
        conn, "https://example.com/apply/?utm_campaign=x", now="2026-08-21T00:00:00Z"
    )
    assert n == 2
    am = applied_map(conn)
    assert am["https://boards.greenhouse.io/wehrtyou/jobs/1"] == "2026-08-21T00:00:00Z"
    assert am["https://www.janestreet.com/join-jane-street/position/1234/"] is None
    assert applied.mark_applied(conn, "https://nowhere.example/x") == 0


def test_canonical_url():
    u = "HTTPS://Jobs.Example.com/Apply/Path/?utm_source=x&ref=li&gh_src=y&lever-source=z&id=7#frag"
    assert applied.canonical_url(u) == "https://jobs.example.com/Apply/Path?id=7"
    assert applied.canonical_url("https://a.com/") == "https://a.com"
