"""scripts/probe_ats.py tests (SPEC §4.2) — HTTP layer fully mocked, no network.

Payload shapes mirror live responses: the greenhouse 'archer' collision payload
is modeled on the real board fetched 2026-08-20 with the fixture-capture UA
(company_name 'Archer Veterinary Clinic', absolute_url echoing the /archer/
slug — one sanity request, not stored as a fixture file); lever/ashby shapes
follow SPEC §5.3-5.4 as verified 2026-08-20.
"""

import importlib.util
import sys
from pathlib import Path

import yaml

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_ats.py"
_spec = importlib.util.spec_from_file_location("probe_ats", _SCRIPT)
probe_ats = importlib.util.module_from_spec(_spec)
sys.modules["probe_ats"] = probe_ats  # dataclasses needs the module registered
_spec.loader.exec_module(probe_ats)


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeClient:
    """url -> FakeResponse; anything unmapped 404s."""

    def __init__(self, mapping):
        self._mapping = mapping
        self.requested = []

    def get(self, url):
        self.requested.append(url)
        return self._mapping.get(url, FakeResponse(404))


def _prober(mapping):
    return probe_ats.Prober(FakeClient(mapping), throttle=probe_ats.Throttle(0.0))


def _gh_url(t):
    return probe_ats.ENDPOINTS["greenhouse"].format(t=t)


def _lever_url(t):
    return probe_ats.ENDPOINTS["lever"].format(t=t)


def _ashby_url(t):
    return probe_ats.ENDPOINTS["ashby"].format(t=t)


def test_candidate_tokens_include_camelcase():
    cands = probe_ats.candidate_tokens("Machina Labs")
    assert "MachinaLabs" in cands  # lever tokens are case-sensitive (trap 3)
    assert "machinalabs" in cands
    assert "machina-labs" in cands


def test_candidate_tokens_variants():
    cands = probe_ats.candidate_tokens("Radix Trading")
    assert "radix" in cands  # known-word variant: generic 'trading' dropped
    assert "radixtrading" in cands
    assert probe_ats.candidate_tokens("Point72") == ["point72", "Point72"]


def test_populated_200_beats_empty_200():
    # Lever answers 200+[] for the first candidate (trap 1: stale board);
    # ashby has the real populated board for the same token.
    ashby_payload = {
        "apiVersion": "1",
        "jobs": [
            {
                "id": "uuid-1",
                "title": "Software Engineer Intern",
                "jobUrl": "https://jobs.ashbyhq.com/fooquant/uuid-1",
                "location": "New York",
            }
        ],
    }
    prober = _prober(
        {
            _lever_url("fooquant"): FakeResponse(200, []),
            _ashby_url("fooquant"): FakeResponse(200, ashby_payload),
        }
    )
    out = prober.probe_company({"name": "Fooquant"})
    assert out.hit is not None
    assert (out.hit.ats, out.hit.token) == ("ashby", "fooquant")
    assert [(r.ats, r.token) for r in out.watch] == [("lever", "fooquant")]


def test_empty_200_alone_is_watch_not_hit():
    prober = _prober({_lever_url("fooquant"): FakeResponse(200, [])})
    out = prober.probe_company({"name": "Fooquant"})
    assert out.hit is None
    assert any(r.status == "empty" for r in out.watch)


def test_identity_check_rejects_wrong_company_board():
    # Trap 2: greenhouse 'archer' is a veterinary clinic, not Archer Aviation.
    vet_payload = {
        "jobs": [
            {
                "id": 4213164002,
                "title": "DVM Student Externship — Archer Veterinary Clinic",
                "absolute_url": "https://boards.greenhouse.io/archer/jobs/4213164002",
                "company_name": "Archer Veterinary Clinic",
                "location": {"name": "Stanwood, WA"},
            }
        ],
        "meta": {"total": 1},
    }
    prober = _prober({_gh_url("archer"): FakeResponse(200, vet_payload)})
    out = prober.probe_company({"name": "Archer Aviation"})
    assert out.hit is None  # populated 200, but identity failed
    assert [(r.ats, r.token) for r in out.suspect] == [("greenhouse", "archer")]


def test_identity_check_accepts_right_company_board():
    real_payload = {
        "jobs": [
            {
                "id": 1,
                "title": "Flight Software Intern",
                "absolute_url": "https://boards.greenhouse.io/archeraviation/jobs/1",
                "company_name": "Archer Aviation",
            }
        ],
        "meta": {"total": 1},
    }
    prober = _prober({_gh_url("archeraviation"): FakeResponse(200, real_payload)})
    out = prober.probe_company({"name": "Archer Aviation"})
    assert out.hit is not None
    assert (out.hit.ats, out.hit.token) == ("greenhouse", "archeraviation")


def test_careers_page_sniff_fallback():
    # Trap 4: unguessable token, discovered via the careers page HTML.
    html = '<a href="https://jobs.lever.co/wehrtyou-like/apply">Jobs</a>'
    lever_payload = [{"id": "u1", "text": "Intern", "hostedUrl": "x"}]
    prober = _prober(
        {
            "https://example.com/careers": FakeResponse(200, text=html),
            _lever_url("wehrtyou-like"): FakeResponse(200, lever_payload),
        }
    )
    out = prober.probe_company(
        {"name": "Zzq Unguessable", "careers_url": "https://example.com/careers"}
    )
    assert out.hit is not None
    assert (out.hit.ats, out.hit.token) == ("lever", "wehrtyou-like")


MINIMAL_REGISTRY = """\
# Company registry — see SPEC.md §4.
companies:
  - {name: Anthropic,            tier: S, ats: greenhouse, token: anthropic}
  # ---- still probe at M1 ----
  - {name: Point72,              tier: 1, ats: probe}
  - {name: Bridgewater,          tier: 2, ats: probe}
aliases:
  point72 asset management: point72
"""


def test_registry_line_edit(tmp_path):
    path = tmp_path / "registry.yaml"
    path.write_text(MINIMAL_REGISTRY)
    text, ok = probe_ats.edit_registry_line(
        path.read_text(), "Point72", "greenhouse", "point72", "2026-08-20"
    )
    assert ok
    path.write_text(text)

    lines = text.splitlines()
    assert "# Company registry — see SPEC.md §4." in lines[0]  # comments preserved
    assert "# ---- still probe at M1 ----" in text
    (edited,) = [ln for ln in lines if "Point72" in ln]
    assert "ats: greenhouse, token: point72}" in edited
    assert edited.rstrip().endswith("# probed 2026-08-20")
    assert "- {name: Bridgewater,          tier: 2, ats: probe}" in text  # miss untouched

    reg = yaml.safe_load(text)  # still valid YAML with the right fields
    row = next(c for c in reg["companies"] if c["name"] == "Point72")
    assert row["ats"] == "greenhouse" and row["token"] == "point72"
    assert reg["aliases"]["point72 asset management"] == "point72"


def test_registry_line_edit_unmatched_row_reports_failure():
    _, ok = probe_ats.edit_registry_line(MINIMAL_REGISTRY, "Nope", "lever", "nope", "2026-08-20")
    assert not ok
