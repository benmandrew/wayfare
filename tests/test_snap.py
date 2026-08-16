"""Snapping an operator's rail shape onto OSM track.

Every way here is hand-built Overpass JSON rather than a recorded response, for the
reason `test_trace.py` builds its relations by hand: the parser is half of what is
under test, and the failures this stage exists to refuse are all shaped like geometry
that is nearly right.

The track is a straight line east along one latitude near the mini feed's Manchester
stops, so a pattern built from that feed lands on it.
"""

from __future__ import annotations

import pytest

from wayfare import aggregate, config, db, gtfs, osm, snap

_LAT = 53.4800
_W1 = (-2.2450, -2.2400)  # way 401
_W2 = (-2.2400, -2.2350)  # way 402
_W3 = (-2.2350, -2.2300)  # way 403


def _way(way_id: int, lons: tuple[float, float], lat: float = _LAT) -> dict:
    """A two-point way. Overpass writes `geometry` inline under `out geom`."""
    return {
        "type": "way",
        "id": way_id,
        "tags": {"railway": "rail"},
        "geometry": [{"lat": lat, "lon": lons[0]}, {"lat": lat, "lon": lons[1]}],
    }


def _track(*ways: dict) -> list[osm.Way]:
    return osm.parse_ways({"elements": list(ways)})


def _shape(lons: list[float], lat: float = _LAT, pattern_id: int = 1) -> snap.Shaped:
    return snap.Shaped(
        pattern_id=pattern_id,
        route_id="R1",
        lat=tuple(lat for _ in lons),
        lon=tuple(lons),
    )


@pytest.fixture
def rail_con(con, gtfs_dir):
    """The mini feed with its rail route kept, given a shape over the test track."""
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB", modes=frozenset({"bus", "rail"}))
    con.execute(
        "INSERT OR REPLACE INTO shapes (shape_id, lon_e6, lat_e6) VALUES (?, ?, ?)",
        [
            "SNAPTEST",
            [round(lo * 1e6) for lo in (-2.2450, -2.2400, -2.2350, -2.2300)],
            [round(_LAT * 1e6)] * 4,
        ],
    )
    con.execute("UPDATE patterns SET shape_id = 'SNAPTEST' WHERE mode = 'rail'")
    return con


# -- parsing -----------------------------------------------------------------


def test_parse_ways_keeps_the_way_id_and_drops_a_stub() -> None:
    """A way of one point cannot be snapped onto, and a way id is the whole output."""
    ways = _track(
        _way(401, _W1),
        {"type": "way", "id": 999, "geometry": [{"lat": _LAT, "lon": -2.24}]},
    )
    assert [w.way_id for w in ways] == [401]
    assert ways[0].points[0] == (_LAT, -2.2450)


def test_the_way_query_excludes_service_track() -> None:
    """A siding sits within metres of the running line, so a shape would snap onto it
    and report a service running through a depot."""
    ql = osm.way_query((53.0, -3.0, 54.0, -2.0))
    assert '["service"!~"."]' in ql
    assert "rail|light_rail" in ql


# -- the fit -----------------------------------------------------------------


def test_a_shape_over_track_records_the_ways_under_it() -> None:
    track = snap.Track(_track(_way(401, _W1), _way(402, _W2), _way(403, _W3)), _LAT)
    out = snap._snap_one(_shape([-2.2450, -2.2400, -2.2350, -2.2300]), track)
    assert out.status == "ok"
    assert out.way_ids == [401, 402, 403]
    assert out.covered == pytest.approx(1.0)
    assert out.worst_m < 1.0


def test_a_shape_over_no_track_is_refused_rather_than_snapped_far() -> None:
    """`SNAP_MAX_M` is what stops a shape reaching for the nearest thing there is."""
    track = snap.Track(_track(_way(401, _W1)), _LAT)
    out = snap._snap_one(_shape([-2.0000, -1.9990, -1.9980]), track)
    assert out.status == "no_track"
    assert out.way_ids == []


def test_a_partial_cover_is_refused_rather_than_trimmed() -> None:
    """The dangerous outcome. Attributing the half that matched would report a short
    working over a line the service runs the length of."""
    track = snap.Track(_track(_way(401, _W1)), _LAT)
    # Half the shape lies on way 401, half runs off the end of the mapped track.
    out = snap._snap_one(_shape([-2.2450, -2.2400, -2.2000, -2.1500]), track)
    assert out.status == "partial_cover"
    assert out.way_ids == []
    assert out.covered < config.SNAP_MIN_COVER
    assert "found track" in (out.detail or "")


