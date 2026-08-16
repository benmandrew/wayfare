"""The incremental rebuild: what a second feed costs, and what it must not break.

The whole scheme rests on one property -- that a pattern keeps its id when the
feed changes -- so most of these tests are about that id, and about the things
that quietly stop working if it moves.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from builders import FakeClient

from wayfare import aggregate, config, db, gtfs, maintenance, match

FEED_1 = "20260806_022608"
FEED_2 = "20260903_014412"


def _write_feed(gtfs_dir: Path, version: str, trips: str, stop_times: str) -> None:
    """Replace the timetable, keeping stops and routes."""
    (gtfs_dir / "feed_info.txt").write_text(
        "feed_publisher_name,feed_publisher_url,feed_lang,feed_version\n"
        f"BODS,http://x,en,{version}\n"
    )
    (gtfs_dir / "trips.txt").write_text(
        "route_id,service_id,trip_id,direction_id,shape_id\n" + trips
    )
    (gtfs_dir / "stop_times.txt").write_text(
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n" + stop_times
    )


def _drop_the_short_working(gtfs_dir: Path) -> None:
    """Feed 2: T3's two-stop pattern is gone, and a three-stop one appears."""
    _write_feed(
        gtfs_dir,
        FEED_2,
        "R1,WK,T1,0,SH1\nR1,WK,T2,0,SH1\nR1,WK,T4,0,\n",
        "T1,09:00:00,09:00:00,S1,1\n"
        "T1,09:05:00,09:05:00,S2,2\n"
        "T1,09:10:00,09:10:00,S3,3\n"
        "T1,09:15:00,09:15:00,S4,4\n"
        "T2,10:00:00,10:00:00,S1,1\n"
        "T2,10:05:00,10:05:00,S2,2\n"
        "T2,10:10:00,10:10:00,S3,3\n"
        "T2,10:15:00,10:15:00,S4,4\n"
        "T4,11:00:00,11:00:00,S1,1\n"
        "T4,11:05:00,11:05:00,S2,2\n"
        "T4,11:10:00,11:10:00,S3,3\n",
    )


def _ids_by_stop_count(con: duckdb.DuckDBPyConnection) -> dict[int, int]:
    return dict(
        con.execute(
            f"SELECT n_stops, pattern_id FROM patterns p WHERE {db.current_feed()}"
        ).fetchall()
    )


def _build(gtfs_dir: Path, con: duckdb.DuckDBPyConnection, **kw) -> None:
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB", **kw)


# -- identity ---------------------------------------------------------------


def test_pattern_id_is_the_same_under_a_new_feed(gtfs_dir: Path, con):
    """The point of the whole exercise. A journey that has not changed keeps its
    id, so the match result already stored against it still applies."""
    _build(gtfs_dir, con)
    before = _ids_by_stop_count(con)

    _drop_the_short_working(gtfs_dir)
    _build(gtfs_dir, con)
    after = _ids_by_stop_count(con)

    assert before[4] == after[4]
    # ...and a different stop sequence is a different pattern, not a renamed one.
    assert after[3] != before[2]


def test_pattern_id_does_not_move_when_popularity_does(gtfs_dir: Path, con):
    """`pattern_id` is a function of the pattern's identity alone, so it survives a
    feed in which other routes' popularity has changed."""
    _build(gtfs_dir, con)
    before = _ids_by_stop_count(con)

    # T3's short working becomes the busiest pattern; the stop sequences are
    # untouched.
    _write_feed(
        gtfs_dir,
        FEED_2,
        "R1,WK,T1,0,SH1\nR1,WK,T3,0,\nR1,WK,T5,0,\nR1,WK,T6,0,\n",
        "T1,09:00:00,09:00:00,S1,1\n"
        "T1,09:05:00,09:05:00,S2,2\n"
        "T1,09:10:00,09:10:00,S3,3\n"
        "T1,09:15:00,09:15:00,S4,4\n"
        "T3,11:00:00,11:00:00,S1,1\n"
        "T3,11:05:00,11:05:00,S2,2\n"
        "T5,12:00:00,12:00:00,S1,1\n"
        "T5,12:05:00,12:05:00,S2,2\n"
        "T6,13:00:00,13:00:00,S1,1\n"
        "T6,13:05:00,13:05:00,S2,2\n",
    )
    _build(gtfs_dir, con)

    assert _ids_by_stop_count(con) == before


