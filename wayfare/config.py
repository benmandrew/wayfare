"""Paths, tunables, and environment.

Everything the pipeline writes lives under ``WAYFARE_DATA`` so a server run can be
pointed at a big volume and nothing escapes into the source tree.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# --- Layout ----------------------------------------------------------------

DATA = Path(os.environ.get("WAYFARE_DATA", "data")).resolve()
RAW = DATA / "raw"  # downloads, exactly as fetched
WORK = DATA / "work"  # unpacked and intermediate files
OUT = DATA / "out"  # publishable artefacts (pmtiles, geojson, art)
DB_PATH = WORK / "wayfare.duckdb"

# --- Sources ---------------------------------------------------------------

# BODS publishes one national GTFS bundle. Regional slugs exist (see docs/data.md) and
# are useful for development, but `all` is what a production run wants.
BODS_GTFS_URL = "https://data.bus-data.dft.gov.uk/timetable/download/gtfs-file/{region}/"
BODS_REGION = os.environ.get("WAYFARE_REGION", "all")

# The National Transport Authority (NTA) publishes the Republic of Ireland's whole
# timetable as one bundle, with no key and no registration. Per-operator bundles
# sit beside it as GTFS_<Operator>.zip; the index is transitData/PT_Data.html.
NTA_GTFS_URL = "https://www.transportforireland.ie/transitData/Data/GTFS_All.zip"

# Translink publishes Northern Ireland on OpenDataNI as four datasets and no GTFS.
# Resource ids and filenames move on every publication, so the datasets are named
# by slug and resolved through CKAN at fetch time -- see `translink.resource`.
OPENDATANI_API = "https://admin.opendatani.gov.uk/api/3/action/package_show"

OGL = "Open Government Licence v3.0"
CC_BY_4 = "Creative Commons Attribution 4.0"
# Not a spelling mistake and not to be tidied to the British form the rest of this
# codebase uses: "Open Database License" is the licence's own name.
ODBL = "Open Database License"

# CC BY 4.0 requires the licence to be *identified*, which in practice means a name
# and a URI; OGL and ODbL ask for the same. A table keyed on the licence rather than
# a field on `Feed`, so two publishers under one licence cannot disagree about where
# it is, and a credit raises on a licence with no entry rather than quietly omitting
# it.
LICENCE_URLS = {
    OGL: "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/",
    CC_BY_4: "https://creativecommons.org/licenses/by/4.0/",
    ODBL: "https://opendatacommons.org/licenses/odbl/",
}

OSM_COPYRIGHT = "https://www.openstreetmap.org/copyright"


@dataclass(frozen=True)
class Part:
    """One dataset of a feed nobody publishes whole.

    `kind` is what the assembler does with it, not what it contains: `timetable`
    is TransXChange, `geometry` is a MapInfo route bundle.
    """

    name: str
    dataset: str
    kind: str


# The dataset slugs are historical names that stopped describing their contents
# years ago -- the "metro timetable valid from 18 June until 31 August 2016" is the
# live Metro and Glider feed. They are the identity OpenDataNI keeps stable, so
# they are what is stored. Do not tidy them.
NI_PARTS = (
    Part(
        "timetable_ulsterbus",
        "ulsterbus-and-goldline-timetable-data-from-08-11-2023",
        "timetable",
    ),
    Part(
        "timetable_metro",
        "metro-timetable-data-valid-from-18-june-until-31-august-2016",
        "timetable",
    ),
    Part("routes_ulsterbus", "translink-ulsterbus-routes", "geometry"),
    Part("routes_metro", "translink-metro-bus-routes", "geometry"),
)


@dataclass(frozen=True)
class Feed:
    """Where a region's timetable comes from, and what comes with it.

    Everything except the Republic is a BODS slug, so `feed` builds those on
    demand and only the exceptions need an entry in `FEEDS`. The licence and
    attribution ride along because they differ between publishers and are the one
    property of a source that has an obligation attached rather than a behaviour.
    """

    url: str
    filename: str
    licence: str
    attribution: str
    # NaPTAN is the GB stop register. It has nothing to say about anywhere else, so
    # a non-GB region must not spend 102 MB fetching it.
    stop_register: bool = True
    # Whether the host answers a Range request with a 206, so an interrupted
    # transfer resumes. Measured against each host, never assumed -- BODS ignores
    # the header, Geofabrik and the NTA honour it.
    resumable: bool = False
    # The datasets a feed is assembled from, where no single file is the feed.
    # `url` is empty in that case and `filename` names the bundle acquire builds.
    parts: tuple[Part, ...] = ()


FEEDS = {
    # `ireland` is the Republic; `northern_ireland` is the province, and the two
    # read the same OSM extract, so they can share a data root.
    "ireland": Feed(
        url=NTA_GTFS_URL,
        filename="nta_gtfs_ireland.zip",
        # The first source here that is not OGL. Attribution is a condition of the
        # licence rather than a courtesy, so anything published from this feed has
        # to carry it -- see the README.
        licence=CC_BY_4,
        attribution="National Transport Authority",
        stop_register=False,
        resumable=True,
    ),
    "northern_ireland": Feed(
        # No URL: there is no Northern Irish GTFS to download. `acquire` fetches
        # the four Translink datasets below and `translink.build_gtfs` assembles
        # this file out of them.
        url="",
        filename="translink_gtfs_northern_ireland.zip",
        licence=OGL,
        attribution="Translink, via OpenDataNI",
        # NaPTAN is GB-only, and the province needs it least of anywhere: every
        # Translink stop arrives with its ATCO code and its position attached.
        stop_register=False,
        resumable=True,
        parts=NI_PARTS,
    ),
}


def feed(region: str | None = None) -> Feed:
    """The bundle for a region slug, BODS unless something else publishes it."""
    region = region or BODS_REGION
    if known := FEEDS.get(region):
        return known
    return Feed(
        url=BODS_GTFS_URL.format(region=region),
        filename=f"bods_gtfs_{region}.zip",
        licence=OGL,
        attribution="Department for Transport",
    )


# What a region's archive is called when it is named after its region. Only the
# exceptions live here; a slug is its own name everywhere else. `all` is the BODS
# scope for the whole of Great Britain rather than a place, and the viewer builds a
# region's label out of the filename, so `all.pmtiles` would label a map "all".
ARCHIVE_NAMES = {"all": "great_britain"}


def archive_name(region: str | None = None) -> str:
    """The filename an archive gets when it is named after its region.

    The name is not only a destination. The viewer carries no list of regions -- it
    labels each archive from its filename -- so this is also what the map calls the
    region in its "Go to..." list.
    """
    region = region or BODS_REGION
    name = ARCHIVE_NAMES.get(region, region)
    # A region slug reaches the filesystem here and nowhere else in the pipeline. A
    # slug carrying a separator would write outside OUT rather than fail.
    if name in ("", ".", "..") or name != Path(name).name:
        raise ValueError(f"region {region!r} does not name an archive")
    return f"{name}.pmtiles"


@dataclass(frozen=True)
class Credit:
    """One thing that has to be acknowledged: what it is, whose it is, its licence."""

    what: str
    who: str
    licence: str
    # Where the work itself lives, where the publisher gives one. The licence's own
    # URI is looked up from `LICENCE_URLS` and is not optional.
    who_url: str | None = None


def credit_parts(region: str | None = None) -> tuple[Credit, ...]:
    """Everything a picture of this region owes an acknowledgement to.

    Built from the `Feed` rather than from a table of its own, so a source added to
    `FEEDS` is credited by the act of describing it.

    Two obligations, and the second is the one that is easy to miss. The timetable
    is the publisher's, under their licence -- a condition rather than a courtesy
    now that the Republic's feed is CC BY 4.0. The geometry is OpenStreetMap's,
    under ODbL, whatever the timetable says: every edge is an OSM way that Valhalla
    matched a route onto, so an archive is a derived database. The viewer's existing
    OpenStreetMap line is about the *backdrop* and says nothing about the lines drawn
    on top of it, which is why the wording here names what each credit covers.

    The basemap is not here. It belongs to the page that chooses it, not to the data,
    and a render carries no basemap at all.
    """
    f = feed(region)
    return (
        Credit("Bus routes", f.attribution, f.licence),
        Credit("Road geometry", "OpenStreetMap contributors", ODBL, OSM_COPYRIGHT),
    )


def credit_html(region: str | None = None) -> str:
    """The credit as a map attribution control wants it.

    `publish` stamps this into the tileset metadata, which is the one place a licence
    condition travels with the data: an archive copied to a bucket takes its credit
    with it, where a line in the viewer or a field in `/archives.json` would be left
    behind.
    """
    return " &middot; ".join(
        f"{c.what}: &copy; {_link(c.who, c.who_url)}, "
        f"{_link(c.licence, LICENCE_URLS[c.licence])}"
        for c in credit_parts(region)
    )


def credit_lines(region: str | None = None, *, links: bool = True) -> tuple[str, ...]:
    """The credit as plain text, one line per thing being credited.

    `links=False` drops the URIs. That is for the one place they cost more than they
    carry: a credit burned into the corner of a picture, where a URI is unclickable,
    doubles the length of a line that has to fit across the canvas, and is spelled
    out in full in the same file's metadata anyway. Everywhere else keeps them,
    because identifying the licence is what the licence asks for.
    """
    return tuple(
        f"{c.what}: \N{COPYRIGHT SIGN} {c.who}"
        + (f" <{c.who_url}>" if c.who_url and links else "")
        + f", {c.licence}"
        + (f" <{LICENCE_URLS[c.licence]}>." if links else ".")
        for c in credit_parts(region)
    )


def credit_text(region: str | None = None) -> str:
    """The same credit with the links spelled out, for anywhere HTML is not read.

    A PNG `tEXt` chunk, an SVG `<metadata>` block, a log line. The copyright sign is
    deliberate and safe in all three: it is in Latin-1, which is what `tEXt` allows.
    """
    return " ".join(credit_lines(region))


def _link(text: str, url: str | None) -> str:
    return f'<a href="{url}">{text}</a>' if url else text


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
    # Geofabrik splits Ireland at the sea and not at the border, so both halves of
    # the island read the same 409 MB file. That is convenient rather than awkward:
    # one extract is one graph build and therefore one GraphId space, so the
    # Republic and Northern Ireland can share a data root and a DuckDB file.
    "ireland": "ireland-and-northern-ireland-latest.osm.pbf",
    "northern_ireland": "ireland-and-northern-ireland-latest.osm.pbf",
}


def osm_url(region: str | None = None) -> str:
    """The extract matching a region, so a dev run need not fetch all of GB.

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
# The same cheap check for one dataset of an assembled feed. The Translink parts
# run from 1.9 MB to 25 MB, so this only ever rejects an empty body or an error
# page; CKAN declares a size and the host sends a Content-Length, which is the
# real check.
MIN_PART_BYTES = 1 << 16

