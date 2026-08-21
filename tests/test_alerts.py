"""Flash alert tests (SPEC §8.6). Zero network: httpx.MockTransport counts sends."""

import httpx
import pytest

from radar import alerts, db, dedup, mail
from radar.models import EnrichmentOut, Fit, GradWindow, Posting
from radar.settings import Settings

NOW = "2026-08-21T12:00:00Z"
# Distinct title_norms: same-company numbered titles collapse into one cluster.
TITLES = [
    "Quant Trading Intern – Summer 2027",
    "Software Engineering Intern – Summer 2027",
    "Quant Research Intern – Summer 2027",
    "Infrastructure Engineering Intern – Summer 2027",
]
REGISTRY = {
    "companies": [
        {"name": "Jane Street", "tier": "S", "ats": "greenhouse", "token": "janestreet"},
        {"name": "Stripe", "tier": "1", "ats": "greenhouse", "token": "stripe"},
    ],
    "aliases": {},
}


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def make_settings(**overrides) -> Settings:
    base = dict(
        db_path=":memory:",
        model="m",
        max_llm_calls_per_run=0,
        bootstrap_max_llm_calls=0,
        send_empty=True,
        send_flash=True,
        flash_max_per_day=3,
        digest_max_rows=150,
        digest_to="me@example.com",
        owner_email="me@example.com",
        anthropic_api_key="",
        openai_api_key="",
        openai_model="",
        llm_provider="anthropic",
        resend_api_key="re_test",
        sheet_webapp_url="",
        sheet_token="",
    )
    base.update(overrides)
    return Settings(**base)


def make_posting(company="Stripe", key="1", source="greenhouse", **overrides) -> Posting:
    base = dict(
        company_name=company,
        title="Software Engineering Intern – Summer 2027",
        url=f"https://example.com/{source}/{key}",
        source=source,
        source_ref="board",
        source_key=key,
        location_raw="New York, NY",
        description="Build things.",
    )
    base.update(overrides)
    return Posting(**base)


def store(conn, postings) -> list[int]:
    return dedup.process_board(conn, postings, "full", {}).new_ids


def enrich(conn, pid, score):
    out = EnrichmentOut(
        category="swe",
        season="summer_2027",
        grad_window=GradWindow(),
        workauth="unspecified",
        fit=Fit(quant=score, ai_infra=score, swe=score, robotics=score),
        rationale="Strong systems match.",
    )
    db.insert_enrichment(conn, pid, out, "m", NOW)
    conn.commit()


class Mailbox:
    def __init__(self, fail_subjects: set[str] | None = None):
        self.requests: list[httpx.Request] = []
        self.fail_subjects = fail_subjects or set()
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        import json

        subject = json.loads(request.content)["subject"]
        if subject in self.fail_subjects:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"id": f"re_{len(self.requests)}"})


def run(conn, ids, settings=None, box=None, **kw):
    box = box or Mailbox()
    n = alerts.send_flash_alerts(
        conn, settings or make_settings(), REGISTRY, ids, transport=box.transport, now=NOW, **kw
    )
    return n, box


def test_tier_s_fires_without_enrichment(conn):
    ids = store(conn, [make_posting("Jane Street")])
    n, box = run(conn, ids)
    assert n == 1 and len(box.requests) == 1
    body = box.requests[0].content.decode()
    assert "not yet scored" in body and "Jane Street" in body
    assert box.requests[0].headers["Idempotency-Key"] == f"flash-{ids[0]}"
    row = conn.execute("SELECT alerted_at FROM postings WHERE id = ?", (ids[0],)).fetchone()
    assert row["alerted_at"] == NOW
    assert conn.execute("SELECT reason FROM alert_log").fetchone()["reason"] == "tier_s"


def test_fit_90_fires(conn):
    ids = store(conn, [make_posting("Stripe")])
    enrich(conn, ids[0], 92)
    n, box = run(conn, ids)
    assert n == 1
    assert "Strong systems match." in box.requests[0].content.decode()
    assert conn.execute("SELECT reason FROM alert_log").fetchone()["reason"] == "fit"


def test_fit_85_tier_1_does_not_fire(conn):
    ids = store(conn, [make_posting("Stripe")])
    enrich(conn, ids[0], 85)
    n, box = run(conn, ids)
    assert n == 0 and box.requests == []


def test_cluster_mate_never_double_alerts(conn):
    [a] = store(conn, [make_posting("Jane Street", key="1")])
    [b] = store(conn, [make_posting("Jane Street", key="h1", source="hn_whoishiring")])
    assert db.cluster_members(conn, a) and len(db.cluster_members(conn, a)) == 2
    n, _ = run(conn, [a, b])
    assert n == 1
    assert all(
        r["alerted_at"] == NOW for r in conn.execute("SELECT alerted_at FROM postings").fetchall()
    )
    n2, box2 = run(conn, [b])
    assert n2 == 0 and box2.requests == []


def test_daily_cap_honored(conn):
    ids = store(
        conn,
        [make_posting("Jane Street", key=str(i), title=TITLES[i]) for i in range(4)],
    )
    # two already sent today (the log is the cap's source of truth)
    db.insert_alert_log(conn, ids[0], NOW, "tier_s")
    db.insert_alert_log(conn, ids[1], NOW, "tier_s")
    conn.commit()
    n, box = run(conn, ids[2:], make_settings(flash_max_per_day=3))
    assert n == 1 and len(box.requests) == 1
    assert db.alerts_sent_on(conn, "2026-08-21") == 3
    n2, box2 = run(conn, ids[3:])
    assert n2 == 0 and box2.requests == []


def test_suppress_and_send_flash_false_send_nothing(conn):
    ids = store(conn, [make_posting("Jane Street")])
    n, box = run(conn, ids, suppress=True)
    assert n == 0 and box.requests == []
    n, box = run(conn, ids, make_settings(send_flash=False))
    assert n == 0 and box.requests == []
    assert conn.execute("SELECT COUNT(*) FROM alert_log").fetchone()[0] == 0


def test_send_error_does_not_stop_next(conn):
    ids = store(
        conn,
        [make_posting("Jane Street", key=str(i), title=TITLES[i]) for i in range(2)],
    )
    box = Mailbox(fail_subjects={f"Radar ⚡ Jane Street — {TITLES[0]}"})
    n, _ = run(conn, ids, box=box)
    assert n == 1 and len(box.requests) == 2
    rows = {r["id"]: r["alerted_at"] for r in conn.execute("SELECT id, alerted_at FROM postings")}
    assert rows[ids[0]] is None and rows[ids[1]] == NOW


def test_send_error_class_is_mail_senderror():
    assert issubclass(mail.SendError, RuntimeError)