def test_a_colliding_id_is_refused_not_merged(gtfs_dir: Path, con, monkeypatch):
    """Two patterns sharing an id would pool their edges and one would never be
    matched. Loudly wrong beats quietly wrong."""
    monkeypatch.setattr(db, "pattern_id_sql", lambda *_: "1")
    with pytest.raises(RuntimeError, match="collision"):
        _build(gtfs_dir, con)


# -- what the second feed costs ---------------------------------------------


def test_only_new_patterns_are_matched_again(gtfs_dir: Path, con):
    _build(gtfs_dir, con)
    match.run(con, client_=FakeClient())

    _drop_the_short_working(gtfs_dir)
    _build(gtfs_dir, con)

    second = FakeClient()
    tally = match.run(con, client_=second)
    # The four-stop pattern is unchanged and stays matched; only the new
    # three-stop one costs a call.
    assert tally == {"ok": 1}
    assert second.calls == ["stops"]


def test_a_returning_pattern_costs_nothing(gtfs_dir: Path, con):
    """Seasonal services come back. match_status is a cache keyed on identity, so
    when they do they are already matched."""
    _build(gtfs_dir, con)
    match.run(con, client_=FakeClient())

    _drop_the_short_working(gtfs_dir)
    _build(gtfs_dir, con)
    match.run(con, client_=FakeClient())

    # Feed 3 restores the original timetable.
    _write_feed(
        gtfs_dir,
        "20261001_030000",
        "R1,WK,T1,0,SH1\nR1,WK,T2,0,SH1\nR1,WK,T3,0,\n",
        "T1,09:00:00,09:00:00,S1,1\n"
        "T1,09:05:00,09:05:00,S2,2\n"
        "T1,09:10:00,09:10:00,S3,3\n"
        "T1,09:15:00,09:15:00,S4,4\n"
        "T2,10:00:00,10:00:00,S1,1\n"
        "T2,10:05:00,10:05:00,S2,2\n"
        "T2,10:10:00,10:10:00,S3,3\n"
        "T2,10:15:00,10:15:00,S4,4\n"
        "T3,11:00:00,11:00:00,S1,1\n"
        "T3,11:05:00,11:05:00,S2,2\n",
    )
    _build(gtfs_dir, con)

    third = FakeClient()
    assert match.run(con, client_=third) == {}
    assert third.calls == []


def test_departed_patterns_keep_their_edges_but_leave_the_map(gtfs_dir: Path, con):
    _build(gtfs_dir, con)
    match.run(con, client_=FakeClient())
    aggregate.build(con)
    assert con.execute("SELECT count(*) FROM edge_services").fetchone()[0] > 0

    # Feed 2 retires route 42 entirely and introduces 99 over the same stops.
    (gtfs_dir / "routes.txt").write_text(
        "route_id,agency_id,route_short_name,route_long_name,route_type\n"
        "R1,OP1,42,Alpha to Delta,3\n"
        "R2,OP1,99,Alpha to Charlie,3\n"
    )
    _write_feed(
        gtfs_dir,
        FEED_2,
        "R2,WK,T7,0,\n",
        "T7,09:00:00,09:00:00,S1,1\nT7,09:05:00,09:05:00,S2,2\nT7,09:10:00,09:10:00,S3,3\n",
    )
    _build(gtfs_dir, con)
    match.run(con, client_=FakeClient())
    aggregate.build(con)

    services = {
        r[0]
        for r in con.execute("SELECT DISTINCT short_name FROM edge_services").fetchall()
    }
    assert services == {"99"}
    # The retired patterns' work is still on disk, ready if 42 comes back.
    assert con.execute("SELECT count(*) FROM match_status").fetchone()[0] == 3
    assert con.execute("SELECT count(*) FROM pattern_edges").fetchone()[0] == 6

    cov = aggregate.funnel(con)
    assert cov["feed_version"] == FEED_2
    assert cov["patterns_total"] == 1
    assert cov["patterns_departed"] == 2
    assert cov["patterns_pending"] == 0


