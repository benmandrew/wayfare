"""Drawing the modes that ship no geometry, from OSM route relations.

Every relation here is hand-built Overpass JSON rather than a recorded response,
because the parser is half of what is under test: the traps this stage exists to
survive are all shaped like a member role or a tag that is not where it looks like
it should be.
"""

from __future__ import annotations

import json

import pytest

from wayfare import aggregate, config, db, gtfs, osm, publish, trace

# A straight line east along one latitude, in three ways that join end to end.
# Coordinates are near the mini feed's Manchester stops so that a pattern built
# from it lands on this track.
_A = (53.4800, -2.2450)
_B = (53.4800, -2.2400)
_C = (53.4800, -2.2350)
_D = (53.4800, -2.2300)


def _way(way_id: int, pts: list[tuple[float, float]], role: str = "") -> dict:
    return {
        "type": "way",
        "ref": way_id,
        "role": role,
        "geometry": [{"lat": la, "lon": lo} for la, lo in pts],
    }


def _stop(node_id: int, role: str = "stop") -> dict:
    return {"type": "node", "ref": node_id, "role": role}


def _node(node_id: int, name: str, at: tuple[float, float]) -> dict:
    return {
        "type": "node",
        "id": node_id,
        "lat": at[0],
        "lon": at[1],
        "tags": {"name": name, "railway": "station"},
    }


def _overpass(
    members: list[dict],
    nodes: list[dict],
    relation_id: int = 900,
    route: str = "subway",
    name: str = "Test line",
) -> dict:
    return {
        "elements": [
            {
                "type": "relation",
                "id": relation_id,
                "tags": {"type": "route", "route": route, "name": name},
                "members": members,
            },
            *nodes,
        ]
    }


def _line() -> dict:
    """Three ways, four stations, one continuous path west to east."""
    return _overpass(
        members=[
            _stop(1),
            _stop(2),
            _stop(3),
            _stop(4),
            _way(101, [_A, _B]),
            _way(102, [_B, _C]),
            _way(103, [_C, _D]),
        ],
        nodes=[
            _node(1, "Alpha Underground Station", _A),
            _node(2, "Bravo Underground Station", _B),
            _node(3, "Charlie Underground Station", _C),
            _node(4, "Delta Underground Station", _D),
        ],
    )


# -- names -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        # The four the Victoria line experiment actually turned on: a suffix on one
        # side and not the other, a full stop inside an abbreviation, an ampersand,
        # and a bare name that must survive untouched.
        ("Blackhorse Road Station", "blackhorse road"),
        ("Blackhorse Road station", "blackhorse road"),
        ("King's Cross St. Pancras Underground Station", "kings cross st pancras"),
        ("King's Cross St Pancras", "kings cross st pancras"),
        ("Highbury & Islington", "highbury and islington"),
        ("Highbury and Islington Station", "highbury and islington"),
        ("Vauxhall", "vauxhall"),
        ("Edgware Road Station Station", "edgware road"),
        ("Pier Head Ferry Terminal", "pier head"),
        (None, ""),
        ("", ""),
    ],
)
def test_normalise_agrees_across_publishers(raw: str | None, want: str) -> None:
    assert osm.normalise(raw) == want


# -- parsing -----------------------------------------------------------------


def test_parse_reads_ways_in_order_and_names_stops_from_their_nodes() -> None:
    (rel,) = osm.parse(_line())
    assert rel.relation_id == 900
    assert rel.route == "subway"
    assert [w.way_id for w in rel.ways] == [101, 102, 103]
    assert [s.name for s in rel.stops] == [
        "Alpha Underground Station",
        "Bravo Underground Station",
        "Charlie Underground Station",
        "Delta Underground Station",
    ]


def test_parse_keeps_platforms_out_of_the_way_chain() -> None:
    """A platform member in the chain is the trap that reads as broken mapping.

    Leaving `role=platform` in produces a break at every station, because a platform
    way runs beside the track rather than along it.
    """
    data = _overpass(
        members=[
            _stop(1),
            _stop(2),
            _way(101, [_A, _B]),
            _way(500, [(53.4801, -2.2450), (53.4801, -2.2440)], role="platform"),
            _way(102, [_B, _C]),
            _way(501, [(53.4801, -2.2350), (53.4801, -2.2340)], role="platform_entry_only"),
        ],
        nodes=[_node(1, "Alpha", _A), _node(2, "Charlie", _C)],
    )
    (rel,) = osm.parse(data)
    assert [w.way_id for w in rel.ways] == [101, 102]
    assert osm.chain(rel).breaks == 0


