"""The British Isles boundary, and the routes it drops.

BODS carries international coach, so a national feed holds correct coordinates for
stops in Warsaw. Everything here is about the boundary being wide enough to keep the
awkward corners of these islands and tight enough to lose the near side of France --
those are separate failures, and a rule that only ever gets tested against Warsaw
would pass while quietly dropping Kent.
"""

from __future__ import annotations

import logging

import pytest

from wayfare import config, db, gtfs, osmroutes, trace

# Real places, at their real coordinates, chosen for being the hard cases rather
# than the obvious ones. The Channel pairs are what a single straight line cannot
# separate: Calais is east of Dover, and Brittany is west of Cornwall.
INSIDE = [
    ("Dover", 51.13, 1.34),
    ("Lowestoft Ness", 52.481, 1.763),
    ("North Foreland", 51.375, 1.446),
    ("Lizard Point", 49.96, -5.20),
    ("Bishop Rock", 49.873, -6.327),
    ("Out Stack, Shetland", 60.858, -0.766),
    ("Tearaght Island", 52.075, -10.665),
    ("Mizen Head", 51.451, -9.819),
]

OUTSIDE = [
    ("Calais", 50.951, 1.856),
    ("Boulogne", 50.725, 1.614),
    ("Dieppe", 49.923, 1.078),
    ("Cherbourg", 49.639, -1.616),
    ("Roscoff", 48.726, -3.985),
    ("Brest", 48.390, -4.486),
    ("Ostend", 51.231, 2.918),
    ("Rotterdam", 51.924, 4.467),
    ("Amsterdam", 52.379, 4.902),
    ("Hamburg", 53.552, 10.012),
    ("Warsaw", 52.218, 20.964),
    ("Bergen", 60.393, 5.325),
    ("Tórshavn", 62.008, -6.771),
]


@pytest.mark.parametrize(("name", "lat", "lon"), INSIDE)
def test_the_awkward_corners_are_kept(name: str, lat: float, lon: float):
    assert config.in_british_isles(lat, lon), name


@pytest.mark.parametrize(("name", "lat", "lon"), OUTSIDE)
def test_the_continent_is_dropped(name: str, lat: float, lon: float):
    assert not config.in_british_isles(lat, lon), name


@pytest.mark.parametrize(("name", "lat", "lon"), INSIDE + OUTSIDE)
def test_sql_and_python_agree(con, name: str, lat: float, lon: float):
    """Two implementations of one boundary, so they are checked against each other.

    `patterns` drops routes in SQL and `art` and the CLI test points in Python. A
    drift between them would show up as a bounding box that disagrees with the rows
    inside it, which is not a shape any error message would describe.
    """
    sql = config.british_isles_sql("?::DOUBLE", "?::DOUBLE")
    assert db.scalar(con, f"SELECT {sql}", [lat, lon, lat]) is config.in_british_isles(
        lat, lon
    ), name


def test_margins_are_wider_than_the_error_in_a_stop_position():
    """Neither side of the boundary is within a rounding error of it.

    A boundary that a real stop sits 100 m from is one that a corrected coordinate
    crosses, so the check is that it does not nearly touch. The tightest pair in the
    August 2026 national feed is Kent against Calais.
    """
    limit = lambda lat: min(  # noqa: E731
        config.ISLES_LON_CAP,
        config.ISLES_CHANNEL_LON + config.ISLES_CHANNEL_SLOPE * (lat - 50.0),
    )
    # Deal, the closest British stop to the line, and Calais Eurotunnel, the closest
    # continental one. Roughly 15 km of clearance each, in degrees of longitude.
    assert limit(51.187) - 1.403 > 0.15
    assert 1.812 - limit(50.935) > 0.15
    # Bishop Rock against Alderney, across the southern bound.
    assert 49.873 - config.ISLES_LAT_MIN > 0.05
    assert config.ISLES_LAT_MIN - 49.717 > 0.05


CONTINENTAL = {
    "stops.txt": (
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "S1,Alpha,53.4800,-2.2450\n"
        "S2,Bravo,53.4800,-2.2400\n"
        "V1,Victoria Coach Station,51.4930,-0.1480\n"
        "V2,Dover,51.1300,1.3400\n"
        "V3,Calais,50.9510,1.8560\n"
    ),
    "routes.txt": (
        "route_id,agency_id,route_short_name,route_long_name,route_type\n"
        "R1,OP1,42,Alpha to Bravo,3\n"
        "N1,OP1,N700,London to Calais,3\n"
    ),
    "trips.txt": (
        "route_id,service_id,trip_id,direction_id,shape_id\nR1,WK,T1,0,\nN1,WK,TN1,0,\n"
    ),
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T1,09:00:00,09:00:00,S1,1\n"
        "T1,09:05:00,09:05:00,S2,2\n"
        "TN1,20:00:00,20:00:00,V1,1\n"
        "TN1,22:00:00,22:00:00,V2,2\n"
        "TN1,23:30:00,23:30:00,V3,3\n"
    ),
}


