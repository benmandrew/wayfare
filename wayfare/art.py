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
import re
import time
from collections.abc import Callable, Sequence
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

    def padded(self, d_lon: float, d_lat: float) -> Bounds:
        return Bounds(
            self.min_lon - d_lon,
            self.min_lat - d_lat,
            self.max_lon + d_lon,
            self.max_lat + d_lat,
        )

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


def resolve(bounds_or_name: Bounds | str) -> Bounds:
    if isinstance(bounds_or_name, Bounds):
        return bounds_or_name
    try:
        return PRESETS[bounds_or_name]
    except KeyError:
        known = ", ".join(sorted(PRESETS))
        raise KeyError(f"unknown area {bounds_or_name!r}; known areas: {known}") from None


# --- Projection -------------------------------------------------------------

# Web Mercator's cut-off, where y runs to infinity.
_MERC_MAX_LAT = 85.05112878
_M_PER_DEG_LAT = 111_320.0


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

# Tolerates 'LINESTRING (...)', trailing whitespace and a Z suffix; anything with
# a different geometry type is a bug upstream and should raise rather than silently
# render nothing.
_WKT = re.compile(r"\s*LINESTRING\s*Z?\s*\((.*)\)\s*", re.IGNORECASE | re.DOTALL)

# Matches only the first coordinate pair, right after the opening bracket. Used in
# SQL, where a full regexp_extract_all over every geometry would be the expensive
# part of the query.
_SQL_FIRST_POINT = r"\(\s*(-?[0-9.]+)\s+(-?[0-9.]+)"


def parse_linestring(wkt: str) -> list[tuple[float, float]]:
    """WKT LINESTRING to a list of (lon, lat). No shapely: this is the only
    geometry operation the renderer needs, and it is two splits."""
    m = _WKT.fullmatch(wkt)
    if m is None:
        raise ValueError(f"not a WKT LINESTRING: {wkt[:60]!r}")
    out: list[tuple[float, float]] = []
    for part in m.group(1).split(","):
        xy = part.split()
        out.append((float(xy[0]), float(xy[1])))
    return out


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
# `geom` is WKT text, so there is no spatial index and no numeric column to compare
# against. Three options were on the table:
#
#   1. String comparison on the WKT. Wrong -- lexicographic order on '-2.24 53.48'
#      has nothing to do with geography, and it silently returns plausible garbage.
#   2. Parse every geometry in SQL (regexp_extract_all + list_min/list_max). Exact,
#      but it runs a regex over every byte of every geometry in the table on every
#      render, which at UK scale is the dominant cost of an otherwise cheap query.
#   3. Filter on the *first* vertex only, with the window padded by the longest
#      edge in the table, then do the exact test in Python.
#
# (3) is what runs below. It is conservative and never drops a real hit: an edge's
# path length is at least the distance from its first vertex to any other vertex,
# so padding by max(length_m) cannot miss an edge that reaches into the window. It
# over-selects a thin collar of edges around the window, which Python then discards
# with a four-comparison bbox test -- cheap, and only on the collar. The SQL regex
# stops at the first coordinate pair rather than scanning the whole string.
#
# Cost, roughly: one full scan of `edges` with a short anchored regex per row, plus
# an indexed aggregate over `edge_services` for the surviving rows. Fine for a city.
# For `uk` it reads the whole table, which is the honest price of no spatial index.

_QUERY = """
WITH win AS (
    SELECT edge_id, road_class, length_m, geom
    FROM edges
    WHERE try_cast(regexp_extract(geom, '{pat}', 1) AS DOUBLE) BETWEEN ? AND ?
      AND try_cast(regexp_extract(geom, '{pat}', 2) AS DOUBLE) BETWEEN ? AND ?
), svc AS (
    SELECT s.edge_id,
           count(DISTINCT s.short_name) AS n_services,
           sum(s.n_trips)               AS n_trips
           {services}
    FROM edge_services s JOIN win USING (edge_id)
    GROUP BY s.edge_id
)
SELECT win.edge_id, win.road_class, win.length_m, win.geom,
       coalesce(svc.n_services, 0), coalesce(svc.n_trips, 0){services_col}
FROM win LEFT JOIN svc USING (edge_id)
"""


