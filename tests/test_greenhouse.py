"""Greenhouse adapter fixture tests (SPEC §5.2, §10) — recorded 2026-08-20."""

import json
from pathlib import Path

from radar.adapters.base import RawItem
from radar.adapters.greenhouse import GreenhouseAdapter

FIXTURES = Path(__file__).parent / "fixtures"


def _parse(fixture: str, company: str, token: str):
    adapter = GreenhouseAdapter(http=None, companies=[])
    payload = json.loads((FIXTURES / fixture).read_text())
    raw = RawItem("greenhouse", token, payload=payload, meta={"company": company})
    return adapter.parse(raw)


def test_field_mapping_hrt():
    postings = _parse("greenhouse_wehrtyou.json", "Hudson River Trading", "wehrtyou")
    assert len(postings) == 3
    p = postings[0]
    assert p.company_name == "Hudson River Trading"
    assert "Summer 2027" in p.title
    assert p.url.startswith("http")
    assert p.source == "greenhouse"
    assert p.source_ref == "wehrtyou"
    assert p.source_key.isdigit()  # int id cast to str
    # posted_at comes from first_published, normalized to UTC Z.
    assert p.posted_at is not None and p.posted_at.endswith("Z")
    assert p.location_raw


def test_description_is_unescaped_plain_text():
    postings = _parse("greenhouse_wehrtyou.json", "Hudson River Trading", "wehrtyou")
    described = [p for p in postings if p.description]
    assert described, "content=true fixture should carry descriptions"
    for p in described:
        assert "<div" not in p.description
        assert "&lt;" not in p.description


def test_parse_anthropic_board():
    postings = _parse("greenhouse_anthropic.json", "Anthropic", "anthropic")
    assert len(postings) == 3
    assert all(p.company_name == "Anthropic" for p in postings)


def test_jane_street_employment_type_folded_into_title():
    """Jane Street strips 'Intern' from public titles; the campus flag lives in
    the Employment Type metadata (fixture recorded 2026-08-20 — 50 campus roles
    live on the board that way). The adapter must fold it into the title so the
    prefilter and digest see it."""
    from radar.normalize import keep

    postings = _parse("greenhouse_janestreet.json", "Jane Street", "janestreet")
    by_title = {p.title: p for p in postings}
    assert "Sales and Trading — Summer Internship" in by_title
    assert "Cybersecurity Analyst — Summer Internship" in by_title
    # Non-intern roles keep their titles untouched...
    assert "Accounting Coordinator" in by_title
    # ...and the folded titles now survive the 'full' prefilter while the
    # experienced role still drops.
    assert keep("Sales and Trading — Summer Internship", "full") is True
    assert keep("Accounting Coordinator", "full") is False