def test_parse_ignores_platform_node_roles_as_calling_points() -> None:
    data = _overpass(
        members=[_stop(1), _stop(2, role="platform"), _way(101, [_A, _B])],
        nodes=[_node(1, "Alpha", _A), _node(2, "Bravo", _B)],
    )
    (rel,) = osm.parse(data)
    assert [s.node_id for s in rel.stops] == [1]


# -- chaining ----------------------------------------------------------------


def test_chain_walks_member_order_into_one_path() -> None:
    (rel,) = osm.parse(_line())
    ch = osm.chain(rel)
    assert ch.breaks == 0
    assert ch.way_ids == [101, 102, 103]
    assert ch.points == [_A, _B, _C, _D]


def test_chain_reverses_a_way_laid_the_other_way_round() -> None:
    data = _overpass(
        members=[_stop(1), _stop(2), _way(101, [_A, _B]), _way(102, [_C, _B])],
        nodes=[_node(1, "Alpha", _A), _node(2, "Charlie", _C)],
    )
    (rel,) = osm.parse(data)
    ch = osm.chain(rel)
    assert ch.breaks == 0
    assert ch.points == [_A, _B, _C]


def test_chain_reverses_a_first_way_laid_backwards() -> None:
    """The first way's orientation is decided by the second, so it is the one case
    the walk cannot settle as it goes."""
    data = _overpass(
        members=[_stop(1), _stop(2), _way(101, [_B, _A]), _way(102, [_B, _C])],
        nodes=[_node(1, "Alpha", _A), _node(2, "Charlie", _C)],
    )
    (rel,) = osm.parse(data)
    ch = osm.chain(rel)
    assert ch.breaks == 0
    assert ch.points == [_A, _B, _C]


def test_chain_counts_a_genuine_gap_as_a_break() -> None:
    far = (53.4900, -2.1000)
    data = _overpass(
        members=[_stop(1), _stop(2), _way(101, [_A, _B]), _way(102, [far, _D])],
        nodes=[_node(1, "Alpha", _A), _node(2, "Delta", _D)],
    )
    (rel,) = osm.parse(data)
    assert osm.chain(rel).breaks == 1


def test_prepare_drops_the_relations_that_do_not_chain() -> None:
    far = (53.4900, -2.1000)
    broken = _overpass(
        members=[_stop(1), _stop(2), _way(101, [_A, _B]), _way(102, [far, _D])],
        nodes=[_node(1, "Alpha", _A), _node(2, "Delta", _D)],
        relation_id=901,
    )
    both = osm.parse(_line()) + osm.parse(broken)
    prepared = trace.prepare(both)
    assert [c.relation.relation_id for c in prepared.candidates] == [900]
    # The broken one's stations are remembered so a pattern on it can say why.
    assert "delta" in prepared.broken_names


# -- geometry ----------------------------------------------------------------


def test_slice_between_cuts_the_chain_at_two_distances() -> None:
    pts = [_A, _B, _C, _D]
    metres = osm.to_metres(pts, _A[0])
    cum = osm.cumulative(metres)
    cut = osm.slice_between(pts, cum, cum[1], cum[2])
    assert cut[0] == pytest.approx(_B, abs=1e-9)
    assert cut[-1] == pytest.approx(_C, abs=1e-9)


def test_project_reports_distance_along_and_distance_off() -> None:
    metres = osm.to_metres([_A, _D], _A[0])
    cum = osm.cumulative(metres)
    # A point 100 m north of the midpoint of the line.
    off_track = osm.to_metres([(_A[0] + 100 / 111_320.0, -2.2375)], _A[0])[0]
    along, off = osm.project(metres, cum, off_track)
    assert off == pytest.approx(100.0, abs=1.0)
    assert along == pytest.approx(cum[-1] / 2, rel=0.02)


# -- resolving ---------------------------------------------------------------


def _candidates(data: dict) -> tuple[trace.Prepared, dict[str, list[int]]]:
    prepared = trace.prepare(osm.parse(data))
    return prepared, trace.index_by_name(prepared.candidates)


def _pattern(names: list[str], points: list[tuple[float, float]]) -> trace.Pattern:
    return trace.Pattern(
        pattern_id=7,
        mode="metro",
        short_name="V",
        names=[osm.normalise(n) for n in names],
        points=points,
    )