@pytest.fixture
def coach_to_calais(gtfs_dir):
    for name, body in CONTINENTAL.items():
        (gtfs_dir / name).write_text(body)
    return gtfs_dir


def test_a_route_leaving_the_isles_is_dropped_whole(coach_to_calais, con, caplog):
    """The London legs go with the Calais one, deliberately.

    Keeping them would leave a pattern whose stop sequence ends at Dover and whose
    name still says Calais, which reads as a domestic service to everything
    downstream -- the span, the detour check, and every bounding box.
    """
    with caplog.at_level(logging.INFO, logger="wayfare.gtfs"):
        gtfs.build_patterns(coach_to_calais, con, memory_limit="1GB")

    names = [r[0] for r in con.execute("SELECT short_name FROM patterns").fetchall()]
    assert names == ["42"]
    assert not con.execute(
        "SELECT 1 FROM pattern_stops WHERE stop_id IN ('V1', 'V2', 'V3')"
    ).fetchall()
    assert "1 patterns dropped for calling outside" in caplog.text


def test_a_route_stored_before_the_rule_existed_is_retired(
    coach_to_calais, con, caplog, monkeypatch
):
    """The database this rule was added to, which is the case the drop alone misses.

    A pattern normally leaves by `last_seen` falling behind, and re-running against
    the feed version already on disk stamps the same value it is holding. So the
    stored row survives the drop while `pattern_stops`, which is rebuilt outright,
    does not -- leaving a live pattern with no stops. Retiring the stored row is the
    only thing that clears one, since nothing else in the build will touch it again.
    """
    monkeypatch.setattr(gtfs, "_drop_routes_off_the_isles", lambda con: None)
    gtfs.build_patterns(coach_to_calais, con, memory_limit="1GB")
    assert db.scalar(con, "SELECT count(*) FROM patterns") == 2

    monkeypatch.undo()
    with caplog.at_level(logging.INFO, logger="wayfare.gtfs"):
        gtfs.build_patterns(coach_to_calais, con, memory_limit="1GB")

    assert db.scalar(con, "SELECT count(*) FROM patterns") == 1
    assert "1 of them were stored by an earlier build" in caplog.text
    # The shape the bug takes, checked directly rather than through the count.
    assert not con.execute("""
        SELECT 1 FROM patterns p
        WHERE NOT EXISTS (SELECT 1 FROM pattern_stops ps WHERE ps.pattern_id = p.pattern_id)
    """).fetchall()


def test_the_dropped_route_departs_rather_than_vanishing(coach_to_calais, con):
    """`stops` keeps Calais; only the pattern goes.

    The stop table is a lookup, not a record of what is drawn, and `INSERT OR
    REPLACE` on it is what makes a re-run cheap. Dropping rows from it would make
    the rule expensive to loosen and would break nothing if it were wrong.
    """
    gtfs.build_patterns(coach_to_calais, con, memory_limit="1GB")
    assert db.scalar(con, "SELECT count(*) FROM stops WHERE stop_id = 'V3'") == 1


def test_bboxes_ignore_a_continental_stop(coach_to_calais, con):
    """Both windows, because both are built the same way.

    `trace`'s has never met a continental stop -- it spans the pending non-road
    patterns, which are urban rail -- but nothing in its construction stops it, and
    the one that did meet one asked Overpass for every railway to Poland.

    Set up by hand rather than through `patterns`, because `patterns` now drops the
    route: this is the database an older build left behind, which is the case the
    clip in the two `bbox` functions exists for.
    """
    gtfs.build_patterns(coach_to_calais, con, memory_limit="1GB")
    con.execute("INSERT OR REPLACE INTO stops VALUES ('W1', 'Warsaw', 52.218, 20.964)")
    con.execute("""
        INSERT INTO patterns (pattern_id, route_id, short_name, direction, n_stops,
                              n_trips, span_m, mode, first_seen, last_seen)
        SELECT 999, 'N1', 'N700', 0, 2, 5, 0.0, 'rail', value, value
        FROM meta WHERE key = 'feed_version'
    """)
    con.execute("INSERT INTO pattern_stops VALUES (999, 0, 'S1'), (999, 1, 'W1')")

    for box in (osmroutes.bbox(con), trace.bbox(con)):
        assert box is not None
        assert box[3] < config.ISLES_LON_CAP + 0.1, box
