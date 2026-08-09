from __future__ import annotations

import json

import pytest
import requests

from wayfare import polyline, valhalla

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
    [(10, 1), (40, 1), (41, 2), (79, 2), (80, 3)],
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


def test_missing_confidence_score_raises_rather_than_defaulting():
    stripped = {k: v for k, v in RESPONSE.items() if k != "confidence_score"}
    with pytest.raises(valhalla.ValhallaError, match="confidence_score"):
        valhalla._to_match(stripped, source="shape")


def test_edge_walk_needs_no_confidence_score():
    """The stops path scores nothing by construction, so its absence is normal."""
    stripped = {k: v for k, v in RESPONSE.items() if k != "confidence_score"}
    assert valhalla._to_match(stripped, source="stops").confidence == 0.0


# -- what a failure means ---------------------------------------------------


class _Session:
    """A requests.Session that answers with a canned response, or raises."""

    def __init__(self, response=None, raises=None):
        self.response = response
        self.raises = raises

    def post(self, url, json=None, timeout=None):
        if self.raises:
            raise self.raises
        return self.response


def _response(http_status: int, body: dict | str) -> requests.Response:
    r = requests.Response()
    r.status_code = http_status
    r._content = (json.dumps(body) if isinstance(body, dict) else body).encode()
    r.headers["Content-Type"] = "application/json"
    return r


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


def test_a_442_is_no_route_and_not_an_error():
    """The regression that mattered. `_post` used to test for the substring "no
    route", and Valhalla's 442 says "No path could be found for input" -- so
    NoRoute was never once raised, and every permanent no-path in every database
    built so far was filed as a transient error instead."""
    c = _client(
        _Session(_response(400, _valhalla_body(442, "No path could be found for input")))
    )
    with pytest.raises(valhalla.NoRoute):
        c.trace_attributes([(53.0, -2.0), (53.1, -2.0)])


@pytest.mark.parametrize("code", sorted(valhalla.NO_PATH_CODES))
def test_every_no_path_code_is_no_route(code):
    """Including 444, which is map_snap refusing a sea crossing, and 154, which is
    a stop chain longer than Valhalla will route. Neither answers differently on a
    second attempt, so both belong with 442 rather than with the retryable set."""
    c = _client(_Session(_response(400, _valhalla_body(code, "prose that may change"))))
    with pytest.raises(valhalla.NoRoute):
        c.trace_attributes([(53.0, -2.0), (53.1, -2.0)])


def test_a_malformed_request_stays_a_plain_error():
    """125 is "No costing method found" -- our bug, permanent, and nothing to do
    with whether a road exists."""
    c = _client(_Session(_response(400, _valhalla_body(125, "No costing method found"))))
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
    c = _client(_Session(raises=exc))
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
    c = _client(_Session(_response(503, body)))
    with pytest.raises(valhalla.TransportError):
        c.trace_attributes([(53.0, -2.0), (53.1, -2.0)])


def test_an_unparseable_body_still_yields_a_message():
    c = _client(_Session(_response(400, "<html>gateway said no</html>")))
    with pytest.raises(valhalla.ValhallaError, match="gateway said no"):
        c.trace_attributes([(53.0, -2.0), (53.1, -2.0)])
