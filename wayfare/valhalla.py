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
from math import asin, cos, radians, sin, sqrt
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
# And it caps the distance a request may cover, which is a different limit for a
# different reason: 40 stops of a city service span a few kilometres, 40 stops of a
# coach span the country. Both bounds hold at once, and the distance one is derived
# from Valhalla's own cap so that raising the cap moves it rather than leaving it
# stranded at a number nobody can place. See `config.VALHALLA_MAX_DISTANCE_M`.
MAX_CHUNK_M = config.VALHALLA_MAX_DISTANCE_M * config.VALHALLA_DISTANCE_HEADROOM
# Valhalla's `service_limits.trace.max_shape`. The distance bound always splits a
# synthesised road shape first -- it runs about 24 points per kilometre, so 180 km is
# some 4,300 points -- so this is a backstop rather than a working limit.
MAX_SHAPE_POINTS = 16_000
# The same sphere `gtfs._HAVERSINE` uses, so a chunk bound and a stored `span_m` are
# the same measurement of the same leg.
EARTH_RADIUS_M = 6_371_000.0


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
        for chunk in _chunks(points, MAX_LOCATIONS, CHUNK_OVERLAP, MAX_CHUNK_M):
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

        The walk is chunked by distance in its own right, not merely inherited from
        the routing. Road is longer than the straight line it follows -- 1.26x and
        1.58x on the long Welsh patterns -- so a stop chain that cleared the route cap
        comfortably can produce a shape that does not clear the trace cap. Splitting
        here is exact rather than a bound, because the shape's length is known before
        the call is made.
        """
        road = self.route_shape(stops)
        if len(road) < 2:
            raise NoRoute("routed shape too short")
        # A synthesised route is a guess about which roads the bus takes, not an
        # observation of it. Confidence from edge_walk is meaningless (it is always
        # 1.0 by construction), so it is not reported as if it were measured.
        out = Match(edges=[], confidence=0.0, road_m=0.0, source="stops")
        for part in _chunks(road, MAX_SHAPE_POINTS, CHUNK_OVERLAP, MAX_CHUNK_M):
            for e in self._walk(part).edges:
                # Parts overlap by a point, so a boundary falling inside an edge puts
                # that edge at the end of one walk and the start of the next. Keeping
                # the first occurrence loses the tail of one edge's geometry and no
                # edge identity; counting it twice would inflate road_m instead.
                if out.edges and e.edge_id == out.edges[-1].edge_id:
                    continue
                out.edges.append(e)
                out.road_m += e.length_m
        return out

    def _walk(self, shape: list[tuple[float, float]]) -> Match:
        try:
            return _to_match(self.trace_attributes(shape, "edge_walk"), source="stops")
        except ValhallaError:
            # edge_walk is strict and refuses on the smallest discontinuity, which
            # chunk stitching can introduce. map_snap tolerates it. A TransportError
            # is not a ValhallaError and so is not caught here: there is nothing for
            # a second algorithm to fix about a connection that was refused.
            return _to_match(
                self.trace_attributes(_thin(shape), "map_snap"), source="stops"
            )


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


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = radians(a[0]), radians(a[1])
    lat2, lon2 = radians(b[0]), radians(b[1])
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * asin(sqrt(h))


def _chunks(
    items: list[tuple[float, float]],
    size: int,
    overlap: int,
    max_m: float | None = None,
) -> list[list[tuple[float, float]]]:
    """Split a point list into overlapping chunks, bounded by count and by length.

    Both bounds are Valhalla's and they answer different questions. `size` bounds the
    request: a route takes 50 locations and a trace 16,000 shape points. `max_m`
    bounds the ground the request covers, which the count cannot stand in for -- 40
    coach stops are half the country and 40 city stops are a suburb.

    A chunk always holds at least two points, so a single leg longer than `max_m`
    comes back whole and is refused by Valhalla rather than looping here. Nothing
    upstream should hand one over: `config.MAX_STOP_GAP_M` is that leg, measured
    against the same cap.
    """
    if len(items) < 2:
        return [items]
    out: list[list[tuple[float, float]]] = []
    start = 0
    while start < len(items) - 1:
        end = start
        run = 0.0
        while end + 1 < len(items) and end - start + 2 <= size:
            step = _haversine_m(items[end], items[end + 1])
            if max_m is not None and end > start and run + step > max_m:
                break
            run += step
            end += 1
        out.append(items[start : end + 1])
        if end == len(items) - 1:
            break  # the overlap would otherwise re-emit the tail for ever
        start = end + 1 - overlap
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
