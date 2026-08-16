from __future__ import annotations

import logging
from pathlib import Path

import pytest

from wayfare import db, gtfs


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


def test_a_shape_is_one_row_holding_its_points_in_order(gtfs_dir: Path, con):
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
    # One row per shape, so a count of 1 is also what proves the orphaned SH2 --
    # referenced by no trip -- never loaded.
    assert con.execute("SELECT count(*) FROM shapes").fetchone()[0] == 1
    lat_e6, lon_e6 = con.execute(
        "SELECT lat_e6, lon_e6 FROM shapes WHERE shape_id = 'SH1'"
    ).fetchone()
    # shape_pt_sequence orders the list, not the order the rows happen to arrive in.
    assert lon_e6 == [-2245000, -2240000, -2235000, -2230000]
    assert lat_e6 == [53480000] * 4


def test_ids_stay_strings(gtfs_dir: Path, con):
    """A route named "07" must not become 7, or the join to patterns silently
    loses every service whose number has a leading zero.

    Every id read out of the feed, not just the one the public sees: a stop id is
    what `pattern_stops` joins on and what the identity hash is built from, so a
    numeric-looking one that loses its zeros takes the stop chain with it."""
    (gtfs_dir / "routes.txt").write_text(
        "route_id,agency_id,route_short_name,route_long_name,route_type\n"
        "R1,OP1,07,Alpha to Delta,3\n"
    )
    stops = (gtfs_dir / "stops.txt").read_text()
    (gtfs_dir / "stops.txt").write_text(stops + "007,Echo,53.4800,-2.2250\n")
    times = (gtfs_dir / "stop_times.txt").read_text()
    (gtfs_dir / "stop_times.txt").write_text(times + "T3,11:10:00,11:10:00,007,3\n")

    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")

    assert con.execute("SELECT DISTINCT short_name FROM patterns").fetchone()[0] == "07"
    assert con.execute("SELECT stop_id FROM stops WHERE name = 'Echo'").fetchone() == (
        "007",
    )
    pid = con.execute("SELECT pattern_id FROM patterns WHERE n_stops = 3").fetchone()[0]
    ordered = con.execute(
        "SELECT stop_id FROM pattern_stops WHERE pattern_id = ? ORDER BY seq", [pid]
    ).fetchall()
    assert [s[0] for s in ordered] == ["S1", "S2", "007"]


def test_rebuild_is_idempotent(gtfs_dir: Path, con):
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
    assert con.execute("SELECT count(*) FROM patterns").fetchone()[0] == 2
    assert con.execute("SELECT count(*) FROM pattern_stops").fetchone()[0] == 6


# --- Modes -------------------------------------------------------------------


def _timetable(gtfs_dir: Path, routes: str, trips: str, stop_times: str) -> None:
    """Replace the routes and the timetable, keeping the stops."""
    (gtfs_dir / "routes.txt").write_text(
        "route_id,agency_id,route_short_name,route_long_name,route_type\n" + routes
    )
    (gtfs_dir / "trips.txt").write_text(
        "route_id,service_id,trip_id,direction_id,shape_id\n" + trips
    )
    (gtfs_dir / "stop_times.txt").write_text(
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n" + stop_times
    )


def test_ferries_and_trains_never_become_patterns(gtfs_dir: Path, con):
    """The mini feed carries a sea crossing and a train. Neither has a road under
    it, and asking Valhalla to snap one produced the largest single class of error
    in the GB run."""
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
    assert {
        r[0] for r in con.execute("SELECT DISTINCT route_id FROM patterns").fetchall()
    } == {"R1"}


