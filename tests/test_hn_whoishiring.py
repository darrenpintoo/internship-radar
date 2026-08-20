"""HN who-is-hiring adapter fixture tests (SPEC §5.6, §10).

Fixtures recorded live 2026-08-20 from hn.algolia.com:
- hn_search.json: search_by_date response trimmed to 3 hits — the Aug 2026
  "Ask HN: Who is hiring?" story (49156683), its same-second "Who wants to be
  hired?" twin (49156682), and the July hiring thread (48747976) — so the
  title-filter + newest selection is exercised against real ties.
- hn_items_49156683.json: the Aug 2026 thread's items response trimmed to 6
  top-level comments (nested replies dropped): 3 genuine intern/co-op mentions
  (SwingVision 49160095, Stellar Science 49163160, Prior Labs 49165353) and 3
  false positives that bare /intern/i would match (49156845 + 49157647
  "internal"/"international" prose, 49185076 "internationally").
"""

import json
from pathlib import Path

from radar.adapters.base import RawItem
from radar.adapters.hn_whoishiring import SEARCH_URL, HnWhoIsHiringAdapter
from radar.http import FixtureHttp

FIXTURES = Path(__file__).parent / "fixtures"

STORY_ID = "49156683"
ITEMS_URL = f"https://hn.algolia.com/api/v1/items/{STORY_ID}"


def _fetch_raw() -> RawItem:
    http = FixtureHttp(
        {
            SEARCH_URL: FIXTURES / "hn_search.json",
            ITEMS_URL: FIXTURES / "hn_items_49156683.json",
        }
    )
    items = HnWhoIsHiringAdapter(http).fetch()
    assert len(items) == 1
    return items[0]


def _parse() -> list:
    adapter = HnWhoIsHiringAdapter(http=None)
    payload = json.loads((FIXTURES / "hn_items_49156683.json").read_text())
    return adapter.parse(RawItem("hn", STORY_ID, payload=payload))


def test_fetch_picks_hiring_thread_not_who_wants_to_be_hired():
    # 49156682 "Who wants to be hired?" ties on created_at; 48747976 is the
    # older July hiring thread. Only the items URL for 49156683 is registered,
    # so a wrong choice raises KeyError inside fetch -> error RawItem.
    raw = _fetch_raw()
    assert raw.error is None
    assert raw.source_ref == STORY_ID
    assert str(raw.payload["id"]) == STORY_ID


def test_strict_intern_regex_excludes_false_positives():
    keys = {p.source_key for p in _parse()}
    assert keys == {"49160095", "49163160", "49165353"}
    # internal/internet/international comments must not leak through.
    assert keys.isdisjoint({"49156845", "49157647", "49185076"})


def test_title_and_company_from_first_line():
    by_key = {p.source_key: p for p in _parse()}
    stellar = by_key["49163160"]
    assert stellar.company_name == "Stellar Science"
    assert stellar.title.startswith("Stellar Science | Hybrid (USA)")
    assert len(stellar.title) <= 120
    prior = by_key["49165353"]
    assert prior.company_name == "Prior Labs"
    # No '|' in SwingVision's first line: company falls back to the full line.
    swing = by_key["49160095"]
    assert swing.company_name.startswith("SwingVision")


def test_posting_field_mapping():
    for p in _parse():
        assert p.source == "hn"
        assert p.source_ref == STORY_ID
        assert p.source_key
        assert p.url == f"https://news.ycombinator.com/item?id={p.source_key}"
        assert p.posted_at is not None and p.posted_at.endswith("Z")
        assert p.description and "<p>" not in p.description
