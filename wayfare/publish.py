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
    subprocess.run(cmd, check=True)
    log.info("tiles built: %.1f MB", out.stat().st_size / 1e6)
    return out


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