def test_parallel_track_does_not_shred_the_run() -> None:
    """Four tracks through a throat sit within metres of each other. Taking the
    nearest at every vertex hops between them and turns one line into a list of
    fragments, each carrying part of the service."""
    # Way 402 is a hair closer to the shape than 401 over the middle, and both are
    # inside the tolerance the whole way.
    parallel = _track(
        _way(401, (-2.2450, -2.2300)), _way(402, (-2.2450, -2.2300), _LAT + 0.00005)
    )
    track = snap.Track(parallel, _LAT + 0.00002)
    out = snap._snap_one(
        _shape([-2.2450, -2.2420, -2.2390, -2.2360, -2.2330, -2.2300], lat=_LAT + 0.00002),
        track,
    )
    assert out.status == "ok"
    assert len(out.way_ids) == 1


def test_a_diverging_way_is_let_go_of_rather_than_held_to_the_tolerance() -> None:
    """The other half of the hold, and the half the first real run got wrong. Holding
    until the previous way leaves `SNAP_MAX_M` entirely gives it another 25 m of track
    it does not carry: all 319 of the Republic's rail patterns reported a worst vertex
    in the 20-25 m band over track with something inside 5 m of 99.5% of it.
    """
    # 401 runs the first half and then peels away north; 402 carries straight on
    # underneath the shape. The shape stays straight.
    diverging = _track(
        _way(401, (-2.2450, -2.2400)),
        {
            "type": "way",
            "id": 401,
            "tags": {"railway": "rail"},
            "geometry": [
                {"lat": _LAT, "lon": -2.2400},
                {"lat": _LAT + 0.00018, "lon": -2.2300},  # ~20 m adrift by the end
            ],
        },
        _way(402, (-2.2400, -2.2300)),
    )
    track = snap.Track(diverging, _LAT)
    out = snap._snap_one(_shape([-2.2450, -2.2400, -2.2350, -2.2300]), track)
    assert out.status == "ok"
    assert 402 in out.way_ids
    assert out.worst_m < config.SNAP_HOLD_M + 1.0


def test_a_way_run_over_twice_is_recorded_once() -> None:
    """`osm.ways_between`'s rule, for the same reason: a line that doubles back must
    not count its own track twice."""
    track = snap.Track(_track(_way(401, _W1), _way(402, _W2)), _LAT)
    out = snap._snap_one(_shape([-2.2450, -2.2400, -2.2380, -2.2400, -2.2450]), track)
    assert out.status == "ok"
    assert sorted(out.way_ids) == [401, 402]
    assert len(out.way_ids) == len(set(out.way_ids))


def test_a_shape_of_one_point_is_skipped() -> None:
    track = snap.Track(_track(_way(401, _W1)), _LAT)
    assert snap._snap_one(_shape([-2.2450]), track).status == "too_short"


# -- the stage ---------------------------------------------------------------


def test_run_snaps_a_shaped_rail_pattern_and_caches_the_outcome(rail_con) -> None:
    assert snap.pending_count(rail_con) == 1
    counts = snap.run(rail_con, ways=_track(_way(401, _W1), _way(402, _W2), _way(403, _W3)))
    assert counts == {"ok": 1}

    status, n_ways, covered = db.row(
        rail_con, "SELECT status, n_ways, covered_pct FROM snap_status"
    )
    assert (status, n_ways) == ("ok", 3)
    assert covered == pytest.approx(100.0)
    # `ways_cut` TRUE and no relation: the ways are this pattern's own, arrived at by
    # snapping rather than by cutting a chain.
    cut, relation_id = db.row(rail_con, "SELECT ways_cut, relation_id FROM traces")
    assert cut is True
    assert relation_id is None
    assert db.scalar(rail_con, "SELECT count(*) FROM ways") == 3


def test_run_records_a_refusal_and_never_asks_again(rail_con) -> None:
    """A permanent cache: the second run must find nothing to do."""
    snap.run(rail_con, ways=_track(_way(401, (-2.0, -1.99))))
    assert db.scalar(rail_con, "SELECT status FROM snap_status") == "no_track"
    assert snap.pending_count(rail_con) == 0
    assert snap.run(rail_con, ways=_track(_way(401, _W1), _way(402, _W2))) == {}


