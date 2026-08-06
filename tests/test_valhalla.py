from __future__ import annotations

import pytest

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
