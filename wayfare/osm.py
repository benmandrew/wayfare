"""OpenStreetMap route relations: ordered track for the modes that ship none.

The Underground, the DLR, London Trams, West Midlands Metro, Blackpool and the
Air-Rail Link all arrive in the timetable feed with a full stop sequence and no
geometry at all -- 1,417 of Great Britain's 1,525 metro patterns carry no
``shape_id``. What draws them is OpenStreetMap, and the thing that makes this
cheap is that a route relation is *already ordered*: its ``role=""`` way members
chain end to end into one continuous path, so there is no snapping, no shortest
path and no ambiguity to resolve. It is an ingestion job rather than a routing one.

``docs/data.md`` rejects OSM ``route=bus`` relations as a source for buses, on
coverage: 12,968 relations against a far larger route population. That argument
does not transfer here. Every one of the eleven Underground lines has a
``route_master``, as do the DLR, London Trams and the Elizabeth line, and the
relations chain with zero breaks over their whole length.

Four traps, every one of which looks like a data problem rather than a mistake:

* **Platform members must leave the way chain.** Leaving ``role=platform``,
  ``platform_entry_only`` and ``platform_exit_only`` in produces 11 to 25 spurious
  breaks per relation, which reads as broken mapping.
* **The Elizabeth line is ``route=train``, not ``route=subway``.** A mode filter
  written from the obvious names misses it. Hence `config.OSM_ROUTE_VALUES` is
  wide and the stop-sequence join is what actually decides.
* **Way tags are not a join key.** ``ref`` is on 2.4% of subway ways and carries
  signalling codes rather than line names; ``line`` reaches 62.1% and is
  multi-valued on shared track with inconsistent separators. Reach the ways
  through the relation, in member order, and never through way tags.
* **There is no ``naptan:AtcoCode`` on Underground stop nodes.** The join is by
  normalised name with a coordinate check, because the obvious identifier is not
  there to join on.

Overpass rather than the OSM API, because this has to *discover* relations over an
area rather than fetch ids it already knows. One request per run returns every
route relation in the region with its member geometry inline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from math import cos, radians, sqrt
from pathlib import Path
from typing import Any

import requests

from . import config, logs

log = logs.get("osm")

# One degree of latitude, near enough anywhere. Distances here are all local -- a
# stop against a station node, a way endpoint against the next way's -- so a plane
# through the local latitude is exact well past the precision any of it carries.
_M_PER_DEG_LAT = 111_320.0


class TransportError(RuntimeError):
    """The Overpass request never got an answer, so nothing was learned.

    Kept apart from a malformed response for the reason `valhalla.TransportError`
    is kept apart from `ValhallaError`: a refused connection is a fact about the
    network at that moment and is safe to redo, where a response that will not
    parse will not parse next time either.
    """


class OverpassError(RuntimeError):
    """Overpass answered, and the answer is unusable."""


@dataclass(frozen=True)
class Way:
    way_id: int
    points: tuple[tuple[float, float], ...]  # (lat, lon), in the relation's order


@dataclass(frozen=True)
class Stop:
    node_id: int
    name: str | None
    lat: float
    lon: float


@dataclass(frozen=True)
class Relation:
    relation_id: int
    route: str | None  # the `route` tag: subway | train | tram | light_rail | ...
    name: str | None
    ways: tuple[Way, ...]
    stops: tuple[Stop, ...]
    # The rest of the relation's tags. `trace` needs none of them -- it joins on the
    # stop sequence and nothing else -- but a relation used as a *service* rather
    # than as geometry needs its `operator` and `ref`, which are the only record of
    # whose line it is. Kept whole rather than as two fields so that reading a third
    # one later is not a schema change.
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class Chain:
    """A relation's ways walked in member order into one continuous path."""

    points: list[tuple[float, float]] = field(default_factory=list)
    way_ids: list[int] = field(default_factory=list)
    breaks: int = 0
    # The way each point came from, exactly parallel to `points`. `way_ids` says
    # which ways the chain is made of and this says *where* each one is, which is
    # the difference between recording what was drawn and being able to cut it up
    # again: a slice of the chain is a range of distances, and without this there is
    # no way back from that range to the ways under it.
    way_at: list[int] = field(default_factory=list)


