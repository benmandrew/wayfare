"""Turn the matched network into vector tiles for the web viewer.

The output is PMTiles: a single file, read over HTTP range requests, needing no
tile server. For a dataset that is rebuilt occasionally and read constantly that is
the right shape -- it can sit on R2 or S3 behind a CDN and cost nothing to serve.

The one real design decision here is what goes *in* the tiles. Tippecanoe stores
attributes per feature per zoom, so a full service list on every edge in central
London would dominate tile size. Instead each edge carries a capped list of service
numbers plus the true count, and the viewer falls back to a sidecar lookup for the
handful of edges that overflow. Almost every edge is under the cap, so the sidecar
is rarely touched and the common case stays a pure tile read.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import duckdb

from . import config, logs

log = logs.get("publish")

LAYER = "bus"


def export_geojsonl(con: duckdb.DuckDBPyConnection, path: Path | None = None) -> Path:
    """Write one GeoJSON feature per line, which is what tippecanoe wants."""
    path = path or (config.WORK / "edges.geojsonl")
    path.parent.mkdir(parents=True, exist_ok=True)

    rows = con.execute(
        """
        SELECT e.edge_id, e.way_id, e.road_name, e.geom,
               s.n, s.refs, s.trips
        FROM edges e
        JOIN (
            SELECT edge_id,
                   count(*)          AS n,
                   list(short_name ORDER BY n_trips DESC) AS refs,
                   sum(n_trips)      AS trips
            FROM edge_services GROUP BY edge_id
        ) s USING (edge_id)
        WHERE e.geom IS NOT NULL
        """
    ).fetchall()

    n_written = 0
    overflow: dict[str, list[str]] = {}

    with path.open("w") as fh:
        for edge_id, way_id, name, wkt, n, refs, trips in rows:
            coords = _wkt_to_coords(wkt)
            if len(coords) < 2:
                continue
            capped = refs[: config.MAX_REFS_IN_TILE]
            if n > config.MAX_REFS_IN_TILE:
                overflow[str(edge_id)] = refs
            props = {
                "id": int(edge_id),
                "way": int(way_id),
                "n": int(n),
                "refs": ",".join(capped),
                "trips": int(trips or 0),
            }
            if name:
                props["name"] = name
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

    sidecar = config.OUT / "overflow.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps(overflow, separators=(",", ":")))

    log.info(
        "%d features to %s (%d edges exceed the %d-service tile cap)",
        n_written,
        path,
        len(overflow),
        config.MAX_REFS_IN_TILE,
    )
    return path


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


def _wkt_to_coords(wkt: str) -> list[list[float]]:
    """Parse 'LINESTRING(lon lat, lon lat, ...)'.

    The WKT here is written by this codebase, never read from elsewhere, so a split
    is sufficient and a WKT parser dependency is not warranted.
    """
    inner = wkt[wkt.index("(") + 1 : wkt.rindex(")")]
    coords = []
    for pair in inner.split(","):
        lon, lat = pair.split()
        coords.append([round(float(lon), 6), round(float(lat), 6)])
    return coords