def test_an_unmatchable_mode_never_counts_as_pending(gtfs_dir: Path, con):
    """`deploy/refresh.sh` refuses to publish while `patterns_pending` is non-zero.
    A ferry will never hold a `match_status` row, so counting it as pending would
    put a floor under that number no drain could lift, and a scheduled region would
    stop publishing for ever -- reporting a drain that never finished."""
    _build(gtfs_dir, con, modes=frozenset({"bus", "ferry"}))
    match.run(con, client_=FakeClient())
    assert match.pending_count(con) == 0

    cov = aggregate.funnel(con)
    assert cov["patterns_pending"] == 0
    assert cov["patterns_pct"] == 100.0
    # ...and the ferry is still visible, so losing one is not silent either.
    assert cov["patterns_by_mode"] == {"bus": 2, "ferry": 1}
    assert cov["modes"] == "bus,ferry"


def test_departed_patterns_do_not_block_pruning(gtfs_dir: Path, con):
    """An unmatched pattern that has left the timetable will never be matched, so
    it must not hold the operator geometry in the file for ever."""
    _build(gtfs_dir, con)
    _drop_the_short_working(gtfs_dir)
    _build(gtfs_dir, con)
    match.run(con, client_=FakeClient())
    # Would raise if departed patterns counted as pending, and the count says the
    # geometry actually went rather than the refusal merely not firing.
    assert maintenance.prune_shapes(con) == 1
    assert db.scalar(con, "SELECT count(*) FROM shapes") == 0


def test_gaining_operator_geometry_is_reported_and_opt_in(gtfs_dir: Path, con):
    _build(gtfs_dir, con)
    match.run(con, client_=FakeClient())
    assert match.pending_count(con) == 0

    # The operator starts emitting TrackPoints for the short working. Same stops,
    # so the same pattern -- but now matchable from real geometry.
    _write_feed(
        gtfs_dir,
        FEED_2,
        "R1,WK,T1,0,SH1\nR1,WK,T2,0,SH1\nR1,WK,T3,0,SH2\n",
        (gtfs_dir / "stop_times.txt").read_text().split("\n", 1)[1],
    )
    _build(gtfs_dir, con)
    assert match.pending_count(con) == 0  # reported only

    _build(gtfs_dir, con, upgrade_shapes=True)
    assert match.pending_count(con) == 1


# -- spreading the work -----------------------------------------------------


def test_a_time_budget_stops_at_a_batch_boundary(gtfs_dir: Path, con, monkeypatch):
    monkeypatch.setattr(config, "CHECKPOINT_EVERY", 1)
    _build(gtfs_dir, con)

    tally = match.run(con, client_=FakeClient(), max_seconds=0.0)
    # One batch of one, then the budget is spent. The rest stays selectable.
    assert sum(tally.values()) == 1
    assert match.pending_count(con) == 1

    match.run(con, client_=FakeClient())
    assert match.pending_count(con) == 0


def test_the_busiest_patterns_are_matched_first(gtfs_dir: Path, con, monkeypatch):
    """A run cut short by a budget must leave a dataset that degrades gracefully."""
    monkeypatch.setattr(config, "CHECKPOINT_EVERY", 1)
    _build(gtfs_dir, con)
    match.run(con, client_=FakeClient(), max_seconds=0.0)

    matched = con.execute(
        "SELECT p.n_stops FROM patterns p JOIN match_status m USING (pattern_id)"
    ).fetchall()
    assert [r[0] for r in matched] == [4]  # ten trips a week, against five


# -- the graph the edge ids belong to ---------------------------------------


