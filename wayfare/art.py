"""Art renderings of the bus network.

Purpose (2) of the project: take a window onto the UK and draw every road that
carries a bus, weighted by how much bus it carries. This module owns the whole
path from a bounding box to a finished PNG or SVG. It is deliberately separate
from the tile publishing code -- tiles are for reading, these are for looking at.

The three styles are the point. ``density`` is the classic glowing-arteries look,
``spectrum`` colours the grid by the compass bearing of each segment, and
``strands`` gives every service its own translucent ribbon so the overlaps weave.
Add more via :data:`STYLES`.
"""

from __future__ import annotations

import colorsys
import hashlib
import math
import time
from array import array
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import config, db, logs

if TYPE_CHECKING:  # pragma: no cover - typing only
    import cairo
    import duckdb

log = logs.get("art")

RGB = tuple[float, float, float]
# Maps a normalised traffic weight in [0, 1] to a line width, alpha or saturation.
Ramp = Callable[[float], float]

# All drawing is done in logical units and the surface is scaled up for print, so
# a "1px" line means the same physical thickness at any `scale`.
BASE_DPI = 96.0


# --- Geography --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bounds:
    """A WGS84 window, west/south/east/north."""

    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float

    def __post_init__(self) -> None:
        if self.max_lon <= self.min_lon or self.max_lat <= self.min_lat:
            raise ValueError(f"degenerate bounds: {self}")

    @property
    def mid_lat(self) -> float:
        return (self.min_lat + self.max_lat) / 2

    def hits(self, lons: Sequence[float], lats: Sequence[float]) -> bool:
        """True if the bbox of these points overlaps this window."""
        return (
            min(lons) <= self.max_lon
            and max(lons) >= self.min_lon
            and min(lats) <= self.max_lat
            and max(lats) >= self.min_lat
        )


# Framing hints, not administrative boundaries -- they exist to point the camera,
# and a bit of slack around the edge of a conurbation usually renders better than
# a tight legal border would.
PRESETS: dict[str, Bounds] = {
    "greater_manchester": Bounds(-2.75, 53.32, -1.90, 53.70),
    "london": Bounds(-0.55, 51.26, 0.32, 51.71),
    "west_midlands": Bounds(-2.20, 52.35, -1.60, 52.68),
    "greater_glasgow": Bounds(-4.55, 55.75, -4.00, 56.00),
    "west_yorkshire": Bounds(-2.05, 53.55, -1.20, 53.98),
    "tyne_and_wear": Bounds(-1.85, 54.83, -1.32, 55.08),
    "bristol": Bounds(-2.75, 51.38, -2.45, 51.56),
    "cardiff": Bounds(-3.32, 51.42, -3.08, 51.57),
    "edinburgh": Bounds(-3.42, 55.87, -3.03, 56.02),
    "liverpool": Bounds(-3.10, 53.29, -2.72, 53.53),
    "sheffield": Bounds(-1.65, 53.30, -1.32, 53.47),
    "belfast": Bounds(-6.10, 54.50, -5.70, 54.72),
    "uk": Bounds(-8.75, 49.85, 1.95, 60.90),
}

# Everything the dataset could ever cover. Used only to warn when a hand-written
# window falls somewhere there will never be buses.
ISLES = Bounds(-11.5, 49.4, 2.6, 61.3)


def resolve(bounds_or_name: Bounds | str) -> Bounds:
    """A Bounds, a preset name, or a raw ``minlon,minlat,maxlon,maxlat`` window."""
    if isinstance(bounds_or_name, Bounds):
        return bounds_or_name
    if "," in bounds_or_name:
        return parse_bbox(bounds_or_name)
    try:
        return PRESETS[bounds_or_name]
    except KeyError:
        known = ", ".join(sorted(PRESETS))
        raise KeyError(
            f"unknown area {bounds_or_name!r}; known areas: {known}"
            " -- or give a window as minlon,minlat,maxlon,maxlat"
        ) from None


