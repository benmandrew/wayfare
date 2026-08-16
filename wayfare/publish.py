"""Turn the matched network into vector tiles for the web viewer.

The output is PMTiles: a single file, read over HTTP range requests, needing no
tile server. For a dataset that is rebuilt occasionally and read constantly that is
the right shape -- it can sit on R2 or S3 behind a CDN and cost nothing to serve.

The one real design decision here is what goes *in* the tiles. Tippecanoe stores
attributes per feature per zoom, so the cost that matters is per *feature*, not per
value: MVT pools attribute values per layer per tile, and a feature pays two varints
to point into that pool. Long service lists are therefore cheap and feature counts
are not, which is why the export carries a generous ref cap and coalesces edges
rather than the other way round.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

import duckdb

from . import config, db, licences, logs, palette

log = logs.get("publish")

LAYER = "bus"

# The non-road modes, drawn from operator geometry rather than matched. A layer of
# its own rather than a `mode` attribute on `bus`, because the two carry different
# attributes and the viewer styles them differently -- and because tile-join keeps
# distinct layer names, so this costs one more tippecanoe pass and nothing else.
LAYER_SEGMENTS = "segments"
# Track drawn from OSM route relations, inverted to one feature per way. Its own
# layer and not merged into `segments`, because its `n` counts relations where the
# road layer's counts timetabled services -- two quantities that must never share
# a colour ramp.
LAYER_TRACK = "track"

# The archive a publish writes when it is told neither a path nor to use the region's
# name. Region-agnostic, and deliberately still the default -- see `default_out`.
#
# From `map.toml` because both viewer pages fall back to this name when there is no
# index to ask, which is what a static host is. Renaming it here alone would leave
# them looking for a file nothing writes.
DEFAULT_ARCHIVE = palette.load().default_archive

Point = tuple[int, int]  # (lon_e6, lat_e6)


# How many rows to pull from DuckDB at a time. Only ever this many rows plus one
# way's worth are resident, which is what keeps the national edge table -- several
# million rows of tuples and integer lists -- out of the Python heap.
FETCH_ROWS = 50_000


def _degrees(points: Iterable[Point]) -> list[list[float]]:
    """Micro-degree integer points as the degrees GeoJSON is written in."""
    return [[x / 1e6, y / 1e6] for x, y in points]


def _polyline(lon_e6: Sequence[int], lat_e6: Sequence[int]) -> list[list[float]]:
    """One polyline held as two parallel arrays, as GeoJSON coordinates.

    strict: the two lists are one polyline, so a length mismatch is corrupt geometry
    rather than a short line. Truncating to the shorter of them would draw a
    confident line that stops halfway.
    """
    return _degrees(zip(lon_e6, lat_e6, strict=True))


def _write_features(
    path: Path, features: Iterable[tuple[dict[str, Any], list[list[float]]]]
) -> int:
    """Write the features one GeoJSON object per line, which is what tippecanoe wants.

    All three exports write through here, so the shape of a line is one decision
    rather than three. `separators` is what `_features` reads back out with a regular
    expression, and each export's properties go out in the order it built them, which
    is what makes a rebuild byte-identical.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w") as fh:
        for props, coords in features:
            fh.write(
                json.dumps(
                    {
                        "type": "Feature",
                        "properties": props,
                        "geometry": {"type": "LineString", "coordinates": coords},
                    },
                    separators=(",", ":"),
                )
            )
            fh.write("\n")
            written += 1
    return written


def export_edges_geojsonl(con: duckdb.DuckDBPyConnection, path: Path | None = None) -> Path:
    """Write the matched road network, one GeoJSON feature per line.

    Streamed rather than materialised. The query is ordered by way_id and coalescing
    never merges across ways, so a way's edges can be collapsed and released as soon
    as the next way starts -- the result is identical to collapsing the whole table
    at once, at a fraction of the resident memory. DuckDB does the sort out of core,
    which is exactly the part it is better at than the Python heap.
    """
    path = path or (config.WORK / "edges.geojsonl")

    cur = con.execute(
        """
        SELECT e.edge_id, e.way_id, e.road_name, e.lon_e6, e.lat_e6,
               s.n, s.refs, s.trips
        FROM edges e
        JOIN (
            SELECT edge_id,
                   count(*)          AS n,
                   -- short_name breaks the tie. Without it, two services running
                   -- the same number of trips come back in whatever order the
                   -- aggregate happened to build, which made the export
                   -- non-deterministic and split segments that should merge.
                   list(short_name ORDER BY n_trips DESC, short_name) AS refs,
                   sum(n_trips)      AS trips
            FROM edge_services GROUP BY edge_id
        ) s USING (edge_id)
        WHERE e.lon_e6 IS NOT NULL
        ORDER BY e.way_id
        """
    )

    stats = {"edges": 0, "capped": 0}
    written = _write_features(path, _edge_features(cur, stats))

    log.info(
        "%d edges coalesced to %d features in %s (%d over the %d-service cap)",
        stats["edges"],
        written,
        path,
        stats["capped"],
        config.MAX_REFS_IN_TILE,
    )
    return path


