from __future__ import annotations

import builders
import pytest
import requests

from wayfare import config, osm, polyline, valhalla

# Two edges along a street, in the shape trace_attributes actually returns:
# lengths in kilometres, geometry as one shared polyline indexed by each edge.
SHAPE = [(53.4800, -2.2450), (53.4800, -2.2400), (53.4800, -2.2350)]
RESPONSE = {
    "shape": polyline.encode(SHAPE, 6),
    "confidence_score": 0.87,
    "edges": [
        {
            "id": 1001,
            "way_id": 44556677,
            "length": 0.331,
            "names": ["Oxford Road"],
            "road_class": "secondary",
            "begin_shape_index": 0,
            "end_shape_index": 1,
        },
        {
            "id": 1002,
            "way_id": 44556678,
            "length": 0.331,
            "names": ["Oxford Road"],
            "road_class": "secondary",
            "begin_shape_index": 1,
            "end_shape_index": 2,
        },
    ],
}


def test_lengths_are_converted_to_metres():
    m = valhalla._to_match(RESPONSE, source="shape")
    assert m.edges[0].length_m == pytest.approx(331.0)
    assert m.road_m == pytest.approx(662.0)


def test_edge_geometry_is_sliced_from_the_shared_shape():
    m = valhalla._to_match(RESPONSE, source="shape")
    assert m.edges[0].geom == pytest.approx(SHAPE[0:2], abs=1e-6)
    assert m.edges[1].geom == pytest.approx(SHAPE[1:3], abs=1e-6)


def test_edges_without_osm_identity_are_dropped():
    """Valhalla emits synthesised connector edges with no way_id. They have no
    place in a dataset keyed on OSM ways."""
    response = dict(RESPONSE, edges=[*RESPONSE["edges"], {"id": 9, "length": 0.1}])
    m = valhalla._to_match(response, source="shape")
    assert [e.edge_id for e in m.edges] == [1001, 1002]


def test_confidence_and_names_survive():
    m = valhalla._to_match(RESPONSE, source="shape")
    assert m.confidence == pytest.approx(0.87)
    assert m.edges[0].road_name == "Oxford Road"
    assert m.edges[0].road_class == "secondary"


@pytest.mark.parametrize(
    ("n", "expected_chunks"),
    [(40, 1), (41, 2), (79, 2), (80, 3)],
)
def test_chunking_covers_long_services(n, expected_chunks):
    points = [(53.0 + i * 0.001, -2.0) for i in range(n)]
    chunks = valhalla._chunks(points, valhalla.MAX_LOCATIONS, valhalla.CHUNK_OVERLAP)
    assert len(chunks) == expected_chunks
    # Consecutive chunks must share an endpoint, or the stitched route has a gap.
    for a, b in zip(chunks, chunks[1:], strict=False):
        assert a[-1] == b[0]
    # Every point must appear somewhere.
    assert {p for c in chunks for p in c} == set(points)


def _chain_m(points):
    return sum(osm.haversine_m(a, b) for a, b in zip(points, points[1:], strict=False))


@pytest.mark.parametrize(("n", "expected_chunks"), [(40, 1), (41, 2), (80, 3)])
def test_the_distance_bound_changes_nothing_where_nothing_was_wrong(n, expected_chunks):
    """A dense service is bounded by the location count, as it always was.

    Forty stops of a city route span a couple of kilometres, so the distance ceiling
    is never in play and the chunking must come out identical -- the same chunks, not
    merely the same number of them."""
    points = [(53.0 + i * 0.001, -2.0) for i in range(n)]  # ~111 m apart
    plain = valhalla._chunks(points, valhalla.MAX_LOCATIONS, valhalla.CHUNK_OVERLAP)
    bounded = valhalla._chunks(
        points, valhalla.MAX_LOCATIONS, valhalla.CHUNK_OVERLAP, valhalla.MAX_CHUNK_M
    )
    assert bounded == plain
    assert len(bounded) == expected_chunks


def test_a_chunk_inside_the_location_count_still_splits_on_distance():
    """Forty coach stops are half the country. The count bound cannot see that, and
    Valhalla refuses the request with error 154 when it happens."""
    points = [(51.0 + i * 0.09, -2.0) for i in range(40)]  # ~10 km apart, ~390 km total
    assert len(valhalla._chunks(points, valhalla.MAX_LOCATIONS, 1)) == 1

    chunks = valhalla._chunks(points, valhalla.MAX_LOCATIONS, 1, valhalla.MAX_CHUNK_M)
    assert len(chunks) > 1
    assert all(_chain_m(c) <= valhalla.MAX_CHUNK_M for c in chunks)
    for a, b in zip(chunks, chunks[1:], strict=False):
        assert a[-1] == b[0]
    assert chunks[0] + [p for c in chunks[1:] for p in c[1:]] == points


