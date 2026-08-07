from __future__ import annotations

from pathlib import Path

from wayfare import acquire, gtfs


def test_trips_collapse_to_patterns(gtfs_dir: Path, con):
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")

    rows = con.execute(
        "SELECT pattern_id, short_name, direction, shape_id, n_stops, n_trips "
        "FROM patterns ORDER BY n_stops DESC"
    ).fetchall()

    # T1 and T2 share a stop sequence; T3 turns short. Two patterns, not three.
    assert len(rows) == 2
    full, short = rows

    assert full[1] == "42"
    assert full[3] == "SH1"
    assert full[4] == 4
    # Two trips, five weekdays each: the weight is service per week, not row count.
    assert full[5] == 10

    assert short[4] == 2
    assert short[5] == 5
    assert short[3] is None  # T3 carries no operator geometry


def test_pattern_stops_are_ordered(gtfs_dir: Path, con):
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
    pid = con.execute("SELECT pattern_id FROM patterns WHERE n_stops = 4").fetchone()[0]
    stops = con.execute(
        "SELECT stop_id FROM pattern_stops WHERE pattern_id = ? ORDER BY seq", [pid]
    ).fetchall()
    assert [s[0] for s in stops] == ["S1", "S2", "S3", "S4"]
    # seq is zero-based, so it indexes the stop list directly.
    assert (
        con.execute(
            "SELECT min(seq) FROM pattern_stops WHERE pattern_id = ?", [pid]
        ).fetchone()[0]
        == 0
    )


def test_span_is_measured_in_metres(gtfs_dir: Path, con):
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
    span = con.execute("SELECT span_m FROM patterns WHERE n_stops = 4").fetchone()[0]
    # Three hops of 0.005 degrees of longitude at 53.48N: about 331 m each.
    assert 900 < span < 1050


def test_orphaned_shapes_are_not_loaded(gtfs_dir: Path, con):
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
    rows = con.execute("SELECT DISTINCT shape_id FROM shapes").fetchall()
    assert {r[0] for r in rows} == {"SH1"}


def test_a_shape_is_one_row_holding_its_points_in_order(gtfs_dir: Path, con):
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
    assert con.execute("SELECT count(*) FROM shapes").fetchone()[0] == 1
    lat_e6, lon_e6 = con.execute(
        "SELECT lat_e6, lon_e6 FROM shapes WHERE shape_id = 'SH1'"
    ).fetchone()
    # shape_pt_sequence orders the list, not the order the rows happen to arrive in.
    assert lon_e6 == [-2245000, -2240000, -2235000, -2230000]
    assert lat_e6 == [53480000] * 4


def test_ids_stay_strings(gtfs_dir: Path, con):
    """A route named "07" must not become 7, or the join to patterns silently
    loses every service whose number has a leading zero."""
    (gtfs_dir / "routes.txt").write_text(
        "route_id,agency_id,route_short_name,route_long_name,route_type\n"
        "R1,OP1,07,Alpha to Delta,3\n"
    )
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
    assert con.execute("SELECT DISTINCT short_name FROM patterns").fetchone()[0] == "07"


def test_rebuild_is_idempotent(gtfs_dir: Path, con):
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
    assert con.execute("SELECT count(*) FROM patterns").fetchone()[0] == 2
    assert con.execute("SELECT count(*) FROM pattern_stops").fetchone()[0] == 6


def test_unpack_and_feed_version(gtfs_zip: Path, tmp_path: Path):
    out = acquire.unpack_gtfs(gtfs_zip, tmp_path / "unpacked")
    assert (out / "stop_times.txt").exists()
    assert acquire.feed_version(out) == "20260806_022608"