def test_resolve_draws_the_whole_line() -> None:
    cands, index = _candidates(_line())
    p = _pattern(["Alpha", "Bravo", "Charlie", "Delta"], [_A, _B, _C, _D])
    got = trace.resolve(p, cands, index)
    assert got.status == "ok"
    assert got.relation_id == 900
    assert got.n_stops == 4
    assert got.geom[0] == pytest.approx(_A, abs=1e-9)
    assert got.geom[-1] == pytest.approx(_D, abs=1e-9)


def test_resolve_cuts_a_short_working_to_its_own_stops() -> None:
    """A pattern is a contiguous sub-path of its line, and must not draw the rest."""
    cands, index = _candidates(_line())
    p = _pattern(["Bravo", "Charlie"], [_B, _C])
    got = trace.resolve(p, cands, index)
    assert got.status == "ok"
    assert got.geom[0] == pytest.approx(_B, abs=1e-9)
    assert got.geom[-1] == pytest.approx(_C, abs=1e-9)
    assert got.length_m < 400  # not the whole line


def test_resolve_matches_a_pattern_running_the_other_way() -> None:
    """A relation is per direction; half the patterns on it run against it."""
    cands, index = _candidates(_line())
    p = _pattern(["Delta", "Charlie", "Bravo", "Alpha"], [_D, _C, _B, _A])
    got = trace.resolve(p, cands, index)
    assert got.status == "ok"
    assert got.n_stops == 4


def test_resolve_reports_no_relation_when_nothing_serves_the_ends() -> None:
    cands, index = _candidates(_line())
    p = _pattern(["Nowhere", "Elsewhere"], [_A, _D])
    assert trace.resolve(p, cands, index).status == "no_relation"


def test_resolve_refuses_a_sequence_the_line_does_not_run() -> None:
    """Same termini, stops the line never calls at in that order."""
    cands, index = _candidates(_line())
    p = _pattern(["Alpha", "Delta", "Bravo"], [_A, _D, _B])
    assert trace.resolve(p, cands, index).status == "no_stop_match"


def test_resolve_refuses_a_stop_too_far_from_the_track() -> None:
    """The name matched something on another line that shares it."""
    cands, index = _candidates(_line())
    adrift = (53.5400, -2.2400)  # ~6.7 km north of the track
    p = _pattern(["Alpha", "Bravo"], [_A, adrift])
    assert trace.resolve(p, cands, index).status == "no_stop_match"


def test_resolve_names_a_sequence_that_turns_round_partway() -> None:
    """The status that separates "wrong line" from "right line, wrong branch".

    Slicing between the ends of a sequence that doubles back takes whichever branch
    the chain happens to run through, and draws it confidently.
    """
    data = _overpass(
        members=[
            _stop(1),
            _stop(3),
            _stop(2),
            _way(101, [_A, _B]),
            _way(102, [_B, _C]),
            _way(103, [_C, _D]),
        ],
        nodes=[_node(1, "Alpha", _A), _node(2, "Bravo", _B), _node(3, "Charlie", _C)],
    )
    cands, index = _candidates(data)
    p = _pattern(["Alpha", "Charlie", "Bravo"], [_A, _C, _B])
    got = trace.resolve(p, cands, index)
    assert got.status == "not_monotonic"
    assert "out of order" in (got.detail or "")


def test_resolve_tells_a_broken_line_apart_from_an_unmapped_one() -> None:
    """A relation that does not chain is dropped before any pattern reaches it, so
    without remembering its stations this would read as "nobody has mapped this"."""
    far = (53.4900, -2.1000)
    broken = _overpass(
        members=[_stop(1), _stop(2), _way(101, [_A, _B]), _way(102, [far, _D])],
        nodes=[_node(1, "Alpha", _A), _node(2, "Delta", _D)],
    )
    cands, index = _candidates(broken)
    on_the_broken_line = _pattern(["Alpha", "Delta"], [_A, _D])
    assert trace.resolve(on_the_broken_line, cands, index).status == "chain_break"

    nowhere = _pattern(["Nowhere", "Elsewhere"], [_A, _D])
    assert trace.resolve(nowhere, cands, index).status == "no_relation"


def test_resolve_takes_the_placement_that_runs_in_order() -> None:
    """A relation calling at one station twice offers more than one placement.

    Only one of them projects in order along the track, and returning the first
    would refuse a pattern on a placement it never had to use.
    """
    data = _overpass(
        members=[
            _stop(1),
            _stop(2),
            _stop(3),
            _stop(1),
            _way(101, [_A, _B]),
            _way(102, [_B, _C]),
            _way(103, [_C, _B]),
            _way(104, [_B, _A]),
        ],
        nodes=[_node(1, "Alpha", _A), _node(2, "Bravo", _B), _node(3, "Charlie", _C)],
    )
    cands, index = _candidates(data)
    p = _pattern(["Bravo", "Charlie"], [_B, _C])
    got = trace.resolve(p, cands, index)
    assert got.status == "ok"
    assert got.n_stops == 2


