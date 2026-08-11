"""Paths, tunables, and environment.

Everything the pipeline writes lives under ``WAYFARE_DATA`` so a server run can be
pointed at a big volume and nothing escapes into the source tree.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from . import licences

# The licence names a `Feed` is declared with. Everything else about them -- their
# URIs, the `Credit` type, and how a credit is rendered -- lives in `licences`,
# because the list only grows and none of it is configuration. Imported by name so
# that a feed reads as a description of a source rather than as a lookup.
from .licences import CC_BY_4, OGL

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


def credit_parts(
    region: str | None = None, *, road: bool = True, operator: bool = False
) -> tuple[licences.Credit, ...]:
    """Everything a picture of this region owes an acknowledgement to.

    This is the only part of crediting that belongs here rather than in `licences`:
    it needs the `Feed`, and a feed is configuration. What a licence is called and
    how a credit is written are not.

    Built from the `Feed` rather than from a table of its own, so a source added to
    `FEEDS` is credited by the act of describing it. The timetable is always the
    publisher's, under their licence -- a condition rather than a courtesy now that
    the Republic's feed is CC BY 4.0.

    The second credit is the one that is easy to miss, and it is *conditional*.
    Where a route was map-matched, every edge is an OpenStreetMap way that Valhalla
    matched onto, so the archive is a derived database and owes ODbL whatever the
    timetable says. Where it was not -- a tram, metro or ferry drawn from the trace
    in the feed -- no OpenStreetMap data was involved at all, and claiming ODbL over
    the operator's own survey would be wrong in the opposite direction: asserting a
    share-alike condition on data whose publisher never imposed one. `road` is what
    tells the two apart, and it is the caller's to set because only the caller knows
    what it built.

    `operator` widens the first credit's noun rather than adding a third line. The
    trace arrives in the same bundle as the timetable and is covered by the same
    licence, so it needs naming rather than crediting separately -- and naming it
    matters, because this is the wording that says what each credit covers.

    The basemap is not here. It belongs to the page that chooses it, not to the data,
    and a render carries no basemap at all.
    """
    f = feed(region)
    what = (
        "Routes, timetables and operator geometry" if operator else "Routes and timetables"
    )
    parts = [licences.Credit(what, f.attribution, f.licence)]
    if road:
        parts.append(licences.OPENSTREETMAP)
    return tuple(parts)


def credit_html(
    region: str | None = None, *, road: bool = True, operator: bool = False
) -> str:
    """This region's credit, rendered for a map attribution control."""
    return licences.html(credit_parts(region, road=road, operator=operator))


def credit_lines(
    region: str | None = None,
    *,
    links: bool = True,
    road: bool = True,
    operator: bool = False,
) -> tuple[str, ...]:
    """This region's credit as plain text, one line per thing being credited."""
    return licences.lines(credit_parts(region, road=road, operator=operator), links=links)


def credit_text(
    region: str | None = None, *, road: bool = True, operator: bool = False
) -> str:
    """This region's credit with the links spelled out, for anywhere HTML is not read."""
    return licences.text(credit_parts(region, road=road, operator=operator))


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