# -- fetching ----------------------------------------------------------------


def query(bbox: tuple[float, float, float, float]) -> str:
    """The Overpass QL for every route relation over a window, geometry included.

    Two `out` statements and one request. The first returns the relations with
    ``geom``, which inlines each member way's coordinates -- that is what makes one
    request enough where walking the relations through the OSM API would be
    thousands. The second returns the member *nodes* with their tags, which the
    first cannot: a relation member carries a role and an id, never the tags of the
    thing it points at, and the stop names are the join key.
    """
    south, west, north, east = bbox
    values = "|".join(config.OSM_ROUTE_VALUES)
    return (
        f"[out:json][timeout:{config.OVERPASS_QUERY_TIMEOUT}];\n"
        f'rel["type"="route"]["route"~"^({values})$"]'
        f"({south:.6f},{west:.6f},{north:.6f},{east:.6f})->.routes;\n"
        ".routes out body geom;\n"
        "node(r.routes)->.stops;\n"
        ".stops out body;\n"
    )


def fetch(
    bbox: tuple[float, float, float, float],
    cache: Path | None = None,
    *,
    refresh: bool = False,
    session: requests.Session | None = None,
) -> list[Relation]:
    """Every route relation over a window, from the cache where there is one.

    Cached as the raw Overpass body rather than as parsed relations. Overpass is a
    metered public service and a national window is a minutes-long query, so a
    re-run of `trace` after a code change must not pay for it again -- and keeping
    the body means a parser fix can be applied to the bytes that were already
    fetched, which is the failure this actually protects against.
    """
    if cache is not None and cache.exists() and not refresh:
        log.info("reading OSM relations from %s", cache)
        return parse(json.loads(cache.read_text()))

    ql = query(bbox)
    log.info("querying Overpass over %s (this takes minutes at national scale)", bbox)
    sess = session or requests.Session()
    try:
        r = sess.post(
            config.OVERPASS_URL,
            data={"data": ql},
            timeout=config.OVERPASS_TIMEOUT,
            headers={"User-Agent": config.USER_AGENT},
        )
    except requests.RequestException as exc:
        raise TransportError(f"{type(exc).__name__}: {exc}") from exc
    # 429 and 504 are Overpass's own load shedding and say nothing about the query.
    if r.status_code in (429, 504):
        raise TransportError(f"Overpass is refusing load: {r.status_code}")
    if not r.ok:
        raise OverpassError(f"{r.status_code}: {r.text[:500]}")
    try:
        data = r.json()
    except ValueError as exc:
        # A body that will not parse is a truncated read, not a bad query. Overpass
        # also answers an over-quota query with HTTP 200 and an HTML error page.
        raise TransportError(f"Overpass response did not parse: {exc}") from exc

    relations = parse(data)
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data))
        log.info("cached %d relations to %s", len(relations), cache)
    return relations


def parse(data: dict[str, Any]) -> list[Relation]:
    """Overpass JSON to relations, resolving each stop member to its tagged node."""
    elements = data.get("elements") or []
    nodes: dict[int, dict[str, Any]] = {
        e["id"]: e for e in elements if e.get("type") == "node" and "id" in e
    }

    out: list[Relation] = []
    for e in elements:
        if e.get("type") != "relation":
            continue
        tags = e.get("tags") or {}
        ways: list[Way] = []
        stops: list[Stop] = []
        for m in e.get("members") or []:
            role = m.get("role") or ""
            if m.get("type") == "way" and role == "":
                geom = m.get("geometry") or []
                # A way with no geometry is a member Overpass could not resolve --
                # usually one clipped by the query window. Kept out of the chain
                # rather than skipped silently: dropping it would weld the two ways
                # either side of it together and draw straight through the gap.
                pts = tuple((float(p["lat"]), float(p["lon"])) for p in geom if p)
                ways.append(Way(int(m["ref"]), pts))
            elif m.get("type") == "node" and role in config.OSM_STOP_ROLES:
                node = nodes.get(int(m["ref"]))
                lat = m.get("lat", node.get("lat") if node else None)
                lon = m.get("lon", node.get("lon") if node else None)
                if lat is None or lon is None:
                    continue
                name = (node.get("tags") or {}).get("name") if node else None
                stops.append(Stop(int(m["ref"]), name, float(lat), float(lon)))
        out.append(
            Relation(
                relation_id=int(e["id"]),
                route=tags.get("route"),
                name=tags.get("name"),
                ways=tuple(ways),
                stops=tuple(stops),
                tags=tags,
            )
        )
    return out