def test_a_single_leg_past_the_ceiling_is_not_split_further():
    """A chunk always holds two points, or the chunker would never advance. Such a
    leg is Valhalla's to refuse, and `config.MAX_STOP_GAP_M` is what keeps one from
    ever being handed over."""
    points = [(51.0, -2.0), (54.0, -2.0), (54.001, -2.0)]  # first leg ~333 km
    chunks = valhalla._chunks(points, valhalla.MAX_LOCATIONS, 1, valhalla.MAX_CHUNK_M)
    assert chunks[0] == points[:2]
    assert _chain_m(chunks[0]) > valhalla.MAX_CHUNK_M


def test_the_ceiling_is_derived_from_valhallas_own_cap():
    """Not a number of its own: raising Valhalla's limit must move this one, and the
    longest leg the matcher will accept is the same arithmetic."""
    ceiling = valhalla.MAX_CHUNK_M
    assert ceiling < config.VALHALLA_MAX_DISTANCE_M
    assert ceiling == pytest.approx(config.MAX_STOP_GAP_M)


class _FakeGraph:
    """Answers /route and /trace_attributes over a straight line of points.

    The route hands back the locations it was given, which is what makes the
    stitched shape exactly the input. The trace hands back one edge per segment,
    identified by where the segment starts -- and, when a part does not begin at the
    start of the line, it repeats the segment the part's first point sits inside.
    That repeat is the real behaviour the merge has to cope with: edge_walk reports
    the whole edge a boundary falls into, at the end of one part and the start of
    the next.
    """

    def __init__(self, points):
        self.points = points
        self.traces = []
        self.routes = []

    def post(self, path, payload):
        if path == "route":
            locs = [(loc["lat"], loc["lon"]) for loc in payload["locations"]]
            self.routes.append(locs)
            return {"trip": {"legs": [{"shape": polyline.encode(locs, 6)}]}}
        shape = [(p["lat"], p["lon"]) for p in payload["shape"]]
        self.traces.append(shape)
        first = min(
            range(len(self.points)),
            key=lambda i: abs(self.points[i][0] - shape[0][0]),
        )
        begin = max(first - 1, 0)
        edges = [
            {"id": 1000 + i, "way_id": 2000 + i, "length": 0.1}
            for i in range(begin, first + len(shape) - 1)
        ]
        return {"shape": polyline.encode(shape, 6), "edges": edges}


def _fake_client(points):
    client = valhalla.Client("http://valhalla.invalid/")
    graph = _FakeGraph(points)
    client._post = graph.post  # type: ignore[method-assign]
    return client, graph


def test_a_long_walk_is_split_and_stitched_back():
    """The trace cap is measured along the routed road, which is longer than the stop
    chain that produced it, so clearing the route cap does not clear this one."""
    points = [(51.0 + i * 0.05, -2.0) for i in range(61)]  # ~5.6 km apart, ~333 km
    client, graph = _fake_client(points)
    m = client.match_stops(points)

    assert len(graph.traces) > 1
    assert all(_chain_m(t) <= valhalla.MAX_CHUNK_M for t in graph.traces)
    # One edge per segment of the line, in order, with the seam counted once.
    assert [e.edge_id for e in m.edges] == [1000 + i for i in range(len(points) - 1)]
    assert m.road_m == pytest.approx(100.0 * (len(points) - 1))
    assert m.confidence == 0.0


def test_a_short_walk_is_one_call():
    points = [(51.0 + i * 0.001, -2.0) for i in range(20)]
    client, graph = _fake_client(points)
    m = client.match_stops(points)
    assert len(graph.routes) == 1
    assert len(graph.traces) == 1
    assert [e.edge_id for e in m.edges] == [1000 + i for i in range(len(points) - 1)]


# -- location types ---------------------------------------------------------
#
# `break_through` forbids a U-turn at a stop, which is right for a bus passing one
# and wrong for a bus turning round at one. These fix which stops get relaxed.


def types(points):
    return "".join("B" if t == "break" else "." for t in valhalla._location_types(points))


def test_an_ordinary_pattern_keeps_break_through_everywhere():
    """The default must not move: it is what makes edge_walk recover edges exactly."""
    points = [(53.4800 + i * 0.005, -2.2450) for i in range(8)]
    assert types(points) == "........"


def test_an_out_and_back_spur_relaxes_the_stops_between_the_two_visits():
    """The route reverses somewhere past the far end of the spur, so those stops
    must be allowed to turn round. The two visits themselves are passed through."""
    points = [
        (53.4800, -2.2450),
        (53.4850, -2.2450),  # out
        (53.4900, -2.2450),  # spur
        (53.4930, -2.2450),  # far end of the spur
        (53.48502, -2.2450),  # back within 2 m of index 1
        (53.4750, -2.2450),
    ]
    assert types(points) == "..BB.."


