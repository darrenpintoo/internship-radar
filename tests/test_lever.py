"""Lever adapter fixture tests (SPEC §5.3, §10).

Fixture provenance: lever_palantir.json was captured live on 2026-08-20 via
curl GET https://api.lever.co/v0/postings/palantir?mode=json with User-Agent
"internship-radar/0.1 (fixture capture; contact github.com/darrenpintoo)"
(full board: 308 postings), then trimmed to 3 internship postings that carry
non-empty `lists` sections plus descriptionPlain and additionalPlain, so the
description-assembly assertions run against real payload shapes. The live
palantir board had no multi-location postings, so the allLocations join is
covered by a minimal constructed payload below (parse is pure).
"""

import json
from pathlib import Path

from radar.adapters.base import RawItem
from radar.adapters.lever import LeverAdapter

FIXTURES = Path(__file__).parent / "fixtures"


def _parse(payload):
    adapter = LeverAdapter(http=None, companies=[])
    raw = RawItem("lever", "palantir", payload=payload, meta={"company": "Palantir"})
    return adapter.parse(raw)


def _fixture():
    return json.loads((FIXTURES / "lever_palantir.json").read_text())


def test_field_mapping_and_array_payload():
    payload = _fixture()
    assert isinstance(payload, list)  # top-level JSON array, not a wrapper object
    postings = _parse(payload)
    assert len(postings) == 3
    p = postings[0]
    assert p.company_name == "Palantir"
    assert "Internship" in p.title
    assert p.url.startswith("https://jobs.lever.co/palantir/")
    assert p.source == "lever"
    assert p.source_ref == "palantir"
    assert p.location_raw


def test_source_key_is_uuid():
    for p in _parse(_fixture()):
        assert p.source_key
        parts = p.source_key.split("-")
        assert len(parts) == 5 and len(p.source_key) == 36


def test_posted_at_epoch_ms_conversion():
    payload = _fixture()
    postings = _parse(payload)
    # First fixture posting: createdAt 1765492275446 ms = 2025-12-11T22:31:15Z.
    assert payload[0]["createdAt"] == 1765492275446
    assert postings[0].posted_at == "2025-12-11T22:31:15Z"
    for p in postings:
        assert p.posted_at is not None and p.posted_at.endswith("Z")


def test_description_assembles_lists_sections():
    payload = _fixture()
    for job, p in zip(payload, _parse(payload), strict=True):
        assert p.description
        assert p.description.startswith(job["descriptionPlain"][:50])
        for section in job["lists"]:
            assert section["text"] in p.description  # heading kept
        assert "<li>" not in p.description  # lists content HTML-stripped
        # additionalPlain appended after the lists sections.
        assert job["additionalPlain"][:50] in p.description
    # Real eligibility text lives in the lists of the first fixture posting.
    first = _parse(payload)[0]
    assert "Security clearance" in first.description


def test_multi_location_joins_all_locations():
    job = {
        "id": "11111111-2222-3333-4444-555555555555",
        "text": "Software Engineer, Internship",
        "hostedUrl": "https://jobs.lever.co/example/11111111",
        "createdAt": 1755600000000,
        "categories": {
            "location": "New York, NY",
            "allLocations": ["New York, NY", "Seattle, WA", "Denver, CO"],
        },
    }
    postings = _parse([job])
    assert len(postings) == 1  # one Posting per source item, never per location
    assert postings[0].location_raw == "New York, NY; Seattle, WA; Denver, CO"
