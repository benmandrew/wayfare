"""Attribute a rail timetable's trips onto the track a route relation supplies.

`osmroutes` draws Great Britain's National Rail from OpenStreetMap alone, which is
what makes it publishable at all. What it cannot supply is how often anything runs.
This is the other half, and it is optional by construction: the geometry ships
without it, and adding it changes one nullable column.

**Legs, not whole patterns.** Measured against the April 2024 national extract:

===========================================  ==============  ==============
matching                                     patterns        weekly trips
===========================================  ==============  ==============
whole calling sequence, contiguous            601 (13.2%)    23,539 (19.4%)
whole calling sequence, subsequence         1,021 (22.4%)    29,051 (23.9%)
**each consecutive pair of calls**                  --       **82.0%**
===========================================  ==============  ==============

877 usable relations against 4,557 CIF stopping patterns is a structural mismatch
that no matching rule fixes. A fast Bedford to Brighton does not need a relation
with its exact stopping pattern; each of its legs runs on track some relation
covers. That is also how `edge_services` has always worked for roads -- trips
accumulate onto track, never onto whole patterns.

The join runs CIF TIPLOC -> NaPTAN name and coordinate -> `osm.spellings` -> the
relation's own stop nodes, and every link in it is OGL v3.0 or ODbL.

One leg is attributed to **one** relation, the one on which the two stations sit
closest together along the chain. Several relations cover most legs -- 75.8% of GB
rail ways carry two or more -- and adding the leg's trips to each of them would
multiply the national total by however thoroughly a corridor happens to be mapped.
The most direct is chosen because a leg is one train's journey between two stations,
and where two relations both cover it the shorter is the one it ran on.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb

from . import cif, db, logs, naptan, osm, osmroutes

log = logs.get("railtrips")

# How far apart two stations may sit along a chain before the pairing is refused,
# as a multiple of the straight-line distance between them. A relation that calls at
# "Newport" in Wales and at a "Newport" on the Isle of Wight would otherwise pair
# them and attribute a leg's trips down a hundred miles of unrelated track.
MAX_DETOUR_RATIO = 3.0


@dataclass(frozen=True)
class Line:
    """One chaining relation, ready to have legs projected onto it."""

    relation_id: int
    chain: osm.Chain
    cum: list[float]
    # Every spelling of every stop it calls at, to the distances along the chain
    # where that name occurs. A list because a relation may call at one station
    # twice -- a loop does -- and the pair that fits is not always the first.
    along: dict[str, list[float]] = field(default_factory=dict)
    latlon: dict[str, list[tuple[float, float]]] = field(default_factory=dict)


@dataclass(frozen=True)
class Attributed:
    """What one attribution run resolved, and what it could not."""

    legs: int
    legs_placed: int
    trips: int
    trips_placed: int
    ways: int
    tiplocs_unresolved: int

    @property
    def trip_coverage(self) -> float:
        return 100.0 * self.trips_placed / self.trips if self.trips else 0.0


def lines(
    relations: list[osm.Relation], routes: dict[str, str] | None = None
) -> list[Line]:
    """The chaining relations, with every stop projected onto the chain.

    The relation's *own* stop nodes are projected, not the timetable's coordinates,
    for the reason `trace._cut` gives: the node is on the track by construction
    where a feed's point is a station entrance, and projecting the further of the
    two risks landing on a parallel line.
    """
    routes = routes or osmroutes.ROUTE_MODES
    out: list[Line] = []
    for r in relations:
        if (r.route or "") not in routes or not r.ways:
            continue
        measured = osm.prepare(r)
        if not measured.chains:
            continue
        along: dict[str, list[float]] = defaultdict(list)
        latlon: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for s in r.stops:
            if not s.name:
                continue
            d, _off = measured.place(s.lat, s.lon)
            for spelling in osm.spellings(s.name):
                if spelling:
                    along[spelling].append(d)
                    latlon[spelling].append((s.lat, s.lon))
        if along:
            out.append(
                Line(r.relation_id, measured.chain, measured.cum, dict(along), dict(latlon))
            )
    return out


def _best_pair(
    line: Line, first: frozenset[str], second: frozenset[str]
) -> tuple[float, float] | None:
    """Where on this line the two stations sit, closest pairing wins.

    Refused where the track between them runs more than `MAX_DETOUR_RATIO` times
    the straight line, which is what stops two unrelated stations sharing a name
    from being paired across half the country.

    The straight line is `osm.planar_m` and not the exact distance, because it is
    compared against a gap measured along `osm.to_metres`' own plane. Mixing the two
    would move the ratio by the plane's error rather than by anything about the
    railway.
    """
    best: tuple[float, float] | None = None
    best_gap = float("inf")
    for a_name in first:
        for b_name in second:
            for a, a_ll in zip(
                line.along.get(a_name, []), line.latlon.get(a_name, []), strict=True
            ):
                for b, b_ll in zip(
                    line.along.get(b_name, []), line.latlon.get(b_name, []), strict=True
                ):
                    gap = abs(a - b)
                    if gap >= best_gap or gap == 0.0:
                        continue
                    direct = osm.planar_m(a_ll, b_ll)
                    if direct > 0.0 and gap > direct * MAX_DETOUR_RATIO:
                        continue
                    best, best_gap = (a, b), gap
    return best


def _resolve_sequence(
    sequence: tuple[str, ...],
    register: dict[str, naptan.Station],
    unresolved: set[str],
) -> list[frozenset[str]] | None:
    """Every spelling of every call in order, or None if one TIPLOC does not resolve.

    The whole sequence goes rather than the call that failed. Dropping the one call
    joins its neighbours into a leg that no train runs, which draws a service past a
    station it stops at -- and the unplaceable code is recorded instead, because a
    register gap nobody can see reads as a quiet railway.
    """
    out: list[frozenset[str]] = []
    for tiploc in sequence:
        station = register.get(tiploc)
        if station is None:
            unresolved.add(tiploc)
            return None
        out.append(osm.spellings(station.name))
    return out


def _place_leg(
    found: list[Line], shared: set[int], first: frozenset[str], second: frozenset[str]
) -> tuple[Line, tuple[float, float]] | None:
    """The one line a leg is attributed to, and where on it the two stations sit.

    One rather than every line covering it: 75.8% of GB rail ways carry two or more
    relations, and adding the leg's trips to each multiplies the national total by
    how thoroughly a corridor happens to be mapped. The most direct wins, because a
    leg is one train's journey between two stations and the shorter of two covering
    lines is the one it ran on.
    """
    best: tuple[Line, tuple[float, float]] | None = None
    best_gap = float("inf")
    for i in shared:
        pair = _best_pair(found[i], first, second)
        if pair is None:
            continue
        gap = abs(pair[0] - pair[1])
        if gap < best_gap:
            best, best_gap = (found[i], pair), gap
    return best


def attribute(
    trips_by_sequence: dict[tuple[str, ...], int],
    register: dict[str, naptan.Station],
    found: list[Line],
) -> tuple[dict[int, int], Attributed]:
    """Weekly trips per way, from calling sequences keyed on TIPLOC.

    A leg whose two stations both place and which some relation covers is added to
    every way between them. A leg that places and which nothing covers is counted
    and dropped -- 18% of GB rail leg-trips, concentrated on the Thameslink core,
    Edinburgh and the Merseyrail loops, where the relations are simply not drawn.
    Reporting that is the point: a number nobody can see is how a coverage gap gets
    mistaken for a quiet railway.
    """
    by_name: defaultdict[str, list[int]] = defaultdict(list)
    for i, line in enumerate(found):
        for name in line.along:
            by_name[name].append(i)

    trips: Counter[int] = Counter()
    legs = legs_placed = total = placed = 0
    unresolved: set[str] = set()

    for sequence, weekly in trips_by_sequence.items():
        spellings = _resolve_sequence(sequence, register, unresolved)
        if spellings is None:
            continue
        for first, second in zip(spellings, spellings[1:], strict=False):
            legs += 1
            total += weekly
            shared = {i for n in first for i in by_name.get(n, [])} & {
                i for n in second for i in by_name.get(n, [])
            }
            placed_on = _place_leg(found, shared, first, second)
            if placed_on is None:
                continue
            line, cut = placed_on
            legs_placed += 1
            placed += weekly
            for way_id in osm.ways_between(line.chain.way_at, line.cum, *cut):
                trips[way_id] += weekly

    out = Attributed(
        legs=legs,
        legs_placed=legs_placed,
        trips=total,
        trips_placed=placed,
        ways=len(trips),
        tiplocs_unresolved=len(unresolved),
    )
    log.info(
        "%d of %d legs placed, %d of %d weekly leg-trips (%.1f%%) over %d ways; "
        "%d TIPLOCs did not resolve",
        out.legs_placed,
        out.legs,
        out.trips_placed,
        out.trips,
        out.trip_coverage,
        out.ways,
        out.tiplocs_unresolved,
    )
    return dict(trips), out


def write(con: duckdb.DuckDBPyConnection, trips: dict[int, int]) -> int:
    """Replace the attributed trips outright.

    Rebuilt rather than merged, like `segments`: it is derived from a timetable and
    a relation set that are both re-read every run, and a way that stopped carrying
    a service must stop being drawn as busy. A stale row here is not a missing
    number, it is a wrong one.

    One row per way carrying a train, which is 55,114 of them nationally, so it is
    staged to a file rather than inserted a row at a time.
    """
    con.execute("DELETE FROM way_trips")
    return db.insert_via_file(
        con, "way_trips", ("way_id", "n_trips"), sorted(trips.items())
    )


def run(
    con: duckdb.DuckDBPyConnection,
    schedule: Path,
    stops: Path,
    relations: list[osm.Relation],
    *,
    on: date,
) -> Attributed:
    """Read a CIF and a stop register, and attribute what runs onto what is drawn."""
    extract = cif.read(schedule)
    running = cif.live(extract.schedules, on)
    weekly = cif.weekly_trips(running)
    register = naptan.read(stops)
    found = lines(relations)
    log.info(
        "%d schedules live on %s over %d calling sequences, against %d relations",
        len(running),
        on,
        len(weekly),
        len(found),
    )
    trips, summary = attribute(weekly, register, found)
    write(con, trips)
    return summary


def run_cached(
    con: duckdb.DuckDBPyConnection,
    schedule: Path,
    stops: Path,
    *,
    on: date,
    cache: Path | None = None,
) -> Attributed:
    """As `run`, reading the relations `wayfare routes` already fetched.

    The same cached Overpass body and never a fresh query: attributing trips must
    not be able to change which track is drawn. If the relations have moved, the
    fix is to re-run `routes`, so that the geometry and the trips over it always
    describe the same snapshot.

    Parsed straight off the file rather than asked for through `osm.fetch`, which
    would need a window to fall back on and there is no honest one to give: a query
    is the outcome this refuses, so a bounding box here could only be a placeholder
    that a missing cache would turn into a national Overpass request.
    """
    from . import config

    path = cache or config.RAW / "osm_routes.json"
    if not path.exists():
        raise RuntimeError(f"{path} is not there; run `wayfare routes` first")
    log.info("reading OSM relations from %s", path)
    return run(con, schedule, stops, osm.parse(json.loads(path.read_text())), on=on)
