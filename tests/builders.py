"""Row and payload builders shared across the suite.

Importable as `builders` because `pyproject.toml` puts `tests` on `pythonpath`.
Anything needing `tmp_path` or `monkeypatch` is a fixture in `conftest` instead.

The edge builders name their columns. Six test files used to spell out an
eleven-column positional `INSERT INTO edges`, so adding a column to the schema
broke every one of them for a reason none of them was about.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from wayfare import osm, valhalla

# One window over Cardiff, wide enough to hold the edges built below. Callers that
# need a `Bounds` wrap it; `server` wants the same four numbers as a query string.
WINDOW = (-3.30, 51.40, -3.10, 51.60)
WINDOW_Q = "-3.30,51.40,-3.10,51.60"

_EDGE_COLS = (
    "edge_id, way_id, road_name, road_class, length_m, "
    "lon_e6, lat_e6, min_lon_e6, min_lat_e6, max_lon_e6, max_lat_e6"
)


def insert_edge(
    con: Any,
    edge_id: int,
    *,
    way_id: int = 1,
    lon_e6: int = 0,
    lat_e6: int = 51480000,
    span_e6: int = 0,
    points: Sequence[tuple[int, int]] | None = None,
    geometry: bool = True,
    road_name: str | None = "R",
    road_class: str | None = "secondary",
    length_m: float = 100.0,
) -> None:
    """One east-west edge from (`lon_e6`, `lat_e6`) running `span_e6` east.

    `span_e6=0` collapses the geometry to a single point, so the edge sits in one
    Z-order cell and the clustering tests can name it. `points` overrides all of
    that with an explicit `(lon_e6, lat_e6)` run, in the edge's own direction --
    which matters where chaining follows direction. `geometry=False` writes the
    geometry and bbox columns NULL, the case `publish` has to skip.
    """
    if not geometry:
        lons = lats = None
        box: list[int | None] = [None, None, None, None]
    else:
        if points is not None:
            lons, lats = [p[0] for p in points], [p[1] for p in points]
        else:
            lons = [lon_e6, lon_e6 + span_e6] if span_e6 else [lon_e6]
            lats = [lat_e6] * len(lons)
        box = [min(lons), min(lats), max(lons), max(lats)]
    con.execute(
        f"INSERT INTO edges ({_EDGE_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [edge_id, way_id, road_name, road_class, length_m, lons, lats, *box],
    )


def insert_services(
    con: Any,
    edge_id: int,
    services: Sequence[str | tuple[str, str]] = ("42",),
    *,
    agency: str = "OP1",
    n_patterns: int = 1,
    n_trips: int | Sequence[int] = 0,
) -> None:
    """A row per service over `edge_id`.

    A service is a name, or a `(name, agency)` pair where the operator matters.
    `n_trips` is one value for all of them, or one per service.
    """
    trips = [n_trips] * len(services) if isinstance(n_trips, int) else list(n_trips)
    for service, weekly in zip(services, trips, strict=True):
        name, operator = service if isinstance(service, tuple) else (service, agency)
        con.execute(
            "INSERT INTO edge_services (edge_id, short_name, agency_id, n_patterns, "
            "n_trips) VALUES (?, ?, ?, ?, ?)",
            [edge_id, name, operator, n_patterns, weekly],
        )


def insert_pattern(
    con: Any,
    pattern_id: int,
    *,
    mode: str = "bus",
    feed: str = "F1",
    route_id: str = "R",
    agency_id: str = "OP1",
    short_name: str | None = None,
    direction: int = 0,
    n_stops: int = 2,
    n_trips: int = 10,
    last_seen: str | None = "",
) -> None:
    """`last_seen=None` retires the pattern, which is how `osmroutes` withdraws one."""
    con.execute(
        "INSERT INTO patterns (pattern_id, route_id, agency_id, short_name, direction, "
        "n_stops, n_trips, mode, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            pattern_id,
            route_id,
            agency_id,
            short_name if short_name is not None else f"S{pattern_id}",
            direction,
            n_stops,
            n_trips,
            mode,
            feed,
            feed if last_seen == "" else last_seen,
        ],
    )


# --- OSM route relations -----------------------------------------------------
#
# Two shapes, because two stages read them at different depths: `osm.Relation`
# objects for the code downstream of parsing, and raw Overpass JSON for the parser
# itself, where a member role or a misplaced tag is half of what is under test.


def way(way_id: int, points: list[tuple[float, float]]) -> osm.Way:
    return osm.Way(way_id, tuple(points))


def stop(node_id: int, name: str, lat: float, lon: float) -> osm.Stop:
    return osm.Stop(node_id, name, lat, lon)


def relation(
    relation_id: int = 1,
    route: str = "train",
    name: str = "Test Line",
    ways: list[osm.Way] | None = None,
    stops: list[osm.Stop] | None = None,
    tags: dict[str, str] | None = None,
) -> osm.Relation:
    """Two ways joining end to end, named by two stops: the shape that passes the gate."""
    if ways is None:
        ways = [
            way(10, [(51.0, -1.0), (51.1, -1.0)]),
            way(11, [(51.1, -1.0), (51.2, -1.0)]),
        ]
    if stops is None:
        stops = [
            stop(100, "Alpha Rail Station", 51.0, -1.0),
            stop(101, "Beta Rail Station", 51.2, -1.0),
        ]
    return osm.Relation(
        relation_id=relation_id,
        route=route,
        name=name,
        ways=tuple(ways),
        stops=tuple(stops),
        tags={"route": route, "name": name, **(tags or {})},
    )


def broken_relation(**kwargs: Any) -> osm.Relation:
    """Two ways joining at neither end. A break draws confident track across a gap
    no service crosses, so every stage that reads a relation has to refuse it."""
    return relation(
        ways=[
            way(10, [(51.0, -1.0), (51.1, -1.0)]),
            way(11, [(52.0, -1.0), (52.1, -1.0)]),
        ],
        **kwargs,
    )


def member_way(way_id: int, pts: list[tuple[float, float]], role: str = "") -> dict:
    return {
        "type": "way",
        "ref": way_id,
        "role": role,
        "geometry": [{"lat": la, "lon": lo} for la, lo in pts],
    }


def member_stop(node_id: int, role: str = "stop") -> dict:
    return {"type": "node", "ref": node_id, "role": role}


def node(node_id: int, name: str, at: tuple[float, float]) -> dict:
    return {
        "type": "node",
        "id": node_id,
        "lat": at[0],
        "lon": at[1],
        "tags": {"name": name, "railway": "station"},
    }


def overpass(
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


# --- HTTP --------------------------------------------------------------------


class FakeResponse:
    """Enough of `requests.Response` for the download and Overpass paths."""

    def __init__(
        self,
        body: bytes | str | dict = b"",
        status: int = 200,
        headers: dict[str, str] | None = None,
    ):
        if isinstance(body, dict):
            body = json.dumps(body)
        self.content = body.encode() if isinstance(body, str) else body
        self.status_code = status
        self.headers = headers or {}
        self.text = self.content.decode(errors="replace")
        self.ok = status < 400

    def json(self) -> Any:
        return json.loads(self.content)

    def iter_content(self, chunk_size: int = 8192):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i : i + chunk_size]

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class FakeSession:
    """A `requests.Session` answering every request with one canned response, or
    raising. `calls` is what the caching tests read: a cache hit must not post."""

    def __init__(
        self,
        response: FakeResponse | None = None,
        raises: Exception | None = None,
    ):
        self.response = response
        self.raises = raises
        self.calls = 0
        self.urls: list[str] = []

    def _answer(self, url: str) -> FakeResponse:
        self.calls += 1
        self.urls.append(url)
        if self.raises:
            raise self.raises
        assert self.response is not None, "FakeSession needs a response or a raises"
        return self.response

    def post(self, url: str, **_: object) -> FakeResponse:
        return self._answer(url)

    def get(self, url: str, **_: object) -> FakeResponse:
        return self._answer(url)


class FakeClient:
    """Stands in for Valhalla. Returns two edges for anything it is asked to match,
    which is enough to exercise checkpointing, resumption and aggregation."""

    def __init__(
        self,
        road_m: float = 1000.0,
        fail: Exception | None = None,
        graph: str | None = "valhalla-test/1",
    ):
        self.base = "http://fake-valhalla/"
        self.road_m = road_m
        self.fail = fail
        self.graph = graph
        self.calls: list[str] = []

    def healthy(self) -> bool:
        return True

    def graph_id(self) -> str | None:
        return self.graph

    def _match(self, source: str) -> valhalla.Match:
        self.calls.append(source)
        if self.fail:
            raise self.fail
        edges = [
            valhalla.Edge(
                1001,
                44556677,
                self.road_m / 2,
                "Oxford Road",
                "secondary",
                [(53.48, -2.245), (53.48, -2.240)],
            ),
            valhalla.Edge(
                1002,
                44556678,
                self.road_m / 2,
                "Oxford Road",
                "secondary",
                [(53.48, -2.240), (53.48, -2.235)],
            ),
        ]
        return valhalla.Match(edges, confidence=0.9, road_m=self.road_m, source=source)

    def match_shape(self, shape):
        return self._match("shape")

    def match_stops(self, stops):
        return self._match("stops")


# --- tippecanoe --------------------------------------------------------------


def argv_map(cmd: Sequence[str]) -> dict[str, Any]:
    """A tippecanoe argv as a mapping, so a band assertion names its flag.

    A flag whose next token is not itself a flag takes that token as its value; a
    repeated flag collects a list. Positional arguments land under `""`. Asserting
    on `cmd.index("-Z") + 1` instead made the tests break on argument order, which
    is the one thing about them that carries no meaning.
    """
    out: dict[str, Any] = {"": []}
    i = 0
    while i < len(cmd):
        token = cmd[i]
        if not token.startswith("-"):
            out[""].append(token)
            i += 1
            continue
        takes_value = i + 1 < len(cmd) and not cmd[i + 1].startswith("-")
        value = cmd[i + 1] if takes_value else True
        if token in out:
            existing = out[token]
            out[token] = (
                [*existing, value]
                if isinstance(existing, list)
                else [
                    existing,
                    value,
                ]
            )
        else:
            out[token] = value
        i += 2 if takes_value else 1
    return out