# -- chaining ----------------------------------------------------------------


def chain(relation: Relation) -> Chain:
    """Walk the ``role=""`` ways in member order into one continuous path.

    Member order is the route's own order, so this is a walk rather than a search:
    each way is oriented to continue from where the last one ended, and a way that
    joins at neither end is a break. Breaks are counted rather than raised, because
    the count is the quality measure -- a relation with any break is refused, and
    knowing it broke once rather than twenty times is what tells snapshot skew apart
    from genuinely broken mapping.
    """
    out = _walk(relation.ways)
    # The first way's orientation is decided by the second, so a relation whose
    # first way happens to be laid the other way round joins at its *start* and
    # counts a break it does not have. If reversing it removes one, it was backwards.
    if out.breaks and len(relation.ways) >= 2:
        first, *rest = relation.ways
        flipped = _walk([Way(first.way_id, tuple(reversed(first.points))), *rest])
        if flipped.breaks < out.breaks:
            return flipped
    return out


def _walk(ways: list[Way] | tuple[Way, ...]) -> Chain:
    out = Chain()
    for way in ways:
        pts = list(way.points)
        if len(pts) < 2:
            # A way Overpass could not resolve, or a degenerate one. It cannot be
            # oriented and must not be silently bridged over.
            out.breaks += 1
            continue
        if not out.points:
            out.points = pts
            out.way_at = [way.way_id] * len(pts)
            out.way_ids.append(way.way_id)
            continue
        tail = out.points[-1]
        if _near(tail, pts[0]):
            out.points.extend(pts[1:])
            out.way_at.extend([way.way_id] * (len(pts) - 1))
        elif _near(tail, pts[-1]):
            out.points.extend(reversed(pts[:-1]))
            out.way_at.extend([way.way_id] * (len(pts) - 1))
        else:
            out.breaks += 1
            out.points.extend(pts)
            out.way_at.extend([way.way_id] * len(pts))
        out.way_ids.append(way.way_id)
    return out


def _near(a: tuple[float, float], b: tuple[float, float]) -> bool:
    """Whether two way endpoints are the same OSM node.

    They should be identical to the 1e-7 of a degree Overpass prints, so this is a
    rounding guard and not a tolerance -- see `config.TRACE_JOIN_TOLERANCE_M`.
    """
    dy = (b[0] - a[0]) * _M_PER_DEG_LAT
    dx = (b[1] - a[1]) * _M_PER_DEG_LAT * cos(radians(a[0]))
    return dy * dy + dx * dx <= config.TRACE_JOIN_TOLERANCE_M**2


# -- names -------------------------------------------------------------------