def export_segments_geojsonl(
    con: duckdb.DuckDBPyConnection, path: Path | None = None
) -> Path | None:
    """Write the non-road patterns as one GeoJSON feature per line, or None.

    None rather than an empty file when there is nothing to draw, so a bus-only
    region skips the extra tippecanoe pass entirely instead of joining an empty
    layer into every archive.

    No coalescing and no streaming, and neither is an oversight. A segment is a
    whole pattern's trace rather than a fragment of one, so there is nothing to
    merge with anything -- and Great Britain has 630 of them against 2.7M edges, so
    the table fits in memory many times over. `ORDER BY pattern_id` is here for the
    same reason the edge export sorts: a rebuild has to be byte-identical.
    """
    if not _has_rows(con, "segments"):
        return None

    path = path or (config.WORK / "segments.geojsonl")
    rows = con.execute(
        """
        SELECT s.pattern_id, s.mode, s.lon_e6, s.lat_e6,
               COALESCE(NULLIF(trim(p.short_name), ''), p.route_id) AS ref,
               p.n_trips
        FROM segments s
        JOIN patterns p USING (pattern_id)
        ORDER BY s.pattern_id
        """
    ).fetchall()

    written = _write_features(
        path,
        (
            (
                {"id": pattern_id, "mode": mode, "ref": ref, "trips": trips},
                _polyline(lon_e6, lat_e6),
            )
            for pattern_id, mode, lon_e6, lat_e6, ref, trips in rows
        ),
    )
    log.info("%d non-road segments written to %s", written, path)
    return path


def export_track_geojsonl(
    con: duckdb.DuckDBPyConnection, path: Path | None = None
) -> Path | None:
    """One feature per way of relation track, with the services that use it.

    The counterpart of the edge export for the modes nothing routed. Where that one
    coalesces edges into ways as it streams, this is already per way: the inversion
    happened in `aggregate.build_track_services`, so a way is one row here and one
    feature out.

    Drawing per way rather than per pattern is the whole point. Great Britain's 911
    chaining `route=train` relations are 1,569,495 vertices drawn one polyline per
    relation and 443,126 drawn once per way, because 75.8% of ways carry two or more
    relations. Nothing is lost in the reduction -- the service list is what the
    overlap was carrying, and it is right here on the feature.

    One feature per way *and mode*, because the layer carries the traced modes too
    now and a way is painted by its mode. Two networks over one alignment are two
    features and are drawn twice, which is deliberate: an Underground line and the
    National Rail service beside it are not the same railway.

    `trips` comes from `way_trips` where a timetable was attributed per leg, and
    from the services on the way where the timetable supplied the patterns
    themselves. Summing the service column is only wrong for the first: there a
    relation count multiplies a figure that belongs to the track, which is what
    `way_trips` exists to keep separate. It stays null rather than becoming zero
    where neither knows. A viewer can style "unknown" and cannot style a lie.

    `ORDER BY way_id` for the reason every other export sorts: a rebuild has to be
    byte-identical, and DuckDB's parallel hash join returns rows in a varying order
    otherwise. `mode` joins the sort because a way is now more than one row.
    """
    if not _has_rows(con, "track_services"):
        return None
    if not db.table_exists(con, "way_trips"):
        return None
    path = path or (config.WORK / "track.geojsonl")

    rows = con.execute(
        """
        SELECT w.way_id, ts.mode, w.lon_e6, w.lat_e6,
               count(*)                                   AS n,
               list(ts.short_name ORDER BY ts.short_name)  AS refs,
               -- `way_trips` first, and it is the only source for the rail this
               -- pipeline built out of relations: a leg's trips are a property of
               -- the track, and summing a per-service column there would multiply
               -- them by however many relations happen to cover it. Where the
               -- timetable supplied the pattern and OSM only supplied its shape,
               -- the services carry the count and summing them is what
               -- `edge_services` does with the same numbers.
               COALESCE(
                   any_value(wt.n_trips),
                   CASE WHEN count(ts.n_trips) = 0 THEN NULL
                        ELSE sum(ts.n_trips) END
               )                                          AS trips
        FROM track_services ts
        JOIN ways w USING (way_id)
        LEFT JOIN way_trips wt USING (way_id)
        GROUP BY w.way_id, ts.mode, w.lon_e6, w.lat_e6
        ORDER BY w.way_id, ts.mode
        """
    ).fetchall()

    written = _write_features(
        path,
        (
            (
                {
                    "way_id": way_id,
                    "mode": mode,
                    "n": n,
                    # Comma-joined, exactly as the edge export writes it, because a
                    # JSON array is not a thing a vector tile can hold. Tippecanoe
                    # does not drop one and does not warn: it stores the array's
                    # *JSON text* as a string, so the viewer's `refs.split(",")` came
                    # back holding `["Northern line` and `Jubilee line"]`. Every name
                    # mangled, and the service search silently matching nothing on
                    # this layer.
                    "refs": ",".join(refs[: config.MAX_REFS_IN_TILE]),
                    "trips": trips,
                },
                _polyline(lon_e6, lat_e6),
            )
            for way_id, mode, lon_e6, lat_e6, n, refs, trips in rows
        ),
    )
    log.info("%d features of relation track written to %s", written, path)
    return path


def _edge_features(
    cur: duckdb.DuckDBPyConnection, stats: dict[str, int]
) -> Iterator[tuple[dict[str, Any], list[list[float]]]]:
    """The coalesced road features, counting the ones whose service list was capped.

    Counted on the way past rather than afterwards, because nothing holds the whole
    export and there is nothing left to count once it is written.
    """
    for props, coords in _coalesce_by_way(cur, stats):
        stats["capped"] += props["n"] > config.MAX_REFS_IN_TILE
        yield props, coords


