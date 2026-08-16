"""Where each region's timetable comes from, and what it owes an acknowledgement to.

Split out of `config`, which holds layout, tunables and environment. A feed is none
of those: it is a description of a source, and the list grows every time a region is
added while nothing else in `config` moves. Keeping them together made a file that
had to be read whole to change a download URL.

`config` re-exports every name here, so anything that reads `config.FEEDS` or
`config.archive_name` keeps working and this file is what to edit.

The dependency runs one way and has to: this imports `licences` for the names a feed
is declared under, and `licences` knows nothing about regions.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import licences

# The licence names a `Feed` is declared with. Everything else about them -- their
# URIs, the `Credit` type, and how a credit is rendered -- lives in `licences`,
# because the list only grows and none of it is configuration. Imported by name so
# that a feed reads as a description of a source rather than as a lookup.
from .licences import CC_BY_4, OGL

# --- Sources ---------------------------------------------------------------

# BODS publishes one national GTFS bundle. Regional slugs exist (see docs/data.md) and
# are useful for development, but `all` is what a production run wants.
BODS_GTFS_URL = "https://data.bus-data.dft.gov.uk/timetable/download/gtfs-file/{region}/"

# The National Transport Authority (NTA) publishes the Republic of Ireland's whole
# timetable as one bundle, with no key and no registration. Per-operator bundles
# sit beside it as GTFS_<Operator>.zip; the index is transitData/PT_Data.html.
NTA_GTFS_URL = "https://www.transportforireland.ie/transitData/Data/GTFS_All.zip"

# Translink publishes Northern Ireland on OpenDataNI as four datasets and no GTFS.
# Resource ids and filenames move on every publication, so the datasets are named
# by slug and resolved through CKAN at fetch time -- see `translink.resource`.
OPENDATANI_API = "https://admin.opendatani.gov.uk/api/3/action/package_show"


def _region_default() -> str:
    """The ambient region, out of `WAYFARE_REGION`.

    Reached through `config` at call time rather than imported, because `config`
    imports this module to re-export it and the environment is `config`'s to own.
    A test that monkeypatches `config.BODS_REGION` therefore reaches every caller,
    which a second binding here would quietly stop doing.
    """
    from . import config

    return config.BODS_REGION


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
    # The window `osmroutes` discovers this region's route relations over, as
    # (south, west, north, east), intersected with the box the region's own stops
    # draw. Only a region whose services leave it needs one: Translink runs coach
    # and rail to Dublin, so a min/max over Northern Ireland's live stops reaches
    # 53.3 N and asks Overpass for most of the Republic.
    bounds: tuple[float, float, float, float] | None = None
    # The names this region's own rail carries in an OSM `operator` tag. A window
    # is a box and a border is not, so the box cannot be the only gate. A region
    # that names none draws whatever its window returns, less anything a region
    # here claims -- which is how Great Britain keeps drawing what it drew while
    # stopping at the Irish Sea.
    operators: tuple[str, ...] = ()


FEEDS = {
    # `ireland` is the Republic; `northern_ireland` is the province, and the two read
    # the same OSM extract, so they share a Valhalla graph. They still need a data
    # root each -- see config.OSM_EXTRACTS for why.
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
        # No bounds: a box cannot describe the Republic, because Donegal reaches
        # further north than any part of Northern Ireland. The operator gate is
        # the whole of the border here.
        operators=("Iarnród Éireann", "Irish Rail"),
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
        # The six counties, padded. Cranfield Point at 54.02 N is the southernmost
        # of them and Burr Point at -5.43 the easternmost; Rathlin is 55.30 N and
        # Fermanagh reaches -8.18. Dublin, at 53.35 N, falls outside, which is what
        # stops Overpass returning the Republic's network. Donegal falls inside and
        # has had no working railway since 1960, and the Sligo line stays south of
        # 54 N as far west as Boyle, so neither meets the box.
        bounds=(54.0, -8.35, 55.35, -5.35),
        operators=("NI Railways", "Northern Ireland Railways", "Translink"),
    ),
}


def feed(region: str | None = None) -> Feed:
    """The bundle for a region slug, BODS unless something else publishes it."""
    region = region or _region_default()
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
    region = region or _region_default()
    name = ARCHIVE_NAMES.get(region, region)
    # A region slug reaches the filesystem here and nowhere else in the pipeline. A
    # slug carrying a separator would write outside OUT rather than fail.
    if name in ("", ".", "..") or name != Path(name).name:
        raise ValueError(f"region {region!r} does not name an archive")
    return f"{name}.pmtiles"


def credit_parts(
    region: str | None = None,
    *,
    road: bool = True,
    operator: bool = False,
    track: bool = False,
) -> tuple[licences.Credit, ...]:
    """Everything a picture of this region owes an acknowledgement to.

    This is the only part of crediting that belongs here rather than in `licences`:
    it needs the `Feed`. What a licence is called and how a credit is written are
    not properties of a source, and `licences.html`, `.lines` and `.text` render
    whatever tuple this returns.

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

    `track` is the second way OpenStreetMap gets into an archive, and it owes the
    same ODbL for a different reason. `road` means Valhalla matched a service onto
    OSM ways; `track` means `wayfare trace` copied an OSM route relation's own
    geometry, which is what draws the Underground. They are independent -- a
    rail-only region has track and no road -- so both set the ODbL credit and
    together they only widen its noun. Getting that noun wrong is not cosmetic: an
    archive of nothing but tube tunnels crediting "Road geometry" describes data it
    does not hold.

    The basemap is not here. It belongs to the page that chooses it, not to the data,
    and a render carries no basemap at all.
    """
    f = feed(region)
    what = (
        "Routes, timetables and operator geometry" if operator else "Routes and timetables"
    )
    parts = [licences.Credit(what, f.attribution, f.licence)]
    if road or track:
        geometry = (
            "Road and track geometry"
            if road and track
            else "Track geometry"
            if track
            else "Road geometry"
        )
        parts.append(licences.openstreetmap(geometry))
    return tuple(parts)
