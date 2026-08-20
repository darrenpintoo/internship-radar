"""Ashby adapter fixture tests (SPEC §5.4, §10).

Fixture provenance: ashby_modal.json recorded 2026-08-20 via
curl GET https://api.ashbyhq.com/posting-api/job-board/modal with
User-Agent "internship-radar/0.1 (fixture capture; contact
github.com/darrenpintoo)". Live board had 31 jobs; trimmed to 3 —
"ML Research Intern" (1 secondary location), "Business Operations
Manager" (empty secondaryLocations), "Customer Engineer" (2 secondary
locations, one without an address).
"""

import json
from pathlib import Path

from radar.adapters.ashby import AshbyAdapter
from radar.adapters.base import RawItem

FIXTURES = Path(__file__).parent / "fixtures"


def _parse():
    adapter = AshbyAdapter(http=None, companies=[])
    payload = json.loads((FIXTURES / "ashby_modal.json").read_text())
    raw = RawItem("ashby", "modal", payload=payload, meta={"company": "Modal"})
    return adapter.parse(raw)


def test_field_mapping():
    postings = _parse()
    assert len(postings) == 3
    p = next(p for p in postings if p.title == "ML Research Intern")
    assert p.company_name == "Modal"
    assert p.source == "ashby"
    assert p.source_ref == "modal"
    assert len(p.source_key) == 36  # UUID id
    assert p.description and "<" not in p.description[:100]


def test_url_is_joburl_not_applyurl():
    for p in _parse():
        assert p.url.startswith("https://jobs.ashbyhq.com/modal/")
        assert not p.url.endswith("/application")


def test_secondary_locations_joined():
    postings = _parse()
    intern = next(p for p in postings if p.title == "ML Research Intern")
    assert intern.location_raw == "New York; San Francisco"
    two_sec = next(p for p in postings if p.title == "Customer Engineer")
    assert two_sec.location_raw == "New York; San Francisco; London or Stockholm"
    no_sec = next(p for p in postings if p.title == "Business Operations Manager")
    assert no_sec.location_raw == "New York"


def test_published_at_normalized_to_utc_z():
    for p in _parse():
        assert p.posted_at is not None
        assert p.posted_at.endswith("Z")
    intern = next(p for p in _parse() if p.title == "ML Research Intern")
    # 2026-07-28T22:02:03.674+00:00 in the fixture.
    assert intern.posted_at == "2026-07-28T22:02:03Z"