def test_resolve_is_deterministic_across_two_identical_relations() -> None:
    """Two relations fitting equally well must not decide the picture by row order."""
    a = _line()
    b = _overpass(
        members=_line()["elements"][0]["members"],
        nodes=[e for e in _line()["elements"] if e["type"] == "node" and "id" in e],
        relation_id=42,
    )
    data = {"elements": a["elements"] + b["elements"]}
    cands, index = _candidates(data)
    p = _pattern(["Alpha", "Bravo", "Charlie", "Delta"], [_A, _B, _C, _D])
    assert {trace.resolve(p, cands, index).relation_id for _ in range(5)} == {42}


# -- fetching ----------------------------------------------------------------


class _Response:
    def __init__(self, payload: dict, status: int = 200):
        self.payload = payload
        self.status_code = status
        self.ok = status < 400
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self.payload


class _Session:
    def __init__(self, payload: dict, status: int = 200):
        self.payload = payload
        self.status = status
        self.calls = 0

    def post(self, url: str, **kw: object) -> _Response:
        self.calls += 1
        return _Response(self.payload, self.status)


def test_fetch_caches_the_body_and_does_not_ask_twice(tmp_path) -> None:
    cache = tmp_path / "relations.json"
    sess = _Session(_line())
    first = osm.fetch((51.0, -1.0, 52.0, 1.0), cache, session=sess)
    second = osm.fetch((51.0, -1.0, 52.0, 1.0), cache, session=sess)
    assert sess.calls == 1
    assert [r.relation_id for r in first] == [r.relation_id for r in second] == [900]


def test_fetch_treats_overpass_load_shedding_as_retryable(tmp_path) -> None:
    """429 says nothing about the query, so it must not become a permanent failure."""
    sess = _Session({}, status=429)
    with pytest.raises(osm.TransportError):
        osm.fetch((51.0, -1.0, 52.0, 1.0), tmp_path / "r.json", session=sess)
    assert not (tmp_path / "r.json").exists()


def test_query_asks_for_every_route_value_including_train() -> None:
    """The Elizabeth line is `route=train`; a set of the obvious names misses it."""
    ql = osm.query((51.0, -1.0, 52.0, 1.0))
    for value in config.OSM_ROUTE_VALUES:
        assert value in ql
    assert "node(r.routes)" in ql  # stop names come from a second statement


# -- the stage end to end ----------------------------------------------------


@pytest.fixture
def rail_con(con, gtfs_dir):
    """The mini feed with its rail route kept: one pattern, no shape, not matchable."""
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB", modes=frozenset({"bus", "rail"}))
    return con


def _rail_relation() -> dict:
    """A line joining the mini feed's Alpha to its Island Quay."""
    alpha = (53.4800, -2.2450)
    mid = (53.4000, -2.7000)
    quay = (53.3200, -3.1800)
    return _overpass(
        members=[
            _stop(1),
            _stop(2),
            _way(201, [alpha, mid]),
            _way(202, [mid, quay]),
        ],
        nodes=[_node(1, "Alpha", alpha), _node(2, "Island Quay", quay)],
        relation_id=555,
        route="train",
        name="Test Rail",
    )


def test_run_draws_a_pattern_that_has_no_shape(rail_con) -> None:
    assert trace.pending_count(rail_con) == 1
    counts = trace.run(rail_con, relations=osm.parse(_rail_relation()))
    assert counts == {"ok": 1}

    status, relation_id, n_stops = db.row(
        rail_con, "SELECT status, relation_id, n_stops FROM trace_status"
    )
    assert (status, relation_id, n_stops) == ("ok", 555, 2)
    lon, lat = db.row(rail_con, "SELECT lon_e6, lat_e6 FROM traces")
    assert len(lon) == len(lat) >= 2


def test_run_records_a_failure_and_never_asks_again(rail_con) -> None:
    """A permanent cache: the second run must find nothing to do."""
    trace.run(rail_con, relations=[])
    assert db.scalar(rail_con, "SELECT status FROM trace_status") == "no_relation"
    assert trace.pending_count(rail_con) == 0
    assert trace.run(rail_con, relations=osm.parse(_rail_relation())) == {}