def test_a_changed_graph_is_refused(gtfs_dir: Path, con):
    _build(gtfs_dir, con)
    match.run(con, client_=FakeClient(graph="build-a"))
    assert db.get_meta(con, "graph_id") == "build-a"

    _drop_the_short_working(gtfs_dir)
    _build(gtfs_dir, con)
    with pytest.raises(RuntimeError, match="belong to Valhalla graph"):
        match.run(con, client_=FakeClient(graph="build-b"))

    match.run(con, client_=FakeClient(graph="build-b"), force_graph=True)
    assert db.get_meta(con, "graph_id") == "build-b"


def test_an_unreported_graph_does_not_block_the_run(gtfs_dir: Path, con):
    """A guard that cannot tell two builds apart should say so, not refuse."""
    _build(gtfs_dir, con)
    match.run(con, client_=FakeClient(graph=None))
    assert db.get_meta(con, "graph_id") is None
    assert match.pending_count(con) == 0


# -- migrating a database built before any of this --------------------------


# A database as it was written before pattern ids became hashes. `mode` postdates
# the ids becoming hashes, so a database this old has no such column either, and the
# migration has to add all three.
_LEGACY = (
    db.SCHEMA,
    "ALTER TABLE patterns DROP COLUMN first_seen",
    "ALTER TABLE patterns DROP COLUMN last_seen",
    "ALTER TABLE patterns DROP COLUMN mode",
    """
    INSERT INTO patterns VALUES
      (1, 'R1', 'OP1', '42', 0, 'SH1', 4, 10, 1000.0),
      (2, 'R1', 'OP1', '42', 0, NULL,  2,  5,  330.0)
    """,
    """
    INSERT INTO pattern_stops VALUES
      (1, 0, 'S1'), (1, 1, 'S2'), (1, 2, 'S3'), (1, 3, 'S4'),
      (2, 0, 'S1'), (2, 1, 'S2')
    """,
    """
    INSERT INTO match_status VALUES
      (1, 'ok', 'shape', 0.9, 1000.0, 1.0, 2, NULL, NULL),
      (2, 'ok', 'stops', 0.0,  330.0, 1.0, 1, NULL, NULL)
    """,
    "INSERT INTO pattern_edges VALUES (1, 0, 1001), (1, 1, 1002), (2, 0, 1001)",
    f"INSERT INTO meta VALUES ('feed_version', '{FEED_1}')",
)


def test_migration_renumbers_without_losing_match_work(legacy_db):
    """A national match run costs a day or two. The rewrite has to carry it."""
    con = db.connect(legacy_db(*_LEGACY, name="legacy"))
    rows = con.execute(
        "SELECT pattern_id, n_stops, first_seen, last_seen FROM patterns ORDER BY n_stops"
    ).fetchall()
    assert len(rows) == 2
    assert [r[0] for r in rows] != [2, 1]  # renumbered, not left as ranks
    assert all(r[2] == FEED_1 and r[3] == FEED_1 for r in rows)

    # Every table that keyed on the old id followed it.
    for table in ("pattern_stops", "match_status", "pattern_edges"):
        orphans = con.execute(
            f"SELECT count(*) FROM {table} t "
            "WHERE NOT EXISTS (SELECT 1 FROM patterns p WHERE p.pattern_id = t.pattern_id)"
        ).fetchone()[0]
        assert orphans == 0, table
    assert con.execute("SELECT count(*) FROM match_status").fetchone()[0] == 2
    assert con.execute("SELECT count(*) FROM pattern_edges").fetchone()[0] == 3

    # And the ids are the ones the current build would produce.
    short = con.execute("SELECT pattern_id FROM patterns WHERE n_stops = 2").fetchone()[0]
    expected = con.execute(
        "SELECT " + db.pattern_id_sql("'R1'", "0", "'S1>S2'")
    ).fetchone()[0]
    assert short == expected
    con.close()


def test_migration_is_not_repeated(legacy_db):
    path = legacy_db(*_LEGACY, name="legacy")
    con = db.connect(path)
    first = con.execute("SELECT pattern_id FROM patterns ORDER BY n_stops").fetchall()
    con.close()

    con = db.connect(path)
    assert (
        con.execute("SELECT pattern_id FROM patterns ORDER BY n_stops").fetchall() == first
    )
    con.close()