def test_coach_routes_are_kept(gtfs_dir: Path, con):
    """route_type 200 is the extended code for coach, and the GB feed's 316 of them
    are National Express and FlixBus -- long-distance buses on ordinary roads. A
    filter written as `route_type = '3'` deletes the lot, which is the whole reason
    the kept set is a range rather than a value."""
    _timetable(
        gtfs_dir,
        "C1,OP1,X10,Alpha to Charlie,200\nF1,OP1,FERRY,Pier to Island,4\n",
        "C1,WK,TC1,0,\nF1,WK,TF1,0,\n",
        "TC1,08:00:00,08:00:00,S1,1\n"
        "TC1,08:20:00,08:20:00,S3,2\n"
        "TF1,12:00:00,12:00:00,P1,1\n"
        "TF1,12:30:00,12:30:00,P2,2\n",
    )
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
    assert [r[0] for r in con.execute("SELECT short_name FROM patterns").fetchall()] == [
        "X10"
    ]


def test_trolleybus_is_kept_under_both_of_its_codes(gtfs_dir: Path, con):
    """11 is the basic code and 800 the extended one, and they mean the same road
    vehicle. Nothing in GB publishes either, which is exactly why it is pinned."""
    _timetable(
        gtfs_dir,
        "T1,OP1,TB1,Alpha to Bravo,11\nT2,OP1,TB2,Alpha to Charlie,800\n",
        "T1,WK,TT1,0,\nT2,WK,TT2,0,\n",
        "TT1,08:00:00,08:00:00,S1,1\n"
        "TT1,08:10:00,08:10:00,S2,2\n"
        "TT2,09:00:00,09:00:00,S1,1\n"
        "TT2,09:10:00,09:10:00,S3,2\n",
    )
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
    assert {r[0] for r in con.execute("SELECT short_name FROM patterns").fetchall()} == {
        "TB1",
        "TB2",
    }


def test_dropped_modes_are_reported_by_type_and_trip_count(gtfs_dir: Path, con, caplog):
    """Silent truncation is the failure this pipeline keeps getting bitten by, so a
    mode leaving the feed has to be visible in the run's own log."""
    with caplog.at_level(logging.INFO, logger="wayfare.gtfs"):
        gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
    lines = [r.getMessage() for r in caplog.records]
    assert "dropping route_type 4 (ferry): 1 routes, 1 trips" in lines
    assert "dropping route_type 2 (rail): 1 routes, 1 trips" in lines
    assert "3 trips on the selected modes (bus, coach), 2 on other modes dropped" in lines


def test_kept_modes_are_reported_too(gtfs_dir: Path, con, caplog):
    """The complement of the dropping lines. Reporting only what went means a feed
    that quietly stops publishing a mode reads the same as one that never had it,
    and now that keeping a mode is a choice, what arrived is the thing to check."""
    with caplog.at_level(logging.INFO, logger="wayfare.gtfs"):
        gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
    assert "keeping bus: 1 routes, 3 trips" in [r.getMessage() for r in caplog.records]


def test_an_unrecognised_mode_is_a_warning_not_a_quiet_omission(
    gtfs_dir: Path, con, caplog
):
    """A future feed publishing something road-going in a range nobody thought to
    keep is how this filter goes wrong, so an unknown type is louder than a known
    one rather than quieter."""
    _timetable(
        gtfs_dir,
        "R1,OP1,42,Alpha to Delta,3\nZ1,OP1,ZZ,Alpha to Bravo,1200\n",
        "R1,WK,T1,0,\nZ1,WK,TZ1,0,\n",
        "T1,09:00:00,09:00:00,S1,1\n"
        "T1,09:05:00,09:05:00,S2,2\n"
        "TZ1,12:00:00,12:00:00,P1,1\n"
        "TZ1,12:30:00,12:30:00,P2,2\n",
    )
    with caplog.at_level(logging.INFO, logger="wayfare.gtfs"):
        gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
    warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert "dropping route_type 1200 (unrecognised): 1 routes, 1 trips" in warned


def test_a_feed_that_drops_to_nothing_is_refused(gtfs_dir: Path, con):
    """Every trip dropping means the join to routes.txt failed, not that the
    timetable is all water. A run that produces no patterns at all must say so."""
    _timetable(
        gtfs_dir,
        "F1,OP1,FERRY,Pier to Island,4\n",
        "F1,WK,TF1,0,\n",
        "TF1,12:00:00,12:00:00,P1,1\nTF1,12:30:00,12:30:00,P2,2\n",
    )
    with pytest.raises(RuntimeError, match="unselected mode"):
        gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")