def parse_bbox(text: str) -> Bounds:
    """Parse ``minlon,minlat,maxlon,maxlat``.

    Ordering is west,south,east,north to match GeoJSON and the OGC convention,
    which is also the order Overpass and Geofabrik use. It is the opposite of the
    lat,lon order a map UI usually shows, so the error messages say which is which
    rather than just reporting a bad number.
    """
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 4:
        raise ValueError(
            f"expected 4 comma-separated numbers as minlon,minlat,maxlon,maxlat, "
            f"got {len(parts)} in {text!r}"
        )
    try:
        min_lon, min_lat, max_lon, max_lat = (float(p) for p in parts)
    except ValueError as exc:
        raise ValueError(f"not a number in {text!r}: {exc}") from None

    for name, lon in (("minlon", min_lon), ("maxlon", max_lon)):
        if not -180.0 <= lon <= 180.0:
            raise ValueError(f"{name}={lon} is out of range; longitudes run -180 to 180")
    for name, lat in (("minlat", min_lat), ("maxlat", max_lat)):
        if not -90.0 <= lat <= 90.0:
            raise ValueError(f"{name}={lat} is out of range; latitudes run -90 to 90")

    b = Bounds(min_lon, min_lat, max_lon, max_lat)
    # Range checks cannot catch a swapped pair here: a UK latitude near 51 is a
    # perfectly valid longitude, and a UK longitude near -3 is a valid latitude, so
    # lat,lon order parses cleanly and silently puts the window off West Africa.
    # What does catch it is that the data only covers these islands.
    if not b.hits([ISLES.min_lon, ISLES.max_lon], [ISLES.min_lat, ISLES.max_lat]):
        log.warning(
            "%s lies outside the British Isles, so the render will be empty. "
            "The order is minlon,minlat,maxlon,maxlat -- lon first, not lat.",
            text,
        )
    return b


# --- Projection -------------------------------------------------------------

# Web Mercator's cut-off, where y runs to infinity.
_MERC_MAX_LAT = 85.05112878


def _merc(lon: float, lat: float) -> tuple[float, float]:
    """Forward spherical Mercator on the unit sphere, y increasing north.

    Mercator because it is conformal: it preserves angles, so a right-angled
    junction renders as a right angle and the screen angle of a segment *is* its
    compass bearing. `spectrum` leans on that second property directly. The earth
    radius is omitted because :meth:`Projection.fit` normalises the scale anyway.
    """
    phi = math.radians(min(max(lat, -_MERC_MAX_LAT), _MERC_MAX_LAT))
    return math.radians(lon), math.log(math.tan(math.pi / 4 + phi / 2))


@dataclass(frozen=True, slots=True)
class Projection:
    """Maps lon/lat to canvas pixels, aspect preserved and letterboxed."""

    width: int
    height: int
    k: float  # pixels per unit of Mercator
    x0: float  # Mercator x at the west edge of the content
    y1: float  # Mercator y at the north edge of the content
    ox: float  # letterbox offset, pixels
    oy: float

    @classmethod
    def fit(cls, b: Bounds, width: int, height: int) -> Projection:
        x0, y0 = _merc(b.min_lon, b.min_lat)
        x1, y1 = _merc(b.max_lon, b.max_lat)
        # min() rather than max() so the whole window fits; the leftover becomes
        # margin. Never scale the axes independently -- that shears the map.
        k = min(width / (x1 - x0), height / (y1 - y0))
        return cls(
            width=width,
            height=height,
            k=k,
            x0=x0,
            y1=y1,
            ox=(width - (x1 - x0) * k) / 2,
            oy=(height - (y1 - y0) * k) / 2,
        )

    @staticmethod
    def canvas_height(b: Bounds, width: int) -> int:
        """Height that makes the window exactly fill a canvas of `width`."""
        x0, y0 = _merc(b.min_lon, b.min_lat)
        x1, y1 = _merc(b.max_lon, b.max_lat)
        return max(1, round(width * (y1 - y0) / (x1 - x0)))

    def __call__(self, lon: float, lat: float) -> tuple[float, float]:
        x, y = _merc(lon, lat)
        return (x - self.x0) * self.k + self.ox, (self.y1 - y) * self.k + self.oy

    def content_rect(self, b: Bounds) -> tuple[float, float, float, float]:
        """(x, y, w, h) of the projected window, for clipping off the letterbox."""
        x_min, y_max = self(b.min_lon, b.min_lat)
        x_max, y_min = self(b.max_lon, b.max_lat)
        return x_min, y_min, x_max - x_min, y_max - y_min


