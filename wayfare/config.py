"""Paths, tunables, and environment.

Everything the pipeline writes lives under ``WAYFARE_DATA`` so a server run can be
pointed at a big volume and nothing escapes into the source tree.
"""

from __future__ import annotations

import os
from pathlib import Path

# --- Layout ----------------------------------------------------------------

DATA = Path(os.environ.get("WAYFARE_DATA", "data")).resolve()
RAW = DATA / "raw"  # downloads, exactly as fetched
WORK = DATA / "work"  # unpacked and intermediate files
OUT = DATA / "out"  # publishable artefacts (pmtiles, geojson, art)
DB_PATH = WORK / "wayfare.duckdb"

# --- Sources ---------------------------------------------------------------

# BODS publishes one national GTFS bundle. Regional slugs exist (see PLAN.md) and
# are useful for development, but `all` is what a production run wants.
BODS_GTFS_URL = "https://data.bus-data.dft.gov.uk/timetable/download/gtfs-file/{region}/"
BODS_REGION = os.environ.get("WAYFARE_REGION", "all")

# Geofabrik rebuilds these daily. The Valhalla graph is built from this exact file,
# and Valhalla edge ids are only stable within one graph build -- so the extract is
# pinned by copying it into RAW and never re-downloading unless asked.
OSM_GB_URL = "https://download.geofabrik.de/europe/great-britain-latest.osm.pbf"
OSM_IE_NI_URL = (
    "https://download.geofabrik.de/europe/ireland-and-northern-ireland-latest.osm.pbf"
)

NAPTAN_URL = "https://naptan.api.dft.gov.uk/v1/access-nodes?dataFormat=csv"

# BODS blocks requests that look like generic scrapers.
USER_AGENT = os.environ.get(
    "WAYFARE_UA", "wayfare/0.1 (+https://github.com/benmandrew/wayfare)"
)

# --- Acquisition -----------------------------------------------------------

DOWNLOAD_CHUNK = 1 << 20
DOWNLOAD_RETRIES = 5
DOWNLOAD_BACKOFF = 30.0  # seconds, multiplied by attempt number
# BODS streams its bulk files with no Content-Length, so a truncated download looks
# like a complete one. Anything smaller than this is treated as a failed fetch.
MIN_GTFS_BYTES = 100 << 20

# --- Valhalla --------------------------------------------------------------

VALHALLA_URL = os.environ.get("WAYFARE_VALHALLA", "http://localhost:8002")
VALHALLA_TIMEOUT = 120.0
VALHALLA_WORKERS = int(os.environ.get("WAYFARE_WORKERS", "6"))

# A pattern whose stops are further apart than this is likely an express or a data
# error; matching it point-to-point tends to invent plausible-but-wrong roads.
MAX_STOP_GAP_M = 25_000
# Valhalla's own score for how well the trace fits the road graph.
MIN_MATCH_CONFIDENCE = 0.30
# Reject a match whose road distance wildly exceeds the straight-line stop chain.
# The slack term matters: on a short pattern a ratio alone is meaningless, because
# a one-way system that sends the bus around a single block can triple a 300 m
# span. Both bounds must be exceeded before a match is called bad.
MAX_DETOUR_RATIO = 3.0
DETOUR_SLACK_M = 1_000.0

# --- Matching batch --------------------------------------------------------

# Results are committed this often. Smaller means less lost work when the server is
# restarted mid-run; larger means fewer write transactions.
CHECKPOINT_EVERY = 200

# --- Publishing ------------------------------------------------------------

MIN_ZOOM = 5
MAX_ZOOM = 14
# Tile attributes are per-feature per-zoom, so a long service list on a busy city
# centre edge dominates tile size. Beyond this many, the tile carries a count and
# the viewer fetches the full list from the sidecar.
MAX_REFS_IN_TILE = 12


def ensure_dirs() -> None:
    for d in (RAW, WORK, OUT):
        d.mkdir(parents=True, exist_ok=True)
