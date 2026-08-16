"""A route relation standing as a service, rather than as geometry for one.

The gate is the same as `trace`'s -- chains cleanly, names two stops -- so most of
what is checked here is what happens *after* the gate: that the rows land in the
four tables that make `aggregate` and `publish` draw them through the path that
already exists, and that a relation which stops qualifying stops being drawn.
"""

from __future__ import annotations

import pytest
from builders import broken_relation, relation, stop, way

from wayfare import db, osmroutes

FEED = "20260812_test"


# --- the gate ----------------------------------------------------------------


def test_a_chaining_relation_becomes_a_candidate():
    sifted = osmroutes.candidates([relation()])
    assert (sifted.broken, sifted.no_stops, sifted.not_ours, sifted.no_ways) == (0, 0, 0, 0)
    (c,) = sifted.kept
    assert c.route_id == "osm:r1"
    assert c.mode == "rail"
    assert c.names == ["alpha", "beta"]
    assert c.way_ids == [10, 11]


def test_a_relation_that_does_not_chain_is_refused():
    """A break draws confident track across a gap no service crosses."""
    sifted = osmroutes.candidates([broken_relation()])
    assert sifted.kept == []
    assert sifted.broken == 1


def test_a_relation_naming_fewer_than_two_stops_is_refused():
    one = [stop(100, "Alpha Rail Station", 51.0, -1.0)]
    sifted = osmroutes.candidates([relation(stops=one)])
    assert sifted.kept == []
    assert sifted.no_stops == 1


def test_a_mode_with_its_own_timetable_is_not_admitted():
    """Admitting `subway` would draw a second Underground beside the BODS one."""
    sifted = osmroutes.candidates([relation(route="subway")])
    assert (sifted.kept, sifted.broken, sifted.no_stops) == ([], 0, 0)
    assert sifted.considered == 0


def test_a_relation_with_no_ways_is_counted_rather_than_dropped_quietly():
    """`chained` is the considered count less the refusals, so a relation that leaves
    the funnel uncounted is reported as one whose ways formed an unbroken path -- and
    a relation with no ways has no path at all."""
    sifted = osmroutes.candidates([relation(ways=[])])
    assert (sifted.kept, sifted.no_ways, sifted.considered) == ([], 1, 1)
    assert sifted.chained == 0


def test_every_relation_of_a_drawn_route_lands_in_exactly_one_count():
    """The funnel adds up, which is the only thing making `chained` a subtraction."""
    sifted = osmroutes.candidates(
        [
            relation(relation_id=1),
            relation(relation_id=2, ways=[]),
            broken_relation(relation_id=3),
            relation(relation_id=4, stops=[stop(100, "Alpha Rail Station", 51.0, -1.0)]),
            relation(relation_id=5, tags={"operator": "Iarnród Éireann"}),
            relation(relation_id=6, route="subway"),
        ]
    )
    refused = sifted.broken + sifted.no_stops + sifted.not_ours + sifted.no_ways
    assert sifted.considered == 5
    assert len(sifted.kept) + refused == sifted.considered
    assert sifted.chained == 2


def test_ref_is_preferred_over_the_relation_name():
    (c,) = osmroutes.candidates([relation(tags={"ref": "XC"})]).kept
    assert c.short_name == "XC"


def test_the_relation_name_is_the_fallback():
    (c,) = osmroutes.candidates([relation()]).kept
    assert c.short_name == "Test Line"


def test_the_operator_becomes_the_agency():
    (c,) = osmroutes.candidates([relation(tags={"operator": "ScotRail"})]).kept
    assert c.agency_id == "ScotRail"


# --- whose relation is it ----------------------------------------------------
#
# The window is a box and the border is not, so the box cannot be the only gate.
# Northern Ireland's live stops reach Dublin and every relation of the Republic's
# network came back inside them -- and both archives are loaded onto one map, so
# the two of them drew the Republic's rail twice.


def ni(tags: dict[str, str]) -> tuple[int, int]:
    """How many candidates and how many refusals a Northern Ireland run gets."""
    sifted = osmroutes.candidates([relation(tags=tags)], region="northern_ireland")
    return len(sifted.kept), sifted.not_ours


def test_another_regions_operator_is_refused():
    assert ni({"operator": "Iarnród Éireann"}) == (0, 1)


def test_the_regions_own_operator_is_kept():
    assert ni({"operator": "NI Railways"}) == (1, 0)


