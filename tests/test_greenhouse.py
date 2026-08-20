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