def _coalesce_by_way(
    cur: duckdb.DuckDBPyConnection, stats: dict[str, int]
) -> Iterator[tuple[dict[str, Any], list[list[float]]]]:
    """Coalesce a way at a time from an ordered cursor.

    Relies on the query's ORDER BY way_id and on way_id being part of the coalescing
    key: no segment can ever span two ways, so a way is complete the moment the next
    one appears and its rows can be dropped.
    """
    buf: list[Any] = []
    current: Any = _NO_WAY
    while chunk := cur.fetchmany(FETCH_ROWS):
        for row in chunk:
            stats["edges"] += 1
            if row[1] != current:
                if buf:
                    yield from coalesce(buf)
                buf = []
                current = row[1]
            buf.append(row)
    if buf:
        yield from coalesce(buf)


# A sentinel, because way_id could in principle be NULL and None is a real value to
# compare against.
_NO_WAY = object()


# --- Coalescing -------------------------------------------------------------
#
# A Valhalla directed edge is a tiny thing: 4.1 coordinates on average, tens of
# metres long. Emitting one feature per edge is what made the tiles expensive.
#
# Every feature pays per-feature overhead no matter how short it is -- a geometry
# header, an absolute moveto, and a key/value varint pair for each attribute. At
# 170k features for Wales that is megabytes before a single coordinate. Worse,
# --simplification cannot do anything with a 4-point line, so low-zoom tiles were
# carrying full-detail geometry and tippecanoe was falling back on
# --drop-densest-as-needed to shed whole roads instead.
#
# So merge runs of edges that a reader could not tell apart. Two edges belong to
# the same segment when every attribute a tile carries is identical -- same way,
# same road name, same service set, same trip count -- and they meet end to end.
# The merge is therefore lossless: nothing that reaches the viewer is averaged or
# dropped, there are simply fewer, longer lines.
#
# Two effects, measured separately on Wales:
#   169,857 directed edges
#   102,925 after collapsing directed pairs (a two-way street with the same buses
#           both ways was drawing two coincident lines, one of them invisible)
#    ~43,000 after chaining the survivors along the way
#
# Direction is only collapsed where the service sets agree, so a one-way pair with
# different buses each way still renders as two lines -- which is the case where
# the two lines carry information.


def coalesce(rows: list[Any]) -> list[tuple[dict[str, Any], list[list[float]]]]:
    """Group edges by their tile attributes, then chain each group end to end."""
    groups: dict[tuple[Any, ...], list[Member]] = defaultdict(list)
    display: dict[tuple[Any, ...], tuple[int, tuple[str, ...]]] = {}
    for edge_id, way_id, name, lon_e6, lat_e6, n, refs, trips in rows:
        pts = list(zip(lon_e6, lat_e6, strict=True))
        if len(pts) < 2:
            continue
        capped = tuple(refs[: config.MAX_REFS_IN_TILE])
        # Identity is the service *set*, not the order it happens to be listed in.
        # Two edges carrying the same buses are the same road to a reader even when
        # the busiest-first ordering differs between them, and splitting the geometry
        # over that would be splitting it over nothing.
        key = (way_id, name, int(n), int(trips or 0), tuple(sorted(capped)))
        groups[key].append((edge_id, pts))
        # Keep the busiest-first list from the lowest edge id, so which of several
        # equivalent orderings gets displayed does not depend on row order.
        held = display.get(key)
        if held is None or edge_id < held[0]:
            display[key] = (edge_id, capped)

    out = []
    for key, members in groups.items():
        way_id, name, n, trips, _set = key
        capped = display[key][1]
        for edge_id, pts in _chain(_dedupe_reversed(members)):
            props: dict[str, Any] = {
                # The overview bands' feature id, and excluded everywhere else --
                # the detail band puts the way id there instead, and a property no
                # band is told to drop is one every band writes. The lowest edge id
                # in the segment names it, so it is stable for a given build.
                "id": int(edge_id),
                # The detail band's feature id, and excluded from the overview
                # bands by `_DETAIL_ONLY`, so it is never both at once.
                "way": int(way_id),
                "n": n,
                "refs": ",".join(capped),
                "trips": trips,
            }
            if name:
                props["name"] = name
            out.append((props, _degrees(pts)))
    return out


Member = tuple[int, list[Point]]


def _dedupe_reversed(members: list[Member]) -> list[Member]:
    """Drop one of each pair of edges that traverse the same road in opposite ways.

    Valhalla edges are directed, so an ordinary two-way street arrives twice. When
    both directions carry the same services the two lines are coincident and the
    second is invisible under the first -- it is pure tile weight. Where the
    services differ the two edges are in different groups and never meet here.
    """
    seen: dict[tuple[Point, ...], Member] = {}
    for edge_id, pts in members:
        key = tuple(pts) if pts[0] <= pts[-1] else tuple(reversed(pts))
        kept = seen.get(key)
        # Keep the lower edge id so the segment's name does not depend on row order.
        if kept is None or edge_id < kept[0]:
            seen[key] = (edge_id, pts)
    return list(seen.values())


