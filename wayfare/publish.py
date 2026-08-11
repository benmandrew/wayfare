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
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import duckdb

from . import config, logs

log = logs.get("publish")

LAYER = "bus"

# The non-road modes, drawn from operator geometry rather than matched. A layer of
# its own rather than a `mode` attribute on `bus`, because the two carry different
# attributes and the viewer styles them differently -- and because tile-join keeps
# distinct layer names, so this costs one more tippecanoe pass and nothing else.
LAYER_SEGMENTS = "segments"

# The archive a publish writes when it is told neither a path nor to use the region's
# name. Region-agnostic, and deliberately still the default -- see `default_out`.
DEFAULT_ARCHIVE = "bus.pmtiles"

Point = tuple[int, int]  # (lon_e6, lat_e6)


# How many rows to pull from DuckDB at a time. Only ever this many rows plus one
# way's worth are resident; the previous fetchall() held the entire edge table as
# Python objects, which at national scale is several million rows of tuples and
# integer lists.
FETCH_ROWS = 50_000


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
    n = con.execute("SELECT count(*) FROM segments").fetchone()
    if not n or not n[0]:
        return None

    path = path or (config.WORK / "segments.geojsonl")
    path.parent.mkdir(parents=True, exist_ok=True)

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

    with path.open("w") as fh:
        for pattern_id, mode, lon_e6, lat_e6, ref, trips in rows:
            fh.write(
                json.dumps(
                    {
                        "type": "Feature",
                        "properties": {
                            "id": pattern_id,
                            "mode": mode,
                            "ref": ref,
                            "trips": trips,
                        },
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [
                                # strict: the two lists are one polyline stored as
                                # parallel arrays, so a length mismatch is corrupt
                                # geometry. Truncating to the shorter would draw a
                                # confident line that stops halfway.
                                [x / 1e6, y / 1e6]
                                for x, y in zip(lon_e6, lat_e6, strict=True)
                            ],
                        },
                    },
                    separators=(",", ":"),
                )
            )
            fh.write("\n")

    log.info("%d non-road segments written to %s", len(rows), path)
    return path


def export_geojsonl(con: duckdb.DuckDBPyConnection, path: Path | None = None) -> Path:
    """Write one GeoJSON feature per line, which is what tippecanoe wants.

    Streamed rather than materialised. The query is ordered by way_id and coalescing
    never merges across ways, so a way's edges can be collapsed and released as soon
    as the next way starts -- the result is identical to collapsing the whole table
    at once, at a fraction of the resident memory. DuckDB does the sort out of core,
    which is exactly the part it is better at than the Python heap.
    """
    path = path or (config.WORK / "edges.geojsonl")
    path.parent.mkdir(parents=True, exist_ok=True)

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

    n_written = 0
    n_capped = 0
    stats = {"edges": 0}

    with path.open("w") as fh:
        for props, coords in _coalesce_by_way(cur, stats):
            n_capped += props["n"] > config.MAX_REFS_IN_TILE
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
            n_written += 1

    log.info(
        "%d edges coalesced to %d features in %s (%d over the %d-service cap)",
        stats["edges"],
        n_written,
        path,
        n_capped,
        config.MAX_REFS_IN_TILE,
    )
    return path


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
                # Consumed by --use-attribute-for-id and then excluded, so it costs
                # nothing in the tile: it lands in the MVT feature id field, which
                # is what setFeatureState addresses. The lowest edge id in the
                # segment names it, so the id is stable for a given build.
                "id": int(edge_id),
                "way": int(way_id),
                "n": n,
                "refs": ",".join(capped),
                "trips": trips,
            }
            if name:
                props["name"] = name
            out.append((props, [[x / 1e6, y / 1e6] for x, y in pts]))
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


# Only `n` survives below DETAIL_ZOOM; it is what the colour and width ramps read.
_DETAIL_ONLY = ("way", "refs", "trips", "name")

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