# --- Data -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Edge:
    """One road segment plus what runs over it."""

    edge_id: int
    road_class: str | None
    length_m: float
    coords: list[tuple[float, float]]  # (lon, lat)
    n_services: int
    n_trips: int  # timetabled trips per week, summed over services
    services: tuple[str, ...] = ()  # only populated when the style needs it


# The bbox filter.
#
# `edges` stores each geometry's bounding box as four micro-degree integers, so the
# window test is an exact integer overlap in SQL and nothing has to look inside the
# geometry. That is worth stating because it used not to be: when geom was WKT text
# there was no numeric column to compare against, and the filter matched the *first*
# vertex only, over-selected a collar of edges padded by the longest edge in the
# table, and re-tested each one in Python. This does the same job in one pass, with
# no collar and no false positives.
#
# There is still no spatial index, so a window over `uk` reads the whole table. That
# is the honest price, and four integer comparisons per row is a cheap way to pay it.

_WINDOW = """
    SELECT edge_id, road_class, length_m, lon_e6, lat_e6
    FROM edges
    WHERE min_lon_e6 <= ? AND max_lon_e6 >= ?
      AND min_lat_e6 <= ? AND max_lat_e6 >= ?
"""

_QUERY = f"""
WITH win AS ({_WINDOW}
), svc AS (
    SELECT s.edge_id,
           count(DISTINCT s.short_name) AS n_services,
           sum(s.n_trips)               AS n_trips
           {{services}}
    FROM edge_services s JOIN win USING (edge_id)
    GROUP BY s.edge_id
)
SELECT win.edge_id, win.road_class, win.length_m, win.lon_e6, win.lat_e6,
       coalesce(svc.n_services, 0), coalesce(svc.n_trips, 0){{services_col}}
FROM win LEFT JOIN svc USING (edge_id)
{{order}}
"""

# Just the trip counts, for the percentile pass. Eight bytes an edge against the
# hundreds its geometry costs, which is what lets the bounds be known before a
# single coordinate is read.
_WEIGHTS_QUERY = f"""
WITH win AS ({_WINDOW})
SELECT coalesce(sum(s.n_trips), 0)
FROM win LEFT JOIN edge_services s USING (edge_id)
GROUP BY win.edge_id
"""

# The shared part of the two strand queries.
#
# `pair` is DISTINCT because a service registered by two operators has two rows per
# edge in edge_services, and a ribbon must cover each edge once.
#
# `trips` deliberately sums each edge's total traffic across every service, not the
# service's own -- so a minor route along a busy corridor gets a wide ribbon. That
# is what the list version measured and what the existing renders look like; it is
# a property of the picture rather than an accident worth silently changing here.
_STRAND_BASE = f"""
WITH win AS ({_WINDOW}
), edge_trips AS (
    SELECT s.edge_id, sum(s.n_trips) AS n_trips
    FROM edge_services s JOIN win USING (edge_id)
    GROUP BY s.edge_id
), pair AS (
    SELECT DISTINCT s.short_name, s.edge_id
    FROM edge_services s JOIN win USING (edge_id)
), svc AS (
    SELECT p.short_name, count(*) AS n_edges, sum(t.n_trips) AS trips
    FROM pair p JOIN edge_trips t USING (edge_id)
    GROUP BY p.short_name
)
"""

# One row per service in the window. Small -- hundreds for a city, thousands
# nationally -- so it is materialised, and it both orders the draw and scales the
# ribbon widths.
_SERVICE_QUERY = f"""
{_STRAND_BASE}
SELECT short_name, n_edges, trips FROM svc
ORDER BY n_edges DESC, short_name
"""

# Every (service, edge) pair in the window, ordered so a service's edges arrive
# together and the widest service comes first. An edge carrying five services
# appears five times, which is the point: the geometry streams past rather than
# being held in a dict of service to edge list.
_STRAND_QUERY = f"""
{_STRAND_BASE}
SELECT p.short_name, win.lon_e6, win.lat_e6
FROM pair p
JOIN win USING (edge_id)
JOIN svc USING (short_name)
ORDER BY svc.n_edges DESC, p.short_name
"""