def _chain(members: list[Member]) -> list[Member]:
    """Join edges that meet end to end into maximal runs.

    Only merges through a point where exactly two of the group's edges meet. At a
    junction of three the continuation is ambiguous, and picking one would draw a
    line that doubles back on itself.

    Sorted first, and each finished run is pointed a consistent way afterwards, so
    the output does not depend on the order DuckDB's parallel scan returned rows in.
    Without both, a closed loop starts wherever the scan happened to put its first
    edge and an open run comes out forwards or backwards at random -- which changes
    nothing on screen but makes every rebuild produce different bytes.
    """
    members = sorted(members)
    at: dict[Point, list[int]] = defaultdict(list)
    for i, (_, pts) in enumerate(members):
        at[pts[0]].append(i)
        at[pts[-1]].append(i)

    used = [False] * len(members)
    out: list[Member] = []

    def step(node: Point, came_from: int) -> int | None:
        """The one unused edge continuing through `node`, if it is unambiguous."""
        if len(at[node]) != 2:
            return None
        nxt = [i for i in at[node] if i != came_from]
        return nxt[0] if nxt and not used[nxt[0]] else None

    # Start from the ends of open runs so a run is walked once, in order; anything
    # left after that is a closed loop, entered at an arbitrary point.
    starts = [
        i
        for i, (_, pts) in enumerate(members)
        if len(at[pts[0]]) != 2 or len(at[pts[-1]]) != 2
    ]
    for i in starts + list(range(len(members))):
        if used[i]:
            continue
        used[i] = True
        edge_id, pts = members[i]
        chain = list(pts)
        ids = [edge_id]

        for forward in (True, False):
            here = i
            while True:
                node = chain[-1] if forward else chain[0]
                nxt = step(node, here)
                if nxt is None:
                    break
                used[nxt] = True
                nid, npts = members[nxt]
                ids.append(nid)
                tail = npts if npts[0] == node else list(reversed(npts))
                chain = chain + tail[1:] if forward else list(reversed(tail[1:])) + chain
                here = nxt

        if chain[0] > chain[-1]:
            chain.reverse()
        out.append((min(ids), chain))
    return out


# `n` and `trips` both survive below DETAIL_ZOOM, because both are weights the
# viewer paints with: `n` drives the service-count ramp, `trips` the journeys-a-day
# one. A colour mode paints at every zoom, so stripping `trips` here alongside the
# info card's attributes would flatten the map below z11 rather than report a
# missing attribute.
#
# It costs a key and a varint per feature in the three overview bands, against
# `refs`, which is a whole comma-joined service list and stays excluded. The way id
# and the road name go with it: the info card is the only reader of all three, and
# the card does not open below DETAIL_ZOOM.
#
# From `map.toml`, because the viewer reads the *presence* of `refs` as "this came
# from the detail band" and so tells the two feature-id spaces apart by it. The two
# sides used to describe each other's rule from memory, in comments.
_DETAIL_ONLY = palette.load().detail_only

# `export_geojsonl` writes with separators=(",", ":"), so this is the shape of every
# line it produces. A line that does not match is a change to the export that this
# has not caught up with, and is raised on rather than skipped: a filter that quietly
# matches nothing builds an empty band, and an empty band looks like a region that
# lost its buses.
_TRIPS = re.compile(rb'"trips":(\d+)')
# A longitude within about a kilometre of the prime meridian is written in scientific
# notation -- Great Britain has 63 such features, around Greenwich -- so the number
# has to allow an exponent. `-?\d+\.?\d*` matches every other line in the file and
# skips those, which is the kind of near miss that takes roads off a map quietly.
_NUM = rb"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
_FIRST_POINT = re.compile(rb'"coordinates":\[\[(' + _NUM + rb"),(" + _NUM + rb")\]")

Cell = tuple[int, int]


class _Band(NamedTuple):
    """One tippecanoe pass: the zooms it covers and what it carries through them.

    The defaults are the road overview bands -- the `bus` layer, the Valhalla edge id
    in the feature id field, tippecanoe's own tile grid, and a top zoom the pass may
    not grow past. `detail` departs from the last three, and the segments and track
    passes only from the first two.

    `extend` defaults off because `-z` is a ceiling
    `--extend-zooms-if-still-dropping` is allowed to raise, which is silently wrong
    for every band with another band above it: a far band that grew from z7 to z9 --
    measured, on Great Britain -- overlaps the near band, and tile-join merges the
    two into tiles holding both copies of every road.
    """

    name: str
    min_zoom: int
    max_zoom: int
    floors: Mapping[Cell, int]
    exclude: Sequence[str]
    layer: str = LAYER
    extend: bool = False
    id_attribute: str = "id"
    low_detail: int | None = None


def _features(geojsonl: Path) -> Iterator[tuple[bytes, Cell, int]]:
    """Every feature as its line, the cell its first point falls in, and its `trips`."""
    with geojsonl.open("rb") as src:
        for line in src:
            trips, point = _TRIPS.search(line), _FIRST_POINT.search(line)
            if trips is None or point is None:
                raise RuntimeError(
                    f"{geojsonl} has a feature this filter cannot read: {line[:120]!r}. "
                    "The export format and this filter have diverged."
                )
            cell = (
                round(float(point[1]) / config.OVERVIEW_CELL),
                round(float(point[2]) / config.OVERVIEW_CELL),
            )
            yield line, cell, int(trips[1])