# --- Valhalla --------------------------------------------------------------

VALHALLA_URL = os.environ.get("WAYFARE_VALHALLA", "http://localhost:8002")
VALHALLA_TIMEOUT = 120.0
VALHALLA_WORKERS = int(os.environ.get("WAYFARE_WORKERS", "6"))

# Valhalla refuses a request past a distance limit and reports error 154. Two of its
# limits bind the `stops` path and both are 200 km as shipped:
# `service_limits.bus.max_distance`, checked against the straight-line chain through a
# /route request's locations, and `service_limits.trace.max_distance`, checked along
# the shape a /trace_attributes request submits. Nothing here may exceed it, and every
# bound below is a fraction of it rather than a number of its own.
VALHALLA_MAX_DISTANCE_M = 200_000
# The fraction of that cap this project is willing to fill. The tenth held back covers
# the difference between Valhalla's great-circle arithmetic and ours, and the metres a
# location moves when it snaps to a road.
VALHALLA_DISTANCE_HEADROOM = 0.9

# A pattern whose consecutive stops are further apart than this is skipped. It is one
# leg, so it is also the smallest chunk the matcher can build, and a leg past the cap
# above cannot be routed at any chunk size -- hence the same derivation, giving
# 180 km.
#
# This was 25 km, which was not a bound on bad data but a bound on long-distance
# coach: triage of all 1,555 patterns it skipped nationally (63,341 trips, 1.64% of
# every trip in the feed) found no null-island stops and no out-of-GB stops that were
# not real international coach halts, and 1,299 of them were National Express or
# FlixBus, median 6 stops and median longest leg 147 km. Recovery against the national
# run, counting patterns whose stops are all in GB and whose chain fits the cap:
#
#     50 km   356 patterns   15,566 trips   325 routable   14,186 routable trips
#    100 km   769            32,122         619            24,074
#    150 km   1,120          47,114         744            29,552
#    180 km   1,319          56,720         808            34,851
#
# 180 km reaches that ceiling. Rounding it up to 200 km buys nothing and costs
# everything: 630 of the 1,555 span more than 200 km and Valhalla will not route them
# at any setting, so the only effect of filling the cap exactly is to turn an honest
# `skipped` row into an `error` one.
MAX_STOP_GAP_M = int(VALHALLA_MAX_DISTANCE_M * VALHALLA_DISTANCE_HEADROOM)
# Valhalla's own score for how well the trace fits the road graph.
MIN_MATCH_CONFIDENCE = 0.30
# Reject a match whose road distance wildly exceeds the straight-line stop chain.
# The slack term matters: on a short pattern a ratio alone is meaningless, because
# a one-way system that sends the bus around a single block can triple a 300 m
# span. Both bounds must be exceeded before a match is called bad.
#
# Neither moved when the stop gap did. A motorway leg is nearly straight -- the long
# Welsh patterns the raised gap admits measure 1.26x and 1.58x -- so 3.0 sits far
# above what a coach costs and still well below the 4.1x that a genuinely lost match
# runs to. The slack is what a long leg makes irrelevant rather than wrong: a
# kilometre against a 150 km span is nothing, which is the same as saying the ratio
# alone decides there, which is correct there and is exactly what the slack exists to
# prevent on a 300 m one.
MAX_DETOUR_RATIO = 3.0
DETOUR_SLACK_M = 1_000.0