FETCH_ROWS = 20_000


class Window:
    """The edges of a view, offered as a stream that can be walked more than once.

    A style never needs more than one edge at a time, but it does need the weight
    scale for the whole window up front. Holding every edge to get that is what made
    a national render expensive -- Wales over the `uk` preset is 439 MB of Edge
    objects, and the country is roughly twenty-five times Wales.

    So the scale comes from its own pass over the trip counts alone, and the
    geometry is pulled through in chunks afterwards. Each `edges()` call reopens the
    query; the cost is another scan, which is cheap next to holding the result.
    """

    def __init__(
        self,
        bounds: Bounds,
        con: duckdb.DuckDBPyConnection,
        *,
        with_services: bool = False,
    ) -> None:
        self.bounds = bounds
        self.con = con
        self.with_services = with_services
        self._params = [
            round(bounds.max_lon * 1e6),
            round(bounds.min_lon * 1e6),
            round(bounds.max_lat * 1e6),
            round(bounds.min_lat * 1e6),
        ]
        self.weights = Weights.over(
            array("q", (r[0] for r in con.execute(_WEIGHTS_QUERY, self._params).fetchall()))
        )

    def edges(self, *, by_weight: bool = False) -> Iterator[Edge]:
        """Every edge whose bbox overlaps the window.

        `by_weight` orders quietest first in SQL, which is what `spectrum` used to
        get by sorting the whole list in memory. The weight is monotonic in the trip
        count, so ordering by one orders by the other.
        """
        sql = _QUERY.format(
            services=", list(DISTINCT s.short_name) AS services"
            if self.with_services
            else "",
            services_col=", svc.services" if self.with_services else "",
            # edge_id breaks the tie, so equally busy roads draw in a fixed order
            # rather than whichever way the scan happened to return them.
            order="ORDER BY coalesce(svc.n_trips, 0), win.edge_id" if by_weight else "",
        )
        cur = self.con.execute(sql, self._params)
        while chunk := cur.fetchmany(FETCH_ROWS):
            for row in chunk:
                edge = _to_edge(row, with_services=self.with_services)
                if edge is not None and self.bounds.hits(
                    [c[0] for c in edge.coords], [c[1] for c in edge.coords]
                ):
                    yield edge

    def strands(self) -> Iterator[tuple[str, float, list[tuple[float, float]]]]:
        """(service, its weight, one edge's coordinates), grouped by service.

        Consecutive tuples sharing a service name belong to the same ribbon, widest
        service first. The caller strokes a name's geometry as one path and moves on.
        """
        rows = self.con.execute(_SERVICE_QUERY, self._params).fetchall()
        if not rows:
            return
        weights = Weights.over(array("q", (r[2] for r in rows)))
        weight_of = {r[0]: weights.of(r[2]) for r in rows}

        cur = self.con.execute(_STRAND_QUERY, self._params)
        while chunk := cur.fetchmany(FETCH_ROWS):
            for name, lon_e6, lat_e6 in chunk:
                if not lon_e6 or len(lon_e6) < 2:
                    continue
                coords = [(x / 1e6, y / 1e6) for x, y in zip(lon_e6, lat_e6, strict=True)]
                if self.bounds.hits([c[0] for c in coords], [c[1] for c in coords]):
                    yield name, weight_of[name], coords


class Held(Window):
    """A window backed by edges already in memory.

    `render(edges=...)` exists so a caller can re-render a window it already has,
    which is worth keeping for anyone tuning options against one area. It is the
    only path that still holds everything, and it is the caller's choice to.
    """

    def __init__(self, edges: Sequence[Edge]) -> None:
        self._edges = list(edges)
        self.weights = Weights.over([e.n_trips for e in self._edges])

    def edges(self, *, by_weight: bool = False) -> Iterator[Edge]:
        if by_weight:
            return iter(sorted(self._edges, key=lambda e: e.n_trips))
        return iter(self._edges)

    def strands(self) -> Iterator[tuple[str, float, list[tuple[float, float]]]]:
        by_service: dict[str, list[Edge]] = {}
        for e in self._edges:
            for name in e.services:
                by_service.setdefault(name, []).append(e)
        if not by_service:
            return
        trips = {n: sum(e.n_trips for e in es) for n, es in by_service.items()}
        weights = Weights.over(list(trips.values()))
        for name in sorted(by_service, key=lambda n: (-len(by_service[n]), n)):
            for e in by_service[name]:
                yield name, weights.of(trips[name]), e.coords