def _quotas(sizes: Mapping[Cell, int], cap: int, weight: float) -> dict[Cell, int]:
    """Share `cap` features out over the cells, in proportion to size ** weight.

    `weight` is the one dial between the two ways of getting this wrong, and both
    ends of it have been on the map. At 1 every cell keeps the same *fraction*, which
    sounds like the fair answer and is not: a quarter of a city is still a city and a
    quarter of a country lane is nothing. At 0 every cell keeps the same *count*, and
    the cities go to speckle. See `config.OVERVIEW_WEIGHT` for what each measured.

    A cell can never keep more than it holds, so cells that saturate are given exactly
    their size and the rest re-share what they did not use. Without that the cap
    undershoots by whatever the countryside had no features to spend it on, and the
    cities are thinned to pay for roads that do not exist.
    """
    free = set(sizes)
    quotas: dict[Cell, int] = {}
    budget = float(cap)
    # Each pass either settles every remaining cell or retires at least one saturated
    # one, so this runs at most once per cell.
    while free:
        weighted = sum(sizes[c] ** weight for c in free)
        if weighted <= 0:
            break
        scale = budget / weighted
        full = [c for c in free if scale * sizes[c] ** weight >= sizes[c]]
        if not full:
            for c in free:
                # At least one, so a cell that holds any road at all still draws one.
                # Rounding alone empties the five sparsest cells in Great Britain.
                quotas[c] = max(1, round(scale * sizes[c] ** weight))
            break
        for c in full:
            quotas[c] = sizes[c]
            budget -= sizes[c]
            free.discard(c)
    return quotas


def _cell_floors(geojsonl: Path, cap: int, weight: float) -> dict[Cell, int]:
    """The `trips` a feature must reach to be drawn, worked out for each cell.

    Empty when the file already holds no more than `cap` features, which is what
    keeps this a no-op for a region that never troubles the tile size limit -- both
    parts of Ireland come through unfiltered and unchanged, and are the reference for
    what an unfiltered map looks like.

    A cell that is allowed to keep everything it holds gets no floor at all rather
    than a floor of zero, so `_hold_back` waves it through on the `.get` default.
    Under a `weight` below 1 that is most of the countryside.

    A histogram per cell rather than every feature's `trips`: the distinct counts are
    a fraction of the features, and nothing here should scale with the network. Ties
    at the floor are kept, so a cell can exceed its share by the width of one `trips`
    value -- overshooting hands tippecanoe a few more features than asked for, where
    undershooting would throw away roads that fit.
    """
    cells: defaultdict[Cell, defaultdict[int, int]] = defaultdict(lambda: defaultdict(int))
    total = 0
    for _, cell, trips in _features(geojsonl):
        cells[cell][trips] += 1
        total += 1
    if total <= cap:
        return {}

    sizes = {cell: sum(hist.values()) for cell, hist in cells.items()}
    quotas = _quotas(sizes, cap, weight)
    floors: dict[Cell, int] = {}
    for cell, hist in cells.items():
        quota = quotas.get(cell, 0)
        if quota >= sizes[cell]:
            continue
        kept = 0
        for trips in sorted(hist, reverse=True):
            kept += hist[trips]
            if kept >= quota:
                floors[cell] = trips
                break
    return floors