def _pad_degrees(con: duckdb.DuckDBPyConnection, b: Bounds) -> tuple[float, float]:
    row = con.execute("SELECT max(length_m) FROM edges").fetchone()
    longest = float(row[0]) if row and row[0] is not None else 0.0
    d_lat = longest / _M_PER_DEG_LAT
    # Cap the cosine so a window near the pole cannot ask for an absurd longitude
    # pad. The UK never gets near that, but `uk` reaches 60.9N and the guard is free.
    d_lon = d_lat / max(math.cos(math.radians(b.mid_lat)), 0.25)
    return d_lon, d_lat


def load_edges(
    bounds: Bounds,
    *,
    with_services: bool = False,
    con: duckdb.DuckDBPyConnection | None = None,
) -> list[Edge]:
    """Every edge whose geometry's bbox overlaps `bounds`.

    `with_services` also returns the distinct service names per edge, which only
    `strands` needs and which costs a list aggregate over `edge_services`.
    """
    own = con is None
    con = con or db.connect(read_only=True)
    t0 = time.monotonic()
    try:
        d_lon, d_lat = _pad_degrees(con, bounds)
        coarse = bounds.padded(d_lon, d_lat)
        sql = _QUERY.format(
            pat=_SQL_FIRST_POINT,
            services=", list(DISTINCT s.short_name) AS services" if with_services else "",
            services_col=", svc.services" if with_services else "",
        )
        rows = con.execute(
            sql,
            [coarse.min_lon, coarse.max_lon, coarse.min_lat, coarse.max_lat],
        ).fetchall()
    finally:
        if own:
            con.close()

    edges: list[Edge] = []
    for row in rows:
        coords = parse_linestring(row[3])
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        if len(coords) < 2 or not bounds.hits(lons, lats):
            continue
        names = tuple(sorted(row[6])) if with_services and row[6] else ()
        edges.append(
            Edge(
                edge_id=int(row[0]),
                road_class=row[1],
                length_m=float(row[2] or 0.0),
                coords=coords,
                n_services=int(row[4]),
                n_trips=int(row[5]),
                services=names,
            )
        )
    log.info("%d edges (%d coarse) in %.1fs", len(edges), len(rows), time.monotonic() - t0)
    return edges


# --- Weighting --------------------------------------------------------------


def _normalise(
    values: Sequence[float], lo_q: float = 0.02, hi_q: float = 0.98
) -> list[float]:
    """Map values to [0, 1] through log1p, clamped to a percentile range.

    Trip counts span three orders of magnitude: a city-centre corridor carries
    thousands of buses a week and a village loop carries eight. Linear scaling
    renders everything but the busiest half-dozen roads as invisible hairlines,
    and a raw min/max lets one outlier flatten the rest, hence log plus clipping.
    """
    if not values:
        return []
    logged = [math.log1p(max(v, 0.0)) for v in values]
    ordered = sorted(logged)
    lo = ordered[min(len(ordered) - 1, int(lo_q * len(ordered)))]
    hi = ordered[min(len(ordered) - 1, int(hi_q * len(ordered)))]
    if hi <= lo:
        return [0.5] * len(values)
    return [min(1.0, max(0.0, (v - lo) / (hi - lo))) for v in logged]


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


StyleFn = Callable[
    ["cairo.Context[cairo.Surface]", Sequence[Edge], Projection, RenderOpts], None
]


@dataclass(frozen=True, slots=True)
class Style:
    draw: StyleFn
    background: RGB = (0.02, 0.02, 0.035)
    needs_services: bool = False
    blurb: str = ""


# --- Styles -----------------------------------------------------------------


def _segments(edges: Sequence[Edge], proj: Projection) -> list[list[tuple[float, float]]]:
    return [[proj(lon, lat) for lon, lat in e.coords] for e in edges]