def _to_edge(row: tuple[Any, ...], *, with_services: bool) -> Edge | None:
    lon_e6, lat_e6 = row[3], row[4]
    if not lon_e6 or len(lon_e6) < 2:
        return None
    return Edge(
        edge_id=int(row[0]),
        road_class=row[1],
        length_m=float(row[2] or 0.0),
        coords=[(x / 1e6, y / 1e6) for x, y in zip(lon_e6, lat_e6, strict=True)],
        n_services=int(row[5]),
        n_trips=int(row[6]),
        services=tuple(sorted(row[7])) if with_services and row[7] else (),
    )


def load_edges(
    bounds: Bounds,
    *,
    with_services: bool = False,
    con: duckdb.DuckDBPyConnection | None = None,
) -> list[Edge]:
    """Every edge whose geometry's bbox overlaps `bounds`, all at once.

    Kept for callers that genuinely want the list -- `render(edges=...)` and tests.
    Rendering itself streams; see :class:`Window`.
    """
    own = con is None
    con = con or db.connect(read_only=True)
    t0 = time.monotonic()
    try:
        edges = list(Window(bounds, con, with_services=with_services).edges())
    finally:
        if own:
            con.close()
    log.info("%d edges in %.1fs", len(edges), time.monotonic() - t0)
    return edges


# --- Weighting --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Weights:
    """A log scale clamped to a percentile range, held as just its two bounds.

    Trip counts span three orders of magnitude: a city-centre corridor carries
    thousands of buses a week and a village loop carries eight. Linear scaling
    renders everything but the busiest half-dozen roads as invisible hairlines,
    and a raw min/max lets one outlier flatten the rest, hence log plus clipping.

    Two bounds rather than a list of normalised values, so a style can weight an
    edge as it arrives instead of holding the whole window to weight it.
    """

    lo: float  # in log space
    hi: float

    @classmethod
    def over(
        cls, values: Iterable[float], lo_q: float = 0.02, hi_q: float = 0.98
    ) -> Weights:
        # log1p is monotonic, so taking the percentiles of the raw values and
        # logging the two picks is the same as logging everything first -- and it
        # means the pass that finds them never has to build a second list.
        ordered = sorted(values)
        if not ordered:
            return cls(0.0, 0.0)
        lo = ordered[min(len(ordered) - 1, int(lo_q * len(ordered)))]
        hi = ordered[min(len(ordered) - 1, int(hi_q * len(ordered)))]
        return cls(math.log1p(max(lo, 0.0)), math.log1p(max(hi, 0.0)))

    def of(self, value: float) -> float:
        if self.hi <= self.lo:
            return 0.5
        v = math.log1p(max(value, 0.0))
        return min(1.0, max(0.0, (v - self.lo) / (self.hi - self.lo)))


def _normalise(
    values: Sequence[float], lo_q: float = 0.02, hi_q: float = 0.98
) -> list[float]:
    """The original list-at-a-time scale, kept as the reference :class:`Weights` is
    tested against.

    Nothing draws through it any more -- the styles weight each edge as it streams
    past -- but the two must agree exactly, or the same window would come out
    differently depending on which path rendered it. Pinning that here is what makes
    the change from one to the other checkable.
    """
    if not values:
        return []
    w = Weights.over(values, lo_q, hi_q)
    return [w.of(v) for v in values]


def _stable_unit(text: str) -> float:
    """A deterministic float in [0, 1) from a string.

    Python's `hash` is salted per process, so using it here would give a different
    palette on every run. Rendering the same area twice must give the same picture.
    """
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2**64


_GOLDEN = 0.6180339887498949


