"""Valhalla client: turn a sequence of points into OSM way identities.

Why Valhalla and not OSRM or GraphHopper: only Valhalla returns OSM way ids from
map matching without a custom graph build. ``/trace_attributes`` exposes
``edge.way_id`` directly. OSRM discards way ids at extract time and can only give
back node ids; GraphHopper needs ``osm_way_id`` added as an encoded value and the
graph reimported.

One trap worth knowing: way ids appear only in Valhalla's *native* response.
Asking for ``format=osrm`` silently drops them.

Two matching strategies, chosen per pattern:

``shape``  The operator supplied road geometry in the GTFS feed (true for about
           48% of trips nationally, and all-or-nothing per operator). The trace is
           already dense and road-following, so map_snap has an easy job.

``stops``  No geometry, so the stop coordinates are the only input. Bus stops sit
           tens to hundreds of metres apart, which is too sparse for map_snap to
           reconstruct turns reliably. Instead the stops are routed through with
           bus costing to synthesise road geometry, and that dense result is then
           walked to recover edges exactly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import requests

from . import config, logs, polyline

log = logs.get("valhalla")

# The fields we need, and nothing else. Valhalla returns a large object per edge by
# default; at national scale the unwanted fields dominate transfer time.
#
# `filters.action=include` is a strict allowlist: anything not named here is absent
# from the response entirely, not null. `confidence_score` is top-level rather than
# per-edge and is easy to forget for that reason -- leaving it out made every
# map_snap match score 0.0 and be rejected as low confidence, which looked like bad
# matching rather than a bad request. Hence the assertion in _to_match.
EDGE_ATTRS = [
    "edge.way_id",
    "edge.id",
    "edge.length",
    "edge.names",
    "edge.road_class",
    "edge.begin_shape_index",
    "edge.end_shape_index",
    "shape",
    "matched.point",
    "confidence_score",
]

# Valhalla caps locations per route request (bus costing defaults to 50). Long
# services run past that, so they are routed in overlapping chunks and stitched.
MAX_LOCATIONS = 40
CHUNK_OVERLAP = 1


class ValhallaError(RuntimeError):
    pass


class NoRoute(ValhallaError):
    """Valhalla could not connect the points at all -- not a transient failure."""


class TransportError(RuntimeError):
    """The request never got an answer: connection refused, timed out, cut off.

    Deliberately *not* a ValhallaError. Valhalla answering "no path" is a fact
    about the input and is permanent; a request that never arrived is a fact about
    the network at that moment and says nothing about the pattern. The two must not
    share a base class, because ``match_stops`` retries an edge_walk failure as a
    map_snap on ``except ValhallaError`` -- a second call down a dead socket is
    pointless, and it would relabel the fault as one of the matcher's own.
    """


# Valhalla answers every one of these with HTTP 400, so the HTTP status says
# nothing; the discriminator is `error_code` in the JSON body (src/exceptions.cc in
# the Valhalla tree, serialised by `serialize_error`). Each of these is a statement
# that no path exists for this input, and none of them will answer differently on a
# second attempt:
#
#   154  Path distance exceeds the max distance limit
#   170  Locations are in unconnected regions
#   171  No suitable edges near location
#   172  Exceeded breakage distance for all pairs
#   440  Cannot reach destination - too far from a transit stop
#   441  Location is unreachable
#   442  No path could be found for input
#   443  Exact route match algorithm failed to find path   (edge_walk)
#   444  Map Match algorithm failed to find path           (map_snap)
#
# This used to be `"no route" in body.lower()`, which matched none of the prose
# Valhalla actually sends, so NoRoute was never raised and every permanent no-path
# was filed as a transient `error` instead. Match on the code, never on the words:
# the message text is a third party's English and is free to change.
NO_PATH_CODES = frozenset({154, 170, 171, 172, 440, 441, 442, 443, 444})

# The one ValhallaError this module raises that did not come off the wire. Named
# because `match.reclassify_transport_faults` has to tell it apart from a genuine
# transport fault when reading old rows back.
NO_SCORE_MESSAGE = "trace_attributes returned no confidence_score"


@dataclass
class Edge:
    edge_id: int
    way_id: int
    length_m: float
    road_name: str | None
    road_class: str | None
    geom: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class Match:
    edges: list[Edge]
    confidence: float
    road_m: float
    source: str


class Client:
    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        self.base = (base_url or config.VALHALLA_URL).rstrip("/") + "/"
        self.timeout = timeout or config.VALHALLA_TIMEOUT
        self.session = requests.Session()

    # -- transport ----------------------------------------------------------

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = urljoin(self.base, path)
        try:
            r = self.session.post(url, json=payload, timeout=self.timeout)
            if r.status_code < 400:
                # A body that will not parse is a truncated read, not a bad match.
                return r.json()
        except requests.RequestException as exc:
            raise TransportError(f"{type(exc).__name__}: {exc}") from exc

        body, code = _valhalla_error(r)
        # A no-path code is a property of the input: recorded, never retried.
        if code in NO_PATH_CODES:
            raise NoRoute(f"{r.status_code}: {body}")
        # 5xx is Valhalla itself unable to answer -- it is shutting down (codes 102,
        # 203, 402), reloading tiles (446), or crashed. Nothing about the pattern.
        if r.status_code >= 500:
            raise TransportError(f"{r.status_code}: {body}")
        raise ValhallaError(f"{r.status_code}: {body}")

    def healthy(self) -> bool:
        try:
            r = self.session.get(urljoin(self.base, "status"), timeout=10)
            return r.ok
        except requests.RequestException:
            return False

    def graph_id(self) -> str | None:
        """A label for the graph build these edge ids belong to.

        ``edge.id`` is a Valhalla GraphId: stable within one build, meaningless
        across builds. Every pattern_edges row in the database is keyed on it, so
        pointing a resumed run at a rebuilt graph produces geometry that looks fine
        and is wrong. This is the value the run is pinned to.

        ``tileset_last_modified`` is what actually changes on a rebuild; version
        alone would not catch a rebuild of the same Valhalla release. Returns None
        if the endpoint reports neither, because a guard that cannot tell builds
        apart should not pretend to.
        """
        try:
            r = self.session.get(urljoin(self.base, "status"), timeout=10)
            if not r.ok:
                return None
            data = r.json()
        except (requests.RequestException, ValueError):
            return None
        stamp = data.get("tileset_last_modified")
        if stamp is None:
            return None
        return f"{data.get('version', '?')}/{stamp}"

    # -- primitives ---------------------------------------------------------

    def route_shape(self, points: list[tuple[float, float]]) -> list[tuple[float, float]]:
        """Road geometry through every point, in order.

        ``break_through`` forces the route to visit each stop without permitting a
        U-turn there, which is what a bus actually does. Plain ``through`` lets
        Valhalla pass the stop on either side and produces jitter at termini.
        """
        shape: list[tuple[float, float]] = []
        for chunk in _chunks(points, MAX_LOCATIONS, CHUNK_OVERLAP):
            payload = {
                "locations": [
                    {"lat": lat, "lon": lon, "type": "break_through"} for lat, lon in chunk
                ],
                "costing": "bus",
                "directions_options": {"units": "kilometers"},
                "shape_format": "polyline6",
            }
            data = self._post("route", payload)
            legs = data.get("trip", {}).get("legs", [])
            if not legs:
                raise NoRoute("route returned no legs")
            for leg in legs:
                pts = polyline.decode(leg["shape"], 6)
                # Chunks overlap by one location, so drop the repeated first point.
                if shape and pts and pts[0] == shape[-1]:
                    pts = pts[1:]
                shape.extend(pts)
        return shape

    def trace_attributes(
        self, shape: list[tuple[float, float]], shape_match: str = "map_snap"
    ) -> dict[str, Any]:
        payload = {
            "shape": [{"lat": lat, "lon": lon} for lat, lon in shape],
            "costing": "bus",
            "shape_match": shape_match,
            "filters": {"attributes": EDGE_ATTRS, "action": "include"},
        }
        return self._post("trace_attributes", payload)

    # -- the two strategies -------------------------------------------------

    def match_shape(self, shape: list[tuple[float, float]]) -> Match:
        """Snap supplied road geometry onto the graph."""
        data = self.trace_attributes(_thin(shape), "map_snap")
        return _to_match(data, source="shape")

    def match_stops(self, stops: list[tuple[float, float]]) -> Match:
        """Synthesise road geometry between stops, then recover its edges.

        ``edge_walk`` is used for the second call rather than ``map_snap`` because
        the input is Valhalla's own output: the points lie exactly on graph edges,
        so an exact walk is both faster and free of snapping error.
        """
        road = self.route_shape(stops)
        if len(road) < 2:
            raise NoRoute("routed shape too short")
        try:
            data = self.trace_attributes(road, "edge_walk")
        except ValhallaError:
            # edge_walk is strict and refuses on the smallest discontinuity, which
            # chunk stitching can introduce. map_snap tolerates it. A TransportError
            # is not a ValhallaError and so is not caught here: there is nothing for
            # a second algorithm to fix about a connection that was refused.
            data = self.trace_attributes(_thin(road), "map_snap")
        m = _to_match(data, source="stops")
        # A synthesised route is a guess about which roads the bus takes, not an
        # observation of it. Confidence from edge_walk is meaningless (it is always
        # 1.0 by construction), so it is not reported as if it were measured.
        m.confidence = 0.0 if m.source == "stops" else m.confidence
        return m


# -- helpers ----------------------------------------------------------------


def _to_match(data: dict[str, Any], source: str) -> Match:
    # A map_snap response without a score means the attribute filter dropped it, not
    # that the match was poor. Defaulting to 0.0 there silently rejects every good
    # match, so fail loudly instead. edge_walk scores nothing, hence shape-only.
    if source == "shape" and "confidence_score" not in data:
        raise ValhallaError(
            f"{NO_SCORE_MESSAGE}; 'confidence_score' must be listed in filters.attributes"
        )

    shape = polyline.decode(data["shape"], 6) if data.get("shape") else []
    edges: list[Edge] = []
    total = 0.0

    for e in data.get("edges", []):
        way_id = e.get("way_id")
        edge_id = e.get("id")
        if way_id is None or edge_id is None:
            continue  # transit or synthesised edge; no OSM identity to record
        length_m = float(e.get("length", 0.0)) * 1000.0  # Valhalla reports km
        names = e.get("names") or []
        begin = e.get("begin_shape_index")
        end = e.get("end_shape_index")
        sliceable = shape and begin is not None and end is not None
        geom = shape[begin : end + 1] if sliceable else []
        edges.append(
            Edge(
                edge_id=int(edge_id),
                way_id=int(way_id),
                length_m=length_m,
                road_name=names[0] if names else None,
                road_class=e.get("road_class"),
                geom=geom,
            )
        )
        total += length_m

    return Match(
        edges=edges,
        confidence=float(data.get("confidence_score", 0.0)),
        road_m=total,
        source=source,
    )


def _chunks(
    items: list[tuple[float, float]], size: int, overlap: int
) -> list[list[tuple[float, float]]]:
    if len(items) <= size:
        return [items]
    out = []
    start = 0
    step = size - overlap
    while start < len(items) - 1:
        out.append(items[start : start + size])
        start += step
    return out


def _thin(
    shape: list[tuple[float, float]], max_points: int = 2000
) -> list[tuple[float, float]]:
    """Cap the trace length for map_snap.

    Meili's cost is superlinear in point count and operator shapes run to 3,700
    points. Uniform thinning keeps the geometry's character while bounding the work;
    it is only ever applied to already-dense road geometry, so no turn is lost.
    """
    if len(shape) <= max_points:
        return shape
    step = len(shape) / max_points
    thinned = [shape[int(i * step)] for i in range(max_points)]
    if thinned[-1] != shape[-1]:
        thinned.append(shape[-1])
    return thinned


def _valhalla_error(r: requests.Response) -> tuple[str, int | None]:
    """The error body, and Valhalla's own error code if it sent one.

    The code is the whole point: every failure Valhalla reports arrives as HTTP
    400, and only ``error_code`` says whether it means "no path exists here" or
    "your request was malformed".
    """
    try:
        data = r.json()
    except ValueError:
        return r.text[:500], None
    code = data.get("error_code") if isinstance(data, dict) else None
    return json.dumps(data)[:500], code if isinstance(code, int) else None
