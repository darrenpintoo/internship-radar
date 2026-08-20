"""Identity, clustering, repost, closed-detection tests (SPEC §6.3, §10)."""

import pytest

from radar import db, dedup
from radar.models import Posting


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


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


ALIASES = {"hrt": "hudson river trading"}


def store(conn, postings, policy="full"):
    return dedup.process_board(conn, postings, policy, ALIASES)


class TestIdentity:
    def test_new_then_unchanged(self, conn):
        s1 = store(conn, [make_posting()])
        assert len(s1.new_ids) == 1
        s2 = store(conn, [make_posting()])
        assert s2.new_ids == [] and s2.unchanged == 1

    def test_edit_updates_in_place_never_second_row(self, conn):
        s1 = store(conn, [make_posting()])
        [pid] = s1.new_ids
        edited = make_posting(title="SWE Internship (C++/Python) – Summer 2027")
        s2 = store(conn, [edited])
        assert s2.new_ids == [] and s2.updated == 1
        rows = conn.execute("SELECT * FROM postings").fetchall()
        assert len(rows) == 1 and rows[0]["id"] == pid
        assert rows[0]["title"] == edited.title
        assert rows[0]["repost_count"] == 0  # an edit is not a repost

    def test_prefiltered_rows_never_inserted_but_existing_rows_survive(self, conn):
        s1 = store(conn, [make_posting(title="Senior Barista", source_key="9")])
        assert s1.new_ids == [] and s1.dropped_prefilter == 1
        # A stored row whose title would NOW fail the prefilter stays touched.
        store(conn, [make_posting()])
        s3 = store(conn, [make_posting()])
        assert s3.unchanged == 1


class TestRepost:
    def test_reappearance_after_closure_is_a_repost(self, conn):
        s1 = store(conn, [make_posting()])
        [pid] = s1.new_ids
        conn.execute("UPDATE postings SET active = 0, digested_at = 'x' WHERE id = ?", (pid,))
        conn.commit()
        s2 = store(conn, [make_posting()])
        assert s2.repost_ids == [pid]
        row = conn.execute("SELECT * FROM postings WHERE id = ?", (pid,)).fetchone()
        assert row["repost_count"] == 1 and row["active"] == 1
        assert row["digested_at"] is None  # re-enters the next digest with a badge


class TestClustering:
    def test_cross_source_same_hash_clusters(self, conn):
        store(conn, [make_posting()])
        tracker_echo = make_posting(
            source="github_trackers",
            source_ref="simplify",
            source_key="uuid-1",
            url="https://example.com/apply",
            description=None,
        )
        store(conn, [tracker_echo], policy="excludes_only")
        clusters = {
            row["cluster_id"] for row in conn.execute("SELECT cluster_id FROM postings").fetchall()
        }
        assert len(clusters) == 1 and None not in clusters

    def test_fuzzy_same_company_clusters(self, conn):
        store(conn, [make_posting()])
        variant = make_posting(
            source_key="2",
            title="Software Engineering Internship (C++ or Python) - Summer 2027 (NYC)",
        )
        store(conn, [variant])
        rows = conn.execute("SELECT cluster_id FROM postings").fetchall()
        assert rows[0]["cluster_id"] == rows[1]["cluster_id"] is not None

    def test_different_companies_never_cluster(self, conn):
        store(conn, [make_posting()])
        other_firm = make_posting(
            company_name="Citadel Securities", source_ref="citsec", source_key="7"
        )
        store(conn, [other_firm])
        clusters = [row["cluster_id"] for row in conn.execute("SELECT cluster_id FROM postings")]
        assert clusters[0] != clusters[1] or clusters == [None, None]


class TestClosedDetection:
    def _seed_board(self, conn, n):
        postings = [
            make_posting(source_key=str(i), title=f"Quant Intern {i} (Summer 2027)")
            for i in range(n)
        ]
        return store(conn, postings)

    def test_two_strike_closure(self, conn):
        self._seed_board(conn, 2)
        # Snapshot 1 without key '1': first strike only.
        s = dedup.BoardStats()
        dedup.detect_closed(conn, "greenhouse", "wehrtyou", {"0"}, s)
        row = conn.execute("SELECT * FROM postings WHERE source_key = '1'").fetchone()
        assert row["active"] == 1 and row["missing_since"] is not None
        # Snapshot 2 still without it: closed.
        s2 = dedup.BoardStats()
        dedup.detect_closed(conn, "greenhouse", "wehrtyou", {"0"}, s2)
        row = conn.execute("SELECT * FROM postings WHERE source_key = '1'").fetchone()
        assert row["active"] == 0 and s2.closed_ids == [row["id"]]

    def test_reappearance_clears_the_first_strike(self, conn):
        self._seed_board(conn, 2)
        s = dedup.BoardStats()
        dedup.detect_closed(conn, "greenhouse", "wehrtyou", {"0"}, s)
        store(conn, [make_posting(source_key="1", title="Quant Intern 1 (Summer 2027)")])
        row = conn.execute("SELECT * FROM postings WHERE source_key = '1'").fetchone()
        assert row["missing_since"] is None

    def test_empty_snapshot_guard_refuses_mass_closure(self, conn):
        self._seed_board(conn, 6)
        s = dedup.BoardStats()
        dedup.detect_closed(conn, "greenhouse", "wehrtyou", set(), s)
        assert s.closure_skipped is not None
        actives = conn.execute("SELECT COUNT(*) FROM postings WHERE active = 1").fetchone()[0]
        assert actives == 6
