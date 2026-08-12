"""Timetable trips attributed onto the track a route relation supplies.

Two things carry this module and both are easy to get wrong in a way that still
produces a plausible national total. Attribution is per *leg*, because matching a
whole calling sequence onto a relation covers 23.9% of GB rail trips where matching
each consecutive pair covers 82.0%. And a leg goes to exactly *one* relation, because
75.8% of GB rail ways carry two or more and adding the trips to each would multiply
the country's timetable by how thoroughly its corridors happen to be mapped.
"""

from __future__ import annotations

from test_osmroutes import stop, way

from wayfare import db, naptan, osm, railtrips

# Three stations 11 km apart on one straight line, in two ways.
A, B, C = (51.0, -1.0), (51.1, -1.0), (51.2, -1.0)


def line_relation(
    relation_id: int = 1, names: tuple[str, ...] = ("Alpha", "Beta", "Gamma")
):
    return osm.Relation(
        relation_id=relation_id,
        route="train",
        name=f"Line {relation_id}",
        ways=(way(10, [A, B]), way(11, [B, C])),
        stops=tuple(
            stop(100 + i, n, lat, lon)
            for i, (n, (lat, lon)) in enumerate(zip(names, (A, B, C), strict=True))
        ),
        tags={"route": "train"},
    )


def station(tiploc: str, name: str, at: tuple[float, float]) -> naptan.Station:
    return naptan.Station(tiploc, name, at[0], at[1], f"9100{tiploc}")


REGISTER = {
    "ALPHA": station("ALPHA", "Alpha", A),
    "BETA": station("BETA", "Beta", B),
    "GAMMA": station("GAMMA", "Gamma", C),
}


# --- building the lines ------------------------------------------------------


def test_a_chaining_relation_becomes_a_line_with_its_stops_projected():
    (found,) = railtrips.lines([line_relation()])
    assert found.relation_id == 1
    assert set(found.along) == {"alpha", "beta", "gamma"}
    # Ordered along the chain, so a leg can be cut between any two of them.
    assert found.along["alpha"][0] < found.along["beta"][0] < found.along["gamma"][0]


def test_a_relation_that_does_not_chain_is_not_a_line():
    broken = osm.Relation(
        relation_id=2,
        route="train",
        name="Broken",
        ways=(way(10, [A, B]), way(11, [(52.0, -1.0), (52.1, -1.0)])),
        stops=(stop(1, "Alpha", *A), stop(2, "Beta", *B)),
        tags={"route": "train"},
    )
    assert railtrips.lines([broken]) == []


# --- attribution -------------------------------------------------------------


def test_a_leg_lands_on_the_ways_between_its_two_stations():
    lines = railtrips.lines([line_relation()])
    trips, got = railtrips.attribute({("ALPHA", "BETA"): 10}, REGISTER, lines)
    assert trips == {10: 10}
    assert (got.legs, got.legs_placed) == (1, 1)
    assert got.trips_placed == 10


def test_a_leg_spanning_both_ways_lands_on_both():
    lines = railtrips.lines([line_relation()])
    trips, _ = railtrips.attribute({("ALPHA", "GAMMA"): 7}, REGISTER, lines)
    assert trips == {10: 7, 11: 7}


def test_a_skip_stop_service_still_places():
    """The whole reason attribution is per leg. Alpha to Gamma calls at no Beta and
    matches no relation as a whole sequence; both of its legs run on drawn track."""
    lines = railtrips.lines([line_relation()])
    trips, got = railtrips.attribute({("ALPHA", "GAMMA"): 3}, REGISTER, lines)
    assert got.legs_placed == 1
    assert trips


def test_every_call_contributes_its_own_leg():
    lines = railtrips.lines([line_relation()])
    trips, got = railtrips.attribute({("ALPHA", "BETA", "GAMMA"): 5}, REGISTER, lines)
    assert got.legs == 2
    assert trips == {10: 5, 11: 5}