# --- Rendering options ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RenderOpts:
    width_px: int = 2000
    height_px: int | None = None  # None fits the window exactly, no letterbox
    scale: float = 1.0  # surface multiplier for print; 2.0 is ~192 dpi
    caption: str | None = None  # off by default
    background: RGB | None = None  # overrides the style's own ground
    hue: float = 0.56  # base hue for density, palette rotation elsewhere
    line_scale: float = 1.0
    alpha_scale: float = 1.0


StyleFn = Callable[["cairo.Context[cairo.Surface]", "Window", Projection, RenderOpts], None]


@dataclass(frozen=True, slots=True)
class Style:
    draw: StyleFn
    background: RGB = (0.02, 0.02, 0.035)
    needs_services: bool = False
    blurb: str = ""


# --- Styles -----------------------------------------------------------------


def _stroke_path(
    ctx: cairo.Context[cairo.Surface], pts: Sequence[tuple[float, float]]
) -> None:
    ctx.move_to(*pts[0])
    for p in pts[1:]:
        ctx.line_to(*p)


def draw_density(
    ctx: cairo.Context[cairo.Surface],
    window: Window,
    proj: Projection,
    opts: RenderOpts,
) -> None:
    """One hue on a dark ground; busy corridors bloom.

    Two additive passes -- a wide, almost invisible halo under a narrow bright
    core. ADD is commutative, so overlapping routes accumulate light exactly the
    way a long exposure does and draw order does not matter.

    The two passes are two walks of the window rather than two walks of a list, so
    the geometry is read twice and held never.
    """
    import cairo

    ctx.set_operator(cairo.Operator.ADD)

    # (width, alpha, saturation) as functions of normalised traffic: first the
    # broad dim halo, then the narrow bright core over it.
    passes: tuple[tuple[Ramp, Ramp, Ramp], ...] = (
        (lambda t: 3.0 + 16.0 * t, lambda t: 0.012 + 0.075 * t, lambda t: 0.95),
        (
            lambda t: 0.5 + 3.6 * t**0.8,
            lambda t: 0.10 + 0.80 * t,
            lambda t: 0.90 - 0.75 * t,
        ),
    )
    for width_of, alpha_of, sat_of in passes:
        for e in window.edges():
            t = window.weights.of(e.n_trips)
            r, g, b = colorsys.hsv_to_rgb(opts.hue, sat_of(t), 1.0)
            ctx.set_source_rgba(r, g, b, min(1.0, alpha_of(t) * opts.alpha_scale))
            ctx.set_line_width(width_of(t) * opts.line_scale)
            ctx.new_path()
            _stroke_path(ctx, [proj(lon, lat) for lon, lat in e.coords])
            ctx.stroke()


def draw_spectrum(
    ctx: cairo.Context[cairo.Surface],
    window: Window,
    proj: Projection,
    opts: RenderOpts,
) -> None:
    """Hue by compass bearing, so the grid's orientation becomes visible colour.

    Bearing is taken modulo 180 degrees and stretched over the full colour wheel.
    A road is an axis, not an arrow -- driving it the other way must not change its
    colour -- and folding at 180 has the happy side effect that perpendicular
    streets come out in complementary hues, which is what makes a gridded city read
    as a plaid and an organic one read as a smear.

    The angle is measured in screen space, which is legitimate here only because
    Mercator is conformal: the projected angle equals the true bearing.
    """
    import cairo

    ctx.set_operator(cairo.Operator.OVER)

    # Quietest first so the busy roads finish on top and stay legible. The ordering
    # is done in SQL rather than by sorting the window in memory -- the weight is
    # monotonic in the trip count, so ordering by one orders by the other.
    for e in window.edges(by_weight=True):
        t = window.weights.of(e.n_trips)
        pts = [proj(lon, lat) for lon, lat in e.coords]
        sat = 0.30 + 0.62 * t
        val = 0.52 + 0.48 * t
        alpha = min(1.0, (0.30 + 0.62 * t) * opts.alpha_scale)
        ctx.set_line_width((0.6 + 3.4 * t**0.8) * opts.line_scale)
        for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
            dx, dy = x1 - x0, y1 - y0
            if dx == 0.0 and dy == 0.0:
                continue
            # Screen y grows downward, so negate it to get a north-up bearing.
            bearing = math.atan2(dx, -dy) % math.pi
            r, g, b = colorsys.hsv_to_rgb((bearing / math.pi + opts.hue) % 1.0, sat, val)
            ctx.set_source_rgba(r, g, b, alpha)
            ctx.new_path()
            ctx.move_to(x0, y0)
            ctx.line_to(x1, y1)
            ctx.stroke()