# --- Modes -----------------------------------------------------------------

# The GTFS route_type values that run on roads, and therefore the only ones the
# matcher is asked about. Everything else is water, rail or wire, and Valhalla's
# `bus` costing has nothing to snap it to: a ferry either fails outright or is
# snapped to whatever coast road happens to be nearby, which is worse.
#
# Both the basic values and the extended ranges are here, because the basic ones
# alone are wrong in a way that is invisible. 200-209 is coach, and the GB feed's
# 316 route_type=200 routes are National Express and FlixBus -- real long-distance
# road services that a `route_type = '3'` filter would silently delete.
#
#   3          bus            700-716  bus (extended)
#   11, 800    trolleybus     200-209  coach (extended)
#
# Nothing else is added speculatively. A type nobody publishes is a line of code
# that cannot be checked against a feed, and an unrecognised one is reported
# rather than guessed at -- see gtfs._drop_unselected_modes.
#
# `MODES` is the vocabulary and `ROAD_ROUTE_TYPES` is derived from it, so the two
# cannot disagree. One entry per mode a feed actually publishes, keyed on the name
# this project uses for it; nothing groups two GTFS types together unless they mean
# the same vehicle, which is why trolleybus sits inside `bus` and a cable tram does
# not sit inside `tram`.
MODES: dict[str, frozenset[int]] = {
    "bus": frozenset({3, 11, 800} | set(range(700, 717))),
    "coach": frozenset(range(200, 210)),
    "tram": frozenset({0}),
    "metro": frozenset({1}),
    "rail": frozenset({2}),
    "ferry": frozenset({4}),
    "cable_tram": frozenset({5}),
    "aerial": frozenset({6}),
    "funicular": frozenset({7}),
    "monorail": frozenset({12}),
}