def test_a_stop_served_twice_is_recognised_at_the_real_separation():
    """Service 86B's two Montgomeryshire Infirmary stops are separate NaPTAN ids
    28 m apart, so an exact coordinate test finds neither of them."""
    points = [
        (52.51839, -3.31595),  # Commercial Street
        (52.52060, -3.31472),  # Montgomeryshire Infirmary, outbound
        (52.52112, -3.31678),  # Bryn Lane
        (52.52205, -3.31632),  # Bryn Meadows
        (52.52062, -3.31431),  # Montgomeryshire Infirmary again, 28 m away
        (52.52200, -3.31044),  # Brynglas Close North
    ]
    assert types(points) == "..BB.."


def test_a_circular_ending_where_it_started_is_not_a_reversal():
    """Otherwise every circular in the country relaxes end to end."""
    points = [
        (53.4800, -2.2450),
        (53.4850, -2.2450),
        (53.4900, -2.2450),
        (53.4800, -2.2450),
    ]
    assert types(points) == "...."


def test_adjacent_stops_at_the_same_place_are_two_kerbs_not_a_turn():
    points = [
        (53.4800, -2.2450),
        (53.48018, -2.2450),
        (53.4900, -2.2450),
        (53.4700, -2.2450),
    ]
    assert types(points) == "...."


@pytest.mark.parametrize(("dlat", "expected"), [(0.0004, ".B.."), (0.0006, "....")])
def test_the_revisit_radius_is_a_distance_not_an_exact_match(dlat, expected):
    """0.0004 degrees of latitude is 45 m and 0.0006 is 67 m, either side of 50."""
    points = [
        (53.4800, -2.2450),
        (53.4850, -2.2450),
        (53.4800 + dlat, -2.2450),
        (53.4700, -2.2450),
    ]
    assert types(points) == expected


def test_short_patterns_need_no_scan():
    assert valhalla._location_types([(53.48, -2.245), (53.49, -2.245)]) == [
        "break_through",
        "break_through",
    ]


class _Recorder(valhalla.Client):
    """A client that answers a route request with the straight line it was given."""

    def __init__(self):
        super().__init__(base_url="http://test/", timeout=1.0)
        self.payloads = []

    def _post(self, path, payload):
        self.payloads.append(payload)
        pts = [(loc["lat"], loc["lon"]) for loc in payload["locations"]]
        return {"trip": {"legs": [{"shape": polyline.encode(pts, 6)}]}}


def test_location_types_stay_aligned_with_their_stops_across_chunk_boundaries():
    """Types are decided over the whole pattern and the request is chunked, so a
    revisit that straddles a chunk boundary is the case that can slip."""
    points = [(53.4800 + i * 0.005, -2.2450) for i in range(45)]
    points[42] = (points[39][0] + 0.0001, points[39][1])  # a revisit spanning the cut
    client = _Recorder()
    client.route_shape(points)

    assert len(client.payloads) == 2
    rebuilt = list(client.payloads[0]["locations"])
    for payload in client.payloads[1:]:
        # Chunks overlap by one location; the repeat must carry the same type.
        assert payload["locations"][0] == rebuilt[-1]
        rebuilt.extend(payload["locations"][valhalla.CHUNK_OVERLAP :])

    expected = valhalla._location_types(points)
    assert [(loc["lat"], loc["lon"]) for loc in rebuilt] == points
    assert [loc["type"] for loc in rebuilt] == expected
    assert expected[40:42] == ["break", "break"]


def test_thinning_preserves_the_endpoints():
    shape = [(53.0 + i * 0.0001, -2.0) for i in range(5000)]
    thinned = valhalla._thin(shape, 2000)
    assert len(thinned) <= 2001
    assert thinned[0] == shape[0]
    assert thinned[-1] == shape[-1]


def test_short_shapes_are_not_thinned():
    shape = [(53.0, -2.0), (53.1, -2.0)]
    assert valhalla._thin(shape, 2000) is shape


def test_confidence_score_is_requested():
    """filters.action=include is a strict allowlist. Omitting confidence_score
    makes every map_snap match score 0.0 and get rejected as low confidence --
    a bad request that looks exactly like bad matching."""
    assert "confidence_score" in valhalla.EDGE_ATTRS