def draw_strands(
    ctx: cairo.Context[cairo.Surface],
    window: Window,
    proj: Projection,
    opts: RenderOpts,
) -> None:
    """Every service its own translucent ribbon, woven together.

    All of a service's edges go into a single path and are stroked once. That is
    the whole trick: cairo composites a stroke as one operation, so a service that
    doubles back on itself stays evenly translucent, and only *different* services
    build up on top of each other. Stroking edge by edge would blotch every
    terminus and shared corridor.

    That requirement is why this one streams grouped rather than flat: the window
    hands back (service, edge) pairs already ordered by service, so a ribbon is
    accumulated into cairo's current path and stroked when the name changes. Only
    one service's geometry is ever live.
    """
    import cairo

    ctx.set_operator(cairo.Operator.SCREEN)
    current: str | None = None
    drew = False

    def finish() -> None:
        if current is not None:
            ctx.stroke()

    # Widest first, so the long trunk routes lie underneath the local fiddly ones.
    for name, weight, coords in window.strands():
        if name != current:
            finish()
            current = name
            drew = True
            u = _stable_unit(name)
            # Golden-ratio hue stepping off a stable hash: adjacent services in the
            # list land far apart on the wheel without a hand-built palette.
            hue = (u + _GOLDEN * len(name)) % 1.0
            # Held deliberately saturated and a little dark: SCREEN washes everything
            # toward white where services pile up, so pale ribbons turn the busy
            # middle of a city into a grey blur instead of a weave.
            r, g, b = colorsys.hsv_to_rgb(
                (hue + opts.hue) % 1.0, 0.68 + 0.27 * u, 0.68 + 0.24 * (1.0 - u)
            )
            ctx.set_source_rgba(
                r, g, b, min(1.0, (0.22 + 0.26 * weight) * opts.alpha_scale)
            )
            ctx.set_line_width((0.9 + 3.0 * weight) * opts.line_scale)
            ctx.new_path()
        _stroke_path(ctx, [proj(lon, lat) for lon, lat in coords])
    finish()

    if not drew:
        log.warning("no service names on these edges; strands has nothing to draw")


STYLES: dict[str, Style] = {
    "density": Style(
        draw=draw_density,
        background=(0.015, 0.018, 0.03),
        blurb="weekly trip volume as light",
    ),
    "spectrum": Style(
        draw=draw_spectrum,
        background=(0.03, 0.03, 0.04),
        blurb="hue by compass bearing",
    ),
    "strands": Style(
        draw=draw_strands,
        background=(0.04, 0.035, 0.045),
        needs_services=True,
        blurb="one ribbon per service",
    ),
}


# --- Canvas -----------------------------------------------------------------


def _require_cairo() -> Any:
    """Imported lazily so `import wayfare.art` works without the extra installed.

    The presets, the projection and the query are all useful to a caller that only
    wants coordinates, and pycairo pulls in a system libcairo that a headless
    pipeline box has no reason to carry.
    """
    try:
        import cairo
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError(
            "rendering needs pycairo. Install the extra with: pip install -e '.[art]'"
        ) from exc
    return cairo


def _caption(ctx: cairo.Context[cairo.Surface], text: str, proj: Projection) -> None:
    """Small, tracked-out, low-contrast, bottom left. Meant to be read second."""
    import cairo

    size = max(10.0, proj.width / 130)
    ctx.save()
    ctx.set_operator(cairo.Operator.OVER)
    ctx.select_font_face("sans-serif", cairo.FontSlant.NORMAL, cairo.FontWeight.NORMAL)
    ctx.set_font_size(size)
    ctx.set_source_rgba(1.0, 1.0, 1.0, 0.40)
    x = size * 2.2
    y = proj.height - size * 2.2
    # The toy text API has no letter-spacing, so advance by hand. Wide tracking is
    # what keeps a caption at this size from looking like a stray label.
    for ch in text.upper():
        ctx.move_to(x, y)
        ctx.show_text(ch)
        x += ctx.text_extents(ch).x_advance + size * 0.22
    ctx.restore()


