"""Northern Ireland, from Translink through OpenDataNI.

BODS covers Great Britain and stops at the Irish Sea. Translink publishes the
whole of Northern Ireland itself, on OpenDataNI, as four datasets and no GTFS:
two TransXChange timetables (Ulsterbus/Goldline and Metro/Glider) and two MapInfo
route-geometry bundles. This module turns those four into the one thing the rest
of the pipeline knows how to read -- a GTFS bundle -- so `patterns` and everything
after it stay unaware that the region arrived any differently.

Three things about that conversion are decisions rather than mechanics.

**The identity is Translink's, not this file's.** `pattern_id` hashes
``route_id || direction || ordered stop ids`` and is a permanent match-cache key,
so anything invented here that moved between releases would silently re-match the
province. Every field that reaches it is therefore copied from the feed rather
than generated: stop ids are the NaPTAN ATCO codes verbatim, direction is the
TransXChange ``Direction``, and `route_id` is the operator code and the line name
(``ULB-40``). The obvious `route_id` -- the ``ServiceCode``, ``2-40-_-y18-1`` --
was rejected for exactly this reason: its leading number is the operating branch,
``y18`` is the schedule dataset, and the trailing number is a registration
revision. All three move without the bus changing.

**`shape_id` is derived from the stop sequence.** Two journey patterns with the
same stops have the same geometry, and `gtfs.py` collapses them with
``mode(shape_id)``, which has no tiebreak. Hashing the stops means those two carry
one id and the mode is unambiguous -- the same "every ORDER BY needs a unique
tiebreak" rule, met by removing the tie instead.

**A shape is all of a journey's hops or none of them.** Geometry is published per
stop-to-stop link and on its own cadence -- eleven months behind the timetable as
this was written -- so some hops have no polyline. Stitching what exists and
jumping the rest would hand the matcher a straight line across a town, which
`map_snap` would confidently lay down the wrong roads; a journey with any hop
missing therefore carries no shape at all and takes the `stops` path, which is
what that path is for. It is a strict rule and it costs: 96.1% of hops have
geometry and only 62.0% of journeys do, because a rural service with sixty hops
needs all sixty.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

from . import config, logs, mapinfo

log = logs.get("translink")

TXC = "{http://www.transxchange.org.uk/}"

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_RANGE = re.compile(rf"^({'|'.join(DAYS)})To({'|'.join(DAYS)})$")
_DURATION = re.compile(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")

# TransXChange Mode against the GTFS route_type this project filters on. Coach is
# the extended code 200, which `config.ROAD_ROUTE_TYPES` keeps and a naive
# `route_type = 3` filter would delete -- the same trap the GB feed sprang.
MODES = {
    "bus": "3",
    "coach": "200",
    "trolleyBus": "11",
    "tram": "0",
    "metro": "1",
    "underground": "1",
    "rail": "2",
    "ferry": "4",
}

# The MapInfo members that matter. PtLinks is the stop-to-stop road geometry;
# StoppingPoints is what carries the ATCO code that joins it to the timetable.
LINKS_MEMBER = re.compile(r"PtLinks.*\.MIF$", re.I)
STOPS_MEMBER = re.compile(r"StoppingPoints.*\.MIF$", re.I)


# --- OpenDataNI --------------------------------------------------------------


@dataclass(frozen=True)
class Resource:
    url: str
    filename: str
    size: int


def resource(dataset: str, session: requests.Session | None = None) -> Resource:
    """The current file for an OpenDataNI dataset.

    Resolved through CKAN rather than hardcoded, because Translink re-uploads
    rather than overwrites: both the resource id in the path and the filename move
    on every publication -- the timetable that was `ulb-gle-16042026.zip` in April
    is `ulsterbus-and-goldline-until-31st-august-26.zip` now. The dataset slug is
    the only stable handle, and the slugs are historical names that do not describe
    their contents. Do not "correct" them.

    A dataset carries specifications and sample images beside the data, so the pick
    is the most recently published ZIP -- which is also what separates this year's
    Metro routes from the 2022 Glider ones filed in the same dataset.
    """
    get = (session or requests).get
    r = get(
        config.OPENDATANI_API,
        params={"id": dataset},
        headers={"User-Agent": config.USER_AGENT},
        timeout=60,
    )
    r.raise_for_status()
    body = r.json()
    if not body.get("success"):
        raise RuntimeError(f"OpenDataNI refused package_show for {dataset!r}")
    zips = [
        x
        for x in body["result"]["resources"]
        if (x.get("format") or "").upper() == "ZIP" and x.get("url")
    ]
    if not zips:
        raise RuntimeError(f"OpenDataNI dataset {dataset!r} publishes no ZIP")
    newest = max(zips, key=lambda x: x.get("last_modified") or x.get("created") or "")
    return Resource(
        newest["url"],
        newest["url"].rsplit("/", 1)[-1],
        int(newest.get("size") or 0),
    )


# --- Geometry ----------------------------------------------------------------


def _extract(zips: Iterable[Path], dest: Path, wanted: re.Pattern[str]) -> list[Path]:
    """Pull one MIF and its MID out of each bundle. Returns the MIF paths."""
    out = []
    for z in zips:
        with zipfile.ZipFile(z) as zf:
            for name in zf.namelist():
                if not wanted.search(name):
                    continue
                for suffix in (".MIF", ".MID"):
                    member = name[: -len(".MIF")] + suffix
                    target = dest / (z.stem + "_" + Path(member).name)
                    with zf.open(member) as fin, target.open("wb") as fout:
                        shutil.copyfileobj(fin, fout)
                out.append(dest / (z.stem + "_" + Path(name).name))
    return out


def _atco_by_point(mifs: Iterable[Path]) -> dict[tuple[str, str, str, str], str]:
    """Translink's internal stopping-point key against the NaPTAN ATCO code.

    ``GlobalId`` is the ATCO code and is the whole reason the two halves of this
    source can be joined at all: the timetable knows stops only by ATCO code and
    the geometry knows them only by a numeric triple.
    """
    out: dict[tuple[str, str, str, str], str] = {}
    for mif in mifs:
        for f in mapinfo.read(mif, mif.with_suffix(".MID")):
            atco = f.values.get("GlobalId", "").strip()
            if atco:
                key = (
                    f.values["SubNetwork"],
                    f.values["StopID"],
                    f.values["StopAreaID"],
                    f.values["StoppingPointID"],
                )
                out[key] = atco
    return out


def _hop_geometry(
    mifs: Iterable[Path], atco: dict[tuple[str, str, str, str], str]
) -> dict[tuple[str, str], tuple[tuple[float, float], ...]]:
    """Road geometry for each ordered pair of stops, keyed on ATCO codes.

    A pair usually has several polylines, one per line and branch that runs it. The
    kept one is the shortest, tie-broken on the coordinates themselves. That is a
    property of the geometry rather than of file order, so the pick survives
    Translink reordering its export -- which "keep the first" would not, and an
    unstable pick would change every shape and every match for no reason.
    """
    best: dict[tuple[str, str], tuple[float, tuple[tuple[float, float], ...]]] = {}
    unresolved = 0
    for mif in mifs:
        for f in mapinfo.read(mif, mif.with_suffix(".MID")):
            if len(f.points) < 2:
                continue
            v = f.values
            net = v["SubNetwork"]
            a = atco.get(
                (net, v["FromStopID"], v["FromStopAreaID"], v["FromStoppingPointID"])
            )
            b = atco.get((net, v["ToStopID"], v["ToStopAreaID"], v["ToStoppingPointID"]))
            if not a or not b:
                unresolved += 1
                continue
            key = (a, b)
            score = (_length(f.points), f.points)
            if key not in best or score < best[key]:
                best[key] = score
    if unresolved:
        log.warning("%d road links name a stopping point with no ATCO code", unresolved)
    return {k: v[1] for k, v in best.items()}


def _length(points: tuple[tuple[float, float], ...]) -> float:
    """Squared-degree path length. Only ever compared against another hop between
    the same two stops, so the projection error over a few hundred metres of
    Northern Ireland cannot change an ordering."""
    return sum(
        (x2 - x1) ** 2 + (y2 - y1) ** 2
        for (x1, y1), (x2, y2) in zip(points, points[1:], strict=False)
    )


# --- TransXChange ------------------------------------------------------------


@dataclass(frozen=True)
class Stop:
    atco: str
    name: str
    lat: float
    lon: float


@dataclass(frozen=True)
class Route:
    route_id: str
    agency: str
    short_name: str
    long_name: str
    route_type: str


@dataclass(frozen=True)
class Trip:
    trip_id: str
    route_id: str
    days: tuple[int, ...]
    start: str
    end: str
    direction: str
    stops: tuple[str, ...]
    times: tuple[int, ...]


def _text(el: ET.Element, path: str) -> str:
    found = el.findtext(TXC + path.replace("/", f"/{TXC}"))
    return (found or "").strip()


def _seconds(duration: str) -> int:
    m = _DURATION.match(duration.strip() or "PT0S")
    if not m:
        log.warning("unreadable run time %r; treated as zero", duration)
        return 0
    d, h, mi, s = (int(g or 0) for g in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def _days(profile: ET.Element | None) -> tuple[int, ...]:
    """The seven-bit week an OperatingProfile describes.

    Only the day pattern is read. Bank holiday and special-day operation shift
    individual dates rather than the shape of the week, and the week is the whole
    of what this feeds -- `patterns` weights a journey by days per week and nothing
    downstream sees a date.
    """
    if profile is None:
        return (1, 1, 1, 1, 1, 0, 0)
    week = [0] * 7
    named = False
    for dow in profile.iter(TXC + "DaysOfWeek"):
        for child in dow:
            tag = child.tag.split("}")[-1]
            named = True
            if tag in DAYS:
                week[DAYS.index(tag)] = 1
            elif tag == "Weekend":
                week[5] = week[6] = 1
            elif m := _RANGE.match(tag):
                lo, hi = DAYS.index(m.group(1)), DAYS.index(m.group(2))
                for i in range(lo, hi + 1):
                    week[i] = 1
            else:
                # A day pattern nobody here has named. Warned rather than guessed
                # at silently, and weighted as a working week -- the same default
                # `gtfs.py` applies to a trip with no calendar row at all.
                log.warning("unrecognised day pattern %r; weighted as Mon-Fri", tag)
                week[:5] = [1] * 5
    if not named and profile.find(TXC + "RegularDayType") is not None:
        # HolidaysOnly and its kin: a real profile that names no ordinary day.
        return (0,) * 7
    return tuple(week)


def _clock(hhmmss: str) -> int:
    parts = (hhmmss or "0:0:0").split(":")
    h, m, s = (int(p or 0) for p in (parts + ["0", "0", "0"])[:3])
    return (h * 60 + m) * 60 + s


def _departure(el: ET.Element) -> int:
    """A journey's first time, in seconds from midnight of the day it is filed under.

    ``DepartureDayShift`` is a journey that leaves after midnight and is timetabled
    against the previous day, so its times run past 24 hours -- which is how GTFS
    writes the same thing, and is what keeps the night buses in the right order.
    """
    depart = _clock(_text(el, "DepartureTime"))
    return depart + 86_400 if _text(el, "DepartureDayShift") else depart


# The tags that arrive in bulk, and so the ones worth freeing as they end. An
# `Operator` is a handful of elements in the whole file and clearing it buys
# nothing.
_CLEARED = ("StopPoint", "JourneyPatternSection", "Service", "VehicleJourney")

_Section = tuple[tuple[str, ...], tuple[int, ...]]
_Service = tuple[Route, tuple[int, ...], str, str]


@dataclass
class _Reading:
    """What one forward pass over a TransXChange file has seen so far.

    Each handler is one tag's effect on this state and reads nothing else. That is
    what keeps the pass to a single loop: the element order the format guarantees
    means a journey arrives after the stops, sections and services it refers back
    to, so every handler finds what the earlier ones left here.
    """

    stops: dict[str, Stop] = field(default_factory=dict)
    operators: dict[str, str] = field(default_factory=dict)
    sections: dict[str, _Section] = field(default_factory=dict)
    # journey pattern id -> (service code, direction, section ref)
    patterns: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    services: dict[str, _Service] = field(default_factory=dict)
    routes: dict[str, Route] = field(default_factory=dict)
    trips: list[Trip] = field(default_factory=list)

    def stop_point(self, el: ET.Element) -> None:
        atco = _text(el, "AtcoCode")
        lat = _text(el, "Place/Location/Latitude")
        lon = _text(el, "Place/Location/Longitude")
        # Not `if lat and lon`: Translink has shipped stops at exactly 0,0, which
        # is in the Gulf of Guinea and passes every non-null test.
        if atco and float(lat or 0) and float(lon or 0):
            self.stops[atco] = Stop(
                atco, _text(el, "Descriptor/CommonName"), float(lat), float(lon)
            )

    def operator(self, el: ET.Element) -> None:
        self.operators[el.get("id") or ""] = _text(el, "OperatorCode")

    def section(self, el: ET.Element) -> None:
        links = el.findall(TXC + "JourneyPatternTimingLink")
        seq = [_text(link, "From/StopPointRef") for link in links]
        runs = [_seconds(_text(link, "RunTime")) for link in links]
        if seq:
            seq.append(_text(links[-1], "To/StopPointRef"))
        self.sections[el.get("id") or ""] = (tuple(seq), tuple(runs))

    def service(self, el: ET.Element) -> None:
        code = _text(el, "ServiceCode")
        agency = self.operators.get(_text(el, "RegisteredOperatorRef"), "")
        line = _text(el, "Lines/Line/LineName")
        mode = _text(el, "Mode") or "bus"
        if mode not in MODES:
            log.warning("unrecognised mode %r on service %s", mode, code)
        route = Route(
            f"{agency}-{line}",
            agency,
            line,
            _text(el, "Description"),
            MODES.get(mode, "3"),
        )
        # Two branches of one line register separately, so the same route_id
        # arrives twice. Keep the first by service code so the long name does
        # not depend on file order.
        self.routes.setdefault(route.route_id, route)
        self.services[code] = (
            route,
            _days(el.find(TXC + "OperatingProfile")),
            _text(el, "OperatingPeriod/StartDate").replace("-", ""),
            _text(el, "OperatingPeriod/EndDate").replace("-", ""),
        )
        for jp in el.iter(TXC + "JourneyPattern"):
            self.patterns[jp.get("id") or ""] = (
                code,
                _text(jp, "Direction"),
                _text(jp, "JourneyPatternSectionRefs"),
            )

    def journey(self, el: ET.Element) -> None:
        pattern = self.patterns.get(_text(el, "JourneyPatternRef"))
        if pattern is None:
            return
        code, direction, section_ref = pattern
        found = self.services.get(code)
        section = self.sections.get(section_ref)
        if found is None or section is None or len(section[0]) < 2:
            return
        stop_seq, run_times = section
        route, week, start, end = found
        own = el.find(TXC + "OperatingProfile")
        times = [_departure(el)]
        for run in run_times:
            times.append(times[-1] + run)
        self.trips.append(
            Trip(
                _text(el, "VehicleJourneyCode"),
                route.route_id,
                _days(own) if own is not None else week,
                start,
                end,
                direction,
                stop_seq,
                tuple(times),
            )
        )


def read_timetable(
    path: Path,
) -> tuple[dict[str, Stop], dict[str, Route], list[Trip]]:
    """One TransXChange file: its stops, its routes and one trip per journey.

    Parsed with ``iterparse`` and cleared as it goes -- the Metro file is 104 MB of
    XML, and holding a tree of it costs about a gigabyte for no purpose. The
    element order the format guarantees is what makes one forward pass enough:
    stops, then journey pattern sections, then services, then the journeys that
    refer back to all three.
    """
    seen = _Reading()
    handlers = {
        "StopPoint": seen.stop_point,
        "Operator": seen.operator,
        "JourneyPatternSection": seen.section,
        "Service": seen.service,
        "VehicleJourney": seen.journey,
    }
    for _, el in ET.iterparse(str(path), events=("end",)):
        tag = el.tag.split("}")[-1]
        handle = handlers.get(tag)
        if handle is not None:
            handle(el)
        if tag in _CLEARED:
            el.clear()
    return seen.stops, seen.routes, seen.trips


def _hhmmss(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


# --- GTFS --------------------------------------------------------------------


def _direction(name: str) -> str:
    """GTFS direction_id. Only inbound and outbound are published, and this must
    stay a total function: direction is part of the pattern identity, so an
    unmapped value silently splitting a route in two would be a cache miss."""
    return "1" if name == "inbound" else "0"


def _shape_id(stops: tuple[str, ...]) -> str:
    key = "|".join(stops).encode()
    return "SH" + hashlib.sha1(key, usedforsecurity=False).hexdigest()[:16]


def _stitch(
    stops: tuple[str, ...],
    hops: dict[tuple[str, str], tuple[tuple[float, float], ...]],
) -> tuple[tuple[float, float], ...] | None:
    """The road a journey takes, or None if any hop of it is unpublished."""
    out: list[tuple[float, float]] = []
    for a, b in zip(stops, stops[1:], strict=False):
        line = hops.get((a, b))
        if line is None:
            return None
        # Consecutive links meet at the shared stop, so the joint vertex arrives
        # twice; a duplicated point is a zero-length segment the matcher has to
        # reason about for nothing.
        out.extend(line[1:] if out and out[-1] == line[0] else line)
    return tuple(out)


def _write(dest: Path, name: str, header: str, rows: Iterator[list[str]]) -> None:
    with (dest / name).open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header.split(","))
        w.writerows(rows)


def _read_timetables(
    timetables: list[Path], work: Path
) -> tuple[dict[str, Stop], dict[str, Route], list[Trip], str]:
    """Every TransXChange file of every bundle, merged, with the newest build stamp."""
    stops: dict[str, Stop] = {}
    routes: dict[str, Route] = {}
    trips: list[Trip] = []
    version = ""
    for path in _timetable_members(timetables, work):
        file_stops, file_routes, file_trips = read_timetable(path)
        stops.update(file_stops)
        routes.update(file_routes)
        trips.extend(file_trips)
        version = max(version, _created(path))
        log.info(
            "%s: %d stops, %d routes, %d journeys",
            path.name,
            len(file_stops),
            len(file_routes),
            len(file_trips),
        )
    return stops, routes, trips, version


def _shapes_for(
    trips: list[Trip], hops: dict[tuple[str, str], tuple[tuple[float, float], ...]]
) -> dict[str, tuple[tuple[float, float], ...]]:
    """One shape per distinct stop sequence, empty where a hop is unpublished.

    The empty entry is kept rather than left out, so a sequence whose geometry is
    incomplete is stitched once and not once per journey that runs it.
    """
    shapes: dict[str, tuple[tuple[float, float], ...]] = {}
    shaped = 0
    for trip in trips:
        sid = _shape_id(trip.stops)
        if sid not in shapes:
            line = _stitch(trip.stops, hops)
            shapes[sid] = line or ()
        if shapes[sid]:
            shaped += 1
    log.info(
        "%d of %d journeys carry road geometry (%.1f%%)",
        shaped,
        len(trips),
        100.0 * shaped / max(len(trips), 1),
    )
    return shapes


def _write_tables(
    stage: Path,
    stops: dict[str, Stop],
    routes: dict[str, Route],
    trips: list[Trip],
    shapes: dict[str, tuple[tuple[float, float], ...]],
    version: str,
) -> None:
    """The seven GTFS files, every one of them in a defined order.

    Order is the whole reason this is one function: nothing here reads a clock or a
    file system, so two builds of one publication write the same bytes.
    """
    _write(
        stage,
        "agency.txt",
        "agency_id,agency_name,agency_url,agency_timezone",
        (
            [a, a, "https://www.translink.co.uk/", "Europe/London"]
            for a in sorted({r.agency for r in routes.values()})
        ),
    )
    _write(
        stage,
        "stops.txt",
        "stop_id,stop_name,stop_lat,stop_lon",
        (
            [s.atco, s.name, f"{s.lat:.6f}", f"{s.lon:.6f}"]
            for s in sorted(stops.values(), key=lambda s: s.atco)
        ),
    )
    _write(
        stage,
        "routes.txt",
        "route_id,agency_id,route_short_name,route_long_name,route_type",
        (
            [r.route_id, r.agency, r.short_name, r.long_name, r.route_type]
            for r in sorted(routes.values(), key=lambda r: r.route_id)
        ),
    )
    calendars = sorted({(t.days, t.start, t.end) for t in trips})
    _write(
        stage,
        "calendar.txt",
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date",
        ([_service_id(d, a, b), *(str(x) for x in d), a, b] for d, a, b in calendars),
    )
    _write(
        stage,
        "trips.txt",
        "route_id,service_id,trip_id,direction_id,shape_id",
        (
            [
                t.route_id,
                _service_id(t.days, t.start, t.end),
                t.trip_id,
                _direction(t.direction),
                _shape_id(t.stops) if shapes[_shape_id(t.stops)] else "",
            ]
            for t in trips
        ),
    )
    _write(
        stage,
        "stop_times.txt",
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence",
        (
            [t.trip_id, _hhmmss(sec), _hhmmss(sec), stop, str(i + 1)]
            for t in trips
            for i, (stop, sec) in enumerate(zip(t.stops, t.times, strict=False))
        ),
    )
    _write(
        stage,
        "shapes.txt",
        "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence",
        (
            [sid, f"{lat:.6f}", f"{lon:.6f}", str(i + 1)]
            for sid, line in sorted(shapes.items())
            for i, (lon, lat) in enumerate(line)
        ),
    )
    # The version is the timetable's creation stamp and deliberately not the
    # geometry's. It keys `first_seen`/`last_seen`, so it has to move when the
    # timetable does and stay still when it does not; a geometry refresh
    # changes no pattern and would report churn that never happened.
    starts = [t.start for t in trips if t.start]
    ends = [t.end for t in trips if t.end]
    _write(
        stage,
        "feed_info.txt",
        "feed_publisher_name,feed_publisher_url,feed_lang,feed_start_date,"
        "feed_end_date,feed_version",
        iter(
            [
                [
                    "Translink",
                    "https://www.opendatani.gov.uk/",
                    "en",
                    min(starts, default=""),
                    max(ends, default=""),
                    version,
                ]
            ]
        ),
    )


def _zip_bundle(stage: Path, out: Path) -> None:
    """Pack the staged tables, through a `.part` so a kill leaves no usable half."""
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_suffix(out.suffix + ".part")
    with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(stage.iterdir()):
            # A fixed timestamp rather than the staged file's own. Every row is
            # written in a defined order, so two builds of one publication differ
            # only in when they ran -- and this is the one feed in the project
            # whose bytes are ours to make comparable.
            info = zipfile.ZipInfo(f.name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, f.read_bytes())
    part.replace(out)


def build_gtfs(timetables: list[Path], geometry: list[Path], out: Path) -> Path:
    """Assemble the four Translink downloads into one GTFS bundle."""
    with tempfile.TemporaryDirectory(prefix="translink-") as tmp:
        work = Path(tmp)
        stage = work / "gtfs"
        stage.mkdir()

        stop_mifs = _extract(geometry, work, STOPS_MEMBER)
        link_mifs = _extract(geometry, work, LINKS_MEMBER)
        hops = _hop_geometry(link_mifs, _atco_by_point(stop_mifs))
        log.info("%d stop-to-stop road links from %d bundles", len(hops), len(geometry))

        stops, routes, trips, version = _read_timetables(timetables, work)
        shapes = _shapes_for(trips, hops)
        _write_tables(stage, stops, routes, trips, shapes, version)
        _zip_bundle(stage, out)
    log.info(
        "built %s (%.1f MB), feed version %s", out.name, out.stat().st_size / 1e6, version
    )
    return out


def _service_id(days: tuple[int, ...], start: str, end: str) -> str:
    """A calendar id built from the calendar itself, prefixed by region.

    The prefix is not decoration. Translink's day patterns and the Republic's
    service ids are both numeric, they collide, and one OSM extract covers the
    island -- so the two feeds are meant to be able to share a database.
    """
    return "NI-" + "".join(str(d) for d in days) + f"-{start}-{end}"


def _timetable_members(zips: Iterable[Path], dest: Path) -> Iterator[Path]:
    for z in zips:
        with zipfile.ZipFile(z) as zf:
            for name in sorted(n for n in zf.namelist() if n.lower().endswith(".xml")):
                target = dest / (z.stem + "_" + Path(name).name)
                with zf.open(name) as fin, target.open("wb") as fout:
                    shutil.copyfileobj(fin, fout)
                yield target


def _created(path: Path) -> str:
    """The timetable's own build stamp, as the sortable form BODS already uses.

    ``CreationDateTime`` is on the root element, so this reads the first tag and
    stops rather than parsing 104 MB to fetch one attribute.
    """
    for _, el in ET.iterparse(str(path), events=("start",)):
        stamp = el.get("CreationDateTime") or ""
        m = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)", stamp)
        return "".join(m.groups()[:3]) + "_" + "".join(m.groups()[3:]) if m else ""
    return ""


def parts_manifest(paths: dict[str, Path]) -> str:
    """What the built bundle was built from, so a rebuild can be skipped."""
    return json.dumps(
        {
            k: [str(v), v.stat().st_size, int(v.stat().st_mtime)]
            for k, v in sorted(paths.items())
        },
        sort_keys=True,
    )
