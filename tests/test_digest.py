"""Digest selection, grouping, render, send-ordering tests (SPEC §8, §7)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from radar import db, dedup, digest
from radar.models import EnrichmentOut, Fit, GradWindow, Posting, RunReport

TODAY = "2026-08-21"
REGISTRY = {
    "companies": [
        {"name": "Jane Street", "tier": "S", "ats": "greenhouse", "token": "janestreet"},
        {"name": "Hudson River Trading", "tier": "1", "ats": "greenhouse", "token": "hrt"},
    ],
    "aliases": {"hrt": "hudson river trading"},
}


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def settings(**overrides):
    base = dict(
        digest_max_rows=150,
        send_empty=True,
        resend_api_key="re_test",
        digest_to="owner@example.com",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def make_posting(**overrides) -> Posting:
    base = dict(
        company_name="Hudson River Trading",
        title="Software Engineering Internship – Summer 2027",
        url="https://boards.greenhouse.io/wehrtyou/jobs/1",
        source="greenhouse",
        source_ref="wehrtyou",
        source_key="1",
        location_raw="New York, NY",
        description="Build trading systems.",
    )
    base.update(overrides)
    return Posting(**base)


def store(conn, postings, policy="full", now=None):
    return dedup.process_board(conn, postings, policy, REGISTRY["aliases"], now=now)


def enrich(
    conn,
    pid,
    fit=80,
    category="swe",
    season="summer_2027",
    deadline=None,
    grad=None,
    workauth="sponsors_ok",
):
    out = EnrichmentOut(
        category=category,
        season=season,
        grad_window=GradWindow(**(grad or {})),
        workauth=workauth,
        deadline=deadline,
        fit=Fit(quant=fit, ai_infra=fit, swe=fit, robotics=fit),
        rationale="Systems-heavy role.",
        red_flags=[],
    )
    db.insert_enrichment(conn, pid, out, "test-model", db.now_utc())
    conn.commit()


def mock_transport(status=200, calls=None):
    def handler(request):
        if calls is not None:
            calls.append(request)
        if status >= 300:
            return httpx.Response(status, json={"message": "boom"})
        return httpx.Response(200, json={"id": "re_123"})

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------- grad flag


class TestGradFlag:
    def test_all(self):
        assert digest.grad_flag("2027-12", "2029-06") == "✓ any grad date"
        assert digest.grad_flag(None, "2029-05") == "✓ any grad date"

    def test_some(self):
        assert digest.grad_flag("2028-06", "2029-06") == "⚠ use 2028-12"
        assert digest.grad_flag(None, "2028-05") == "⚠ use 2028-05"

    def test_none(self):
        assert digest.grad_flag("2026-05", "2027-06") == "✗ outside window"

    def test_null(self):
        assert digest.grad_flag(None, None) is None


# ---------------------------------------------------------------- selection + send


class TestSend:
    def test_cluster_renders_once_and_all_members_digested(self, conn):
        s1 = store(conn, [make_posting()])
        echo = make_posting(
            source="github_trackers",
            source_ref="simplify",
            source_key="uuid-1",
            url="https://example.com/apply",
            description=None,
            location_raw="NYC",
        )
        s2 = store(conn, [echo], policy="excludes_only")
        enrich(conn, s1.new_ids[0], fit=88)
        calls = []
        rc = digest.run_digest(
            conn, settings(), REGISTRY, transport=mock_transport(calls=calls), today=TODAY
        )
        assert rc == 0 and len(calls) == 1
        assert calls[0].headers["Idempotency-Key"] == f"digest-{TODAY}"
        ids = {s1.new_ids[0], s2.new_ids[0]}
        rows = conn.execute("SELECT id, digested_at FROM postings").fetchall()
        assert {r["id"] for r in rows if r["digested_at"]} == ids
        logged = conn.execute("SELECT * FROM digest_log").fetchone()
        assert set(json.loads(logged["posting_ids"])) == ids
        assert logged["resend_id"] == "re_123"
        assert "Radar: 1 new (1 strong)" in logged["subject"]

        # Never resend: second run renders nothing new.
        d2 = digest.build_digest(conn, settings(), REGISTRY, today=TODAY)
        assert d2.counts["new"] == 0 and d2.rendered_member_ids == []
        assert d2.subject == "Radar: 0 new — Fri Aug 21"

    def test_one_row_aggregates_locations_and_sources(self, conn):
        store(conn, [make_posting()])
        echo = make_posting(
            source="github_trackers",
            source_ref="simplify",
            source_key="uuid-1",
            url="https://example.com/apply",
            location_raw="NYC; Chicago, IL",
        )
        store(conn, [echo], policy="excludes_only")
        d = digest.build_digest(conn, settings(), REGISTRY, today=TODAY)
        [row] = d.strong + d.worth + d.other
        assert row["url"].startswith("https://boards.greenhouse.io")  # direct-ATS rep
        assert row["locations"] == ["New York, NY", "NYC", "Chicago, IL"]
        assert row["sources"] == ["github_trackers", "greenhouse"]
        assert "tracker" in d.html and "github_trackers/greenhouse" in d.text

    def test_send_failure_marks_nothing(self, conn):
        store(conn, [make_posting()])
        rc = digest.run_digest(
            conn, settings(), REGISTRY, transport=mock_transport(status=500), today=TODAY
        )
        assert rc == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM postings WHERE digested_at IS NOT NULL").fetchone()[
                0
            ]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM digest_log").fetchone()[0] == 0

    def test_empty_and_send_empty_false_sends_nothing(self, conn):
        calls = []
        rc = digest.run_digest(
            conn,
            settings(send_empty=False),
            REGISTRY,
            transport=mock_transport(calls=calls),
            today=TODAY,
        )
        assert rc == 0 and calls == []

    def test_empty_heartbeat_sends_when_send_empty_true(self, conn):
        calls = []
        rc = digest.run_digest(
            conn, settings(), REGISTRY, transport=mock_transport(calls=calls), today=TODAY
        )
        assert rc == 0 and len(calls) == 1
        body = json.loads(calls[0].content)
        assert body["subject"] == "Radar: 0 new — Fri Aug 21"


# ---------------------------------------------------------------- grouping


class TestGrouping:
    def test_tier_s_unenriched_is_strong(self, conn):
        store(
            conn,
            [
                make_posting(
                    company_name="Jane Street",
                    source_ref="janestreet",
                    title="Software Engineer Intern",
                )
            ],
        )
        d = digest.build_digest(conn, settings(), REGISTRY, today=TODAY)
        assert len(d.strong) == 1 and d.strong[0]["enriched"] is False
        assert "pending scoring" in d.html and "pending scoring" in d.text

    def test_tier_s_off_track_low_fit_is_not_strong(self, conn):
        s = store(
            conn,
            [
                make_posting(
                    company_name="Jane Street",
                    source_ref="janestreet",
                    title="Barista Intern",
                )
            ],
        )
        enrich(conn, s.new_ids[0], fit=10, category="other")
        d = digest.build_digest(conn, settings(), REGISTRY, today=TODAY)
        assert d.strong == [] and d.worth == [] and len(d.other) == 1

    def test_worth_band_and_null_enrichment_sorts_last(self, conn):
        s = store(
            conn,
            [
                make_posting(source_key="1", title="Quant Research Intern (Summer 2027)"),
                make_posting(source_key="2", title="Compiler Engineering Intern (Summer 2027)"),
                make_posting(source_key="3", title="FPGA Hardware Intern (Summer 2027)"),
            ],
        )
        a, b, c = s.new_ids
        enrich(conn, a, fit=60)
        enrich(conn, c, fit=20)
        db.insert_enrichment(conn, b, None, "test-model", db.now_utc())  # enrichment_failed row
        conn.commit()
        d = digest.build_digest(conn, settings(), REGISTRY, today=TODAY)
        assert [r["member_ids"][0] for r in d.worth] == [a]
        assert [r["member_ids"][0] for r in d.other] == [c, b]
        assert d.other[-1]["enriched"] is False
        assert "pending scoring" in d.text

    def test_everything_else_cap_leaves_remainder_undigested(self, conn):
        postings = [
            make_posting(company_name=f"Firm {i}", source_ref=f"firm{i}", source_key=str(i))
            for i in range(45)
        ]
        store(conn, postings)
        d = digest.build_digest(conn, settings(), REGISTRY, today=TODAY)
        assert len(d.other) == 40 and d.omitted_count == 5
        assert len(d.rendered_member_ids) == 40
        assert "and 5 more" in d.html
        rc = digest.run_digest(conn, settings(), REGISTRY, transport=mock_transport(), today=TODAY)
        assert rc == 0
        left = conn.execute("SELECT COUNT(*) FROM postings WHERE digested_at IS NULL").fetchone()[0]
        assert left == 5
        d2 = digest.build_digest(conn, settings(), REGISTRY, today=TODAY)
        assert d2.counts["new"] == 5

    def test_digest_max_rows_tightens_other_cap(self, conn):
        postings = [
            make_posting(company_name=f"Firm {i}", source_ref=f"firm{i}", source_key=str(i))
            for i in range(10)
        ]
        store(conn, postings)
        d = digest.build_digest(conn, settings(digest_max_rows=4), REGISTRY, today=TODAY)
        assert len(d.other) == 4 and d.omitted_count == 6

    def test_rejected_cluster_excluded(self, conn):
        s = store(conn, [make_posting()])
        pid = s.new_ids[0]
        db.set_applied(conn, [pid], "2026-08-01")
        db.kv_set(conn, f"applied_status:{pid}", "Rejected")
        conn.commit()
        d = digest.build_digest(conn, settings(), REGISTRY, today=TODAY)
        assert d.counts["new"] == 0 and d.rendered_member_ids == []

    def test_applied_badge_and_closed_badge(self, conn):
        s = store(conn, [make_posting()])
        pid = s.new_ids[0]
        db.set_applied(conn, [pid], "2026-08-01")
        db.kv_set(conn, f"applied_status:{pid}", "Interview")
        conn.execute("UPDATE postings SET active = 0 WHERE id = ?", (pid,))
        conn.commit()
        d = digest.build_digest(conn, settings(), REGISTRY, today=TODAY)
        [row] = d.other
        assert row["applied"] and row["closed"]
        assert "applied · Interview" in d.html and ">closed<" in d.html


# ---------------------------------------------------------------- closing soon


class TestClosingSoon:
    def test_repeats_across_runs_without_marking(self, conn):
        s = store(conn, [make_posting()])
        pid = s.new_ids[0]
        enrich(conn, pid, fit=60, deadline="2026-08-30")  # >= CLOSING_MIN_FIT
        d1 = digest.build_digest(conn, settings(), REGISTRY, today=TODAY)
        assert len(d1.closing) == 1 and d1.counts["new"] == 1
        rc = digest.run_digest(conn, settings(), REGISTRY, transport=mock_transport(), today=TODAY)
        assert rc == 0
        d2 = digest.build_digest(conn, settings(), REGISTRY, today=TODAY)
        assert d2.counts["new"] == 0
        assert len(d2.closing) == 1 and d2.closing[0]["deadline"] == "2026-08-30"
        assert d2.rendered_member_ids == []
        assert "Closing soon" in d2.html

    def test_applied_and_far_deadlines_excluded(self, conn):
        s = store(
            conn,
            [
                make_posting(source_key="1", title="Quant Research Intern (Summer 2027)"),
                make_posting(source_key="2", title="Compiler Engineering Intern (Summer 2027)"),
            ],
        )
        a, b = s.new_ids
        enrich(conn, a, deadline="2026-08-25")
        enrich(conn, b, deadline="2026-10-01")
        db.set_applied(conn, [a], "2026-08-10")
        conn.commit()
        d = digest.build_digest(conn, settings(), REGISTRY, today=TODAY)
        assert d.closing == []


# ---------------------------------------------------------------- subject + html


class TestSubjectAndHtml:
    def test_sources_down_prefix_and_footer(self, conn):
        db.record_run(
            conn,
            RunReport(
                mode="poll",
                started_at="2026-08-21T06:00:00Z",
                finished_at="2026-08-21T06:01:00Z",
                sources_ok=5,
                sources_failed={"reddit_rss": "403", "hn_whoishiring": "timeout"},
                new_postings=3,
                llm_calls=2,
            ),
        )
        conn.commit()
        store(conn, [make_posting()])
        d = digest.build_digest(conn, settings(), REGISTRY, today=TODAY)
        assert d.subject == "Radar [2 sources down]: 1 new (0 strong) — Fri Aug 21"
        assert d.sources_failed == {"reddit_rss": "403", "hn_whoishiring": "timeout"}
        assert "reddit_rss" in d.html and "hn_whoishiring" in d.text

    def test_html_is_safe_and_escaped(self, conn):
        store(conn, [make_posting(title="<script>alert(1)</script> Intern (Summer 2027)")])
        d = digest.build_digest(conn, settings(), REGISTRY, today=TODAY)
        assert "<script" not in d.html
        assert "&lt;script&gt;" in d.html
        assert "#fff;" not in d.html.lower() and "#000;" not in d.html.lower()
        assert "https://boards.greenhouse.io/wehrtyou/jobs/1" in d.html

    def test_season_chip_only_when_not_summer(self, conn):
        s = store(
            conn,
            [
                make_posting(source_key="1", title="Quant Research Intern (Summer 2027)"),
                make_posting(source_key="2", title="Compiler Engineering Intern (Summer 2027)"),
            ],
        )
        a, b = s.new_ids
        enrich(conn, a, season="summer_2027", grad={"earliest": "2028-06", "latest": None})
        enrich(conn, b, season="other")
        d = digest.build_digest(conn, settings(), REGISTRY, today=TODAY)
        by_id = {r["member_ids"][0]: r for r in d.strong}
        assert by_id[a]["season_chip"] is None
        assert by_id[b]["season_chip"] == "off-season"
        assert by_id[a]["grad_flag"] == "⚠ use 2028-12"
        assert "off-season" in d.html

    def test_render_text_keeps_dry_run_shape(self, conn):
        store(conn, [make_posting()])
        out = digest.render_text(conn, registry=REGISTRY)
        assert out.startswith("would-be digest: 1 cluster(s)")
        assert "Hudson River Trading" in out and "greenhouse" in out


def test_html_never_exceeds_gmail_budget(conn):
    """A backlog-sized digest trims its tail until the HTML fits under the
    Gmail clip budget; trimmed clusters stay undigested (roll to tomorrow)."""
    from radar import dedup
    from radar.models import Posting

    postings = [
        Posting(
            company_name=f"Company {i:03d} Holdings",
            title=f"Software Engineering Intern - Summer 2027 - Team {i:03d} (Platform Infrastructure)",
            url=f"https://example.com/jobs/{i}",
            source="greenhouse",
            source_ref=f"board{i}",
            source_key=str(i),
            location_raw="New York, NY; San Francisco, CA; Austin, TX",
            description="x",
        )
        for i in range(400)
    ]
    dedup.process_board(conn, postings, "full", REGISTRY["aliases"])
    d = digest.build_digest(conn, settings(), REGISTRY, today="2026-08-21")
    assert len(d.html.encode("utf-8")) <= digest.HTML_BYTE_BUDGET
    assert d.omitted_count > 0
    assert len(d.rendered_member_ids) + d.omitted_count == 400


def test_best_effort_source_failures_do_not_prefix_subject(conn):
    from radar.models import RunReport

    report = RunReport(
        mode="poll",
        started_at="2026-08-23T12:00:00Z",
        finished_at="2026-08-23T12:01:00Z",
        sources_ok=6,
        sources_failed={"best-effort:reddit:feed": "403 Blocked"},
    )
    db.record_run(conn, report)
    d = digest.build_digest(conn, settings(), REGISTRY, today="2026-08-23")
    assert not d.subject.startswith("Radar [")
    assert "reddit" in d.html  # still visible in the footer


def test_min_fit_suppresses_and_consumes(conn):
    """Clusters below DIGEST_MIN_FIT are marked digested without rendering."""
    st = store(
        conn,
        [
            make_posting(source_key="lo", title="QA Intern (Summer 2027)"),
            make_posting(source_key="hi", title="Quant Dev Intern (Summer 2027)"),
        ],
    )
    lo, hi = st.new_ids
    enrich(conn, lo, fit=20)
    enrich(conn, hi, fit=90)
    d = digest.build_digest(conn, settings(digest_min_fit=40), REGISTRY, today=TODAY)
    assert d.counts["new"] == 1
    assert lo in d.rendered_member_ids  # consumed silently
    assert "QA Intern" not in d.html


def test_workauth_ok_hides_citizenship_chip(conn):
    st = store(conn, [make_posting(title="Defense SWE Intern (Summer 2027)")])
    [pid] = st.new_ids
    enrich(conn, pid, fit=80, workauth="citizen_only")
    flagged = digest.build_digest(conn, settings(), REGISTRY, today=TODAY)
    assert "citizen only" in flagged.html  # default: warn
    ok = digest.build_digest(
        conn,
        settings(workauth_ok=frozenset({"citizen_only", "no_sponsorship", "sponsors_ok"})),
        REGISTRY,
        today=TODAY,
    )
    assert "citizen only" not in ok.html  # US citizen: no warning
