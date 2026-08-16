"""Paths, tunables, and environment.

Everything the pipeline writes lives under ``WAYFARE_DATA`` so a server run can be
pointed at a big volume and nothing escapes into the source tree.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

# The feed definitions, re-exported. They moved to `feeds` because the list of
# sources grows on its own schedule and nothing else in this file moves with it, but
# `config.FEEDS` and `config.archive_name` are what every stage reads and this keeps
# them reading it. Edit them in `feeds`.
#
# Spelled `X as X` because mypy's strict mode treats a plain import as private to
# this module, and every one of these is read through `config.` somewhere.
from .feeds import ARCHIVE_NAMES as ARCHIVE_NAMES
from .feeds import BODS_GTFS_URL as BODS_GTFS_URL
from .feeds import FEEDS as FEEDS
from .feeds import NI_PARTS as NI_PARTS
from .feeds import NTA_GTFS_URL as NTA_GTFS_URL
from .feeds import OPENDATANI_API as OPENDATANI_API
from .feeds import Feed as Feed
from .feeds import Part as Part
from .feeds import archive_name as archive_name
from .feeds import credit_parts as credit_parts
from .feeds import feed as feed

# --- Layout ----------------------------------------------------------------

DATA = Path(os.environ.get("WAYFARE_DATA", "data")).resolve()
RAW = DATA / "raw"  # downloads, exactly as fetched
WORK = DATA / "work"  # unpacked and intermediate files
OUT = DATA / "out"  # publishable artefacts (pmtiles, geojson, art)
DB_PATH = WORK / "wayfare.duckdb"


def retarget(data: Path) -> None:
    """Point every derived path at a different data root, for `--data`.

    Here rather than in the caller because the list has to be complete: a path
    derived from DATA and not reassigned is one stage writing to the default root
    while every other stage writes to `--data`, which looks like a stage that
    silently did nothing. A constant added above belongs in this function in the
    same edit, and `tests/test_cli.py` walks the module to check that it is.
    """
    global DATA, RAW, WORK, OUT, DB_PATH
    DATA = data.resolve()
    RAW = DATA / "raw"
    WORK = DATA / "work"
    OUT = DATA / "out"
    DB_PATH = WORK / "wayfare.duckdb"


# Which region this data root holds. Environment, so it stays here rather than in
# `feeds`, which reaches it through this module at call time -- one binding, so a
# test that patches it reaches every caller.
BODS_REGION = os.environ.get("WAYFARE_REGION", "all")

# --- The OpenStreetMap extract ----------------------------------------------

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
    # the island read the same 409 MB file. One extract is one graph build and
    # therefore one GraphId space, so the Republic and Northern Ireland share a
    # Valhalla graph. They still need a data root each: `meta.feed_version` holds one
    # value, so acquiring the second region into the first's database makes it the
    # current feed and retires every pattern of the first.
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


# --- Geography -------------------------------------------------------------

# The British Isles, as four bounds on a stop's own coordinates.
#
# BODS carries international coach: 41 of Great Britain's live stops stand in
# France, Belgium, the Netherlands, Germany, Czechia and Poland, the furthest being
# Warsaw at 20.96 E. They are correct coordinates for real services, so no validity
# check catches them -- and a plain min/max over every live stop therefore draws a
# window from Ireland to Poland, which is what `osmroutes.bbox` would hand Overpass
# without these bounds.
#
# Three of the four bounds are a box. The fourth is a line through the Channel,
# because a box cannot do it: Calais sits east of Dover by half a degree, so the
# cut there is longitude, while Brittany sits *west* of Cornwall, so the cut there
# is latitude. One line cannot serve both -- which is why the southern bound is
# separate, and why the line is capped rather than run on north of the Wash, where
# extending it would eventually take in Bergen.
#
# Measured against the August 2026 national feed: it drops 52 patterns of 55,198,
# every one of them coach, and every stop it drops is continental. The nearest
# British stop it keeps is Jarvist Place near Deal, 0.209 deg clear of the line;
# the nearest continental stop it drops is Calais (Eurotunnel), 0.279 deg beyond
# it. Both margins are around 15 km, so the two sides do not nearly touch.
ISLES_LAT_MIN = 49.80  # Bishop Rock is 49.87; Alderney, deliberately out, is 49.72
ISLES_LAT_MAX = 61.10  # Out Stack is 60.86; the Faroes are 62.0
ISLES_LON_MIN = -11.00  # Tearaght Island is -10.67
ISLES_LON_CAP = 2.00  # Lowestoft Ness, the easternmost point, is 1.76
ISLES_CHANNEL_LON = 0.90  # the line's longitude at 50 N, in the western Channel
ISLES_CHANNEL_SLOPE = 0.60  # degrees of longitude it gains per degree north


def british_isles_sql(lat: str, lon: str) -> str:
    """The bounds above as a SQL predicate over two coordinate columns.

    A fragment rather than a table so that both the stage that drops the routes and
    the stages that size a bounding box read the same definition. A second copy
    would drift, and the way it would fail is a window that quietly reaches the
    continent again -- which is why there is no Python twin of this. Every place the
    boundary is applied holds a connection: `gtfs` drops the routes with it, and
    `osmroutes.bbox` and `trace.bbox` clip their windows with it.
    """
    return (
        f"({lat} BETWEEN {ISLES_LAT_MIN} AND {ISLES_LAT_MAX}"
        f" AND {lon} BETWEEN {ISLES_LON_MIN} AND least("
        f"{ISLES_LON_CAP}, {ISLES_CHANNEL_LON}"
        f" + {ISLES_CHANNEL_SLOPE} * ({lat} - 50.0)))"
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
# A tighter cap bounds long-distance coach rather than bad data. Triage of the 1,555
# patterns a 25 km cap excludes nationally (63,341 trips, 1.64% of every trip in the
# feed) found no null-island stops and no out-of-GB stops that were not real
# international coach halts, and 1,299 of them were National Express or FlixBus,
# median 6 stops and median longest leg 147 km. Recovery against the national run,
# counting patterns whose stops are all in GB and whose chain fits the cap:
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

# The modes `trace` fits even where the operator published a shape, so the result can
# be inverted per way and drawn as shared track.
#
# The default rule is that the operator's own recording wins, because it is a survey
# of where the *vehicle* goes and an OSM relation is a survey of where the *track*
# is. Heavy rail is where those two are the same line: a train has no route choice
# within a station throat, so the difference between the two is the platform approach
# and a few metres of it. The gain on the other side is the whole point of the track
# layer -- one polyline per pattern cannot answer which services run over a stretch,
# and the Republic's rail is 319 shaped patterns and 392,939 vertices of mainline
# drawn over itself.
#
# Tram is deliberately not here and is the reason this is a set rather than "has a
# shape at all". A tram's shape includes street running and depot moves that no route
# relation carries, so trading it for a relation's chain loses geometry that is
# correct. Metro is out for the same reason at lower stakes: Great Britain's metro
# shapes are 109 patterns against 1,885 unshaped ones already traced, so there is
# almost nothing to win and a depot move to lose.
TRACE_OVER_SHAPE_MODES = frozenset({"rail"})


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

# Results are committed this often, and a batch is also the unit of concurrency.
# Smaller means less lost work when the server is restarted mid-run; larger means
# fewer write transactions.
CHECKPOINT_EVERY = 200

# --- Tracing from OSM route relations ---------------------------------------

# Overpass, not the OSM API. `trace` needs to *discover* relations over an area
# rather than fetch one it already knows the id of, and only Overpass answers that.
# One request per run returns every route relation in the region with its member
# geometry inline, which is why the quota this endpoint meters is affordable here
# where it would not be for a per-relation walk.
OVERPASS_URL = os.environ.get("WAYFARE_OVERPASS", "https://overpass-api.de/api/interpreter")
OVERPASS_TIMEOUT = 600.0
# The `[timeout:]` Overpass itself is asked to honour, which is a different number
# for a different party: ours bounds how long we wait for the socket, this bounds how
# long the server will spend before giving up and saying so.
OVERPASS_QUERY_TIMEOUT = 540

# Every `type=route` value worth fetching. Deliberately wider than the modes that
# will be traced, because the discriminator is the stop-sequence join and not this
# list: the Elizabeth line is tagged `route=train` rather than `subway`, so a set
# written from the obvious names misses it entirely and the failure looks like
# missing data rather than a wrong filter. Costing a few thousand extra relations in
# one request is the cheaper mistake.
OSM_ROUTE_VALUES = (
    "subway",
    "light_rail",
    "tram",
    "train",
    "monorail",
    "funicular",
    "aerialway",
)

# Roles that name a calling point in a PTv2 relation. `platform` is deliberately
# absent: a platform member is a way or a node beside the track, and leaving the
# platform roles in the *way* chain produces 11 to 25 spurious breaks per relation,
# which reads as broken mapping rather than as a filter mistake.
OSM_STOP_ROLES = ("stop", "stop_entry_only", "stop_exit_only")

# How far a timetabled stop may sit from the OSM node it matched, in metres.
# Calibrated against a measurement rather than intuition: over the Victoria line 15
# of 16 stops land within 150 m, and the exception is Highbury & Islington at 216 m,
# where the GTFS point is the National Rail entrance and the OSM node is the tube
# platform. A large interchange is exactly where the two disagree most, so the bound
# has to clear that case; 400 m does, and is still far under the ~1.2 km spacing
# between Underground stations.
TRACE_STOP_MAX_M = 400.0
# How far apart two consecutive projections along the chain may sit before the match
# is refused as out of order. A relation that loops -- the New Addington branch is
# one -- projects a later stop *behind* an earlier one, and slicing between them
# draws a confident line down the wrong side of the loop.
TRACE_MONOTONIC_SLACK_M = 250.0
# Two OSM ways are joined when their endpoints are this close. They should be the
# same node and therefore identical to the 1e-7 degree Overpass prints, so this is a
# rounding guard rather than a tolerance; anything larger would start welding genuine
# gaps shut.
TRACE_JOIN_TOLERANCE_M = 1.0

# How much wider than the pending patterns' own extent to ask Overpass for, in
# degrees. A relation is only returned if it intersects the window, and a line whose
# stops sit just inside the box still runs out of it -- the Central line reaches
# Epping, well past anything a London window would be drawn around. 0.2 degrees is
# about 22 km. `osmroutes` pads its own window by less, because there the window is
# sized off a region's stops rather than one stage's backlog.
TRACE_BBOX_PAD_DEG = 0.2

# `routes` sizes its window off a region's live stops rather than one stage's
# backlog, so the extent is already most of the country and a wide pad only buys the
# neighbour's network.
ROUTES_BBOX_PAD_DEG = 0.05

# Results are committed this often, for the reason `CHECKPOINT_EVERY` exists. Larger
# than the matching batch because a trace is arithmetic against relations already in
# memory rather than a wait on another process.
TRACE_CHECKPOINT_EVERY = 500


def pad_and_clip(
    box: tuple[float, float, float, float],
    *,
    pad: float,
    region: str | None,
    what: str,
) -> tuple[float, float, float, float]:
    """Pad a (south, west, north, east) box, then narrow it to the region's bounds.

    A window is a box and a border is not, so a region whose stops reach across one
    asks Overpass for the neighbour's network and draws it into its own archive.
    `Feed.bounds` is what stops that, and a box the bounds leave empty raises,
    because discovering nothing reads exactly like a region whose track is unmapped.

    `what` names the stops the box was sized off, so the error says which window
    missed its region.
    """
    south, west, north, east = box
    south, west = south - pad, west - pad
    north, east = north + pad, east + pad
    limit = feed(region).bounds
    if limit is not None:
        south, west = max(south, limit[0]), max(west, limit[1])
        north, east = min(north, limit[2]), min(east, limit[3])
        if south >= north or west >= east:
            raise RuntimeError(
                f"region {region or BODS_REGION!r} has bounds {limit}, which its "
                f"{what} never meet; no relation could be discovered"
            )
    return (south, west, north, east)


# The share of a region's live relation patterns a `routes` run must rediscover
# before it is allowed to retire the rest.
#
# The stage rewrites everything on every invocation, so the retire is what keeps a
# withdrawn line from being drawn for ever -- and the same statement thins a region's
# rail to a handful of lines when a run comes back nearly empty for a reason that has
# nothing to do with OpenStreetMap. Nothing downstream can see the difference: a few
# relations is what a truncated Overpass body looks like, and also what a country with
# two railways looks like.
#
# 0.5 is deliberately loose. This is a floor under a catastrophe, not a churn budget:
# real withdrawal is one line at a time, while a window that missed and an operator
# gate tightened too far both take out most of the region at once.
#
# A run that finds *nothing* is exempt, and the exemption is the point rather than a
# corner: `Feed.route_relations = ()` makes a region draw none on purpose, and the
# retire it causes is what removes the second copy of every line. Only the partial
# collapse is ambiguous, so only the partial collapse is refused.
ROUTES_COLLAPSE_FLOOR = 0.5

# --- Snapping an operator shape onto OSM track ------------------------------

# `railway` values worth fetching as bare ways. `trace` reaches ways through a route
# relation, which is the wrong instrument here: a relation covers only the track
# somebody drew a route over, and against the Republic's rail the ways stored that
# way cover 78.7% of the timetabled shape length, with Dublin-Belfast at 7.1% and
# Limerick-Waterford at 3.3%. Asking for the track itself covers 100.0% within 25 m
# and costs 7.2 MB in one request, so `snap` has its own query and its own cache.
#
# Narrower than `OSM_ROUTE_VALUES` on purpose, because this list is the snap target
# rather than a discovery net: a shape must not land on a siding, a yard or a
# disused alignment, and `service=*` track is excluded in the query for the same
# reason. `preserved` and `construction` are absent because a train does not run on
# them and a shape that snaps to one draws a confident line down track nobody uses.
OSM_RAILWAY_VALUES = ("rail", "light_rail", "subway", "narrow_gauge", "tram")

# How far a shape vertex may sit from the track it is snapped onto, in metres.
# Measured rather than guessed: over the Republic's 3,000.6 km of rail shape, the
# covered share is 99.5% at 5 m, 99.8% at 10 m and 100.0% at both 25 m and 50 m.
# The answer barely moves across that range because a survey either follows the
# track or is somewhere else entirely, so this is a wide margin on a decision with
# no near miss in it rather than a threshold anything balances on.
SNAP_MAX_M = 25.0

# How much further than the nearest track the way a run is already on may sit before
# the run switches to the nearer one, in metres.
#
# Hysteresis without this bound is worse than none. Holding the previous way until it
# leaves `SNAP_MAX_M` entirely means a way that has already diverged keeps the shape
# for another 25 m, and the first run measured exactly that: all 319 of the Republic's
# rail patterns reported a worst vertex in the 20-25 m band, against an independent
# measurement putting 99.5% of that same length within 5 m of *some* track. Every one
# of those was a junction or a throat where the run should have changed way and did
# not, and the metres went to the wrong way's service list.
#
# 3 m keeps the property the hold exists for -- parallel tracks a metre or two apart
# do not make the choice flap from vertex to vertex -- while a genuine divergence
# switches on the first vertex where the other way is meaningfully nearer.
SNAP_HOLD_M = 3.0

# The share of a shape's length that must find track within `SNAP_MAX_M` before the
# snap is accepted. A partial cover is the dangerous outcome, not the useless one:
# attributing the half that matched and silently dropping the half that did not
# would report a short working over a line the service runs the length of. Refusing
# the pattern leaves it drawn from its own shape, which is what it had before.
SNAP_MIN_COVER = 0.98

# Grid cell for the spatial index over the target track, in metres. Only a
# performance knob -- the answer is the same at any size -- and 200 m keeps the
# per-vertex candidate list to a handful at Irish track density.
SNAP_GRID_M = 200.0

# --- Publishing ------------------------------------------------------------

# How hard tippecanoe simplifies a line, in tile units. Lower keeps more of the road's
# shape and costs bytes.
#
# It applies at every zoom including the maximum, measured on Ireland's detail band:
# `--simplify-only-low-zooms` builds 6.16% *bigger*, and `--simplification-at-maximum-
# zoom=8` alongside `--simplification=8` is byte-identical to `--simplification=8` on
# its own. Turning simplification off altogether costs 25.89%, which makes this knob
# the largest single geometry saving in the build.
#
# 4 is tippecanoe's default and it stays there. Building Great Britain at 4, 2 and 1
# gives 130.4 MB, 135.2 MB and 140.8 MB, and moves the ink in a London window at z8 by
# 0.14 percentage points and at z12 by 0.05. There is very little for simplification
# to remove -- the export is already short coalesced runs along single ways, and
# `SIMPLIFY_SHARED_NODES` pins every junction -- so a lower setting buys geometry
# nobody can see at 8% more bytes.
SIMPLIFICATION = 4
# The grid the detail band's lower zooms are quantised to, as a power of two --
# tippecanoe's `-D`. Its default is 12, a 4096-unit tile; this is 1024 units.
#
# It reaches z11-z13 and never z14, which keeps `-d` at the default and comes out
# bit-identical, so the zoom a street is read at does not move. MapLibre draws a
# vector tile across 512 screen pixels, which puts detail 9 at one grid unit per
# pixel at native zoom and this at half a unit.
#
# Measured on Great Britain's detail band: 106.2 MB to 98.2 MB, 7.53%. The 2.97% of
# z11 features it costs are short edges collapsing to a single point on the coarser
# grid, and the ink they carried is not visible at that zoom -- rasterised over
# Leinster, z11 lit pixels go 2.524% to 2.516%.
#
# 9 was measured and rejected: 10.81% on Ireland for a 375 m worst-case collapse at
# z11, taking 8.58% of that zoom's features with it.
LOW_DETAIL = 10
# Whether to keep a node shared between two features fixed while simplifying.
#
# On, and it should stay on: simplification is free to move a vertex, and moving one
# that two roads meet at pulls them apart, which is a hole in the network at exactly
# the zooms the network is what you can see.
#
# Off is a workaround for one thing only. tippecanoe 2.79.0 on macOS arm64 dies with
# SIGTRAP on the national export -- 861,410 features -- with this flag on, while the
# same command on the same version on Linux is what builds every published archive.
# Isolated by bisecting the flags: no other flag, the zoom range, or `-P` does it, and
# a small input with the flag is fine. So a local build on that machine needs this off,
# and **an archive built with it off is not comparable to a published one**: it is a
# different simplification, not a faster one.
SIMPLIFY_SHARED_NODES = True
# The per-tile ceiling that decides when `--drop-densest-as-needed` fires.
#
# tippecanoe's default is 500,000, which is a Mapbox hosted-service limit rather than
# anything in the vector tile format -- the spec sets no size at all. This archive is
# served off one machine over HTTP range requests, so nothing rejects a larger tile and
# the number is a choice about fetch and decode time.
#
# It binds at the low zooms and only there, because those are the tiles tippecanoe had
# to thin to fit. Measured on Great Britain by building the archive at both:
#
#   limit    archive   worst z5   worst z8   worst z10   worst z12
#   500 KB   130.4 MB     406 KB     428 KB      366 KB      116 KB
#     1 MB   137.9 MB     932 KB     776 KB      366 KB      116 KB
#
# z5 more than doubles and z8 goes from thinned to essentially complete -- it wanted
# 790 KB -- for 7.5 MB, 5.7% of the archive, on about thirty tiles. z10 and below are
# identical, correctly: they never reached the old limit, so the ceiling was never
# what was holding them back.
#
# Judge a change to this on tile bytes rather than on `coverage draw`'s lit fraction.
# The features a higher ceiling restores are in the densest tiles, where the pixels are
# already lit, so the fraction moved 9.42% to 9.54% for content that doubled. Lit
# pixels detect a missing network and saturate on added density.
MAX_TILE_BYTES = 1_000_000
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
# How a cap is judged decides whether it survives. A cap keeps many short features
# spread over many cells; no cap keeps fewer, longer ones. Counting features, or
# populated cells, or bins holding any feature, rewards the first -- and only the
# second is visible, so anything proposed here is judged on a rendered line map and
# never on a count. `wayfare coverage` counts the same way and has the same blind
# spot; the line renderer in the working notes is what to reach for.
#
# tippecanoe's `--drop-densest-as-needed` chooses by density rather than by service
# level, which is a real fault. It is also, measured, better than anything offered to
# replace it: it thins only the tiles that will not fit -- 18 at z5-z7 and 4 at z8-z9
# on Great Britain -- where a cap thins the whole country to spare them.
FAR_ZOOM = 8
OVERVIEW_CAP_FAR: int | None = None
# Where the capped part of the overview ends. z10 is the only overview zoom not under
# pressure -- tippecanoe keeps 86.1% of the network there against 37.6% at z8 -- so it
# is banded off and handed the export whole. Capping z8-z10 as one band takes z10 from
# 943,040 features to 411,255 for a cap that quietens z8, which is paying for a fixed
# zoom with a working one.
MID_ZOOM = 10
# The cap on z8-z9, or None to hand tippecanoe everything and let
# `--drop-densest-as-needed` be the only thing standing between the network and the
# tile size limit. See the note above for why nothing is capped here.
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
# Dormant along with the caps above, and kept with them. What follows is what it was
# measured to do while the quota was live.
#
# Within a cell the
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
# The cell `wayfare coverage` counts over, which is a reading grain rather than an
# allocation one and is deliberately not `OVERVIEW_CELL`. Tying the two together made
# the report unreadable the moment the quota grid went fine: 33,545 cells over Great
# Britain gives medians of 0 and 2 features and a busiest-to-emptiest ratio of
# infinity, which says nothing about anything. 0.25 degrees is about 28 km and puts
# 655 cells over the country, which is few enough for a median to mean something.
COVERAGE_CELL = 0.25
# A backstop against one pathological city-centre edge, not a routine truncation.
#
# A long service list does not dominate tile size, which is what a tight cap would be
# for. MVT pools attribute *values* per layer per tile, so an edge carrying
# "1,2,42,X57" costs a value-pool entry shared with every other edge carrying the same
# set, plus two varints on the feature. Measured on Wales, 1,405 of 169,857 edges held
# more than 12 services and the longest held 53 -- a few hundred KB across a handful
# of city tiles, against a sidecar file, a fetch path and a cap to keep in sync.
#
# 64 is set above that measured longest list, so it clears Wales entirely and only a
# city centre denser than any there can reach it. Where it does bite the feature still
# carries the true count in `n`, so the viewer says so rather than quietly showing a
# short list.
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