def test_accents_do_not_decide_ownership():
    """The tag is written both ways across the network and neither is wrong."""
    assert ni({"operator": "Iarnrod Eireann"}) == (0, 1)


def test_a_relation_naming_no_operator_is_left_to_the_window():
    assert ni({}) == (1, 0)


def test_an_operator_no_region_claims_is_left_to_the_window():
    """What keeps every BODS slug drawing what it has always drawn."""
    sifted = osmroutes.candidates([relation(tags={"operator": "ScotRail"})])
    assert len(sifted.kept) == 1


def test_a_region_with_no_operators_still_refuses_a_claimed_one():
    """Great Britain's window is clipped to the British Isles, so Iarnród
    Éireann's relations reach it and were drawn into its archive too."""
    sifted = osmroutes.candidates([relation(tags={"operator": "Iarnród Éireann"})])
    assert (len(sifted.kept), sifted.not_ours) == (0, 1)


def test_a_jointly_run_line_goes_to_the_operator_named_first():
    """The Enterprise. Both regions read the one tag, so which archive gets it is
    arbitrary and its landing in only one of them is not."""
    joint = {"operator": "Iarnród Éireann;NI Railways"}
    assert ni(joint) == (0, 1)
    kept = osmroutes.candidates([relation(tags=joint)], region="ireland").kept
    assert len(kept) == 1


def test_a_bilingual_operator_pair_separates_on_the_slash():
    assert ni({"operator": "Iarnród Éireann / Irish Rail"}) == (0, 1)


# --- what lands in the database ----------------------------------------------


@pytest.fixture
def seeded(con):
    db.set_meta(con, "feed_version", FEED)
    return con


def test_a_pattern_is_written_live_and_without_trips(seeded):
    sifted = osmroutes.candidates([relation()])
    assert osmroutes.write(seeded, sifted.kept) == 1
    row = db.row(
        seeded,
        "SELECT route_id, mode, n_trips, shape_id, last_seen, n_stops FROM patterns",
    )
    assert row == ("osm:r1", "rail", None, None, FEED, 2)


def test_the_geometry_lands_in_traces(seeded):
    osmroutes.write(seeded, osmroutes.candidates([relation()]).kept)
    way_ids, lon = db.row(seeded, "SELECT way_ids, lon_e6 FROM traces")
    assert way_ids == [10, 11]
    assert lon[0] == -1_000_000


def test_a_trace_status_row_stops_trace_redoing_the_work(seeded):
    """Without it these are live, not matchable and shapeless -- `trace`'s
    definition of pending -- so a national Overpass query gets spent re-deriving
    geometry this stage already holds."""
    osmroutes.write(seeded, osmroutes.candidates([relation()]).kept)
    status, relation_id = db.row(seeded, "SELECT status, relation_id FROM trace_status")
    assert status == "ok"
    assert relation_id == 1


def test_the_pattern_id_is_stable_across_runs(seeded):
    kept = osmroutes.candidates([relation()]).kept
    osmroutes.write(seeded, kept)
    first = db.scalar(seeded, "SELECT pattern_id FROM patterns")
    osmroutes.write(seeded, kept)
    assert db.scalar(seeded, "SELECT count(*) FROM patterns") == 1
    assert db.scalar(seeded, "SELECT pattern_id FROM patterns") == first


def test_the_pattern_id_is_the_relation_id_and_its_normalised_stops(seeded):
    """A literal, because two runs of one build agree however the id is minted.

    `pattern_id` hashes `route_id | direction | stop key`, and here the stop key is
    the relation's stop names put through `osm.normalise`. So the suffix, the
    punctuation and the apostrophe tables in `osm` are all inside a permanent cache
    key: widening one of them re-mints every `osm:r` pattern in the country and
    leaves its `traces` and `trace_status` rows behind under ids nothing selects.
    """
    osmroutes.write(seeded, osmroutes.candidates([relation()]).kept)
    # hash('osm:r1' || '|' || '' || '|' || 'alpha\x1fbeta') >> 1, where the two
    # names are "Alpha Rail Station" and "Beta Rail Station" normalised.
    assert db.scalar(seeded, "SELECT pattern_id FROM patterns") == 5764178481033107580


