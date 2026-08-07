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

# Geofabrik rebuilds these daily. Valhalla downloads its own copy at graph-build
# time (see `tile_urls` in docker-compose.yml), so the pipeline does not need the
# pbf at all -- nothing in Python reads it. Fetching it here is purely archival: a
# record of which extract a set of edge ids belongs to. Hence `acquire --with-osm`
# rather than doing it by default and spending 2 GB to no purpose.
GEOFABRIK = "https://download.geofabrik.de/europe/"
OSM_EXTRACTS = {
    "all": "great-britain-latest.osm.pbf",
    "england": "united-kingdom/england-latest.osm.pbf",
    "scotland": "united-kingdom/scotland-latest.osm.pbf",
    "wales": "united-kingdom/wales-latest.osm.pbf",
    # Geofabrik files Greater London under england/, not at the top level like the
    # nations. Without this the london region silently falls back to the 2.16 GB
    # Great Britain extract, which builds a graph for hours to answer questions
    # about one city.
    "london": "united-kingdom/england/greater-london-latest.osm.pbf",
    # There is no standalone Northern Ireland extract.
    "northern_ireland": "ireland-and-northern-ireland-latest.osm.pbf",
}


def osm_url(region: str | None = None) -> str:
    """The extract matching a BODS region, so a dev run need not fetch all of GB.

    BODS splits England far more finely than Geofabrik does, so anything without
    its own extract falls back to Great Britain.
    """
    if override := os.environ.get("WAYFARE_OSM_URL"):
        return override
    region = region or BODS_REGION
    return GEOFABRIK + OSM_EXTRACTS.get(region, OSM_EXTRACTS["all"])


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
# like a complete one. Size alone cannot catch that -- regional bundles run from
# 37 MB to 1.28 GB, so any floor high enough to detect a truncated national feed
# would reject a complete Welsh one. The real check is structural: acquire opens
# the zip and requires the members the pipeline needs. This floor only exists to
# reject an empty body or an HTML error page cheaply, before that.
MIN_GTFS_BYTES = 1 << 20

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

# --- Patterns --------------------------------------------------------------

# How many passes the stop_times group-by is split into. An ordered list aggregate
# pins its per-group sort state, so DuckDB cannot spill it and a big feed fails
# regardless of memory_limit -- see gtfs._collapse_to_sequences. Partitioning by a
# hash of trip_id bounds the state at 1/N of the groups. More partitions means less
# memory and more scans of a small projected table; 16 clears London on a 17 GB
# machine with room to spare.
SEQ_PARTITIONS = int(os.environ.get("WAYFARE_SEQ_PARTITIONS", "16"))

# --- Matching batch --------------------------------------------------------

# Results are committed this often. Smaller means less lost work when the server is
# restarted mid-run; larger means fewer write transactions.
CHECKPOINT_EVERY = 200

# --- Publishing ------------------------------------------------------------

MIN_ZOOM = 5
MAX_ZOOM = 14
# Below this zoom the archive carries geometry and `n` and nothing else.
#
# Attributes are stored per feature per zoom, so `name`, `refs`, `way` and `trips`
# are paid for at every zoom a feature survives to -- and at z5-z10 nothing reads
# them. The whole country is a few hundred pixels across, the info card only
# appears on hover, and hovering a road is not a thing anyone does at that scale.
# `n` stays everywhere because it drives the colour and width ramps.
DETAIL_ZOOM = 11
# A backstop against one pathological city-centre edge, not a routine truncation.
#
# The original 12 assumed a long service list would dominate tile size. It does not:
# MVT pools attribute *values* per layer per tile, so an edge carrying "1,2,42,X57"
# costs a value-pool entry shared with every other edge carrying the same set, plus
# two varints on the feature. Measured on Wales, only 1,405 of 169,857 edges held
# more than 12 services and the longest held 53 -- a few hundred KB across a handful
# of city tiles, against a sidecar file, a fetch path and a cap to keep in sync.
#
# 64 clears Wales entirely. Where it does bite the feature still carries the true
# count in `n`, so the viewer says so rather than quietly showing a short list.
MAX_REFS_IN_TILE = 64


def ensure_dirs() -> None:
    for d in (RAW, WORK, OUT):
        d.mkdir(parents=True, exist_ok=True)
