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
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import duckdb

from . import config, logs

log = logs.get("publish")

LAYER = "bus"

Point = tuple[int, int]  # (lon_e6, lat_e6)


# How many rows to pull from DuckDB at a time. Only ever this many rows plus one
# way's worth are resident; the previous fetchall() held the entire edge table as
# Python objects, which at national scale is several million rows of tuples and
# integer lists.
FETCH_ROWS = 50_000


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


def build_tiles(
    geojsonl: Path, out: Path | None = None, attribution: str | None = None
) -> Path:
    """Build the archive in two zoom bands and join them.

    tippecanoe stores attributes per feature per zoom, and -x is global to a run --
    there is no way to say "keep the road name only where someone might read it" in
    a single pass. So the overview band is built without the four attributes that
    exist purely for the info card, the detail band is built with everything, and
    tile-join concatenates the two into one PMTiles file. The join is cheap; the
    second tippecanoe pass is the real cost, and it only touches z5-z10.

    `attribution` goes into both passes. tile-join carries an input's attribution
    through to the joined archive -- measured, including where only one of the two
    inputs has one -- but a band that can be inspected on its own should say where
    it came from, and passing it twice costs nothing.
    """
    out = out or (config.OUT / "bus.pmtiles")
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
        overview, detail = tmp / "overview.pmtiles", tmp / "detail.pmtiles"
        joined = tmp / out.name
        _tippecanoe(
            geojsonl,
            overview,
            config.MIN_ZOOM,
            config.DETAIL_ZOOM - 1,
            _DETAIL_ONLY,
            attribution,
        )
        _tippecanoe(geojsonl, detail, config.DETAIL_ZOOM, config.MAX_ZOOM, (), attribution)
        _tile_join(joined, [overview, detail])
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


def _tippecanoe(
    geojsonl: Path,
    out: Path,
    min_zoom: int,
    max_zoom: int,
    exclude: Sequence[str],
    attribution: str,
) -> None:
    cmd = [
        "tippecanoe",
        "-o",
        str(out),
        "--force",
        "-l",
        LAYER,
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
        "--extend-zooms-if-still-dropping",
        # Line simplification is what makes national coverage tractable, but at max
        # zoom the geometry should be the real road.
        "--simplification=4",
        "--no-simplification-of-shared-nodes",
    ]
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


def build(con: duckdb.DuckDBPyConnection, region: str | None = None) -> Path:
    config.ensure_dirs()
    return build_tiles(export_geojsonl(con), attribution=config.credit_html(region))