def test_the_trace_request_never_asks_for_the_osrm_format():
    """The one reason this project uses Valhalla is that `edge.way_id` comes back
    without a custom graph build, and `format=osrm` drops the way ids silently: the
    response still parses, still holds edges, and the whole dataset keys on an
    identity that is no longer in it. So the request must carry no `format` at all,
    which is what leaves Valhalla answering in its native shape."""
    posted = {}

    def record(path, payload):
        posted[path] = payload
        return {}

    client = valhalla.Client("http://valhalla.test/")
    client._post = record  # type: ignore[method-assign]
    client.trace_attributes([(53.0, -2.0), (53.1, -2.0)])
    assert "format" not in posted["trace_attributes"]
    assert posted["trace_attributes"]["filters"]["attributes"] == valhalla.EDGE_ATTRS


def test_missing_confidence_score_raises_rather_than_defaulting():
    stripped = {k: v for k, v in RESPONSE.items() if k != "confidence_score"}
    with pytest.raises(valhalla.ValhallaError, match="confidence_score"):
        valhalla._to_match(stripped, source="shape")


def test_edge_walk_needs_no_confidence_score():
    """The stops path scores nothing by construction, so its absence is normal."""
    stripped = {k: v for k, v in RESPONSE.items() if k != "confidence_score"}
    assert valhalla._to_match(stripped, source="stops").confidence == 0.0


# -- what a failure means ---------------------------------------------------


def _client(session) -> valhalla.Client:
    c = valhalla.Client("http://valhalla.test/")
    c.session = session
    return c


def _valhalla_body(error_code: int, message: str) -> dict:
    """Valhalla's error envelope. Every one of these comes back as HTTP 400, which
    is exactly why the code and not the status is what carries the meaning."""
    return {
        "error_code": error_code,
        "error": message,
        "status_code": 400,
        "status": "Bad Request",
    }


@pytest.mark.parametrize("code", sorted(valhalla.NO_PATH_CODES))
def test_every_no_path_code_is_no_route(code):
    """Including 444, which is map_snap refusing a sea crossing, and 154, which is
    a stop chain longer than Valhalla will route. Neither answers differently on a
    second attempt, so both belong with 442 rather than with the retryable set.

    442 is the code that means no path, and permanence is decided on the numeric
    code and never on the message. Valhalla's "No path could be found for input" is
    a third party's English, free to change between releases, and a mismatch there
    would file every permanent no-path as a transient error instead."""
    c = _client(
        builders.FakeSession(
            builders.FakeResponse(_valhalla_body(code, "prose that may change"), 400)
        )
    )
    with pytest.raises(valhalla.NoRoute):
        c.trace_attributes([(53.0, -2.0), (53.1, -2.0)])


def test_a_malformed_request_stays_a_plain_error():
    """125 is "No costing method found" -- our bug, permanent, and nothing to do
    with whether a road exists."""
    c = _client(
        builders.FakeSession(
            builders.FakeResponse(_valhalla_body(125, "No costing method found"), 400)
        )
    )
    with pytest.raises(valhalla.ValhallaError) as exc:
        c.trace_attributes([(53.0, -2.0), (53.1, -2.0)])
    assert not isinstance(exc.value, valhalla.NoRoute)


@pytest.mark.parametrize(
    "exc",
    [
        requests.ConnectionError("Connection refused"),
        requests.Timeout("Read timed out (read timeout=120.0)"),
        requests.exceptions.ChunkedEncodingError("Remote end closed connection"),
    ],
)
def test_transport_faults_are_their_own_exception(exc):
    c = _client(builders.FakeSession(raises=exc))
    with pytest.raises(valhalla.TransportError) as raised:
        c.trace_attributes([(53.0, -2.0), (53.1, -2.0)])
    # The class name is kept, because "which fault" is the whole diagnosis later.
    assert type(exc).__name__ in str(raised.value)


def test_a_transport_fault_is_not_a_valhalla_error():
    """match_stops retries an edge_walk failure as a map_snap on `except
    ValhallaError`. A refused connection must not go round that loop, and must not
    be recorded as if the matcher had been told something about the pattern."""
    assert not issubclass(valhalla.TransportError, valhalla.ValhallaError)


def test_valhalla_shutting_down_is_a_transport_fault():
    """Code 102/203/402, HTTP 503: the server is restarting. This is the case that
    put 227 connection failures into one national run."""
    body = {"error_code": 402, "error": "The service is shutting down", "status_code": 503}
    c = _client(builders.FakeSession(builders.FakeResponse(body, 503)))
    with pytest.raises(valhalla.TransportError):
        c.trace_attributes([(53.0, -2.0), (53.1, -2.0)])


def test_an_unparseable_body_still_yields_a_message():
    c = _client(
        builders.FakeSession(builders.FakeResponse("<html>gateway said no</html>", 400))
    )
    with pytest.raises(valhalla.ValhallaError, match="gateway said no"):
        c.trace_attributes([(53.0, -2.0), (53.1, -2.0)])
