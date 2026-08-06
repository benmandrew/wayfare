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
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

import duckdb

from . import config, logs

log = logs.get("publish")

LAYER = "bus"

Point = tuple[int, int]  # (lon_e6, lat_e6)


def export_geojsonl(con: duckdb.DuckDBPyConnection, path: Path | None = None) -> Path:
    """Write one GeoJSON feature per line, which is what tippecanoe wants."""
    path = path or (config.WORK / "edges.geojsonl")
    path.parent.mkdir(parents=True, exist_ok=True)

    rows = con.execute(
        """
        SELECT e.edge_id, e.way_id, e.road_name, e.lon_e6, e.lat_e6,
               s.n, s.refs, s.trips
        FROM edges e
        JOIN (
            SELECT edge_id,
                   count(*)          AS n,
                   list(short_name ORDER BY n_trips DESC) AS refs,
                   sum(n_trips)      AS trips
            FROM edge_services GROUP BY edge_id
        ) s USING (edge_id)
        WHERE e.lon_e6 IS NOT NULL
        """
    ).fetchall()

    n_written = 0
    n_capped = 0

    with path.open("w") as fh:
        for props, coords in coalesce(rows):
            n = props["n"]
            n_capped += n > config.MAX_REFS_IN_TILE
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
        len(rows),
        n_written,
        path,
        n_capped,
        config.MAX_REFS_IN_TILE,
    )
    return path


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
    for edge_id, way_id, name, lon_e6, lat_e6, n, refs, trips in rows:
        pts = list(zip(lon_e6, lat_e6, strict=True))
        if len(pts) < 2:
            continue
        capped = tuple(refs[: config.MAX_REFS_IN_TILE])
        # refs is ordered by frequency, so the same set can arrive in two orders.
        # Group on the sorted form and carry the frequency-ordered one through.
        key = (way_id, name, int(n), int(trips or 0), tuple(sorted(capped)), capped)
        groups[key].append((edge_id, pts))

    out = []
    for (way_id, name, n, trips, _sorted_refs, capped), members in groups.items():
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
    """
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

        out.append((min(ids), chain))
    return out


def build_tiles(geojsonl: Path, out: Path | None = None) -> Path:
    """Run tippecanoe. Requires felt/tippecanoe >= 2.17 for native PMTiles output."""
    out = out or (config.OUT / "bus.pmtiles")
    out.parent.mkdir(parents=True, exist_ok=True)

    if not shutil.which("tippecanoe"):
        raise RuntimeError(
            "tippecanoe is not on PATH. Install felt/tippecanoe "
            "(`brew install tippecanoe`, or use the Docker service in "
            "docker-compose.yml). The mapbox/tippecanoe fork is unmaintained and "
            "cannot write PMTiles."
        )

    cmd = [
        "tippecanoe",
        "-o", str(out),
        "--force",
        "-l", LAYER,
        "-Z", str(config.MIN_ZOOM),
        "-z", str(config.MAX_ZOOM),
        # The edge id belongs in the MVT feature id field, not in the attributes.
        # It is two varints and a pool entry per feature cheaper there, and it is
        # where setFeatureState looks -- so the viewer needs no promoteId either.
        "--use-attribute-for-id=id",
        "-x", "id",
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
        str(geojsonl),
    ]
    log.info("tippecanoe -> %s", out)
    # tippecanoe writes a per-tile progress bar to stderr -- hundreds of kilobytes
    # of it for a national build. On a server run that buries everything else in
    # the log, so it is captured and reduced to what actually matters.
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        log.error("tippecanoe failed:\n%s", _tail(proc.stderr))
        raise subprocess.CalledProcessError(proc.returncode, cmd, proc.stdout, proc.stderr)

    _report_dropping(proc.stderr)
    log.info("tiles built: %.1f MB", out.stat().st_size / 1e6)
    return out


# tippecanoe announces each thinning decision as it fills a tile.
_DROPPED = re.compile(r"keeping the sparsest ([\d.]+)% of the features")


def _report_dropping(stderr: str) -> None:
    """Say how hard the tiles were thinned.

    --drop-densest-as-needed silently sheds features to keep a tile under the size
    limit. That is the right behaviour, but a build that kept a quarter of the
    network at low zoom should say so rather than look like full coverage.
    """
    kept = [float(m) for m in _DROPPED.findall(stderr)]
    if not kept:
        log.info("no features dropped; every zoom holds the full network")
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


def build(con: duckdb.DuckDBPyConnection) -> Path:
    config.ensure_dirs()
    return build_tiles(export_geojsonl(con))


