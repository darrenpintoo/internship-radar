"""Enrichment driver tests (SPEC §7): stub client, in-memory DB, zero network."""

import json

import pytest

from radar import db, dedup, enrich
from radar.models import EnrichmentOut, Fit, GradWindow, Posting
from radar.settings import Settings

ALIASES = {"hrt": "hudson river trading"}
REGISTRY = {
    "companies": [
        {"name": "Hudson River Trading", "ats": "greenhouse", "token": "x", "tier": "S"},
        {"name": "Stripe", "ats": "greenhouse", "token": "y", "tier": "2"},
    ],
    "aliases": ALIASES,
}


def make_settings(**overrides) -> Settings:
    base = dict(
        db_path=":memory:",
        model="claude-haiku-4-5-20251001",
        max_llm_calls_per_run=300,
        bootstrap_max_llm_calls=2000,
        send_empty=True,
        send_flash=True,
        flash_max_per_day=3,
        digest_max_rows=150,
        digest_to="",
        owner_email="",
        anthropic_api_key="ak",
        openai_api_key="",
        openai_model="gpt-4.1-mini",
        llm_provider="anthropic",
        resend_api_key="",
        sheet_webapp_url="",
        sheet_token="",
    )
    base.update(overrides)
    return Settings(**base)


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


def good_out() -> EnrichmentOut:
    return EnrichmentOut(
        category="quant",
        season="summer_2027",
        grad_window=GradWindow(earliest="2027-12", latest="2028-06"),
        workauth="unspecified",
        deadline=None,
        fit=Fit(quant=92, ai_infra=40, swe=70, robotics=15),
        rationale="C++ trading systems internship, bullseye.",
        red_flags=[],
    )


class StubClient:
    model_name = "stub-model"

    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.calls: list[tuple[str, str]] = []

    def parse(self, system, user):
        self.calls.append((system, user))
        outcome = self.outcomes.pop(0) if self.outcomes else good_out()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def store(conn, postings):
    return dedup.process_board(conn, postings, "full", ALIASES)


def run(conn, client, **kw):
    return enrich.enrich_postings(conn, make_settings(), REGISTRY, client=client, **kw)


def test_success_writes_all_fit_columns(conn):
    [pid] = store(conn, [make_posting()]).new_ids
    client = StubClient()
    stats = run(conn, client, now="2026-08-21T00:00:00Z")
    assert (stats.calls, stats.enriched) == (1, 1)
    row = db.enrichments_for(conn, [pid])[pid]
    assert row["category"] == "quant"
    assert (row["fit_quant"], row["fit_ai_infra"], row["fit_swe"], row["fit_robotics"]) == (
        92,
        40,
        70,
        15,
    )
    assert row["grad_earliest"] == "2027-12" and row["grad_latest"] == "2028-06"
    assert row["model"] == "stub-model" and row["enriched_at"] == "2026-08-21T00:00:00Z"
    assert json.loads(row["red_flags"]) == []


def test_transient_failure_writes_no_row_and_retries_next_run(conn):
    [pid] = store(conn, [make_posting()]).new_ids
    client = StubClient([RuntimeError("500")])
    stats = run(conn, client)
    assert (stats.calls, stats.deferred, stats.enriched) == (1, 1, 0)
    assert db.enrichments_for(conn, [pid]) == {}
    assert conn.execute("SELECT enrich_attempts FROM postings").fetchone()[0] == 1
    stats2 = run(conn, client)
    assert stats2.enriched == 1 and pid in db.enrichments_for(conn, [pid])


def test_third_failure_writes_null_row(conn):
    [pid] = store(conn, [make_posting()]).new_ids
    client = StubClient([ValueError("a"), enrich.Truncated("b"), ValueError("c")])
    assert run(conn, client).deferred == 1
    assert run(conn, client).deferred == 1
    stats = run(conn, client)
    assert (stats.failed_final, stats.deferred) == (1, 0)
    row = db.enrichments_for(conn, [pid])[pid]
    assert row["category"] is None and json.loads(row["red_flags"]) == ["enrichment_failed"]
    # Exhausted rows leave the queue for good.
    assert run(conn, client).calls == 0


def test_cluster_mate_skipped(conn):
    [a] = store(conn, [make_posting()]).new_ids
    [b] = store(
        conn,
        [make_posting(source="lever", source_ref="hrt", source_key="7", url="https://l/7")],
    ).new_ids
    assert conn.execute("SELECT cluster_id FROM postings WHERE id = ?", (b,)).fetchone()[0] == a
    client = StubClient()
    stats = run(conn, client)
    assert (stats.calls, stats.enriched, stats.skipped_cluster) == (1, 1, 1)
    assert len(db.enrichments_for(conn, [a, b])) == 1
    assert run(conn, client).skipped_cluster == 1


def test_cap_respected(conn):
    store(
        conn,
        [
            make_posting(source_key=str(i), url=f"https://x/{i}", title=f"SWE Intern {i}")
            for i in range(5)
        ],
    )
    client = StubClient()
    stats = run(conn, client, cap=2)
    assert stats.calls == 2 and stats.enriched == 2
    assert len(client.calls) == 2


