"""Relation track inverted from per-pattern to per-way, and published as its own layer.

The test that matters most here is the one about which patterns get inverted.
`trace` stores `way_ids` as the whole candidate chain rather than the ways inside
the slice it cut, so inverting a timetable-derived trace attributes a short working
to every way of its line. That is harmless while the column only documents what was
drawn and is a confident lie once it is inverted.
"""

from __future__ import annotations

import json

import pytest
from test_osmroutes import FEED, relation

from wayfare import aggregate, db, osmroutes, publish


@pytest.fixture
def built(con):
    """A database holding one two-way rail relation, drawn and aggregated."""
    db.set_meta(con, "feed_version", FEED)
    rels = [relation(relation_id=1, tags={"ref": "XC", "operator": "CrossCountry"})]
    osmroutes.write(con, osmroutes.candidates(rels)[0])
    osmroutes.write_ways(con, rels)
    return con


def _timetabled_trace(con, pattern_id: int = 999) -> None:
    """A pattern from the timetable whose geometry `trace` resolved: the case that
    must not be inverted."""
    con.execute(
        "INSERT INTO patterns (pattern_id, route_id, short_name, mode, n_trips, "
        "first_seen, last_seen) VALUES (?, '43', 'Northern', 'metro', 12, ?, ?)",
        [pattern_id, FEED, FEED],
    )
    con.execute(
        "INSERT INTO traces (pattern_id, relation_id, way_ids, lon_e6, lat_e6) "
        "VALUES (?, 7, [10, 11], [-1000000, -1000000], [51000000, 51200000])",
        [pattern_id],
    )


# --- the inversion -----------------------------------------------------------


def test_a_relation_becomes_one_row_per_way(built):
    assert aggregate.build_track_services(built) == 2
    rows = built.execute(
        "SELECT way_id, short_name, agency_id, n_patterns, n_trips "
        "FROM track_services ORDER BY way_id"
    ).fetchall()
    assert rows == [
        (10, "XC", "CrossCountry", 1, None),
        (11, "XC", "CrossCountry", 1, None),
    ]


def test_two_relations_over_one_way_collapse_to_one_way(built):
    """75.8% of GB rail ways carry two or more relations; that overlap is the
    reason to invert at all."""
    # One call, not two: `write` retires everything it did not just see, so a
    # second call would retire the first relation rather than add to it.
    both = [
        relation(relation_id=1, tags={"ref": "XC", "operator": "CrossCountry"}),
        relation(relation_id=2, name="Other", tags={"ref": "TP"}),
    ]
    osmroutes.write(built, osmroutes.candidates(both)[0])
    aggregate.build_track_services(built)
    assert db.scalar(built, "SELECT count(DISTINCT way_id) FROM track_services") == 2
    assert db.scalar(built, "SELECT count(*) FROM track_services") == 4


def test_a_timetabled_trace_is_not_inverted(built):
    """`trace` records the whole relation chain, not the slice it cut, so
    inverting one attributes a short working to the whole line."""
    _timetabled_trace(built)
    aggregate.build_track_services(built)
    names = {
        r[0] for r in built.execute("SELECT short_name FROM track_services").fetchall()
    }
    assert names == {"XC"}


def test_trips_stay_null_rather_than_becoming_zero(built):
    """Zero trips a week and an unknown number are different claims."""
    aggregate.build_track_services(built)
    assert db.scalar(built, "SELECT count(n_trips) FROM track_services") == 0


def test_trips_are_summed_once_a_timetable_has_been_attributed(built):
    built.execute("UPDATE patterns SET n_trips = 21 WHERE route_id LIKE 'osm:r%'")
    aggregate.build_track_services(built)
    rows = built.execute("SELECT n_trips FROM track_services").fetchall()
    assert {r[0] for r in rows} == {21}


def test_a_retired_relation_stops_being_inverted(built):
    built.execute("UPDATE patterns SET last_seen = NULL")
    assert aggregate.build_track_services(built) == 0


# --- the export --------------------------------------------------------------


def test_an_empty_table_writes_no_file(con):
    """None rather than an empty file, so a bus-only region skips the extra pass."""
    assert publish.export_track_geojsonl(con) is None


def test_one_feature_per_way_with_its_service_list(built, tmp_path):
    aggregate.build_track_services(built)
    path = publish.export_track_geojsonl(built, tmp_path / "track.geojsonl")
    assert path is not None
    features = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(features) == 2
    props = features[0]["properties"]
    assert props["way_id"] == 10
    assert props["n"] == 1
    assert props["refs"] == ["XC"]
    assert props["trips"] is None
    assert features[0]["geometry"]["type"] == "LineString"


def test_the_export_is_ordered_so_a_rebuild_is_byte_identical(built, tmp_path):
    aggregate.build_track_services(built)
    a = publish.export_track_geojsonl(built, tmp_path / "a.geojsonl")
    b = publish.export_track_geojsonl(built, tmp_path / "b.geojsonl")
    assert a is not None and b is not None
    assert a.read_bytes() == b.read_bytes()


def test_relation_track_is_credited_to_openstreetmap(built):
    aggregate.build_segments(built)
    assert publish.contents(built)["track"] is True