def _fmt(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix not in (".png", ".svg"):
        raise ValueError(f"unsupported output format {suffix!r}; use .png or .svg")
    return suffix


def _surface(path: Path, w: int, h: int, scale: float) -> tuple[cairo.Surface, float]:
    """Surface plus the factor drawing should be scaled by.

    SVG is resolution independent, so `scale` is ignored there and the surface is
    sized in points; PNG gets a bigger pixel buffer and a matching context scale,
    which keeps every line width in the styles meaning the same physical thickness.
    """
    import cairo

    if _fmt(path) == ".svg":
        return cairo.SVGSurface(str(path), w, h), 1.0
    return (
        cairo.ImageSurface(
            cairo.FORMAT_ARGB32, max(1, round(w * scale)), max(1, round(h * scale))
        ),
        scale,
    )


def _default_path(bounds_or_name: Bounds | str, style: str) -> Path:
    stem = bounds_or_name if isinstance(bounds_or_name, str) else "custom"
    return config.OUT / f"{stem}-{style}.png"


def render(
    bounds_or_name: Bounds | str,
    style: str = "density",
    out_path: str | Path | None = None,
    *,
    opts: RenderOpts | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
    edges: Sequence[Edge] | None = None,
) -> Path:
    """Draw `bounds_or_name` in `style` and return the path written.

    `out_path` decides the format by suffix (.png or .svg) and defaults to
    ``OUT/<area>-<style>.png``. Pass `edges` to re-render a window you already
    loaded without touching the database again.
    """
    # Argument checks come first, and before requiring cairo: a mistyped style
    # should say which styles exist, not tell the caller to install a dependency
    # they would then discover was not the problem.
    try:
        spec = STYLES[style]
    except KeyError:
        known = ", ".join(sorted(STYLES))
        raise KeyError(f"unknown style {style!r}; known styles: {known}") from None

    bounds = resolve(bounds_or_name)
    opts = opts or RenderOpts()
    path = Path(out_path) if out_path else _default_path(bounds_or_name, style)
    fmt = _fmt(path)  # before the query, so a typo'd suffix fails in milliseconds

    cairo = _require_cairo()
    config.ensure_dirs()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Streamed from the database unless the caller handed over edges it already
    # holds; either way the styles see the same interface.
    own_con = con is None and edges is None
    if edges is not None:
        window: Window = Held(edges)
    else:
        con = con or db.connect(read_only=True)
        window = Window(bounds, con, with_services=spec.needs_services)

    width = opts.width_px
    height = opts.height_px or Projection.canvas_height(bounds, width)
    proj = Projection.fit(bounds, width, height)

    surface, draw_scale = _surface(path, width, height, opts.scale)
    ctx = cairo.Context(surface)
    ctx.scale(draw_scale, draw_scale)
    ctx.set_antialias(cairo.Antialias.BEST)
    ctx.set_line_cap(cairo.LineCap.ROUND)
    ctx.set_line_join(cairo.LineJoin.ROUND)

    r, g, b = opts.background or spec.background
    ctx.set_source_rgb(r, g, b)
    ctx.paint()

    ctx.save()
    # Clip to the window rather than the frame: the query returns a collar of edges
    # just outside the bounds, and without this they bleed into the letterbox.
    ctx.rectangle(*proj.content_rect(bounds))
    ctx.clip()
    t0 = time.monotonic()
    try:
        spec.draw(ctx, window, proj, opts)
    finally:
        if own_con and con is not None:
            con.close()
    ctx.restore()

    if opts.caption:
        _caption(ctx, opts.caption, proj)

    if fmt == ".png":
        surface.write_to_png(str(path))
    surface.finish()
    log.info(
        "%s %dx%d %s in %.1fs -> %s",
        style,
        width,
        height,
        f"@{opts.scale:g}x" if opts.scale != 1.0 else "",
        time.monotonic() - t0,
        path,
    )
    return path
