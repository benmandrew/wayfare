"""A route relation standing as a service, rather than as geometry for one.

The gate is the same as `trace`'s -- chains cleanly, names two stops -- so most of
what is checked here is what happens *after* the gate: that the rows land in the
four tables that make `aggregate` and `publish` draw them through the path that
already exists, and that a relation which stops qualifying stops being drawn.
"""

from __future__ import annotations

import pytest

from wayfare import db, osm, osmroutes

FEED = "20260812_test"


def way(way_id: int, points: list[tuple[float, float]]) -> osm.Way:
    return osm.Way(way_id, tuple(points))


def stop(node_id: int, name: str, lat: float, lon: float) -> osm.Stop:
    return osm.Stop(node_id, name, lat, lon)


def relation(
    relation_id: int = 1,
    route: str = "train",
    name: str = "Test Line",
    ways: list[osm.Way] | None = None,
    stops: list[osm.Stop] | None = None,
    tags: dict[str, str] | None = None,
) -> osm.Relation:
    ways = (
        ways
        if ways is not None
        else [
            way(10, [(51.0, -1.0), (51.1, -1.0)]),
            way(11, [(51.1, -1.0), (51.2, -1.0)]),
        ]
    )
    stops = (
        stops
        if stops is not None
        else [
            stop(100, "Alpha Rail Station", 51.0, -1.0),
            stop(101, "Beta Rail Station", 51.2, -1.0),
        ]
    )
    return osm.Relation(
        relation_id=relation_id,
        route=route,
        name=name,
        ways=tuple(ways),
        stops=tuple(stops),
        tags={"route": route, "name": name, **(tags or {})},
    )


# --- the gate ----------------------------------------------------------------


def test_a_chaining_relation_becomes_a_candidate():
    found, broken, no_stops = osmroutes.candidates([relation()])
    assert (broken, no_stops) == (0, 0)
    (c,) = found
    assert c.route_id == "osm:r1"
    assert c.mode == "rail"
    assert c.names == ["alpha", "beta"]
    assert c.way_ids == [10, 11]


def test_a_relation_that_does_not_chain_is_refused():
    """A break draws confident track across a gap no service crosses."""
    broken_ways = [
        way(10, [(51.0, -1.0), (51.1, -1.0)]),
        way(11, [(52.0, -1.0), (52.1, -1.0)]),  # joins at neither end
    ]
    found, broken, _ = osmroutes.candidates([relation(ways=broken_ways)])
    assert found == []
    assert broken == 1


def test_a_relation_naming_fewer_than_two_stops_is_refused():
    one = [stop(100, "Alpha Rail Station", 51.0, -1.0)]
    found, _, no_stops = osmroutes.candidates([relation(stops=one)])
    assert found == []
    assert no_stops == 1


def test_a_mode_with_its_own_timetable_is_not_admitted():
    """Admitting `subway` would draw a second Underground beside the BODS one."""
    found, broken, no_stops = osmroutes.candidates([relation(route="subway")])
    assert (found, broken, no_stops) == ([], 0, 0)


def test_ref_is_preferred_over_the_relation_name():
    (c,) = osmroutes.candidates([relation(tags={"ref": "XC"})])[0]
    assert c.short_name == "XC"


def test_the_relation_name_is_the_fallback():
    (c,) = osmroutes.candidates([relation()])[0]
    assert c.short_name == "Test Line"


def test_the_operator_becomes_the_agency():
    (c,) = osmroutes.candidates([relation(tags={"operator": "ScotRail"})])[0]
    assert c.agency_id == "ScotRail"


# --- what lands in the database ----------------------------------------------


@pytest.fixture
def seeded(con):
    db.set_meta(con, "feed_version", FEED)
    return con


def test_a_pattern_is_written_live_and_without_trips(seeded):
    found, *_ = osmroutes.candidates([relation()])
    assert osmroutes.write(seeded, found) == 1
    row = db.row(
        seeded,
        "SELECT route_id, mode, n_trips, shape_id, last_seen, n_stops FROM patterns",
    )
    assert row == ("osm:r1", "rail", None, None, FEED, 2)


def test_the_geometry_lands_in_traces(seeded):
    found, *_ = osmroutes.candidates([relation()])
    osmroutes.write(seeded, found)
    way_ids, lon = db.row(seeded, "SELECT way_ids, lon_e6 FROM traces")
    assert way_ids == [10, 11]
    assert lon[0] == -1_000_000


def test_a_trace_status_row_stops_trace_redoing_the_work(seeded):
    """Without it these are live, not matchable and shapeless -- `trace`'s
    definition of pending -- so a national Overpass query gets spent re-deriving
    geometry this stage already holds."""
    found, *_ = osmroutes.candidates([relation()])
    osmroutes.write(seeded, found)
    status, relation_id = db.row(seeded, "SELECT status, relation_id FROM trace_status")
    assert status == "ok"
    assert relation_id == 1


def test_the_pattern_id_is_stable_across_runs(seeded):
    found, *_ = osmroutes.candidates([relation()])
    osmroutes.write(seeded, found)
    first = db.scalar(seeded, "SELECT pattern_id FROM patterns")
    osmroutes.write(seeded, found)
    assert db.scalar(seeded, "SELECT count(*) FROM patterns") == 1
    assert db.scalar(seeded, "SELECT pattern_id FROM patterns") == first


def test_a_relation_that_stops_qualifying_stops_being_drawn(seeded):
    """Retired rather than deleted, so its row survives and its geometry does not
    keep appearing in the current feed."""
    osmroutes.write(seeded, osmroutes.candidates([relation(relation_id=1)])[0])
    osmroutes.write(seeded, osmroutes.candidates([relation(relation_id=2)])[0])
    live = seeded.execute(
        "SELECT route_id FROM patterns WHERE last_seen = ?", [FEED]
    ).fetchall()
    assert [r[0] for r in live] == ["osm:r2"]
    assert db.scalar(seeded, "SELECT count(*) FROM patterns") == 2


def test_writing_without_a_feed_version_is_refused(con):
    found, *_ = osmroutes.candidates([relation()])
    with pytest.raises(RuntimeError, match="feed_version"):
        osmroutes.write(con, found)


# --- ways --------------------------------------------------------------------


def test_a_way_shared_by_two_relations_is_stored_once(seeded):
    """The whole reason the table exists: 75.8% of GB rail ways carry two or more
    relations, and drawing per pattern draws each of them once per relation."""
    a = relation(relation_id=1)
    b = relation(relation_id=2, name="Other Line")
    assert osmroutes.write_ways(seeded, [a, b]) == 2
    assert db.scalar(seeded, "SELECT count(*) FROM ways") == 2


def test_a_ways_bbox_is_stored_for_the_window_test(seeded):
    osmroutes.write_ways(seeded, [relation()])
    row = db.row(
        seeded,
        "SELECT min_lon_e6, min_lat_e6, max_lon_e6, max_lat_e6 FROM ways WHERE way_id = 10",
    )
    assert row == (-1_000_000, 51_000_000, -1_000_000, 51_100_000)


def test_ways_are_rebuilt_not_merged(seeded):
    osmroutes.write_ways(seeded, [relation()])
    osmroutes.write_ways(seeded, [relation(ways=[way(99, [(50.0, 0.0), (50.1, 0.0)])])])
    assert [r[0] for r in seeded.execute("SELECT way_id FROM ways").fetchall()] == [99]


def test_a_degenerate_way_is_not_stored(seeded):
    """One point cannot be drawn and cannot be oriented."""
    assert osmroutes.write_ways(seeded, [relation(ways=[way(10, [(51.0, -1.0)])])]) == 0