# How hard tippecanoe simplifies a line below the maximum zoom, in tile units. Lower
# keeps more of the road's shape and costs bytes; the maximum zoom is never simplified,
# so this is about z5-z13 only.
#
# 4 was tippecanoe's default and was never measured. The archive has room for less at
# the zooms where anyone looks closely: the largest z12 tile is 116 KB against the
# 500 KB limit, z13 50 KB and z14 18 KB, so those bands are using a fraction of what
# they may. z10 and z11 have less to give, at 366 KB and 308 KB.
SIMPLIFICATION = 4
# The per-tile ceiling that decides when `--drop-densest-as-needed` fires.
#
# tippecanoe's default is 500,000, which is a Mapbox hosted-service limit rather than
# anything in the vector tile format -- the spec sets no size at all. This archive is
# served off one machine over HTTP range requests, so nothing rejects a larger tile and
# the number is a choice about fetch and decode time.
#
# It binds hard at the low zooms and only there. Measured on Great Britain, the largest
# z5 tile wanted 1.50 MB and was cut to 406 KB, z8 wanted 0.79 MB and z9 0.55 MB, while
# nothing at z10 or above reached the limit at all. Raising it is the only way to give
# z5-z9 more detail, because they are already at the ceiling rather than under it.
MAX_TILE_BYTES = 500_000
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
# How many features each overview band may carry, and **both are None: the overview
# is not capped, and everything below is switched off.**
#
# It was capped four different ways and every one of them made the map worse. The
# machinery is kept because z5 is still thinner than Ireland and is the one place a
# selection might yet earn its keep; nothing else here is live.
#
# What the four attempts were, in order, and what each did to Great Britain:
#
#   national `trips` floor  310 of 655 populated cells lost every feature they had.
#   per-cell, proportional  no cell emptied, and the countryside drew 15 features a
#                           cell at z6 where Ireland drew 53.
#   per-cell, square root   the countryside recovered and the cities flattened, so
#                           London stopped reading as denser than the fields.
#   the same on a 0.02 grid 88.9% of 1.4 km bins drawn against 37.8%, and still worse
#                           on the screen than no cap at all.
#
# The measurement that settled it counts lit pixels in a window, which is the closest
# thing to what a reader sees. Every cap loses ink at every zoom in every window:
#
#   window                z5     z6     z7
#   Ireland, Dublin      4.7%   4.6%   4.5%
#   GB uncapped, London  3.8%   7.0%   9.3%
#   GB capped, London    2.7%   3.7%   3.7%
#   GB uncapped, Wales   1.1%   1.2%   1.3%
#   GB capped, Wales     1.0%   1.0%   1.0%
#
# At z8 around London the capped archive lit 5.0% against 8.2% uncapped, and the
# render showed the city hollowed to a radial skeleton. That is what "speckly" was.
#
# The counting mistake underneath all four is worth keeping. A cap keeps many short
# features spread over many cells; no cap keeps fewer, longer ones. Counting features,
# or populated cells, or bins holding any feature, rewards the first -- and only the
# second is visible. Four rounds were judged on measurements that could not see the
# map. `wayfare coverage` counts the same way and has the same blind spot; the line
# renderer in the working notes is what to reach for.
#
# tippecanoe's `--drop-densest-as-needed` chooses by density rather than by service
# level, which is a real fault and is why this was attempted. On the 2026-08-07
# archive it shed 922,505 features at z5. It is also, measured, better than anything
# offered to replace it: it thins only the tiles that will not fit -- 18 at z5-z7 and
# 4 at z8-z9 on Great Britain -- where a cap thins the whole country to spare them.
FAR_ZOOM = 8
OVERVIEW_CAP_FAR: int | None = None
# Where the capped part of the overview ends. z10 is the only overview zoom that was
# never under pressure -- tippecanoe kept 86.1% of the network there against 37.6% at
# z8 -- so it is banded off and handed the export whole. Capping z8-z10 as one band
# was tried and withdrawn for exactly this: the cap that quietens z8 takes z10 from
# 943,040 features to 411,255, which is paying for a fixed zoom with a working one.
MID_ZOOM = 10
# The cap on z8-z9, or None to hand tippecanoe everything and let
# `--drop-densest-as-needed` be the only thing standing between the network and the
# tile size limit. See the note above for why this exists and why it once did not.
OVERVIEW_CAP_MID: int | None = None
# How the quota is shared out: a cell's share goes as its feature count to this power.
#
# This is the dial the low-zoom map kept swinging between the ends of, and both ends
# are wrong in a way that looks like the other one's fix.
#
#   1.0  every cell keeps the same fraction. The obvious answer, and the one that put
#        black gaps between Britain's cities: a quarter of a city is still a city, a
#        quarter of a country lane is nothing. Deployed briefly. Rural cells drew 15
#        features at z6 where Ireland's equally-sized rural cells drew 53, and 28 of
#        them held fewer than 5.
#   0.0  every cell keeps the same count. The correction taken too far -- the cities
#        go to speckle, the busiest tenth of cells falling to 7.0% of full detail.
#   0.5  what is here. The countryside keeps everything it has and the cities pay for
#        it out of density nobody can see: measured on Great Britain, rural cells go
#        from 32.1% kept to 100%, urban from 23.7% to 21.2%.
#
# Ireland is the reference for what the result should look like, and it is a reference
# precisely because none of this touches it. At 87,179 features it is under every cap,
# so `_cell_floors` returns nothing and the file goes to tippecanoe whole. Its
# retention is flat across the country -- 51.8% in the emptiest quarter of cells
# against 52.8% in the busiest, all of it sub-pixel simplification. A weighting works
# when Great Britain's profile is flat too; a weighting that tilts is the bug, whether
# it tilts towards the cities or away from them.
OVERVIEW_WEIGHT = 0.7
# The side of the cell the quota is shared out over, in degrees. A cell is a bucket for
# allocating the quota and nothing else: it never becomes a tile boundary, and the
# geometry that crosses it is untouched.
#
# **This is the setting that decides whether the map is covered.** Within a cell the
# quota goes to the highest `trips`, and the highest `trips` in any cell are in that
# cell's town centre -- so a cell spends its whole allowance on one spot and draws
# nothing in between. How much "in between" there is depends entirely on how big the
# cell is, and 0.25 degrees, at 28 km by 17 km, is 20x too big. Measured on Great
# Britain against 0.02-degree bins of about 1.4 km, of which the export covers 33,435:
#
#   cell    bins drawn   of the export's
#   0.25         12,626           37.8%
#   0.05         24,475           73.2%
#   0.02         30,608           91.5%
#   0.01         32,673           97.7%
#
# At 0.25 nearly two thirds of the country drew nothing while every aggregate looked
# healthy -- 655 populated cells, none of them empty, more features at z6 than the
# uncapped build carried. Rendering the drawn geometry is what showed it: England came
# out as speckle with voids through it where the uncapped archive was continuous, and
# one 1.4 km bin held 188 features at a zoom where that is a fraction of a pixel.
#
# 0.02 puts 33,571 cells over Great Britain. The cap is read down to match, because a
# finer grid keeps more than it is asked for -- every populated cell is guaranteed one
# feature, and ties at each floor are kept -- so 130,000 comes out at 192,400, which is
# what 190,000 over the coarse grid came out at. Same features, 2.4x the places.
OVERVIEW_CELL = 0.02
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