def test_tier_s_before_tier_2_when_cap_is_one(conn):
    store(conn, [make_posting(company_name="Stripe", source_ref="stripe", source_key="1")])
    store(conn, [make_posting(company_name="Random Startup", source_ref="rs", source_key="1")])
    [hrt] = store(conn, [make_posting()]).new_ids  # newest first_seen_at yet tier S wins
    client = StubClient()
    stats = run(conn, client, cap=1)
    assert stats.enriched == 1
    assert list(db.enrichments_for(conn, [hrt])) == [hrt]
    assert client.calls[0][1].startswith("company: Hudson River Trading")


def test_system_prompt_contains_profile_and_rubric(conn, monkeypatch):
    # Pin the profile: locally config/profile.local.md would otherwise win.
    monkeypatch.setenv("RADAR_PROFILE", "Candidate: Example University ECE undergrad.")
    store(conn, [make_posting(tracker_hints={"sponsorship": "Does not offer"})])
    client = StubClient()
    run(conn, client)
    system, user = client.calls[0]
    assert "Example University ECE" in system
    assert "Use the full range" in system
    assert "{profile}" not in system and "respond with JSON" not in system.lower()
    assert "tracker_sponsorship: Does not offer" in user
    assert "description:\nBuild trading systems." in user


def test_posting_ids_restricts_queue(conn):
    ids = store(
        conn,
        [
            make_posting(source_key=str(i), url=f"https://x/{i}", title=f"Intern {i}")
            for i in range(3)
        ],
    ).new_ids
    client = StubClient()
    stats = run(conn, client, posting_ids=[ids[1]])
    assert stats.calls == 1 and list(db.enrichments_for(conn, ids)) == [ids[1]]


def test_re_enrich_replaces_row(conn):
    [pid] = store(conn, [make_posting()]).new_ids
    run(conn, StubClient())
    new = good_out().model_copy(update={"category": "swe"})
    stats = enrich.re_enrich(conn, make_settings(), REGISTRY, [pid], client=StubClient([new]))
    assert stats.enriched == 1
    assert db.enrichments_for(conn, [pid])[pid]["category"] == "swe"


class TestBuildClient:
    def test_anthropic(self, monkeypatch):
        import anthropic

        seen = {}
        monkeypatch.setattr(anthropic, "Anthropic", lambda api_key: seen.setdefault("key", api_key))
        client = enrich.build_client(
            make_settings(llm_provider="anthropic", anthropic_api_key="ak")
        )
        assert isinstance(client, enrich.AnthropicClient) and seen["key"] == "ak"
        assert client.model_name == "claude-haiku-4-5-20251001"

    def test_openai(self, monkeypatch):
        import openai

        seen = {}
        monkeypatch.setattr(openai, "OpenAI", lambda api_key: seen.setdefault("key", api_key))
        client = enrich.build_client(
            make_settings(llm_provider="openai", openai_api_key="ok", anthropic_api_key="")
        )
        assert isinstance(client, enrich.OpenAIClient) and seen["key"] == "ok"
        assert client.model_name == "gpt-4.1-mini"

    def test_missing_key_raises(self):
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            enrich.build_client(make_settings(llm_provider="openai", openai_api_key=""))


class TestAnthropicParse:
    class FakeMessages:
        def __init__(self, resp):
            self.resp, self.kwargs = resp, None

        def parse(self, **kwargs):
            self.kwargs = kwargs
            return self.resp

    def _client(self, model, resp):
        c = enrich.AnthropicClient.__new__(enrich.AnthropicClient)
        c.model_name = model
        c._client = type("C", (), {"messages": self.FakeMessages(resp)})()
        return c

    def test_temperature_only_for_haiku_45(self):
        resp = type("R", (), {"stop_reason": "end_turn", "parsed_output": good_out()})()
        c = self._client("claude-haiku-4-5-20251001", resp)
        assert c.parse("s", "u") == good_out()
        assert c._client.messages.kwargs["temperature"] == 0
        assert c._client.messages.kwargs["output_format"] is EnrichmentOut
        c2 = self._client("claude-sonnet-4-6", resp)
        c2.parse("s", "u")
        assert "temperature" not in c2._client.messages.kwargs

    def test_max_tokens_raises_truncated(self):
        resp = type("R", (), {"stop_reason": "max_tokens", "parsed_output": None})()
        with pytest.raises(enrich.Truncated):
            self._client("claude-haiku-4-5-20251001", resp).parse("s", "u")


def test_openrouter_provider_builds_openai_compatible_client(monkeypatch):
    import openai

    captured = {}

    class FakeOpenAI:
        def __init__(self, api_key=None, base_url=None):
            captured["base_url"] = base_url

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    s = make_settings(llm_provider="openrouter", openrouter_api_key="or_test")
    client = enrich.build_client(s)
    assert captured["base_url"] == "https://openrouter.ai/api/v1"
    assert client.model_name == s.openrouter_model