# What a station is called on one side and not the other. BODS writes "Blackhorse
# Road Station", "Pimlico Station" and "King's Cross St. Pancras Underground
# Station" against OSM's "Blackhorse Road station", "Pimlico" and "King's Cross St
# Pancras -- so the suffix, the full stops and the ampersand all have to go before
# the two sequences can be compared. Measured: this takes the Victoria line from a
# partial match to 16 of 16.
#
# The platform forms are the ones that are easy to miss, and they carry the whole of
# the DLR: a PTv2 stop member is a node on the platform rather than a point for the
# station, so OSM writes "Lewisham Platform 6" and "Canary Wharf Platforms 5 & 6"
# where BODS writes "Lewisham DLR Station". Both sides carry a qualifier the other
# does not, both have to go, and with them gone the two sequences agree 16 for 16. A
# DLR pattern that keeps either is recorded `no_stop_match` against a relation that
# chains perfectly.
#
# Ordered longest first, because `re` alternation takes the first branch that
# matches: "rail station" has to be offered before bare "station" or the pass leaves
# a stranded "rail" behind.
_SUFFIXES = re.compile(
    r"\s*\b("
    r"platforms? \d+(?: and \d+)*|"
    r"underground station|rail station|railway station|tram stop|dlr station|"
    r"metro station|light railway station|bus station|ferry terminal|"
    r"station|halt|stop|dlr|underground"
    r")\s*$",
    re.I,
)
# Apostrophes are deleted where the rest of the punctuation becomes a space, and the
# difference is load-bearing: "King's Cross" spaced reads as "king s cross", which
# matches nothing on the other side. Everything else is a separator in at least one
# publisher's spelling -- "Shepherd's Bush (Central)" against "Shepherd's Bush
# Central" -- and has to become one here.
_APOSTROPHE = re.compile(r"['’]")
_PUNCT = re.compile(r"[.,()\-/]")
_SPACE = re.compile(r"\s+")


_BRACKETED = re.compile(r"\s*\([^)]*\)")


def spellings(name: str | None) -> frozenset[str]:
    """Every form of a station name the other publisher might have written.

    One name is not enough, because the two publishers disambiguate differently and
    only one of them does it in the name. There are two Edgware Road stations a few
    hundred metres apart, so BODS writes "Edgware Road (Bakerloo)" and "Edgware Road
    (Circle Line)" where OSM writes "Edgware Road" twice and lets the relation say
    which is which. Flattening the brackets gives "edgware road bakerloo", which
    matches neither.

    So the bracketed form is offered as well as the flattened one, and a stop matches
    if *any* spelling agrees. The looser join is safe because it is not the only
    check: the matched node still has to sit within `TRACE_STOP_MAX_M` of the
    timetable's own coordinate, which is what keeps Edgware Road apart from Edgware
    8 km up the Northern line. Brackets are not simply deleted, because sometimes
    they hold the name -- OSM spells out "Cutty Sark for Maritime Greenwich" in full.
    """
    if not name:
        return frozenset({""})
    return frozenset({normalise(name), normalise(_BRACKETED.sub("", name))})


def normalise(name: str | None) -> str:
    """A station name reduced to what both publishers agree on."""
    if not name:
        return ""
    s = _SPACE.sub(" ", _PUNCT.sub(" ", _APOSTROPHE.sub("", name.lower()))).strip()
    s = _SPACE.sub(" ", s.replace("&", " and ")).strip()
    # Until it stops changing rather than a fixed number of passes: the qualifiers
    # stack, and "Lewisham DLR Station" needs two while "Cutty Sark for Maritime
    # Greenwich Platform 2" needs one. Bounded so a pathological name cannot spin,
    # and a strip that empties the string is refused -- a station called "Bank" or
    # "Underground" must survive being its own qualifier.
    for _ in range(4):
        shorter = _SPACE.sub(" ", _SUFFIXES.sub("", s)).strip()
        if not shorter or shorter == s:
            break
        s = shorter
    return s


# -- geometry ----------------------------------------------------------------


def to_metres(
    points: list[tuple[float, float]], ref_lat: float
) -> list[tuple[float, float]]:
    """(lat, lon) to local (x, y) metres on a plane through ``ref_lat``.

    Everything measured here is local -- a stop against a node, a projection along
    one line -- so a plane is exact well past the precision the geometry carries,
    and it keeps the projection loop to arithmetic.
    """
    scale = _M_PER_DEG_LAT * cos(radians(ref_lat))
    return [(lon * scale, lat * _M_PER_DEG_LAT) for lat, lon in points]