def test_route_type_is_stored_as_text(gtfs_dir: Path, con):
    """It is kept on `routes` so a later run can report on a mode it dropped, and
    as text like every other column read out of the feed."""
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
    assert con.execute(
        "SELECT route_type FROM routes WHERE route_id = 'F1'"
    ).fetchone() == ("4",)


# --- Selecting modes ----------------------------------------------------------


def test_the_default_selection_is_road_only(gtfs_dir: Path, con):
    """Adding modes must not change what an existing run produces. The mini feed
    carries a ferry and a train, and neither becomes a pattern unless asked for."""
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
    assert {r[0] for r in con.execute("SELECT DISTINCT mode FROM patterns").fetchall()} == {
        "bus"
    }


def test_a_selected_mode_becomes_a_pattern_and_says_which_mode(gtfs_dir: Path, con):
    """The whole point of the change: a ferry kept for its operator geometry is a
    pattern like any other, and carries the mode that decides how it gets drawn."""
    gtfs.build_patterns(
        gtfs_dir, con, memory_limit="1GB", modes=frozenset({"bus", "ferry"})
    )
    assert dict(
        con.execute("SELECT mode, count(*) FROM patterns GROUP BY 1").fetchall()
    ) == {"bus": 2, "ferry": 1}


def test_selecting_a_mode_does_not_move_any_other_pattern_id(gtfs_dir: Path, con):
    """`pattern_id` is route, direction and stops, and mode is deliberately not in
    it. Were it in the hash, turning a mode on would renumber every pattern already
    matched and throw away a national match run."""
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
    road = {r[0] for r in con.execute("SELECT pattern_id FROM patterns").fetchall()}
    gtfs.build_patterns(
        gtfs_dir, con, memory_limit="1GB", modes=frozenset({"bus", "ferry", "rail"})
    )
    after = {r[0] for r in con.execute("SELECT pattern_id FROM patterns").fetchall()}
    assert road < after


def test_the_selection_is_remembered_across_a_bare_rebuild(gtfs_dir: Path, con):
    """`deploy/refresh.sh` runs `wayfare patterns` with no flags, weekly, with
    nobody watching. Were the default a constant rather than what this database was
    last built with, the first refresh after a multi-modal build would drop every
    ferry -- and report a healthy run while doing it."""
    gtfs.build_patterns(
        gtfs_dir, con, memory_limit="1GB", modes=frozenset({"bus", "ferry"})
    )
    assert db.get_meta(con, "modes") == "bus,ferry"

    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
    assert dict(
        con.execute("SELECT mode, count(*) FROM patterns GROUP BY 1").fetchall()
    ) == {"bus": 2, "ferry": 1}


def test_an_explicit_selection_still_narrows_a_remembered_one(gtfs_dir: Path, con):
    """Inheriting must not become a one-way door: going back to road only is a
    thing a person does deliberately, and `--modes` is how they say so."""
    gtfs.build_patterns(
        gtfs_dir, con, memory_limit="1GB", modes=frozenset({"bus", "ferry"})
    )
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB", modes=frozenset({"bus"}))
    assert {r[0] for r in con.execute("SELECT DISTINCT mode FROM patterns").fetchall()} == {
        "bus"
    }
    assert db.get_meta(con, "modes") == "bus"


def test_an_unknown_mode_name_is_refused(gtfs_dir: Path, con):
    """A typo in --modes would otherwise read as a feed that happens to carry no
    trams, which is the quiet-wrong-answer failure this codebase keeps hitting."""
    with pytest.raises(ValueError, match="unknown mode"):
        gtfs.build_patterns(
            gtfs_dir, con, memory_limit="1GB", modes=frozenset({"bus", "tramm"})
        )