def test_a_relation_that_stops_qualifying_stops_being_drawn(seeded):
    """Retired rather than deleted, so its row survives and its geometry does not
    keep appearing in the current feed."""
    osmroutes.write(seeded, osmroutes.candidates([relation(relation_id=1)]).kept)
    osmroutes.write(seeded, osmroutes.candidates([relation(relation_id=2)]).kept)
    live = seeded.execute(
        "SELECT route_id FROM patterns WHERE last_seen = ?", [FEED]
    ).fetchall()
    assert [r[0] for r in live] == ["osm:r2"]
    assert db.scalar(seeded, "SELECT count(*) FROM patterns") == 2


def test_writing_without_a_feed_version_is_refused(con):
    kept = osmroutes.candidates([relation()]).kept
    with pytest.raises(RuntimeError, match="feed_version"):
        osmroutes.write(con, kept)


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


def test_two_writers_both_reach_the_table(seeded):
    """`routes` writes the ways of its `route=train` relations and `trace` writes
    the ways of the subway and tram relations it cut patterns out of. Neither sees
    the other's, so a writer that cleared the table first would take the tube's
    track out of the archive on the next `routes` run -- and the export joins
    `ways` inside, so nothing would raise."""
    osmroutes.write_ways(seeded, [relation()])
    osmroutes.write_ways(seeded, [relation(ways=[way(99, [(50.0, 0.0), (50.1, 0.0)])])])
    got = [
        r[0] for r in seeded.execute("SELECT way_id FROM ways ORDER BY way_id").fetchall()
    ]
    assert got == [10, 11, 99]


def test_a_way_no_trace_runs_over_is_pruned(seeded):
    """A way is in the table because some pattern's geometry runs over it, so one
    named by no `traces` row is drawn by nothing and is dead weight."""
    osmroutes.write(seeded, osmroutes.candidates([relation()]).kept)
    osmroutes.write_ways(seeded, [relation()])
    osmroutes.write_ways(seeded, [relation(ways=[way(99, [(50.0, 0.0), (50.1, 0.0)])])])
    assert osmroutes.prune_ways(seeded) == 1
    got = [
        r[0] for r in seeded.execute("SELECT way_id FROM ways ORDER BY way_id").fetchall()
    ]
    assert got == [10, 11]


def test_a_degenerate_way_is_not_stored(seeded):
    """One point cannot be drawn and cannot be oriented."""
    assert osmroutes.write_ways(seeded, [relation(ways=[way(10, [(51.0, -1.0)])])]) == 0


# --- the window --------------------------------------------------------------


def live(con, stops: list[tuple[float, float]]) -> None:
    """One live pattern calling at these coordinates, and nothing else."""
    db.set_meta(con, "feed_version", FEED)
    con.execute(
        "INSERT INTO patterns (pattern_id, route_id, short_name, direction, n_stops,"
        " n_trips, span_m, mode, first_seen, last_seen)"
        " VALUES (1, 'R1', 'X', 0, ?, 1, 0.0, 'bus', ?, ?)",
        [len(stops), FEED, FEED],
    )
    for i, (lat, lon) in enumerate(stops):
        con.execute(
            "INSERT INTO stops VALUES (?, ?, ?, ?)", [f"S{i}", f"Stop {i}", lat, lon]
        )
        con.execute("INSERT INTO pattern_stops VALUES (1, ?, ?)", [i, f"S{i}"])


BELFAST = (54.60, -5.93)
DUBLIN = (53.35, -6.25)


def test_a_cross_border_stop_no_longer_widens_the_window(con):
    """Translink runs coach and rail to Dublin, so a min/max over the province's
    live stops reaches 53.3 N and asks Overpass for most of the Republic."""
    live(con, [BELFAST, DUBLIN])
    box = osmroutes.bbox(con, "northern_ireland")
    assert box is not None
    assert box[0] == 54.0


def test_a_region_with_no_bounds_keeps_the_window_its_stops_draw(con):
    live(con, [BELFAST, DUBLIN])
    box = osmroutes.bbox(con, "all")
    assert box is not None
    assert box[0] == pytest.approx(DUBLIN[0] - osmroutes.BBOX_PAD)


def test_bounds_that_never_meet_the_stops_are_an_error(con):
    """A misconfigured region would otherwise query an empty box and report that
    it discovered nothing, which reads as an Overpass that answered."""
    live(con, [DUBLIN])
    with pytest.raises(RuntimeError, match="bounds"):
        osmroutes.bbox(con, "northern_ireland")