def cumulative(pts: list[tuple[float, float]]) -> list[float]:
    """Distance along a metre-space polyline at each vertex."""
    out = [0.0]
    for a, b in zip(pts, pts[1:], strict=False):
        out.append(out[-1] + sqrt((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2))
    return out


def project(
    pts: list[tuple[float, float]], cum: list[float], pt: tuple[float, float]
) -> tuple[float, float]:
    """Where a point falls along a polyline: (distance along, distance from).

    Both in metres, both in the same plane. The caller wants the first to slice the
    chain and the second to decide whether the match is real at all.
    """
    best_along, best_off = 0.0, float("inf")
    for i, (a, b) in enumerate(zip(pts, pts[1:], strict=False)):
        dx, dy = b[0] - a[0], b[1] - a[1]
        seg = dx * dx + dy * dy
        if seg == 0.0:
            t = 0.0
        else:
            t = ((pt[0] - a[0]) * dx + (pt[1] - a[1]) * dy) / seg
            t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
        cx, cy = a[0] + t * dx, a[1] + t * dy
        off = sqrt((pt[0] - cx) ** 2 + (pt[1] - cy) ** 2)
        if off < best_off:
            best_off = off
            best_along = cum[i] + t * sqrt(seg)
    return best_along, best_off


def slice_between(
    latlon: list[tuple[float, float]], cum: list[float], start_m: float, end_m: float
) -> list[tuple[float, float]]:
    """The part of a path between two distances along it, endpoints interpolated.

    A pattern is a contiguous sub-path of its line -- a short working of the
    Northern line is still the Northern line's track -- so drawing it means cutting
    the relation's chain at the first and last stop rather than drawing the whole
    line under every pattern that touches it.
    """
    if start_m > end_m:
        start_m, end_m = end_m, start_m
    out: list[tuple[float, float]] = [_at(latlon, cum, start_m)]
    for i, d in enumerate(cum):
        if start_m < d < end_m:
            out.append(latlon[i])
    out.append(_at(latlon, cum, end_m))
    # Two stops on one segment give a start and an end and nothing between, which is
    # a real two-point line. Only an exactly-zero-length cut is degenerate.
    return out if out[0] != out[-1] else out[:1]


def _at(
    latlon: list[tuple[float, float]], cum: list[float], d: float
) -> tuple[float, float]:
    """The (lat, lon) at a distance along the path, interpolated within a segment."""
    if d <= cum[0]:
        return latlon[0]
    if d >= cum[-1]:
        return latlon[-1]
    for i in range(len(cum) - 1):
        if cum[i] <= d <= cum[i + 1]:
            span = cum[i + 1] - cum[i]
            t = 0.0 if span == 0.0 else (d - cum[i]) / span
            a, b = latlon[i], latlon[i + 1]
            return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
    return latlon[-1]


def ways_between(
    way_at: list[int], cum: list[float], start_m: float, end_m: float
) -> list[int]:
    """The ways a slice of a chain runs over, in order, without duplicates.

    `slice_between` answers the same question in geometry and throws the way ids
    away doing it, which is exactly the gap that stops relation track being
    attributed: a timetable knows a train ran from one station to another, and the
    only way to turn that into "these ways carried it" is to keep the identity
    through the cut.

    A *segment* rather than a vertex is what belongs to a way -- the segment ending
    at point i+1 is part of whichever way contributed that point -- so a slice that
    starts and ends mid-way still names both of them. Getting that wrong at the ends
    is how a service loses the half-way it entered on.

    Takes `Chain.way_at` rather than the whole chain, because that list is the only
    part of it this reads and two callers reach it differently: `railtrips` holds
    the chain and `trace` keeps the projection it built from one.
    """
    if start_m > end_m:
        start_m, end_m = end_m, start_m
    out: list[int] = []
    for i in range(len(cum) - 1):
        if cum[i + 1] > start_m and cum[i] < end_m:
            way = way_at[i + 1]
            if not out or out[-1] != way:
                out.append(way)
    # A cut shorter than one segment overlaps no segment strictly, and the train
    # still ran over the way it happened on.
    if not out and way_at:
        idx = min(range(len(cum)), key=lambda i: abs(cum[i] - start_m))
        out.append(way_at[idx])
    return out
