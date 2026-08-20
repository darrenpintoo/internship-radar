"""Reddit RSS adapter fixture tests (SPEC §5.7, §10).

Fixture provenance: captured live 2026-08-20 with a single curl GET of
https://www.reddit.com/r/csMajors/new.rss (UA "internship-radar/0.1
(fixture capture; contact github.com/darrenpintoo)"; one request only —
the anonymous throttle is real), then trimmed from 25 to 6 representative
entries: 3 matching the title filter (Citadel internship, HRT Summer 2027,
Google intern), 1 matching via the bare \\bintern prefix ("Interviews"),
and 2 non-matching.
"""

from pathlib import Path

from radar.adapters.base import RawItem
from radar.adapters.reddit_rss import RedditRssAdapter

FIXTURE = Path(__file__).parent / "fixtures" / "reddit_csmajors_new.rss"
FEED_URL = "https://www.reddit.com/r/csMajors/new.rss"


def _parse():
    adapter = RedditRssAdapter(http=None)
    raw = RawItem("reddit", FEED_URL, payload=FIXTURE.read_text())
    return adapter.parse(raw)


def test_title_filter_keeps_and_drops():
    titles = [p.title for p in _parse()]
    assert any("HRT Algorithm Dev Summer 2027" in t for t in titles)  # 2027 arm
    assert any("Citadel Internship" in t for t in titles)  # intern arm
    assert any("Google intern update" in t for t in titles)
    # Non-matching entries are dropped.
    assert not any("How to learn how to build" in t for t in titles)
    assert not any("Young Americans" in t for t in titles)


def test_field_mapping():
    postings = _parse()
    p = next(p for p in postings if "HRT" in p.title)
    assert p.source == "reddit"
    assert p.source_ref == "csMajors"
    assert p.source_key == "t3_1vtpmeo"  # entry id, 't3_xxx'
    assert (
        p.url == "https://www.reddit.com/r/csMajors/comments/1vtpmeo/hrt_algorithm_dev_summer_2027/"
    )
    assert p.company_name == "r/csMajors"
    assert p.posted_at == "2026-08-20T17:15:37Z"  # published, normalized UTC Z
    assert p.description and "<" not in p.description


def test_all_source_keys_nonempty_t3():
    postings = _parse()
    assert postings
    assert all(p.source_key.startswith("t3_") for p in postings)