def _hold_back(geojsonl: Path, out: Path, floors: Mapping[Cell, int]) -> Path:
    """Write out only the features that reach their own cell's floor.

    Returns the input unchanged when there are no floors, so a region under its cap
    is handed the same file and pays for no copy.
    """
    if not floors:
        return geojsonl
    kept = 0
    seen: set[Cell] = set()
    with out.open("wb") as dst:
        for line, cell, trips in _features(geojsonl):
            seen.add(cell)
            if trips >= floors.get(cell, 0):
                dst.write(line)
                kept += 1
    grades = sorted(floors.values())
    # How many cells were left whole is the number worth watching. It is the
    # countryside, and the failure this filter keeps being rebuilt around is the
    # countryside being thinned in proportion to cities that can spare it.
    log.info(
        "held back to %d features; %d of %d cells thinned, %d kept whole; "
        "trips floor %d to %d, median %d",
        kept,
        len(floors),
        len(seen),
        len(seen) - len(floors),
        grades[0],
        grades[-1],
        grades[len(grades) // 2],
    )
    return out


def build_tiles(
    geojsonl: Path,
    out: Path | None = None,
    attribution: str | None = None,
    segments: Path | None = None,
    track: Path | None = None,
) -> Path:
    """Build the road bands and a pass per extra layer, and join them into one archive.

    tippecanoe stores attributes per feature per zoom, and -x is global to a run --
    there is no way to say "keep the road name only where someone might read it" in
    a single pass. So the road export is built in bands, the segments and track
    layers get a pass each, and tile-join concatenates whatever exists into one
    PMTiles file. The join is cheap.

    `attribution` goes into every pass. tile-join carries an input's attribution
    through to the joined archive -- measured, including where only one of the
    inputs has one -- but a band that can be inspected on its own should say where
    it came from, and passing it each time costs nothing.
    """
    for tool in ("tippecanoe", "tile-join"):
        if not shutil.which(tool):
            raise RuntimeError(
                f"{tool} is not on PATH. Install felt/tippecanoe "
                "(`brew install tippecanoe`, or use the Docker service in "
                "docker-compose.yml). The mapbox/tippecanoe fork is unmaintained and "
                "cannot write PMTiles."
            )

    # `default_out` and not the archive name directly, so a build given no path
    # answers the question the same way `build` does -- including its refusal to
    # write the default beside an archive this data root already publishes by name.
    out = out or default_out()
    attribution = attribution or licences.html(config.credit_parts())
    out.parent.mkdir(parents=True, exist_ok=True)

    # Every intermediate goes in a scratch directory, and only the finished archive
    # is moved into place. The output directory is served: `wayfare serve` offers
    # whatever `*.pmtiles` it globs there, and the viewer loads every archive it is
    # offered onto one map. Bands built beside the archive under their own
    # `.pmtiles` names were therefore advertised as regions for the length of a
    # publish -- an overview band and a detail band appearing next to Great Britain
    # as though they were two more countries.
    #
    # A subdirectory of the output rather than the system temp: glob does not
    # descend, and `translate_path` refuses a name with a separator in it, so
    # nothing here is reachable. It also keeps the rename below on one filesystem,
    # which is the whole reason it is atomic.
    with tempfile.TemporaryDirectory(dir=out.parent, prefix=".publish-") as scratch:
        tmp = Path(scratch)
        passes = [(band, geojsonl) for band in _road_bands(geojsonl)]
        # One pass over the whole zoom range rather than banded like the roads. The
        # bands exist to stop millions of edges paying for info-card attributes at
        # zooms nobody reads them at, and to thin the quietest roads out of the far
        # view; there are hundreds of segments, not millions, and a tram line thinned
        # out of its own layer would just be missing.
        if segments:
            band = _Band(
                "segments",
                config.MIN_ZOOM,
                config.MAX_ZOOM,
                {},
                (),
                layer=LAYER_SEGMENTS,
            )
            passes.append((band, segments))
        # Same single pass, same reasoning: 55,114 ways is not millions, and a
        # rail line thinned out of the only layer that draws it is just absent.
        #
        # The way id goes in the MVT feature id field, as it does for the detail
        # band and for the same two reasons: it is what `setFeatureState` addresses,
        # so a hover can light the track under the cursor, and a near-unique value is
        # the one thing a tile's attribute pool cannot dedupe. The default `id` is
        # not a property this export writes, so taking it would leave every feature
        # reaching the viewer with no id at all.
        if track:
            band = _Band(
                "track",
                config.MIN_ZOOM,
                config.MAX_ZOOM,
                {},
                (),
                layer=LAYER_TRACK,
                id_attribute="way_id",
            )
            passes.append((band, track))
        if not passes:
            # Louder than an empty archive. A published file with no features in it
            # loads without complaint and shows a blank map, which reads as a broken
            # viewer rather than as a stage that had nothing to write.
            raise RuntimeError(
                f"nothing to publish: {geojsonl} has no matched edges and there are "
                "no segments. Run `wayfare match` and `wayfare aggregate` first."
            )
        parts = [_run(band, src, tmp, attribution) for band, src in passes]
        joined = tmp / out.name
        _tile_join(joined, parts)
        size = joined.stat().st_size
        # os.replace, not shutil.move: a rename within one filesystem is atomic, so
        # a reader either gets the whole old archive or the whole new one. Writing
        # the final path directly left a window -- a republish is minutes of a file
        # that PMTiles clients are reading in byte ranges, and a range served across
        # the rewrite spans two different archives.
        os.replace(joined, out)

    log.info("tiles built: %.1f MB", size / 1e6)
    log.info("attribution: %s", attribution)
    return out


def _road_bands(geojsonl: Path) -> list[_Band]:
    """The passes over the road export, or none at all where it holds nothing.

    A region can have no matched edges -- Irish Rail on its own is 331 patterns and
    not one of them is a road -- and tippecanoe exits 110 on an empty input rather
    than writing an empty archive, so the road bands are skipped outright. That is
    the same rule the segments pass follows, in the other direction.

    The overview is three bands rather than one because a band is the only place a
    cap can be applied and the three parts of it are under different amounts of
    pressure. Both caps are `None` today and the quota machinery under them is
    dormant: what a low zoom holds is left to tippecanoe, and every cap tried thinned
    the whole country to spare the handful of tiles that would not fit. Read the
    block in `config` before reviving any of it.

    A cap, where one is set, is applied to the *input* and not with tippecanoe's own
    `-j`. That is not a style preference: `-x` runs first, so a `-j` filter naming an
    attribute the same command excludes matches nothing and writes an empty band.
    Measured on London -- `-x trips` with `-j` on `trips` built a 2.4 KB archive
    holding no tiles at all, and said nothing about it.
    """
    if not _has_features(geojsonl):
        return []
    far_floors, mid_floors = (
        _cell_floors(geojsonl, cap, config.OVERVIEW_WEIGHT) if cap else {}
        for cap in (config.OVERVIEW_CAP_FAR, config.OVERVIEW_CAP_MID)
    )
    return [
        _Band("far", config.MIN_ZOOM, config.FAR_ZOOM - 1, far_floors, _DETAIL_ONLY),
        _Band("mid", config.FAR_ZOOM, config.MID_ZOOM - 1, mid_floors, _DETAIL_ONLY),
        _Band("near", config.MID_ZOOM, config.DETAIL_ZOOM - 1, {}, _DETAIL_ONLY),
        # The one band that carries `way`, and so the one band that can spend it on
        # the feature id instead of an attribute. `id` is excluded by hand here
        # because `--use-attribute-for-id` consumes `way` instead, and a property
        # tippecanoe is not told to drop is a property it writes into every feature.
        #
        # It is also the only band that may extend past its own top zoom, because it
        # is the only one with nothing above it to overlap.
        _Band(
            "detail",
            config.DETAIL_ZOOM,
            config.MAX_ZOOM,
            {},
            ("id",),
            extend=True,
            id_attribute="way",
            low_detail=config.LOW_DETAIL,
        ),
    ]


def _run(band: _Band, geojsonl: Path, tmp: Path, attribution: str) -> Path:
    """Build one band into the scratch directory, and say what it wrote.

    A band with no floors is handed the file it was given rather than a copy of it,
    so a region under its cap -- which is every region today -- pays for no extra
    pass over a 1.6 GB export.
    """
    src = _hold_back(geojsonl, tmp / f"{band.name}.geojsonl", band.floors)
    part = tmp / f"{band.name}.pmtiles"
    _tippecanoe(band, src, part, attribution=attribution)
    return part


def _has_features(geojsonl: Path) -> bool:
    """Whether a GeoJSONL file holds anything worth handing to tippecanoe.

    One feature per line, so a non-empty file has at least one. Size rather than a
    line count because the national edge export is 1.6 GB and the question is only
    ever "is this empty".
    """
    return geojsonl.exists() and geojsonl.stat().st_size > 0


def _tippecanoe(band: _Band, geojsonl: Path, out: Path, *, attribution: str) -> None:
    cmd = [
        "tippecanoe",
        "-o",
        str(out),
        "--force",
        "-l",
        band.layer,
        "-Z",
        str(band.min_zoom),
        "-z",
        str(band.max_zoom),
        # One id belongs in the MVT feature id field rather than in the attributes.
        # It is two varints and a pool entry per feature cheaper there, and it is
        # where setFeatureState looks -- so the viewer needs no promoteId either.
        #
        # Which id pays for itself is measured, and it is the OSM way. A tile's
        # value pool dedupes repeated attributes within the tile, and it does that
        # 5.9x for `refs` and 6.1x for `name` against 1.37x for `way`: a way id is
        # near-unique per feature, so it is the one attribute pooling cannot help.
        # Moving it into the id field takes 20.28% off Great Britain's detail band,
        # 21.5 MB, and saves more than deleting it outright would, because a sorted
        # way id compresses where the Valhalla GraphId it displaces did not.
        #
        # It costs uniqueness. A way is several features wherever its service set
        # changes along it, and they now share an id -- which the MVT spec asks for
        # and does not require, MapLibre does not mind, and the viewer turns into
        # hovering a whole way rather than one segment of it.
        f"--use-attribute-for-id={band.id_attribute}",
        "-x",
        band.id_attribute,
        # Where the credit is kept. It lands in the tileset metadata, PMTiles carries
        # that verbatim, and MapLibre reads a source's own attribution into the
        # control without the page saying anything -- so both the viewer and the art
        # page's window picker credit whichever archive they happen to be showing.
        f"--attribution={attribution}",
        # The national GeoJSONL is around 1.6 GB. Reading it single-threaded is
        # minutes of wall clock for nothing.
        "-P",
        # Keep every road at high zoom; shed the quietest ones when a low-zoom tile
        # would otherwise be too large. Without this, dense cities lose whole areas
        # rather than losing their least-served streets.
        "--drop-densest-as-needed",
        # Line simplification is what makes national coverage tractable, but at max
        # zoom the geometry should be the real road.
        f"--simplification={config.SIMPLIFICATION}",
        # Left at tippecanoe's default until it was measured. It is a Mapbox hosting
        # limit rather than anything about the format, and this archive is served off
        # a box over range requests, so it is ours to set.
        f"--maximum-tile-bytes={config.MAX_TILE_BYTES}",
    ]
    if band.low_detail is not None:
        cmd += ["-D", str(band.low_detail)]
    if config.SIMPLIFY_SHARED_NODES:
        cmd.append("--no-simplification-of-shared-nodes")
    if band.extend:
        cmd.append("--extend-zooms-if-still-dropping")
    for name in band.exclude:
        cmd += ["-x", name]
    cmd.append(str(geojsonl))

    log.info("tippecanoe z%d-z%d -> %s", band.min_zoom, band.max_zoom, out.name)
    # tippecanoe writes a per-tile progress bar to stderr -- hundreds of kilobytes
    # of it for a national build. On a server run that buries everything else in
    # the log, so it is captured and reduced to what actually matters.
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        log.error("tippecanoe failed:\n%s", _tail(proc.stderr))
        raise subprocess.CalledProcessError(proc.returncode, cmd, proc.stdout, proc.stderr)
    _report_dropping(proc.stderr)


def _tile_join(out: Path, parts: list[Path]) -> None:
    cmd = ["tile-join", "-o", str(out), "--force", "-pk", *[str(p) for p in parts]]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        log.error("tile-join failed:\n%s", _tail(proc.stderr))
        raise subprocess.CalledProcessError(proc.returncode, cmd, proc.stdout, proc.stderr)


# tippecanoe announces each thinning decision as it fills a tile.
_DROPPED = re.compile(r"keeping the sparsest ([\d.]+)% of the features")


def _report_dropping(stderr: str) -> None:
    """Say how hard the tiles were thinned to fit the size limit.

    --drop-densest-as-needed silently sheds features to keep a tile under the size
    limit. That is the right behaviour, but a build that kept a quarter of the
    network at low zoom should say so rather than look like full coverage.

    This reports that backstop only. It is *not* a statement that every zoom holds
    every road, and must not be read as one: a tile is a 4096-unit grid, so at z5 a
    unit is about 300 m and a 40 m segment simplifies to nothing and is discarded
    long before any size limit is reached. Measured on Wales and London together,
    z5 carries 21,720 of 136,393 features for that reason alone. Losing sub-pixel
    geometry at low zoom is correct; conflating it with "nothing was dropped" is
    what makes a thinned map look complete.
    """
    kept = [float(m) for m in _DROPPED.findall(stderr)]
    if not kept:
        log.info("no tile hit the size limit; nothing was thinned to fit")
        return
    log.info(
        "thinned %d tiles to fit; sparsest kept %.1f%% of its features "
        "(low zooms only -- max zoom %d is complete)",
        len(kept),
        min(kept),
        config.MAX_ZOOM,
    )


def _tail(text: str, lines: int = 20) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def default_out(region: str | None = None) -> Path:
    """Where a publish that was given no path writes -- and when it refuses to.

    The default name has not moved. An archive filename is a deployment's contract:
    a Compose mount, a bucket object key, a `?tiles=` URL, and the viewer's own
    `./bus.pmtiles` fallback. Renaming it under a running deployment would leave the
    served file untouched while the command still reported success, which is the one
    failure this whole area exists to avoid.

    The mirror image of that failure is visible from here, so it is caught here. An
    archive already named after this region, in the directory about to receive a
    `bus.pmtiles`, says the caller has published this region by name before and has
    just left the flag off. Writing the default beside it would update nothing anyone
    serves, so it stops instead of quietly succeeding.
    """
    named = config.OUT / config.archive_name(region)
    if named.exists():
        raise RuntimeError(
            f"{named} is already here, so this data root publishes by region: "
            f"writing {DEFAULT_ARCHIVE} beside it would leave {named.name} stale. "
            "Pass --name-by-region to rewrite it, or --out to name an archive."
        )
    return config.OUT / DEFAULT_ARCHIVE


def contents(con: duckdb.DuckDBPyConnection) -> dict[str, bool]:
    """What this archive actually holds, which is what decides its credits.

    Read from the database rather than assumed, because every part is optional: a
    region with no matched edges owes OpenStreetMap nothing, and one with no segments
    has no operator geometry to name. Getting any of them wrong is a licence statement
    that is false in one direction or the other, and none of it is visible in the
    picture.

    `track` is the third, and it is the one that is easy to get backwards. A pattern
    drawn by `wayfare trace` carries OpenStreetMap geometry without any edge in
    `edge_services` -- nothing routed it -- so an archive of nothing but Underground
    track would read as owing ODbL nothing while being wholly derived from OSM.
    """

    return {
        "road": _has_rows(con, "edge_services"),
        "operator": _has_rows(con, "segments"),
        "track": _has_traced_segments(con),
    }


# What a publish with no database to read assumes it is publishing. It names every
# key `contents` answers, so a fourth kind of geometry added there fails here as a
# missing key rather than passing silently as a credit that omits it.
ROAD_ONLY: dict[str, bool] = {"road": True, "operator": False, "track": False}


def _has_traced_segments(con: duckdb.DuckDBPyConnection) -> bool:
    """Whether anything actually drawn came from an OSM route relation.

    Two layers can carry that geometry and either one alone owes the credit, so
    this asks both. `track_services` is the per-way inversion and is rebuilt every
    `aggregate` against the live patterns, so its being non-empty is already a
    statement about this archive.

    The join on the other arm is the point. `traces` is a cache keyed on pattern
    identity and keeps its rows when a service leaves the timetable, exactly as
    `match_status` does, so its being non-empty says what was once resolved rather
    than what this archive holds. `segments` is rebuilt against the current feed
    every `aggregate`, so the intersection is the honest answer -- and a credit has
    to describe the bytes being published, not the database they came out of.

    Both arms have to be asked, because a pattern reaches exactly one of them: an
    archive of nothing but relation track has an empty `segments` while being wholly
    derived from OpenStreetMap, and asking that arm alone would credit none of it.
    """
    if _has_rows(con, "track_services"):
        return True
    if not (db.table_exists(con, "traces") and db.table_exists(con, "segments")):
        return False
    return bool(
        db.scalar(con, "SELECT count(*) FROM segments s JOIN traces t USING (pattern_id)")
    )


def _has_rows(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    """Whether a table exists and holds anything.

    A table that is not there reads as empty rather than raising. `segments` post-dates
    Great Britain's database and `prune` reclaims tables once matching is done, so an
    older or pruned data root is a normal thing to be handed -- and `contents` reads
    this into the credit, where raising would fail the publish over a mode the region
    does not have.
    """
    if not db.table_exists(con, table):
        return False
    return bool(db.scalar(con, f"SELECT count(*) FROM {table}"))  # noqa: S608


def build(
    con: duckdb.DuckDBPyConnection | None = None,
    region: str | None = None,
    out: Path | None = None,
    from_export: Path | None = None,
) -> Path:
    """Export the matched network and build the archive from it.

    `from_export` builds from a GeoJSONL that is already on disk and takes no
    connection. That is for a data root whose database has been taken away -- a
    `prune` reclaims the tables `match` needed, and the export is then the only
    record of the network left in it. The tiles are the same tiles: the export is
    deterministic, so rebuilding from one is rebuilding from the rows that wrote it.
    It is not a way to refresh a region, because nothing here can tell how old the
    file is.
    """
    config.ensure_dirs()
    if from_export is None:
        if con is None:
            raise ValueError("publish needs a connection unless it is given an export")
        from_export = export_edges_geojsonl(con)
    elif not from_export.exists():
        raise RuntimeError(
            f"{from_export} is not there. --from-export names the GeoJSONL a previous "
            "publish wrote, and this data root has none."
        )
    # Without a connection there is no `segments` table to read and no way to tell
    # what the archive holds, so the credit falls back to what a road-only export
    # owes -- which is what this path exists for. The segments GeoJSONL is
    # deliberately not rebuilt from the export instead: the export *is* the road
    # network, and a rebuild that quietly dropped a region's trams would look like a
    # successful publish.
    held = contents(con) if con is not None else ROAD_ONLY
    return build_tiles(
        from_export,
        out or default_out(region),
        attribution=licences.html(config.credit_parts(region, **held)),
        segments=export_segments_geojsonl(con) if con is not None else None,
        track=export_track_geojsonl(con) if con is not None else None,
    )