def _cell_floors(geojsonl: Path, cap: int) -> dict[Cell, int]:
    """The `trips` a feature must reach to be drawn, worked out for each cell.

    Empty when the file already holds no more than `cap` features, which is what
    keeps this a no-op for a region that never troubles the tile size limit -- both
    parts of Ireland come through unfiltered and unchanged.

    Every cell keeps the same *fraction* of what it holds, so the map's spatial
    distribution survives the cut and the floor is a local one. See the cap in
    `config` for what a single national floor did instead, which was to draw the
    cities and nothing between them.

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

    share = cap / total
    floors: dict[Cell, int] = {}
    for cell, hist in cells.items():
        # At least one, so a cell that holds any road at all still draws one. Rounding
        # alone empties the five sparsest cells in Great Britain.
        quota = max(1, round(share * sum(hist.values())))
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
    with out.open("wb") as dst:
        for line, cell, trips in _features(geojsonl):
            if trips >= floors.get(cell, 0):
                dst.write(line)
                kept += 1
    grades = sorted(floors.values())
    log.info(
        "held back to %d features across %d cells; trips floor %d to %d, median %d",
        kept,
        len(floors),
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
) -> Path:
    """Build the archive in three zoom bands and join them.

    tippecanoe stores attributes per feature per zoom, and -x is global to a run --
    there is no way to say "keep the road name only where someone might read it" in
    a single pass. So the two overview bands are built without the four attributes
    that exist purely for the info card, the detail band is built with everything,
    and tile-join concatenates the three into one PMTiles file. The join is cheap.

    The overview is two bands rather than one because only the far half needs the
    quietest roads held back: below `FAR_ZOOM` the input is cut to
    `OVERVIEW_CAP_FAR` features, and at `FAR_ZOOM` and above every road is handed
    over and tippecanoe's own backstop is left to it. See the cap in `config` for
    what it is measured against and why the choice is not left to tippecanoe.

    The holding back is done to the *input* and not with tippecanoe's own `-j`.
    That is not a style preference: `-x` runs first, so a `-j` filter naming an
    attribute the same command excludes matches nothing and writes an empty band.
    Measured on London -- `-x trips` with `-j` on `trips` built a 2.4 KB archive
    holding no tiles at all, and said nothing about it.

    `attribution` goes into both passes. tile-join carries an input's attribution
    through to the joined archive -- measured, including where only one of the two
    inputs has one -- but a band that can be inspected on its own should say where
    it came from, and passing it twice costs nothing.
    """
    out = out or (config.OUT / DEFAULT_ARCHIVE)
    attribution = attribution or config.credit_html()
    out.parent.mkdir(parents=True, exist_ok=True)

    for tool in ("tippecanoe", "tile-join"):
        if not shutil.which(tool):
            raise RuntimeError(
                f"{tool} is not on PATH. Install felt/tippecanoe "
                "(`brew install tippecanoe`, or use the Docker service in "
                "docker-compose.yml). The mapbox/tippecanoe fork is unmaintained and "
                "cannot write PMTiles."
            )

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
        joined = tmp / out.name
        parts = []
        # A region can have no matched edges at all -- Irish Rail on its own is 331
        # patterns and not one of them is a road -- and tippecanoe exits 110 on an
        # empty input rather than writing an empty archive. Skipping the road bands
        # is the same rule the segments pass already follows, in the other direction.
        if _has_features(geojsonl):
            far_floors = _cell_floors(geojsonl, config.OVERVIEW_CAP_FAR)
            # Only the last band may extend past its own top zoom. `-z` is a ceiling
            # that --extend-zooms-if-still-dropping is allowed to raise, which is
            # harmless when there is nothing above it and silently wrong when there
            # is: a far band that grew from z7 to z9 -- measured, on Great Britain --
            # overlaps the near band, and tile-join merges the two into tiles holding
            # both copies of every road.
            bands = [
                (
                    "far",
                    config.MIN_ZOOM,
                    config.FAR_ZOOM - 1,
                    far_floors,
                    _DETAIL_ONLY,
                    False,
                ),
                ("near", config.FAR_ZOOM, config.DETAIL_ZOOM - 1, {}, _DETAIL_ONLY, False),
                ("detail", config.DETAIL_ZOOM, config.MAX_ZOOM, {}, (), True),
            ]
            for name, lo, hi, floors, exclude, extend in bands:
                src = _hold_back(geojsonl, tmp / f"{name}.geojsonl", floors)
                part = tmp / f"{name}.pmtiles"
                _tippecanoe(src, part, lo, hi, exclude, attribution, extend)
                parts.append(part)
        # One pass over the whole zoom range rather than banded like the roads. The
        # bands exist to stop millions of edges paying for info-card attributes at
        # zooms nobody reads them at, and to thin the quietest roads out of the far
        # view; there are hundreds of segments, not millions, and a tram line thinned
        # out of its own layer would just be missing. `extend` is off because the
        # band already reaches MAX_ZOOM and there is nothing above it to grow into.
        if segments:
            tiles = tmp / "segments.pmtiles"
            _tippecanoe(
                segments,
                tiles,
                config.MIN_ZOOM,
                config.MAX_ZOOM,
                (),
                attribution,
                False,
                layer=LAYER_SEGMENTS,
            )
            parts.append(tiles)
        if not parts:
            # Louder than an empty archive. A published file with no features in it
            # loads without complaint and shows a blank map, which reads as a broken
            # viewer rather than as a stage that had nothing to write.
            raise RuntimeError(
                f"nothing to publish: {geojsonl} has no matched edges and there are "
                "no segments. Run `wayfare match` and `wayfare aggregate` first."
            )
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


def _has_features(geojsonl: Path) -> bool:
    """Whether a GeoJSONL file holds anything worth handing to tippecanoe.

    One feature per line, so a non-empty file has at least one. Size rather than a
    line count because the national edge export is 1.6 GB and the question is only
    ever "is this empty".
    """
    return geojsonl.exists() and geojsonl.stat().st_size > 0


def _tippecanoe(
    geojsonl: Path,
    out: Path,
    min_zoom: int,
    max_zoom: int,
    exclude: Sequence[str],
    attribution: str,
    extend: bool = True,
    layer: str = LAYER,
) -> None:
    cmd = [
        "tippecanoe",
        "-o",
        str(out),
        "--force",
        "-l",
        layer,
        "-Z",
        str(min_zoom),
        "-z",
        str(max_zoom),
        # The edge id belongs in the MVT feature id field, not in the attributes.
        # It is two varints and a pool entry per feature cheaper there, and it is
        # where setFeatureState looks -- so the viewer needs no promoteId either.
        "--use-attribute-for-id=id",
        "-x",
        "id",
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
        "--simplification=4",
        "--no-simplification-of-shared-nodes",
    ]
    if extend:
        cmd.append("--extend-zooms-if-still-dropping")
    for name in exclude:
        cmd += ["-x", name]
    cmd.append(str(geojsonl))

    log.info("tippecanoe z%d-z%d -> %s", min_zoom, max_zoom, out.name)
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

    Read from the database rather than assumed, because both halves are now
    optional: a region with no matched edges owes OpenStreetMap nothing, and one
    with no segments has no operator geometry to name. Getting either wrong is a
    licence statement that is false in one direction or the other, and neither is
    visible in the picture.
    """

    def any_rows(sql: str) -> bool:
        row = con.execute(sql).fetchone()
        return bool(row and row[0])

    return {
        "road": any_rows("SELECT count(*) FROM edge_services"),
        "operator": any_rows("SELECT count(*) FROM segments"),
    }


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
        from_export = export_geojsonl(con)
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
    held = contents(con) if con is not None else {"road": True, "operator": False}
    return build_tiles(
        from_export,
        out or default_out(region),
        attribution=config.credit_html(region, **held),
        segments=export_segments_geojsonl(con) if con is not None else None,
    )