def test_retry_clears_only_what_it_is_asked_for(rail_con) -> None:
    trace.run(rail_con, relations=[])
    assert trace.retry(rail_con, ["transient"]) == 0
    assert trace.retry(rail_con, ["no_relation"]) == 1
    assert trace.pending_count(rail_con) == 1


def test_run_ignores_a_pattern_that_already_carries_operator_geometry(con, gtfs_dir):
    """The ferry has no shape either, but the bus does -- and a road pattern is
    Valhalla's, whatever OSM holds."""
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB", modes=frozenset({"bus"}))
    assert trace.pending_count(con) == 0


def test_bbox_covers_the_pending_stops_with_room_for_the_line(rail_con) -> None:
    south, west, north, east = trace.bbox(rail_con)
    assert south < 53.32 and north > 53.48
    assert west < -3.18 and east > -2.245


# -- what the rest of the pipeline does with it ------------------------------


def test_segments_are_drawn_from_traces(rail_con) -> None:
    trace.run(rail_con, relations=osm.parse(_rail_relation()))
    aggregate.build(rail_con)
    mode, lon = db.row(rail_con, "SELECT mode, lon_e6 FROM segments")
    assert mode == "rail"
    assert len(lon) >= 2


def test_an_operator_shape_beats_a_relation(con, gtfs_dir) -> None:
    """`trace` never selects a pattern that has a shape, so the two cannot collide --
    and `segments` keeps its one row per pattern."""
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB", modes=frozenset({"bus", "rail"}))
    con.execute("UPDATE patterns SET shape_id = 'SH1' WHERE mode = 'rail'")
    assert trace.pending_count(con) == 0
    aggregate.build(con)
    assert db.scalar(con, "SELECT count(*) FROM segments") == 1


def test_a_traced_archive_owes_openstreetmap_for_its_track(rail_con) -> None:
    """Nothing routed it, so `edge_services` is empty -- and it is still OSM's."""
    trace.run(rail_con, relations=osm.parse(_rail_relation()))
    aggregate.build(rail_con)
    held = publish.contents(rail_con)
    assert held == {"road": False, "operator": True, "track": True}
    credit = config.credit_html("all", **held)
    assert "Track geometry" in credit
    assert "Road geometry" not in credit
    assert "OpenStreetMap contributors" in credit


def test_a_departed_traced_pattern_stops_being_credited(rail_con) -> None:
    """`traces` is a cache keyed on identity and keeps its rows; the credit has to
    describe the archive, not the database."""
    trace.run(rail_con, relations=osm.parse(_rail_relation()))
    aggregate.build(rail_con)
    assert publish.contents(rail_con)["track"] is True
    db.set_meta(rail_con, "feed_version", "a-later-feed")
    aggregate.build(rail_con)
    assert db.scalar(rail_con, "SELECT count(*) FROM traces") == 1
    assert publish.contents(rail_con)["track"] is False


def test_an_untraceable_pattern_never_gates_a_publish(rail_con) -> None:
    """The mistake the mode filter already made once, in a second place.

    `deploy/refresh.sh` refuses to publish while `patterns_pending` is non-zero. A
    relation that will never resolve is permanent, so counting it there would stop a
    scheduled region publishing again for good.
    """
    trace.run(rail_con, relations=[])
    funnel = aggregate.coverage(rail_con)
    assert funnel["traced"]["by_status"] == {"no_relation": 1}
    assert funnel["traced"]["patterns_owed"] == 1
    assert funnel["traced"]["patterns_pending"] == 0
    # The two bus patterns, which Valhalla does still owe, and not the rail one.
    assert funnel["patterns_pending"] == 2
    assert funnel["patterns_total"] == 2


def test_the_funnel_survives_a_database_from_before_this_stage(con, gtfs_dir) -> None:
    """`status` connects read-only, so an unmigrated data root must not raise."""
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB", modes=frozenset({"bus", "rail"}))
    con.execute("DROP TABLE trace_status")
    assert aggregate.coverage(con)["traced"] == {}


def test_credit_nouns_cover_both_kinds_of_osm_geometry() -> None:
    def what(**held: bool) -> list[str]:
        return [c.what for c in config.credit_parts("all", **held)]

    assert what(road=True) == ["Routes and timetables", "Road geometry"]
    assert what(road=False, track=True) == ["Routes and timetables", "Track geometry"]
    assert what(road=True, track=True) == [
        "Routes and timetables",
        "Road and track geometry",
    ]
    assert what(road=False, track=False) == ["Routes and timetables"]