# The modes Valhalla can be asked about, because they run on ways in its graph.
# Everything else is water, rail or wire, and `bus` costing has nothing to snap it
# to: a ferry either fails outright or is snapped to whatever coast road happens to
# be nearby, which is worse. Verified from Valhalla's own `lua/graph.lua`, which
# admits `route=ferry` and `route=shuttle_train` and drops every `railway=*` way.
ROAD_MODES = frozenset({"bus", "coach"})

# What `patterns` keeps when nothing says otherwise. Road only, so a run that does
# not ask for anything else behaves exactly as it did before modes existed.
DEFAULT_MODES = ROAD_MODES


def route_types(modes: Iterable[str]) -> frozenset[int]:
    """The GTFS route_type values covered by a set of mode names.

    Raises on a name that is not in the vocabulary rather than quietly selecting
    nothing, because a typo in `--modes` would otherwise read as a feed that
    happens to carry no trams.
    """
    unknown = sorted(set(modes) - set(MODES))
    if unknown:
        raise ValueError(
            f"unknown mode(s) {', '.join(unknown)}; known: {', '.join(sorted(MODES))}"
        )
    return frozenset().union(*(MODES[m] for m in modes)) if modes else frozenset()


ROAD_ROUTE_TYPES = route_types(ROAD_MODES)

# Names for the log line that reports what was dropped, and nothing else. Only the
# basic types, which is what a GB or Irish feed actually carries; anything outside
# this is logged as unrecognised, which is the point of reporting at all.
ROUTE_TYPE_NAMES = {
    0: "tram",
    1: "metro",
    2: "rail",
    4: "ferry",
    5: "cable tram",
    6: "aerial lift",
    7: "funicular",
    12: "monorail",
}

