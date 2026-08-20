"""iCIMS adapter fixture tests (SPEC §5.9, §10).

Fixture provenance: recorded live 2026-08-20 with curl
(UA "internship-radar/0.1 (fixture capture; contact github.com/darrenpintoo)")
from https://careers.sig.com/api/jobs?page=1&limit=5 (totalCount was 239) and
https://careers.sig.com/api/jobs?page=1&limit=5&keywords=Summer%202027
(totalCount was 47), then trimmed to 3 representative jobs with totalCount
rewritten to 3: req 11007 "Quantitative Research Internship: Summer 2027"
(no `state` key at all, plain-text description), req 11269 (state=None,
HTML description + qualifications), req 11270 (Co-Op 2027, US location).
"""

import json
from pathlib import Path

from radar.adapters.base import RawItem
from radar.adapters.icims import IcimsAdapter

FIXTURES = Path(__file__).parent / "fixtures"


def _parse():
    adapter = IcimsAdapter(http=None, companies=[])
    fixture = json.loads((FIXTURES / "icims_sig.json").read_text())
    raw = RawItem("icims", "careers.sig.com", payload=fixture["jobs"], meta={"company": "SIG"})
    return adapter.parse(raw)


def test_field_mapping():
    postings = _parse()
    assert len(postings) == 3
    by_key = {p.source_key: p for p in postings}
    p = by_key["11007"]
    assert p.company_name == "SIG"
    assert p.title == "Quantitative Research Internship: Summer 2027"
    assert p.url == "https://careers-sig.icims.com/jobs/11007/login"
    assert p.source == "icims"
    assert p.source_ref == "careers.sig.com"
    # No state key at all on this job — location joins only what exists.
    assert p.location_raw == "London, United Kingdom"
    assert p.description and "Susquehanna" in p.description


def test_posted_at_normalized_utc():
    for p in _parse():
        assert p.posted_at is not None
        assert p.posted_at.endswith("Z") and "T" in p.posted_at
    assert {p.source_key: p for p in _parse()}["11007"].posted_at == "2026-07-15T10:38:00Z"


def test_location_skips_null_state():
    p = {p.source_key: p for p in _parse()}["11269"]  # state is null in payload
    assert p.location_raw == "London, United Kingdom"


def test_description_includes_qualifications_html_stripped():
    p = {p.source_key: p for p in _parse()}["11269"]
    assert "<" not in p.description and "&rsquo;" not in p.description
    assert "OTC operations" in p.description  # lives in qualifications


def test_summer_2027_posting_survives():
    assert any("Summer 2027" in p.title for p in _parse())