def _stroke_path(
    ctx: cairo.Context[cairo.Surface], pts: Sequence[tuple[float, float]]
) -> None:
    ctx.move_to(*pts[0])
    for p in pts[1:]:
        ctx.line_to(*p)


def draw_density(
    ctx: cairo.Context[cairo.Surface],
    edges: Sequence[Edge],
    proj: Projection,
    opts: RenderOpts,
) -> None:
    """One hue on a dark ground; busy corridors bloom.

    Two additive passes -- a wide, almost invisible halo under a narrow bright
    core. ADD is commutative, so overlapping routes accumulate light exactly the
    way a long exposure does and draw order does not matter.
    """
    import cairo

    weights = _normalise([e.n_trips for e in edges])
    paths = _segments(edges, proj)
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
        for pts, t in zip(paths, weights, strict=True):
            r, g, b = colorsys.hsv_to_rgb(opts.hue, sat_of(t), 1.0)
            ctx.set_source_rgba(r, g, b, min(1.0, alpha_of(t) * opts.alpha_scale))
            ctx.set_line_width(width_of(t) * opts.line_scale)
            ctx.new_path()
            _stroke_path(ctx, pts)
            ctx.stroke()


def draw_spectrum(
    ctx: cairo.Context[cairo.Surface],
    edges: Sequence[Edge],
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

    weights = _normalise([e.n_trips for e in edges])
    paths = _segments(edges, proj)
    ctx.set_operator(cairo.Operator.OVER)

    # Quietest first so the busy roads finish on top and stay legible.
    for idx in sorted(range(len(paths)), key=lambda i: weights[i]):
        t = weights[idx]
        pts = paths[idx]
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
    edges: Sequence[Edge],
    proj: Projection,
    opts: RenderOpts,
) -> None:
    """Every service its own translucent ribbon, woven together.

    All of a service's edges go into a single path and are stroked once. That is
    the whole trick: cairo composites a stroke as one operation, so a service that
    doubles back on itself stays evenly translucent, and only *different* services
    build up on top of each other. Stroking edge by edge would blotch every
    terminus and shared corridor.
    """
    import cairo

    by_service: dict[str, list[Edge]] = {}
    for e in edges:
        for name in e.services:
            by_service.setdefault(name, []).append(e)
    if not by_service:
        log.warning("no service names on these edges; strands has nothing to draw")
        return

    trips = {n: sum(e.n_trips for e in es) for n, es in by_service.items()}
    weights = dict(zip(trips, _normalise(list(trips.values())), strict=True))
    ctx.set_operator(cairo.Operator.SCREEN)

    # Widest first, so the long trunk routes lie underneath the local fiddly ones.
    for name in sorted(by_service, key=lambda n: -len(by_service[n])):
        u = _stable_unit(name)
        # Golden-ratio hue stepping off a stable hash: adjacent services in the
        # list land far apart on the wheel without a hand-built palette.
        hue = (u + _GOLDEN * len(name)) % 1.0
        # Held deliberately saturated and a little dark: SCREEN washes everything
        # toward white where services pile up, so pale ribbons turn the busy middle
        # of a city into a grey blur instead of a weave.
        r, g, b = colorsys.hsv_to_rgb(
            (hue + opts.hue) % 1.0, 0.68 + 0.27 * u, 0.68 + 0.24 * (1.0 - u)
        )
        w = weights[name]
        ctx.set_source_rgba(r, g, b, min(1.0, (0.22 + 0.26 * w) * opts.alpha_scale))
        ctx.set_line_width((0.9 + 3.0 * w) * opts.line_scale)
        ctx.new_path()
        for e in by_service[name]:
            _stroke_path(ctx, [proj(lon, lat) for lon, lat in e.coords])
        ctx.stroke()


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

    if edges is None:
        edges = load_edges(bounds, with_services=spec.needs_services, con=con)
    if not edges:
        log.warning("no edges in %s; writing an empty frame", bounds)

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
    spec.draw(ctx, edges, proj, opts)
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