# route_type -> mode name, inverted from MODES so there is one source of truth.
# Stored on `patterns` because mode decides how a pattern gets its geometry, and
# joining back to `routes` for it at every read is how that gets forgotten.
MODE_OF_TYPE: dict[int, str] = {t: name for name, ts in MODES.items() for t in ts}

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
# Where the overview band is cut in two, and how many features each half may carry.
#
# Tippecanoe's `--drop-densest-as-needed` is the backstop when a tile will not fit,
# and it chooses by *density*, so it thins cities hardest and leaves a rural road
# carrying two buses a week untouched. On Great Britain that is what the low zooms
# looked like: measured on the 2026-08-07 archive it shed 922,505 features at z5 and
# 298,823 at z9, and the survivors were whatever happened to be sparse. Ireland, a
# ninth of the network, hit the limit at no zoom at all and drew a continuous map.
#
# Holding back the quietest roads before tippecanoe sees them puts that choice on
# service level instead. The cap is a feature count rather than a `trips` threshold
# because the threshold that fits depends on the region -- `publish` reads the cap
# back into whatever `trips` floor the data needs, and a region already under its cap
# is not filtered at all. Both parts of Ireland are under the cap, so this changes
# nothing for them.
#
# **The floor is per cell of `OVERVIEW_CELL`, never one figure for the whole region.**
# A single national floor was tried and it was worse than the problem: `trips` is an
# absolute count, so ranking the country on one scale ranks it by how urban it is. At
# the 703-trip floor that produced, 310 of Great Britain's 655 populated cells lost
# every feature they had, the busiest ten cells held 45.9% of the survivors, and the
# top tenth of cells went from 48.7% of the map to 81.7% of it. That draws the cities
# and nothing between them.
#
# Each cell keeps the same *fraction* instead -- `cap` over the region's feature count
# -- with at least one feature per populated cell, and `trips` decides which ones
# within that cell. The spatial distribution therefore survives: measured on Great
# Britain, 0 cells emptied and the top tenth holds 47.8% against the input's 48.7%,
# while the floor itself ranges from 1 trip in the countryside to 5,600 in the busiest
# cell, median 218. Cities stay dense, the country between them stays drawn, and what
# is dropped anywhere is the least-served road in that place rather than the
# least-served road in Britain.
#
# Only the far half is capped, because only the far half is in trouble. Measured on
# the 2026-08-07 Great Britain archive, tippecanoe kept 5.1% of the network at z5 and
# 14.3% at z7, against 37.6% at z8 and 86.1% at z10. Capping z8-z10 as well was tried
# and withdrawn: the caps that stop the drops there take more roads off the map than
# the drops did, and z10 was never under pressure at all.
#
# The number is measured rather than derived. Tile bytes do not fall as fast as the
# feature count -- holding Great Britain to 33.0% of its features took the largest z5
# tile from the 1.51 MB it wanted to 630 KB, an exponent of about 0.79, because what
# survives a `trips` floor is the busy roads and those carry the longer geometry. At
# 205,000 -- 209,493 features once ties at each cell's floor are kept, 24.1% of the
# export -- no z5-z7 tile reaches the 500 KB limit and tippecanoe drops nothing, which
# is the whole point: what is on the map is then chosen by service level rather than
# by which cities happened to be dense. z5 carries 98,313 features, z6 130,947 and z7
# 168,255, against 55,983, 106,672 and 157,193 with no cap at all.
FAR_ZOOM = 8
OVERVIEW_CAP_FAR = 205_000
# The side of the cell the quota is shared out over, in degrees. 0.25 is about 28 km
# by 17 km at this latitude, and puts 655 cells over Great Britain's network -- fine
# enough that a town is not averaged in with the city forty miles away, coarse enough
# that a cell holds a few hundred features rather than a handful. A cell is a bucket
# for allocating the quota and nothing else: it never becomes a tile boundary, and
# the geometry that crosses it is untouched.
OVERVIEW_CELL = 0.25
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

# --- Serving ---------------------------------------------------------------

# Whether `wayfare serve` answers /art. On by default: rendering where the data
# already is the reason the endpoint exists. Set WAYFARE_ART=off on a deployment
# whose port is reachable by people you would not hand a CPU to -- serving tiles is
# reading bytes off disk, and a render is not.
#
# An environment variable as well as the `--no-art` flag because Compose cannot
# conditionally omit an argument, and an empty string is an argument.
ART_ENABLED = os.environ.get("WAYFARE_ART", "on").lower() not in ("off", "0", "false")


def ensure_dirs() -> None:
    for d in (RAW, WORK, OUT):
        d.mkdir(parents=True, exist_ok=True)