def test_a_half_written_run_leaves_nothing_behind(rail_con, monkeypatch) -> None:
    """The failure this stage cannot survive without a transaction. Work is selected
    by the absence of a `snap_status` row, so a status committed without its geometry
    marks a pattern resolved that nothing will ever ask about again -- and it just
    stops being drawn. Measured on a real run killed between the two writes: 48 of 319
    patterns `ok` with no trace, and 2,436 way ids pointing at geometry never stored.
    """
    monkeypatch.setattr(snap, "write_ways", _boom)
    with pytest.raises(RuntimeError, match="interrupted"):
        snap.run(rail_con, ways=_track(_way(401, _W1), _way(402, _W2), _way(403, _W3)))

    assert db.scalar(rail_con, "SELECT count(*) FROM snap_status") == 0
    assert db.scalar(rail_con, "SELECT count(*) FROM traces") == 0
    # And the pattern is still owed, which is the property the rollback buys.
    assert snap.pending_count(rail_con) == 1


def _boom(*_args, **_kwargs):
    raise RuntimeError("interrupted")


def test_retry_clears_only_what_it_is_asked_for(rail_con) -> None:
    snap.run(rail_con, ways=_track(_way(401, (-2.0, -1.99))))
    assert snap.retry(rail_con, ["partial_cover"]) == 0
    assert snap.retry(rail_con, ["no_track"]) == 1
    assert snap.pending_count(rail_con) == 1


def test_a_pattern_trace_already_resolved_is_left_alone(rail_con) -> None:
    """`traces` holds one row per pattern, and a relation fitted by stop sequence is
    the stronger evidence of the two."""
    pid = db.scalar(rail_con, "SELECT pattern_id FROM patterns WHERE mode = 'rail'")
    rail_con.execute(
        "INSERT INTO traces (pattern_id, relation_id, way_ids, ways_cut, lon_e6, lat_e6) "
        "VALUES (?, 555, [9001], TRUE, [1], [1])",
        [pid],
    )
    assert snap.pending_count(rail_con) == 0


def test_only_the_modes_that_trade_a_shape_for_way_ids_are_snapped(rail_con) -> None:
    """A tram's shape carries street running and depot moves no route relation has,
    so it must not be handed to the snapper either."""
    assert "tram" not in config.TRACE_OVER_SHAPE_MODES
    rail_con.execute("UPDATE patterns SET mode = 'tram' WHERE mode = 'rail'")
    assert snap.pending_count(rail_con) == 0


def test_the_window_is_clipped_to_the_british_isles(rail_con) -> None:
    """A feed that carries international coach puts correct coordinates in Warsaw,
    and one of them in the min/max is a query for every railway across Europe."""
    rail_con.execute(
        "INSERT OR REPLACE INTO shapes (shape_id, lon_e6, lat_e6) VALUES (?, ?, ?)",
        ["WARSAW", [round(20.96 * 1e6), round(21.0 * 1e6)], [round(52.23 * 1e6)] * 2],
    )
    rail_con.execute("""
        INSERT INTO patterns (pattern_id, route_id, mode, shape_id, first_seen, last_seen)
        SELECT 909, 'R9', 'rail', 'WARSAW', value, value
        FROM meta WHERE key = 'feed_version'
    """)
    window = snap.bbox(rail_con)
    assert window is not None
    _, _, north, east = window
    assert east < 0.0
    assert north < 54.0


# -- what the rest of the pipeline does with it ------------------------------


def test_a_snapped_pattern_is_drawn_per_way_and_not_as_its_own_polyline(rail_con) -> None:
    """The whole point. The shape stops being a coincident polyline and becomes rows
    against the ways it runs over, which is what a hover can answer from."""
    snap.run(rail_con, ways=_track(_way(401, _W1), _way(402, _W2), _way(403, _W3)))
    aggregate.build(rail_con)
    assert db.scalar(rail_con, "SELECT count(*) FROM segments") == 0
    rows = rail_con.execute(
        "SELECT way_id, mode FROM track_services ORDER BY way_id"
    ).fetchall()
    assert rows == [(401, "rail"), (402, "rail"), (403, "rail")]


def test_a_refused_pattern_keeps_its_operator_shape(rail_con) -> None:
    """What makes the stage unable to take a line off the map: a region whose track is
    unmapped loses the sharing and never the line."""
    snap.run(rail_con, ways=_track(_way(401, (-2.0, -1.99))))
    aggregate.build(rail_con)
    assert db.scalar(rail_con, "SELECT count(*) FROM segments") == 1
    assert db.scalar(rail_con, "SELECT count(*) FROM track_services") == 0