def test_trips_are_not_multiplied_across_relations_covering_one_leg():
    """75.8% of GB rail ways carry two or more relations; adding a leg's trips to
    each would scale the national timetable by how well a corridor is mapped."""
    lines = railtrips.lines([line_relation(1), line_relation(2)])
    assert len(lines) == 2
    trips, got = railtrips.attribute({("ALPHA", "BETA"): 10}, REGISTER, lines)
    assert sum(trips.values()) == 10
    assert got.trips_placed == 10


def test_a_leg_no_relation_covers_is_counted_and_dropped():
    """Reported rather than silently absent: an invisible coverage gap reads as a
    quiet railway."""
    lines = railtrips.lines([line_relation()])
    register = REGISTER | {"DELTA": station("DELTA", "Delta", (55.0, -3.0))}
    trips, got = railtrips.attribute({("ALPHA", "DELTA"): 9}, register, lines)
    assert trips == {}
    assert (got.legs, got.legs_placed) == (1, 0)
    assert (got.trips, got.trips_placed) == (9, 0)


def test_an_unresolved_tiploc_drops_its_sequence_and_is_counted():
    """Dropping the call instead would shorten the pattern, which draws a train
    running past a station it stops at."""
    lines = railtrips.lines([line_relation()])
    trips, got = railtrips.attribute({("ALPHA", "NOWHERE"): 4}, REGISTER, lines)
    assert trips == {}
    assert got.tiplocs_unresolved == 1
    assert got.legs == 0


def test_two_stations_sharing_a_name_are_not_paired_across_the_country():
    """A Newport in Wales and a Newport on the Isle of Wight would otherwise
    attribute a leg's trips down a hundred miles of unrelated track."""
    # Two stops a kilometre apart on the ground and 140 km apart along the track,
    # which is what a shared name on opposite sides of a network looks like once
    # both have been projected onto the same chain.
    far = osm.Relation(
        relation_id=3,
        route="train",
        name="Long",
        ways=(way(20, [(51.0, -1.0), (51.0, 0.0), (51.01, -1.0)]),),
        stops=(stop(1, "Alpha", 51.0, -1.0), stop(2, "Alpha", 51.01, -1.0)),
        tags={"route": "train"},
    )
    lines = railtrips.lines([far])
    trips, got = railtrips.attribute({("ALPHA", "ALPHA"): 5}, REGISTER, lines)
    assert got.legs_placed == 0
    assert trips == {}


def test_coverage_is_reported_as_a_share_of_trips():
    lines = railtrips.lines([line_relation()])
    register = REGISTER | {"DELTA": station("DELTA", "Delta", (55.0, -3.0))}
    _, got = railtrips.attribute(
        {("ALPHA", "BETA"): 80, ("ALPHA", "DELTA"): 20}, register, lines
    )
    assert got.trip_coverage == 80.0


# --- the ways a cut runs over ------------------------------------------------


def test_ways_between_names_both_ends_of_a_part_way_cut():
    """A slice starting mid-way still ran over that way; losing it is how a service
    loses the half-way it entered on."""
    chain = osm.chain(line_relation())
    metres = osm.to_metres(chain.points, chain.points[0][0])
    cum = osm.cumulative(metres)
    assert osm.ways_between(chain, cum, 0.0, cum[-1]) == [10, 11]
    assert osm.ways_between(chain, cum, 0.0, cum[1] / 2) == [10]


def test_ways_between_is_ordered_and_free_of_duplicates():
    chain = osm.chain(line_relation())
    cum = osm.cumulative(osm.to_metres(chain.points, chain.points[0][0]))
    out = osm.ways_between(chain, cum, 0.0, cum[-1])
    assert out == sorted(set(out), key=out.index)


# --- storage -----------------------------------------------------------------


def test_write_replaces_rather_than_merges(con):
    """A way that stopped carrying a service must stop being drawn as busy; a stale
    row here is a wrong number rather than a missing one."""
    railtrips.write(con, {10: 5, 11: 6})
    assert railtrips.write(con, {11: 9}) == 1
    rows = con.execute("SELECT way_id, n_trips FROM way_trips").fetchall()
    assert rows == [(11, 9)]


def test_an_empty_attribution_clears_the_table(con):
    railtrips.write(con, {10: 5})
    assert railtrips.write(con, {}) == 0
    assert db.scalar(con, "SELECT count(*) FROM way_trips") == 0
