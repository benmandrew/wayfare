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
import io
import json
import math
import os
import struct
import time
import zlib
from array import array
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Protocol
from xml.sax.saxutils import escape

from . import config, db, licences, logs

if TYPE_CHECKING:  # pragma: no cover - typing only
    import cairo
    import duckdb

log = logs.get("art")

RGB = tuple[float, float, float]
# Maps a normalised traffic weight in [0, 1] to a line width, alpha or saturation.
Ramp = Callable[[float], float]

# The canvas width `density`'s stroke widths are quoted against. `RenderOpts.scale`
# handles print resolution; this handles the other axis, how much map a pixel
# covers. Chosen as 2,000 because that is `RenderOpts.width_px`'s own default.
DENSITY_REF_PX = 2000.0


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

    def as_wsen_e6(self) -> tuple[int, int, int, int]:
        """This window in micro-degrees, west/south/east/north.

        Geometry comes out of the database as the integers it is stored as, so the
        drawing path tests it against these rather than dividing every vertex by a
        million first. Scaling the window up once is four operations; scaling the
        vertices down is one per point, on the hottest loop there is.

        Named for its order, because :meth:`as_predicate_params` is the same four
        numbers in a different one and the two are not interchangeable.
        """
        return (
            round(self.min_lon * 1e6),
            round(self.min_lat * 1e6),
            round(self.max_lon * 1e6),
            round(self.max_lat * 1e6),
        )

    def as_predicate_params(self) -> list[int]:
        """The same four numbers, in the order `_Sql.where` binds them.

        The window predicate is an overlap test rather than a containment one, so
        each stored bound is compared against the *opposite* edge of the window:
        `min_lon_e6 <= max_lon AND max_lon_e6 >= min_lon`, and the same for latitude.
        That crossing is why the parameter order is not west/south/east/north, and
        why it is written down once here rather than restated at each call site.
        """
        return [
            round(self.max_lon * 1e6),
            round(self.min_lon * 1e6),
            round(self.max_lat * 1e6),
            round(self.min_lat * 1e6),
        ]


# Framing hints, not administrative boundaries -- they exist to point the camera,
# and a bit of slack around the edge of a conurbation usually renders better than
# a tight legal border would.
PRESETS: dict[str, Bounds] = {
    "manchester": Bounds(-2.75, 53.32, -1.90, 53.70),
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

    def flat(self, lon_e6: Any, lat_e6: Any) -> tuple[list[float], list[float]]:
        """Project one flat run of micro-degree vertices to canvas pixels.

        Takes and returns the coordinates unsplit -- no per-edge structure at all --
        because that is the shape the arithmetic wants and, since the geometry
        arrives from DuckDB as Arrow list columns, it is also the shape it comes in.
        An Arrow list column *is* a flat child buffer plus a vector of offsets, so
        the child buffer goes straight into numpy with no copy and no Python object
        per vertex. Splitting it into per-edge lists first was the single largest
        cost in a render's database half: 852ms against 198ms over the London window
        at 3000px, 197,276 edges and 585,287 vertices.

        Lists rather than numpy arrays come back because every consumer is a Python
        loop feeding cairo one vertex at a time, and indexing a list of floats beats
        indexing an ndarray -- the latter boxes a fresh scalar on every access. The
        one bulk `.tolist()` is C-level and pays for itself immediately.
        """
        np = _require_numpy()

        lon = np.asarray(lon_e6, dtype=np.int64) / 1e6
        lat = np.clip(
            np.asarray(lat_e6, dtype=np.int64) / 1e6, -_MERC_MAX_LAT, _MERC_MAX_LAT
        )
        # The same forward Mercator as `_merc`, one array at a time.
        mx = np.radians(lon)
        my = np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))
        xs: list[float] = ((mx - self.x0) * self.k + self.ox).tolist()
        ys: list[float] = ((self.y1 - my) * self.k + self.oy).tolist()
        return xs, ys

    def batch(
        self, lon_e6: Sequence[Sequence[int]], lat_e6: Sequence[Sequence[int]]
    ) -> list[list[tuple[float, float]]]:
        """Project many edges' micro-degree geometry to canvas pixels at once.

        Vectorised because the arithmetic is per *vertex*, and a whole fetch of
        20,000 edges is around 100,000 of them. numpy over one edge's four or five
        vertices would lose to the list comprehension it replaces -- array setup
        costs more than the trig it saves -- so the batching is the point, not the
        library. Measured over a million edges: 4.78s scalar, 2.94s here.

        The lists come back in the order given, one per edge, so a caller can zip
        them straight back against the rows it fetched.

        The drawing path does not come through here -- :meth:`flat` projects a whole
        fetch and :class:`Polyline` keeps the per-edge view without materialising it.
        What this form is for is checking that one: it is where the vectorised
        projection is tested against a scalar `__call__` per vertex, which needs the
        per-edge structure the fast path deliberately does not build.
        """
        lens = [len(v) for v in lon_e6]
        if not sum(lens):
            return [[] for _ in lens]
        xs, ys = self.flat(
            [v for edge in lon_e6 for v in edge], [v for edge in lat_e6 for v in edge]
        )
        out: list[list[tuple[float, float]]] = []
        at = 0
        for n in lens:
            out.append(list(zip(xs[at : at + n], ys[at : at + n], strict=True)))
            at += n
        return out


def simplify(pts: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    """Drop vertices that land within `tol` pixels of the last one kept.

    The list-of-tuples form of :meth:`Polyline.simplified`, and a delegate to it so
    that the rule has one implementation. Nothing on the drawing path comes through
    here -- geometry reaches a style as a :class:`Polyline` -- but the rule is easier
    to read and to test over plain points, and a second copy of it would be free to
    drift.
    """
    return Polyline.of(pts).simplified(tol).points()


@dataclass(frozen=True, slots=True, eq=False)
class Polyline:
    """One edge's projected geometry, as indices into a batch's flat coordinates.

    The point is what it does *not* hold. `xs` and `ys` belong to the whole fetch of
    20,000 edges and are shared by every path in it; this carries only which of
    those vertices are its own. So a path costs one small object rather than a list
    of tuples, and simplification is a narrowing of `idx` rather than a second list
    of tuples built from the first.

    `idx` is a `range` for an unsimplified path, which allocates nothing at all --
    the overwhelmingly common case, since `spectrum` never simplifies and a preview
    at low tolerance drops nothing. It becomes a list only once vertices are
    actually dropped.

    Equality is over the *points*, not the representation, because the two ways of
    producing a path do not agree on the representation: :class:`Window` hands out
    slices of a shared buffer and :class:`Held` builds a two-element buffer per
    edge. Only the polyline is meant to be the same, and a test comparing the two
    implementations is testing exactly that.
    """

    xs: Sequence[float]
    ys: Sequence[float]
    idx: Sequence[int]

    def __len__(self) -> int:
        return len(self.idx)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Polyline):
            return NotImplemented
        return self.points() == other.points()

    def points(self) -> list[tuple[float, float]]:
        """The polyline as (x, y) tuples. Materialises; the draw path avoids it."""
        xs, ys = self.xs, self.ys
        return [(xs[i], ys[i]) for i in self.idx]

    def segments(self) -> Iterator[tuple[float, float, float, float]]:
        """Consecutive vertex pairs as bare floats, for a style that colours by one.

        Four floats rather than two points because `spectrum` immediately unpacks
        them into a subtraction; building the tuples to take them apart again is
        the allocation this class exists to avoid, and that style is the one that
        never simplifies, so it sees every vertex there is.
        """
        xs, ys = self.xs, self.ys
        it = iter(self.idx)
        i = next(it, None)
        if i is None:
            return
        x0, y0 = xs[i], ys[i]
        for j in it:
            x1, y1 = xs[j], ys[j]
            yield x0, y0, x1, y1
            x0, y0 = x1, y1

    def simplified(self, tol: float) -> Polyline:
        """This path with sub-`tol` vertices dropped.

        A Valhalla directed edge averages 4.14 coordinates over tens of metres, so at
        a preview width most of an edge is smaller than a pixel and collapses to its
        two endpoints. That matters because cairo's cost is tessellating joins and
        caps once per vertex -- not per pixel, and not per stroke. Over a million
        edges this drops 64% of the vertices and 30% of the draw time, for a
        difference in 0.05% of the output bytes.

        The comparison is against the last *kept* vertex rather than the previous
        one, so a gently curving road accumulates its small steps and survives;
        comparing against the previous vertex would straighten it out entirely.
        Endpoints are always kept, so edges still meet where they met. What narrows
        is `idx`, so a simplified path costs a list of indices rather than a second
        list of tuples.

        Not every style may use this -- see `draw_spectrum`, which takes colour from
        the angle between points and so cannot afford to lose any.
        """
        idx = self.idx
        if tol <= 0.0 or len(idx) <= 2:
            return self
        xs, ys = self.xs, self.ys
        first, last = idx[0], idx[-1]
        keep = [first]
        kx, ky = xs[first], ys[first]
        for i in idx[1:-1]:
            x, y = xs[i], ys[i]
            if abs(x - kx) >= tol or abs(y - ky) >= tol:
                keep.append(i)
                kx, ky = x, y
        keep.append(last)
        return Polyline(xs, ys, keep)

    @classmethod
    def of(cls, pts: Sequence[tuple[float, float]]) -> Polyline:
        """A path over its own buffer, for a caller that already has the tuples."""
        return cls([p[0] for p in pts], [p[1] for p in pts], range(len(pts)))


# --- Data -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Edge:
    """One road segment plus what runs over it."""

    edge_id: int
    road_class: str | None
    length_m: float
    coords: list[tuple[float, float]]  # (lon, lat)
    # Whatever `QuerySpec.weight` asked for -- trips per week by default, but possibly
    # a service count or traffic per metre. Named for its role rather than its usual
    # contents, because a field called n_trips holding a count of operators is a lie.
    weight: float
    groups: tuple[str, ...] = ()  # only populated when the style draws grouped paths


# --- The query spec ---------------------------------------------------------
#
# What a style paints is one half of a render; the other half is which edges are in
# frame, what scalar drives the ramps, and what a "group" means. That second half
# lives in the spec because it is the half that reaches pictures no paint knob can:
# the same three styles grouped by operator or filtered to one road class are
# genuinely different maps.
#
# It is exposed as a closed vocabulary rather than a query language. Substituted text
# is only ever a value looked up in one of the dicts below; anything the caller
# supplies is a bound parameter. That matters more than it looks: DuckDB's read_only
# applies to the database file and not to the filesystem, so `read_csv` and `ATTACH`
# still work and user SQL would be an arbitrary file read on the server.

# The scalar the ramps see, per edge. Aggregated over `edge_services`, so it may
# reference `s.*`; `win.*` is joined alongside, so edge columns are available too.
WEIGHTS: dict[str, str] = {
    "trips": "sum(s.n_trips)",
    "services": "count(DISTINCT s.short_name)",
    "operators": "count(DISTINCT s.agency_id)",
    "patterns": "sum(s.n_patterns)",
    "busiest": "max(s.n_trips)",
    # Traffic per metre rather than per edge. An edge is tens of metres, so a long
    # rural link and a short city block carrying the same buses currently weigh the
    # same; this asks the other question. greatest() guards a zero length.
    "density": "sum(s.n_trips) / greatest(min(win.length_m), 1.0)",
}

# What one ribbon is, for styles that draw grouped paths. A service key puts an edge
# in as many groups as it has services; an edge-level key puts it in exactly one.
# Both work through the same query, which is what makes this cheap.
GROUPS: dict[str, str] = {
    "service": "s.short_name",
    "operator": "s.agency_id",
    "road_class": "win.road_class",
    "way": "win.way_id",
    "road_name": "win.road_name",
}

# Which group is drawn first, and so ends up underneath. `widest` is the original and
# the default: long trunk routes lie under the local fiddly ones. Held as (column,
# direction) rather than a SQL string because the same ordering has to be written
# against two different sets of table aliases; see _order_sql.
ORDERS: dict[str, tuple[str, str]] = {
    "widest": ("n_edges", "DESC"),
    "narrowest": ("n_edges", "ASC"),
    "busiest": ("trips", "DESC"),
    "quietest": ("trips", "ASC"),
    "name": ("grp", "ASC"),
}

# Above this many groups, `strands` is not drawing ribbons any more -- it is drawing
# one stroke per edge with the compositing cost of a ribbon, and _SERVICE_QUERY
# materialises a row per group. `way` over a city is the way to trip it.
MAX_GROUPS = 20_000

# Below this many edges in the window, a render is drawn on one core. Banding starts
# eight interpreters -- `spawn`, not `fork`, because the parent holds a DuckDB handle
# -- and that costs about a second whatever the picture. Cardiff at 1,200px is 56,000
# edges and 0.75s serial, so banding it made it twice as slow; London at 200,000 goes
# 8.2s to 4.2s. The floor sits between them, nearer the small end because the loss
# below it is bounded by the start-up cost and the win above it is not.
MIN_BAND_EDGES = 150_000

# Where a render's chain assignment is registered while it draws. An identifier this
# code chose, never anything a request supplied -- the same rule `Source` follows.
CHAIN_VIEW = "wf_chain"


@dataclass(frozen=True, slots=True)
class QuerySpec:
    """Which edges, weighted how, grouped by what.

    Defaults reproduce the plain traffic-by-service query exactly, so a render that
    does not ask for anything is unaffected by the spec.
    """

    weight: str = "trips"
    group: str = "service"
    order: str = "widest"
    # Filters. The service-plane ones (operator, service) and min_trips change which
    # services contribute; the edge-plane ones (road_class) shrink the scan itself.
    operator: tuple[str, ...] = ()
    service: tuple[str, ...] = ()
    road_class: tuple[str, ...] = ()
    min_trips: int = 0
    # Draw one edge in `sample`, chosen by a hash of the edge id. Not a filter and
    # not a picture anyone wants: it exists because a render costs per edge and
    # hardly anything per pixel, so thinning the edges is the only way to make a
    # preview cheap. It belongs here rather than in RenderOpts because it decides
    # *which edges there are*, which is what this spec is for -- but unlike the
    # filters it is deliberately absent from `selective`, since it narrows nothing
    # semantically and an edge with no services should still drop out at the same
    # rate as any other.
    sample: int = 1

    def __post_init__(self) -> None:
        # Named, because this message is what an HTTP caller sees: "unknown 'busiest'"
        # is ambiguous when `busiest` is a valid weight *and* a valid order, and a
        # request carrying two mistakes should not make the reader guess which one
        # this is about.
        for name, value, table in (
            ("weight", self.weight, WEIGHTS),
            ("group", self.group, GROUPS),
            ("order", self.order, ORDERS),
        ):
            if value not in table:
                known = ", ".join(sorted(table))
                raise ValueError(f"unknown {name}={value!r}; known {name}s: {known}")
        if self.sample < 1:
            raise ValueError(f"sample={self.sample} must be 1 or more")

    @property
    def selective(self) -> bool:
        """Whether any filter narrows what is drawn.

        This decides join semantics, and it is the one place the spec is not a free
        substitution. Unfiltered, an edge with no services still draws -- black, at
        weight zero -- which is the original behaviour and worth keeping. Filtered to
        one operator, an edge that operator does not use must vanish rather than
        render as a black line through the middle of the picture.
        """
        return bool(self.operator or self.service or self.road_class or self.min_trips)

    @property
    def key(self) -> str:
        """Stable identity, for a cache key or an ETag.

        JSON rather than a delimiter join, because a join is not injective: with
        commas inside pipe-separated fields, `service=("A", "B")` and
        `service=("A,B",)` produce the same string. They are different specs, and two
        specs sharing a key means one's picture is served for the other.
        """
        return json.dumps(
            [
                self.weight,
                self.group,
                self.order,
                list(self.operator),
                list(self.service),
                list(self.road_class),
                self.min_trips,
                self.sample,
            ],
            separators=(",", ":"),
        )


DEFAULT_SPEC = QuerySpec()


# The two tables a render reads, named rather than spelled out at each of the half
# dozen sites that scan them. Substituting either for a materialised or extracted
# window was tried and is not worth having: a density render is 75% cairo and the
# whole database scan is a quarter of the rest, and a Parquet extract of the window
# measured *slower* on a filtered spec. See docs/rendering.md.
EDGES = "edges"
SERVICES = "edge_services"


@dataclass(frozen=True, slots=True)
class Source:
    """The relations a render is handed rather than deriving for itself.

    Both exist for banding, and both are the same argument: a band draws a fraction
    of the window, so anything it works out from its own rows is worked out from a
    fraction of the country. The parent computes each once over the whole picture and
    every band is given it. Every name here is an identifier this code chose and
    registered; nothing a request supplies ever reaches one.
    """

    # A relation holding (grp, n_edges, trips) already computed, in place of the
    # `gstat` a grouped query would derive from this window. Only banding sets it,
    # and it is the whole reason a band draws the same picture as the serial render
    # rather than one that merely looks like it: `gstat` decides both how wide a
    # ribbon is and what order the ribbons are laid down in, and a band that derives
    # it from its own rows gets both from a fraction of the country. The widths would
    # be visibly wrong; the order is subtler, because SCREEN is commutative in real
    # arithmetic but rounds in eight-bit, so reordering shifted 2.8% of the pixels by
    # up to 4/255 across the whole image -- diffuse, tiny, and not a seam, which is
    # exactly the kind of difference that gets waved through.
    groups: str | None = None
    # The same arrangement for a coalesced render's chain assignment, and set by the
    # same caller for a sharper reason. Which edges share a stroke is decided by the
    # shape of the graph, and a band sees a truncated graph -- so a node just outside
    # its collar can look like a through node when it is really a fork, and joining
    # there puts two edges in one stroke that the serial render draws as two. Under
    # ADD that is not a local difference: two strokes double-count wherever they
    # overlap, which may be nowhere near the node that decided it. Handing every band
    # the parent's assignment makes chain membership a property of the window rather
    # than of the cut, which is what the collar argument needs to be true.
    chains: str | None = None


DEFAULT_SOURCE = Source()


class _Sql:
    """The query skeleton, with its holes filled from the spec.

    One builder rather than module-level f-strings, because the holes depend on each
    other: a filter decides a join type, and the group key decides which table the
    strand queries group on.
    """

    def __init__(self, spec: QuerySpec, source: Source, bbox: Sequence[int]) -> None:
        self.spec = spec
        self.source = source
        self.bbox = list(bbox)
        self.weight = WEIGHTS[spec.weight]
        # Every group key is coerced to a non-null string here rather than in GROUPS,
        # so the dict stays readable and the guarantee lives in one place. It has to
        # hold: `strands` hashes the key to pick a hue, and `way_id` is a BIGINT while
        # `road_name` is frequently null. Casting is identity for the string columns,
        # so the default grouping produces exactly the values it always did.
        self.group = f"coalesce(CAST({GROUPS[spec.group]} AS VARCHAR), 'unknown')"

    # -- fragments ---------------------------------------------------------
    def window(self, *, sampled: bool = False) -> tuple[str, list[Any]]:
        """The bbox filter, plus any edge-plane filter.

        `edges` stores each geometry's bounding box as four micro-degree integers, so
        the window test is an exact integer overlap in SQL and nothing has to look
        inside the geometry. Without those columns the predicate can only reach the
        first vertex, which over-selects by the length of the longest edge in the
        table and leaves every row to be re-tested in Python.

        There is no spatial index, so a window over `uk` reads the whole table. Four
        integer comparisons a row is a cheap way to pay that, and an edge-plane
        filter here is the one kind of customisation that makes a render *faster*.

        `sampled` adds the preview thinning, and only the queries that produce drawn
        geometry ask for it. The weight scale and the group statistics are taken over
        the whole window whatever the sample rate, because they decide colour, line
        width and draw order -- a preview whose palette differs from the render it
        previews is worse than no preview at all.
        """
        where, params = self.where(sampled=sampled)
        sql = f"""
    SELECT edge_id, way_id, road_name, road_class, length_m, lon_e6, lat_e6
    FROM {EDGES}
    WHERE {where}
"""
        return sql, params

    def where(self, *, sampled: bool = False) -> tuple[str, list[Any]]:
        """Just the predicate, for a caller selecting something other than geometry.

        Split out for `band_cuts`, which counts the window and takes quantiles of the
        stored bounding boxes. Sharing the predicate rather than restating it is what
        keeps the decision to band honest: a spec filtered to one road class draws a
        fraction of the edges, and a count that ignored the filter would start eight
        processes for a picture one core finishes in a tenth of a second.
        """
        sql = (
            "min_lon_e6 <= ? AND max_lon_e6 >= ?\n"
            "      AND min_lat_e6 <= ? AND max_lat_e6 >= ?\n"
        )
        # The bounds belong to this fragment, so it carries them. Every query puts the
        # window first, but a fragment that owns its own parameters can be moved
        # without a caller having to know where its holes went.
        params: list[Any] = list(self.bbox)
        if self.spec.road_class:
            sql += f"      AND road_class IN ({_holes(self.spec.road_class)})\n"
            params += list(self.spec.road_class)
        if sampled and self.spec.sample > 1:
            # `hash` rather than `random`, so the same window always drops the same
            # edges: a preview that redrew a different eighth on every keystroke
            # would flicker, and two runs would not be comparable.
            sql += f"      AND hash(edge_id) % {int(self.spec.sample)} = 0\n"
        return sql, params

    def services(self) -> tuple[str, list[Any]]:
        """The `edge_services` scan, aliased `s`, plus any service-plane filter.

        A filter becomes a subquery rather than an extra WHERE on the outer join, so
        the same fragment drops into all four queries unchanged. Its predicates are
        unqualified because inside the subquery the alias does not exist yet.
        """
        params: list[Any] = []
        clauses = []
        if self.spec.operator:
            clauses.append(f"agency_id IN ({_holes(self.spec.operator)})")
            params += list(self.spec.operator)
        if self.spec.service:
            clauses.append(f"short_name IN ({_holes(self.spec.service)})")
            params += list(self.spec.service)
        if not clauses:
            return f"{SERVICES} s", params
        where = " AND ".join(clauses)
        return f"(SELECT * FROM {SERVICES} WHERE {where}) s", params

    def _having(self) -> str:
        """A floor on traffic, deliberately on trips and not on the chosen weight.

        `min_trips=50` means "roads carrying at least 50 buses a week" whatever the
        picture is coloured by; thresholding the weight instead would make the same
        number mean a different thing under every `weight=`.
        """
        if not self.spec.min_trips:
            return ""
        return f"HAVING sum(s.n_trips) >= {int(self.spec.min_trips)}"

    def _join(self) -> str:
        # An inner join drops edges no surviving service uses; see QuerySpec.selective.
        return "JOIN" if self.spec.selective else "LEFT JOIN"

    def _win_svc(
        self, *, sampled: bool, name: str = "svc", extra_cols: str = ""
    ) -> tuple[str, list[Any]]:
        """`WITH win AS (...), svc AS (...)`: the window, and a weight per edge in it.

        Every query in this class opens with this pair, so it is written once. The
        alternative was six copies of it, each with its own hand-ordered parameter
        list -- and bound parameters follow *textual* order, so a copy that grew a
        hole in a different place binds the window's numbers to the filter's holes
        and either raises or, worse, filters on a longitude.

        `extra_cols` is appended to the aggregate's select list, for the one caller
        that needs more than the weight out of it. `name` renames the second CTE,
        because the grouped queries call it `edge_w` and join it a second time.
        """
        win, win_p = self.window(sampled=sampled)
        svc, svc_p = self.services()
        sql = f"""
WITH win AS ({win}
), {name} AS (
    SELECT s.edge_id, {self.weight} AS weight{extra_cols}
    FROM {svc} JOIN win USING (edge_id)
    GROUP BY s.edge_id
    {self._having()}
)"""
        return sql, win_p + svc_p

    # -- whole queries -----------------------------------------------------
    def edges_query(self, *, with_groups: bool, by_weight: bool) -> tuple[str, list[Any]]:
        # Sampled: this is the geometry that gets drawn. Each surviving edge still
        # carries its own true weight, because a weight is computed from that edge's
        # own service rows -- only how many edges there are changes.
        groups = f", list(DISTINCT {self.group}) AS groups" if with_groups else ""
        groups_col = ", svc.groups" if with_groups else ""
        # edge_id breaks the tie, so equally busy roads draw in a fixed order rather
        # than whichever way the scan happened to return them.
        #
        # Unordered, it is edge_id alone -- and that clause is not decoration. A
        # query with no ORDER BY has no defined row order, and DuckDB's parallel
        # hash join genuinely returns one that varies between runs of the same query
        # against the same file: `density` to SVG produced four distinct outputs in
        # four runs. PNG hid it, because ADD is saturating and therefore commutative
        # so the buffer is identical whatever order the strokes arrive in, and SVG
        # does not, because it records the strokes in the order they were issued.
        # This is the same failure `_order_sql` already fixes for `strands`, found
        # the same way and fixed the same way.
        #
        # Which order hardly matters -- no style whose geometry comes through here
        # draws differently for it -- so it is edge_id, matching the tiebreak above.
        # The sort is what it costs: +1.2 ms over cardiff, +10.1 ms over `uk` and
        # +9.1 ms over London on the real databases, against renders of 0.4 s to
        # 4.4 s. Reproducibility is worth 0.2%.
        order = (
            "ORDER BY coalesce(svc.weight, 0), win.edge_id"
            if by_weight
            else "ORDER BY win.edge_id"
        )
        base, params = self._win_svc(sampled=True, extra_cols=groups)
        sql = f"""{base}
SELECT win.edge_id, win.road_class, win.length_m,
       win.lon_e6 AS lon_e6, win.lat_e6 AS lat_e6,
       coalesce(svc.weight, 0) AS weight{groups_col}
FROM win {self._join()} svc USING (edge_id)
{order}
"""
        return sql, params

    def weights_query(self) -> tuple[str, list[Any]]:
        """Just the weight per edge, for the percentile pass.

        Eight bytes an edge against the hundreds its geometry costs, which is what
        lets the bounds be known before a single coordinate is read.
        """
        base, params = self._win_svc(sampled=False)
        sql = f"""{base}
SELECT coalesce(svc.weight, 0) FROM win {self._join()} svc USING (edge_id)
"""
        return sql, params

    def bounds_query(self, lo_q: float, hi_q: float) -> tuple[str, list[Any]]:
        """The two order statistics :class:`Weights` needs, without shipping the rest.

        :meth:`weights_query` is eight bytes an edge in SQL and then a Python float
        object an edge out of it, which is where the cost actually lands: over the
        `uk` window at 4.2M edges the query itself is 232ms and `.fetchall()` on it
        is 1,918ms. Only two of those numbers are ever used.

        This has to agree with :meth:`Weights.over` exactly, not approximately -- the
        same window rendered through either path must give the same picture -- so it
        reproduces that method's rank convention rather than reaching for a quantile
        aggregate. `quantile_disc` interpolates ranks differently and would shift the
        bounds slightly, which is invisible in a test and visible in a render's
        contrast. Hence `row_number()` and an explicit `floor(q * n)`: `CAST(x AS
        BIGINT)` in DuckDB rounds where Python's `int()` truncates, and on
        non-negative weights `floor` is the one that matches.

        Ties need no thought: two rows with equal weight can take either rank, and
        the value read off at a given rank is the same either way.
        """
        base, params = self._win_svc(sampled=False)
        sql = f"""{base}, w AS (
    SELECT coalesce(svc.weight, 0) AS weight
    FROM win {self._join()} svc USING (edge_id)
), ranked AS (
    SELECT weight,
           row_number() OVER (ORDER BY weight) - 1 AS rn,
           count(*) OVER () AS n
    FROM w
)
SELECT max(weight) FILTER (WHERE rn = least(n - 1, floor(? * n)::BIGINT)),
       max(weight) FILTER (WHERE rn = least(n - 1, floor(? * n)::BIGINT))
FROM ranked
"""
        # Textual order again -- the two CTEs first, then the quantiles in the SELECT.
        return sql, params + [lo_q, hi_q]

    def _grouped_base(self) -> tuple[str, list[Any]]:
        """The shared part of the two grouped queries.

        `pair` is DISTINCT because a service registered by two operators has two rows
        per edge in edge_services, and a ribbon must cover each edge once.

        `trips` deliberately sums each edge's total weight across every service, not
        the group's own -- so a minor route along a busy corridor gets a wide ribbon.
        That is what the existing renders look like; it is a property of the picture
        rather than an accident worth silently changing here.

        `pair` joins `edge_w` rather than only `win`, and that join is load-bearing.
        `edge_w` is where `min_trips` is applied, so without it a below-floor edge
        still drew as long as one of its groups survived -- `Window.edges()` and
        `Window.group_paths()` would then disagree about which network they draw,
        from an identical spec, and only the grouped styles would be wrong. It also
        skewed ribbon widths, because `gstat` summed the surviving edges and then the
        whole unfiltered set got stroked. Unfiltered the join is a no-op: `edge_w`
        already holds exactly the edges with a service row in the window.

        The stats CTE is `gstat`, not `grp`: `grp` is the group *column*, and a CTE
        sharing the name makes `JOIN ... USING (grp)` read as a self-reference.
        """
        base, params = self._win_svc(sampled=False, name="edge_w")
        svc, svc_p = self.services()
        # Substituted, not derived, when the caller has already computed it over a
        # wider window than this one -- see `Source.groups`. The identifier comes
        # from the Source and never from a request, the same rule as the two tables.
        gstat = (
            f"SELECT grp, n_edges, trips FROM {self.source.groups}"
            if self.source.groups
            else """SELECT p.grp, count(*) AS n_edges, sum(w.weight) AS trips
    FROM pair p JOIN edge_w w USING (edge_id)
    GROUP BY p.grp"""
        )
        sql = f"""{base}, pair AS (
    SELECT DISTINCT {self.group} AS grp, s.edge_id
    FROM {svc} JOIN win USING (edge_id) JOIN edge_w USING (edge_id)
), gstat AS (
    {gstat}
)
"""
        # The services fragment appears twice, in edge_w and again in pair, and bound
        # parameters follow textual order -- so its own parameters follow the shared
        # opening's a second time.
        return sql, params + svc_p

    def group_query(self) -> tuple[str, list[Any]]:
        order = _order_sql(self.spec.order, grouped=False)
        if self.source.groups:
            # Already computed; the window CTEs would be a full scan to read a
            # relation that does not depend on them.
            return (
                f"SELECT grp, n_edges, trips FROM {self.source.groups}\nORDER BY {order}\n",
                [],
            )
        base, params = self._grouped_base()
        return f"{base}\nSELECT grp, n_edges, trips FROM gstat\nORDER BY {order}\n", params

    def grouped_query(self) -> tuple[str, list[Any]]:
        """Every (group, edge) pair, ordered so a group's edges arrive together.

        An edge carrying five services appears five times, which is the point: the
        geometry streams past rather than being held in a dict of group to edge list.

        Never `list(geom ORDER BY ...)`. DuckDB cannot spill an ordered list
        aggregate -- it pins the per-group sort state, which is what killed the
        patterns stage on the London feed -- so the grouping is done by the caller in
        one pass over rows SQL has already ordered.
        """
        base, params = self._grouped_base()
        order = _order_sql(self.spec.order, grouped=True)
        # The thinning goes here rather than into the shared `win` CTE, because
        # `gstat` is built from that same CTE and decides every ribbon's width and
        # the order they are drawn in. Sampling upstream of it would make a preview
        # weight its ribbons differently from the render it stands in for; sampling
        # here drops only geometry, which is the whole intent.
        thin = (
            f"WHERE hash(win.edge_id) % {int(self.spec.sample)} = 0\n"
            if self.spec.sample > 1
            else ""
        )
        return (
            f"{base}\nSELECT p.grp, win.lon_e6, win.lat_e6\n"
            "FROM pair p JOIN win USING (edge_id) JOIN gstat USING (grp)\n"
            f"{thin}ORDER BY {order}\n",
            params,
        )

    def chain_query(self) -> tuple[str, list[Any]]:
        """Each edge in the window, and the one edge that continues it, if any.

        The successor relation, not the chains themselves -- following it is a linked
        list walk and belongs in Python, but deciding whether a node is a through node
        is a group-by over the whole window and belongs here.

        Directed, unlike `publish._chain`. An ordinary two-way street arrives as two
        edges pointing opposite ways, so at a node where two roads meet there are four
        incidences and the undirected "exactly two edges meet here" rule fires on
        nothing at all. Head to tail, the same node has one edge arriving and one
        leaving in each direction, and the two directions chain independently -- which
        is also why the doubling-back trap `publish` records cannot arise here: a
        directed chain never turns round.

        `weight` is part of the node identity because it is what the paint is a
        function of. Two edges meeting end to end but stroked at different widths are
        two different shapes, and joining them would change the picture rather than
        remove a duplicated cap from it. It is safe to key on: an edge's weight is
        aggregated over its own service rows and does not depend on the window.

        `o.eid <> e.edge_id` drops a self-loop, which is the one edge that is its own
        unique predecessor and successor.
        """
        base, params = self._win_svc(sampled=True)
        sql = f"""{base}, e AS (
    SELECT win.edge_id AS edge_id,
           coalesce(svc.weight, 0) AS weight,
           win.lon_e6[1] AS sx, win.lat_e6[1] AS sy,
           win.lon_e6[-1] AS ex, win.lat_e6[-1] AS ey
    FROM win {self._join()} svc USING (edge_id)
    WHERE len(win.lon_e6) >= 2
), ind AS (
    SELECT ex, ey, weight, count(*) AS n FROM e GROUP BY 1, 2, 3
), outd AS (
    SELECT sx, sy, weight, count(*) AS n, min(edge_id) AS eid FROM e GROUP BY 1, 2, 3
)
SELECT e.edge_id,
       coalesce(
           CASE WHEN i.n = 1 AND o.n = 1 AND o.eid <> e.edge_id THEN o.eid END, -1
       ) AS next_id
FROM e
JOIN ind i ON i.ex = e.ex AND i.ey = e.ey AND i.weight = e.weight
LEFT JOIN outd o ON o.sx = e.ex AND o.sy = e.ey AND o.weight = e.weight
ORDER BY e.edge_id
"""
        return sql, params

    def chained_query(self, *, by_weight: bool) -> tuple[str, list[Any]]:
        """The drawn geometry, ordered so one chain's edges arrive together.

        The same trick `grouped_query` uses for `strands`: SQL puts the rows in the
        order the caller wants to consume them, so only one run is ever live and the
        streaming property survives. `chain_id` and `seq` between them are unique, so
        the order is fully defined without a further tiebreak.
        """
        base, params = self._win_svc(sampled=True)
        order = "w.weight, c.chain_id, c.seq" if by_weight else "c.chain_id, c.seq"
        sql = f"""{base}, w AS (
    SELECT win.edge_id AS edge_id, win.lon_e6, win.lat_e6,
           coalesce(svc.weight, 0) AS weight
    FROM win {self._join()} svc USING (edge_id)
)
SELECT c.chain_id, w.lon_e6, w.lat_e6, w.weight
FROM w JOIN {self.source.chains or CHAIN_VIEW} c USING (edge_id)
ORDER BY {order}
"""
        return sql, params


def _order_sql(order: str, *, grouped: bool) -> str:
    """The ORDER BY for a group listing, qualified for whichever query wants it.

    Both grouped queries sort on the same two columns, but one selects them bare out
    of `gstat` and the other reaches them across a join. The group key always breaks
    the tie, so two equally broad groups draw in a fixed order rather than whichever
    way the scan returned them -- the determinism the export already depends on.

    `edge_id` is the second tiebreak, and it fixes a real bug rather than guarding
    against one. Ordering by group alone leaves the edges *within* a ribbon in
    whatever order the scan produced, which a PNG hides -- SCREEN compositing is
    commutative, so the image is identical either way -- and an SVG does not, because
    it records the strokes in the order they were issued. Two runs of `strands` to
    SVG differed in 180,365 of 293,842 bytes while the PNG was byte-identical.
    """
    col, direction = ORDERS[order]
    key = "p.grp" if grouped else "grp"
    qualified = key if col == "grp" else f"{'gstat.' if grouped else ''}{col}"
    tiebreak = f"{key}, p.edge_id" if grouped else key
    return f"{qualified} {direction}, {tiebreak}"


def _held_order(
    order: str, by_group: dict[str, list[Edge]], trips: dict[str, float]
) -> list[str]:
    """The same ordering as :func:`_order_sql`, over lists rather than over SQL.

    `Held` draws from a list and cannot ask the database for an order, but the order
    is what decides which ribbon ends up underneath -- so a spec that asks for
    `narrowest` has to get it here too. The group key breaks the tie, ascending,
    exactly as it does in SQL.
    """
    col, direction = ORDERS[order]
    if col == "grp":
        return sorted(by_group, reverse=direction == "DESC")
    sign = -1.0 if direction == "DESC" else 1.0

    def of(name: str) -> float:
        return float(len(by_group[name])) if col == "n_edges" else trips[name]

    return sorted(by_group, key=lambda n: (sign * of(n), n))


def _holes(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values)


FETCH_ROWS = 20_000


def _batches(cur: duckdb.DuckDBPyConnection) -> Iterator[Any]:
    """The result as Arrow record batches, FETCH_ROWS at a time.

    Arrow rather than `fetchmany` because of one column type. A DuckDB `INTEGER[]`
    arrives over the Python row protocol as a list object per edge holding an int
    object per vertex, and building those is most of what a render's database half
    costs -- the scan for the whole London window is 4.4ms and materialising its
    rows is 303ms. In Arrow the same column *is* a flat 32-bit child buffer plus a
    vector of offsets, which numpy adopts without copying and
    :meth:`Projection.flat` consumes directly.

    Batched rather than one table, because streaming is the property
    :class:`Window` exists for: peak memory stays one fetch, not one window.

    DuckDB renamed this method in 1.3 and kept the old spelling working, so both
    are tried rather than raising the floor in pyproject for a rename.
    """
    reader = getattr(cur, "to_arrow_reader", None) or cur.fetch_record_batch
    return iter(reader(FETCH_ROWS))


def _lists(column: Any) -> tuple[Any, Any, Any]:
    """An Arrow list column as (flat values, offsets, per-edge lengths), in numpy.

    A record batch's column is a plain array rather than a chunked one, so its
    child buffer is contiguous and no combine step is needed.
    """
    np = _require_numpy()
    offsets = np.asarray(column.offsets, dtype=np.int64)
    return np.asarray(column.values, dtype=np.int64), offsets, np.diff(offsets)


def _in_window(
    lon: Any, lat: Any, offsets: Any, lengths: Any, box: tuple[int, int, int, int]
) -> Any:
    """Per-edge mask: does this edge's own geometry overlap the window?

    `edges` stores a bounding box per edge and the SQL has already filtered on it,
    so this repeats that test against the vertices themselves. The box is the cheap
    over-approximation and the vertices are the answer: an edge whose box clips the
    window but whose geometry does not is selected by the query and must not draw.

    Vectorised with `reduceat`, which needs every start index to be in bounds even
    for a group it will not be asked about -- an empty list at the end of a batch
    would otherwise index off the end -- hence the clip. Lengths below two are
    masked out afterwards, which is where those groups go.
    """
    np = _require_numpy()
    w, s, e, n = box
    if not len(lon):
        return np.zeros(len(lengths), dtype=bool)
    starts = np.minimum(offsets[:-1], len(lon) - 1)
    return (
        (lengths >= 2)
        & (np.minimum.reduceat(lon, starts) <= e)
        & (np.maximum.reduceat(lon, starts) >= w)
        & (np.minimum.reduceat(lat, starts) <= n)
        & (np.maximum.reduceat(lat, starts) >= s)
    )


class Frame(Protocol):
    """The whole of what a style may ask a window for.

    A style is handed one of these and nothing else. Naming the four members is what
    keeps the line between paint and query where it belongs: a style that wanted the
    query spec would have to come through here, where adding a member is a decision
    rather than an attribute access.

    Two things implement it and they share no code. :class:`Window` streams from the
    database; :class:`Held` walks a list a caller already has. Inheriting the second
    from the first is what this replaces -- `Held` never ran the base class's
    constructor, so every method it did not override raised AttributeError on a field
    that was never set.
    """

    @property
    def weights(self) -> Weights:
        """The scale that turns an edge's weight into a number in [0, 1]."""

    @property
    def alpha_compensation(self) -> float:
        """What an additive style must scale its alpha by for the edges left out."""

    def paths(
        self,
        proj: Projection,
        *,
        tol: float = 0.0,
        by_weight: bool = False,
        coalesce: bool = False,
    ) -> Iterator[tuple[float, Polyline]]:
        """(weight, geometry in canvas pixels), one drawn shape at a time."""

    def group_paths(
        self, proj: Projection, *, tol: float = 0.0
    ) -> Iterator[tuple[str, float, Polyline]]:
        """The same, grouped: a group's shapes arrive together and never return."""


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
        with_groups: bool = False,
        spec: QuerySpec = DEFAULT_SPEC,
        source: Source = DEFAULT_SOURCE,
    ) -> None:
        self.bounds = bounds
        self.con = con
        self.with_groups = with_groups
        self.spec = spec
        self.sql = _Sql(spec, source, bounds.as_predicate_params())
        self._weights: Weights | None = None

    @property
    def alpha_compensation(self) -> float:
        """How much light a style owes the edges this window is not handing over.

        A sampled window hands over one edge in `spec.sample`, so a style compositing
        additively has to put the light of the edges that were dropped onto the ones
        that remain. The factor is the window's to state rather than the style's to
        work out: a style may not read the spec, and what it needs is a number to
        multiply an alpha by rather than what the spec asked for.
        """
        return float(max(1, self.spec.sample))

    @property
    def weights(self) -> Weights:
        """The window's weight scale, computed on first use and then held.

        Lazy because one of the three styles never asks. `strands` takes its widths
        and alphas from the *group* statistics inside `group_paths`, so for that
        style the scale here would be a whole extra pass over the window whose result
        is thrown away -- 94ms over London, and about three seconds over `uk` at 4.2M
        edges. Held once computed, because the other two read it per edge.
        """
        if self._weights is None:
            query, params = self.sql.bounds_query(_LO_Q, _HI_Q)
            row = self.con.execute(query, params).fetchone()
            self._weights = Weights.at(*row) if row else Weights(0.0, 0.0)
        return self._weights

    def edges(self, *, by_weight: bool = False) -> Iterator[Edge]:
        """Every edge whose bbox overlaps the window.

        `by_weight` orders quietest first in SQL, because `spectrum` needs that order
        and nothing here may hold the whole window to sort it. The weight is monotonic
        in the trip count, so ordering by one orders by the other.
        """
        query, params = self.sql.edges_query(
            with_groups=self.with_groups, by_weight=by_weight
        )
        cur = self.con.execute(query, params)
        while chunk := cur.fetchmany(FETCH_ROWS):
            for row in chunk:
                edge = _to_edge(row, with_groups=self.with_groups)
                if edge is not None and self.bounds.hits(
                    [c[0] for c in edge.coords], [c[1] for c in edge.coords]
                ):
                    yield edge

    def chain_table(self) -> Any | None:
        """Every edge in the window mapped to (chain_id, seq), or None if empty.

        Two steps and neither holds geometry. The successor relation comes out of SQL
        -- see `_Sql.chain_query`, which does the degree counting where the data is --
        and the walk that turns a linked list into runs is done here over flat lists
        of ints. About 48 bytes an edge, against the hundreds its geometry costs, and
        it is released before the drawing stream opens.

        The head of a run names it, so a chain's identity comes from the graph rather
        than from the order rows arrived in. A run that is a closed loop has no head;
        it is entered at its lowest edge id, which is the same fix `publish._chain`
        needed for the same reason.
        """
        import pyarrow

        query, params = self.sql.chain_query()
        cur = self.con.execute(query, params)
        # Renamed in DuckDB 1.3 with the old spelling kept but deprecated, so both are
        # tried -- the same accommodation `_batches` makes for the streaming reader.
        table = (getattr(cur, "to_arrow_table", None) or cur.fetch_arrow_table)()
        n = table.num_rows
        if not n:
            return None
        np = _require_numpy()
        ids = table.column("edge_id").to_numpy(zero_copy_only=False)
        nxt = table.column("next_id").to_numpy(zero_copy_only=False)
        # ids arrive sorted, so searchsorted is the edge id -> row index map and no
        # dict is built. Every next_id is an id, so every lookup lands.
        succ = np.full(n, -1, dtype=np.int64)
        has = np.nonzero(nxt >= 0)[0]
        succ[has] = np.searchsorted(ids, nxt[has])
        # At most one predecessor each: two edges sharing a successor would mean two
        # edges ending at that node, which makes it ambiguous and clears both.
        pred = np.full(n, -1, dtype=np.int64)
        pred[succ[has]] = has

        succ_l: list[int] = succ.tolist()
        ids_l: list[int] = ids.tolist()
        heads: list[int] = np.nonzero(pred < 0)[0].tolist()
        chain = [0] * n
        seq = [0] * n
        seen = [False] * n

        def walk(start: int) -> None:
            head, k, j = ids_l[start], start, 0
            while k != -1 and not seen[k]:
                seen[k] = True
                chain[k] = head
                seq[k] = j
                j += 1
                k = succ_l[k]

        for i in heads:
            walk(i)
        # Whatever is left is a cycle. Ascending order means the first unvisited row
        # of one is its lowest edge id, so where a loop is broken is a property of the
        # loop and not of the scan.
        for i in range(n):
            if not seen[i]:
                walk(i)
        return pyarrow.table(
            {
                "edge_id": pyarrow.array(ids_l, pyarrow.int64()),
                "chain_id": pyarrow.array(chain, pyarrow.int64()),
                "seq": pyarrow.array(seq, pyarrow.int32()),
            }
        )

    def paths(
        self,
        proj: Projection,
        *,
        tol: float = 0.0,
        by_weight: bool = False,
        coalesce: bool = False,
    ) -> Iterator[tuple[float, Polyline]]:
        """(this edge's weight, its geometry in canvas pixels), one edge at a time.

        The projection runs once per *fetch* rather than once per edge, so numpy sees
        a hundred thousand vertices instead of five and the vectorising pays. Nothing
        is held across chunks, so the streaming property the class exists for
        survives: peak memory is one fetch, not one window.

        A :class:`Polyline` rather than a list of points, because building those
        tuples was the largest single cost left in the database half of a render and
        every consumer of them is a loop that takes them apart again. See
        :meth:`Projection.flat` for the measurements.

        Degrees never reach the caller, which is deliberate -- building the float
        lon/lat tuples was itself a measurable share of a render, and a style that
        only strokes lines has no use for them. `edges()` remains for callers that
        do want them.

        `coalesce` hands back maximal runs of edges that meet end to end and paint
        the same, as one polyline each, instead of one polyline per edge -- see the
        coalescing section below for what that is for and what it costs.
        """
        # Resolve the scale *before* opening the stream, and never during it. A
        # DuckDB connection holds one result at a time, so a second `execute` on it
        # abandons the first -- silently, with no error and no short read to notice:
        # a 200,000-row stream interrupted after its first batch simply ends at
        # 20,000. Every caller of this method wants the scale anyway (the two styles
        # that stream flat edges are exactly the two that weight them), so warming it
        # here costs nothing and keeps the lazy pass honest for `strands`, which goes
        # through `group_paths` instead and still never asks.
        self.weights  # noqa: B018
        if coalesce:
            yield from self._chained_paths(proj, tol, by_weight)
            return
        query, params = self.sql.edges_query(with_groups=False, by_weight=by_weight)
        cur = self.con.execute(query, params)
        # The window test stays in micro-degrees, against the same integers the
        # database holds -- see `Bounds.as_wsen_e6`.
        box = self.bounds.as_wsen_e6()
        for batch in _batches(cur):
            lon, offsets, lengths = _lists(batch.column("lon_e6"))
            lat, _, _ = _lists(batch.column("lat_e6"))
            keep = _in_window(lon, lat, offsets, lengths, box)
            xs, ys = proj.flat(lon, lat)
            weights = batch.column("weight").to_pylist()
            offs = offsets.tolist()
            for i in keep.nonzero()[0].tolist():
                line = Polyline(xs, ys, range(offs[i], offs[i + 1]))
                yield float(weights[i]), line.simplified(tol)

    def _chained_paths(
        self, proj: Projection, tol: float, by_weight: bool
    ) -> Iterator[tuple[float, Polyline]]:
        """:meth:`paths`, with runs of edges handed back as one polyline each.

        Each edge is simplified *before* it is concatenated, rather than the finished
        run being simplified as a whole. That is not a detail. :func:`simplify` drops
        a vertex by comparing it with the last one kept, so which vertices survive
        depends on where the run started -- and a band's run starts wherever its
        collar cut the picture, not where the serial render's did. Simplifying per
        edge keeps the decision a property of the edge, so the vertices a coalesced
        render draws are exactly the vertices an uncoalesced one draws. The only
        difference between the two pictures is the duplicated round cap at a shared
        node, which is the whole point of the exercise.

        One run is live at a time, in two lists that are handed away when it ends.
        Nothing accumulates across runs.
        """
        own = self.sql.source.chains is None
        if own:
            table = self.chain_table()
            if table is None:
                return
            self.con.register(CHAIN_VIEW, table)
        try:
            query, params = self.sql.chained_query(by_weight=by_weight)
            cur = self.con.execute(query, params)
            box = self.bounds.as_wsen_e6()
            ax: list[float] = []
            ay: list[float] = []
            live = -1  # chain id of the run being accumulated, -1 for none
            weight = 0.0
            for batch in _batches(cur):
                lon, offsets, lengths = _lists(batch.column("lon_e6"))
                lat, _, _ = _lists(batch.column("lat_e6"))
                keep = _in_window(lon, lat, offsets, lengths, box).tolist()
                xs, ys = proj.flat(lon, lat)
                chains = batch.column("chain_id").to_pylist()
                weights = batch.column("weight").to_pylist()
                offs = offsets.tolist()
                for i in range(len(keep)):
                    # An edge the window test drops leaves a hole, and the edges
                    # either side of it no longer meet. Breaking the run there is what
                    # keeps the drawn geometry the same set the flat path draws.
                    if not keep[i]:
                        if ax:
                            yield weight, Polyline(ax, ay, range(len(ax)))
                        ax, ay, live = [], [], -1
                        continue
                    idx = Polyline(xs, ys, range(offs[i], offs[i + 1])).simplified(tol).idx
                    if chains[i] != live:
                        if ax:
                            yield weight, Polyline(ax, ay, range(len(ax)))
                        ax, ay = [], []
                        live, weight = chains[i], float(weights[i])
                        skip = 0
                    else:
                        # The junction vertex is already there, from the last edge's
                        # tail. Appending it again would be a zero-length segment.
                        skip = 1
                    if isinstance(idx, range):
                        # The overwhelmingly common shape, and a C-level slice.
                        a, b = idx.start + skip, idx.stop
                        ax.extend(xs[a:b])
                        ay.extend(ys[a:b])
                    else:
                        ax.extend([xs[k] for k in idx[skip:]])
                        ay.extend([ys[k] for k in idx[skip:]])
            if ax:
                yield weight, Polyline(ax, ay, range(len(ax)))
        finally:
            if own:
                self.con.unregister(CHAIN_VIEW)

    def group_stats(self) -> list[tuple[str, int, float]]:
        """(group, edges it covers, its total traffic), widest first.

        Split out of `_group_rows` because banding needs it as data: the parent
        computes it once over the whole picture and every band is handed the same
        rows, through `Source.groups`. A band deriving it from its own edges would
        both mis-size the ribbons and reorder them.

        The listing is materialised because it is small -- hundreds of services for a
        city, thousands nationally. That assumption is the spec's to break:
        `group=way` over a city is one group per OSM way, so the count is checked
        against MAX_GROUPS here rather than discovered as a render that never ends.
        """
        query, params = self.sql.group_query()
        rows = self.con.execute(query, params).fetchall()
        if len(rows) > MAX_GROUPS:
            raise ValueError(
                f"group={self.spec.group!r} gives {len(rows)} groups in this window, "
                f"over the {MAX_GROUPS} limit. Each group is a separate composited "
                "stroke, so this would draw slowly and read as noise. Narrow the "
                "window, or group by something coarser."
            )
        return [(r[0], int(r[1]), float(r[2] or 0.0)) for r in rows]

    def group_weights(self) -> dict[str, float]:
        """Every group in this window, mapped to its normalised ribbon weight."""
        rows = self.group_stats()
        if not rows:
            return {}
        weights = Weights.over(array("d", (t for _, _, t in rows)))
        return {g: weights.of(t) for g, _, t in rows}

    def group_paths(
        self, proj: Projection, *, tol: float = 0.0
    ) -> Iterator[tuple[str, float, Polyline]]:
        """(group, its weight, one edge's geometry in canvas pixels), grouped.

        Consecutive tuples sharing a group belong to the same ribbon, widest first by
        default. The caller strokes a group's geometry as one path and moves on, so
        nothing but one group's shapes is ever live.
        """
        weight_of = self.group_weights()
        if not weight_of:
            return

        query, params = self.sql.grouped_query()
        box = self.bounds.as_wsen_e6()
        cur = self.con.execute(query, params)
        for batch in _batches(cur):
            lon, offsets, lengths = _lists(batch.column("lon_e6"))
            lat, _, _ = _lists(batch.column("lat_e6"))
            keep = _in_window(lon, lat, offsets, lengths, box)
            names = batch.column("grp").to_pylist()
            offs = offsets.tolist()
            xs, ys = proj.flat(lon, lat)
            for i in keep.nonzero()[0].tolist():
                line = Polyline(xs, ys, range(offs[i], offs[i + 1])).simplified(tol)
                yield names[i], weight_of[names[i]], line


class Held:
    """A :class:`Frame` backed by edges already in memory.

    `render(edges=...)` exists so a caller can re-render a window it already has,
    which is worth keeping for anyone tuning options against one area. It is the
    only path that still holds everything, and it is the caller's choice to.

    It cannot honour the whole of a spec: the edges it was handed were already
    weighted, grouped and filtered by whatever produced them. What it can honour is
    the draw order, which is a property of the list rather than of the query, so
    `spec.order` is read and the rest is recorded for a caller to inspect.
    """

    def __init__(self, edges: Sequence[Edge], *, spec: QuerySpec = DEFAULT_SPEC) -> None:
        self._edges = list(edges)
        self.spec = spec
        # There is no query to take a scale from, so it is taken from the edges.
        self.weights = Weights.over([e.weight for e in self._edges])

    @property
    def alpha_compensation(self) -> float:
        return float(max(1, self.spec.sample))

    def edges(self, *, by_weight: bool = False) -> Iterator[Edge]:
        if by_weight:
            return iter(sorted(self._edges, key=lambda e: (e.weight, e.edge_id)))
        return iter(self._edges)

    def paths(
        self,
        proj: Projection,
        *,
        tol: float = 0.0,
        by_weight: bool = False,
        coalesce: bool = False,
    ) -> Iterator[tuple[float, Polyline]]:
        # `coalesce` is accepted and ignored. Chaining is done by reordering the
        # stream in SQL, and there is no query here -- the caller handed over edges
        # that were already selected, weighted and grouped by something else. Held is
        # a convenience for re-rendering a window one already has, not the path any
        # measurement is taken on.
        #
        # Already in degrees and already in memory, so this projects per edge. The
        # batching that `Window` needs would buy nothing against a list, and neither
        # would sharing a flat buffer between paths -- there is nothing to share it
        # with, so each gets its own two-element one.
        for e in self.edges(by_weight=by_weight):
            pts = [proj(lon, lat) for lon, lat in e.coords]
            yield e.weight, Polyline.of(pts).simplified(tol)

    def group_paths(
        self, proj: Projection, *, tol: float = 0.0
    ) -> Iterator[tuple[str, float, Polyline]]:
        by_group: dict[str, list[Edge]] = {}
        for e in self._edges:
            for name in e.groups:
                by_group.setdefault(name, []).append(e)
        if not by_group:
            return
        trips = {n: sum(e.weight for e in es) for n, es in by_group.items()}
        weights = Weights.over(list(trips.values()))
        for name in _held_order(self.spec.order, by_group, trips):
            for e in by_group[name]:
                pts = [proj(x, y) for x, y in e.coords]
                yield name, weights.of(trips[name]), Polyline.of(pts).simplified(tol)


def _to_edge(row: tuple[Any, ...], *, with_groups: bool) -> Edge | None:
    lon_e6, lat_e6 = row[3], row[4]
    if not lon_e6 or len(lon_e6) < 2:
        return None
    return Edge(
        edge_id=int(row[0]),
        road_class=row[1],
        length_m=float(row[2] or 0.0),
        coords=[(x / 1e6, y / 1e6) for x, y in zip(lon_e6, lat_e6, strict=True)],
        weight=float(row[5]),
        groups=tuple(sorted(row[6])) if with_groups and row[6] else (),
    )


def load_edges(
    bounds: Bounds,
    *,
    with_groups: bool = False,
    spec: QuerySpec = DEFAULT_SPEC,
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
        edges = list(Window(bounds, con, with_groups=with_groups, spec=spec).edges())
    finally:
        if own:
            con.close()
    log.info("%d edges in %.1fs", len(edges), time.monotonic() - t0)
    return edges


# --- Weighting --------------------------------------------------------------


# The percentile range the scale is clamped to. Named because three places have to
# agree on them: the Python pass, the SQL that replaced it, and the reference
# implementation both are checked against.
_LO_Q = 0.02
_HI_Q = 0.98


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
        cls, values: Iterable[float], lo_q: float = _LO_Q, hi_q: float = _HI_Q
    ) -> Weights:
        # log1p is monotonic, so taking the percentiles of the raw values and
        # logging the two picks is the same as logging everything first -- and it
        # means the pass that finds them never has to build a second list.
        ordered = sorted(values)
        if not ordered:
            return cls(0.0, 0.0)
        lo = ordered[min(len(ordered) - 1, int(lo_q * len(ordered)))]
        hi = ordered[min(len(ordered) - 1, int(hi_q * len(ordered)))]
        return cls.at(lo, hi)

    @classmethod
    def at(cls, lo: float | None, hi: float | None) -> Weights:
        """The scale for two already-chosen bounds, from wherever they were found.

        Split out of :meth:`over` so the database can find them -- see
        `_Sql.bounds_query` -- without a second copy of the log-and-clamp. `None` is
        what an aggregate over no rows gives back, and means the same as an empty
        sequence does above.
        """
        if lo is None or hi is None:
            return cls(0.0, 0.0)
        return cls(math.log1p(max(lo, 0.0)), math.log1p(max(hi, 0.0)))

    def of(self, value: float) -> float:
        if self.hi <= self.lo:
            return 0.5
        v = math.log1p(max(value, 0.0))
        return min(1.0, max(0.0, (v - self.lo) / (self.hi - self.lo)))


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
    # Burn the data credit into the corner. Off by default, unlike the metadata
    # every render carries: this one changes the artwork, and whether a picture is
    # going somewhere that keeps a file's metadata is the caller's knowledge, not
    # this module's. See the provenance section.
    credit: bool = False
    background: RGB | None = None  # overrides the style's own ground
    hue: float = 0.56  # base hue for density, palette rotation elsewhere
    line_scale: float = 1.0
    alpha_scale: float = 1.0
    # Vertices closer than this to the last one kept are dropped. In canvas pixels,
    # so the detail retained follows the output size: the same window keeps four
    # times the vertices at 4,000px that it does at 1,000. Half a pixel is below
    # what antialiasing can show. Set to 0 to keep every vertex as stored.
    #
    # A drawing concern rather than a query one, which is why it lives here and
    # `QuerySpec.sample` does not: this changes how a line is stroked, not which
    # lines there are.
    simplify_px: float = 0.5
    # Join runs of edges that meet end to end and paint the same into one stroke, so
    # a shared node is capped once instead of twice. Off by default: it is a change
    # to the picture, and which picture is right is a judgement about what the render
    # is for. See the coalescing section. Only `density` reads it.
    coalesce: bool = False


StyleFn = Callable[["cairo.Context[cairo.Surface]", Frame, Projection, RenderOpts], None]


@dataclass(frozen=True, slots=True)
class Style:
    draw: StyleFn
    # The widest stroke this style lays down at `line_scale=1`. Required rather than
    # defaulted: only banding reads it, and only to work out how far outside a band
    # an edge can still be and paint into it -- so a style that inherited someone
    # else's number would either grow a seam or query a collar it never draws in,
    # and both are invisible until a picture is looked at closely. It is a property
    # of the ramps in `draw`, so declaring it is part of writing a style.
    #
    # There are two regimes, and `ref_px` is which one this style is in. Left None,
    # `max_line_px` is absolute pixels: the style strokes the same width whatever the
    # canvas. Set, it is pixels *at a canvas `ref_px` wide*, and the real width scales
    # with `width_px` -- which is what `density` does, so that the map and the lines
    # shrink together. A canvas-scaling style that inherited the absolute reading
    # would get a collar too wide below `ref_px` and, worse, too narrow above it.
    max_line_px: float
    background: RGB = (0.02, 0.02, 0.035)
    needs_groups: bool = False
    blurb: str = ""
    ref_px: float | None = None
    # Whether this style reads `RenderOpts.coalesce`. Declared rather than inferred so
    # a request for it against a style that ignores it says so instead of quietly
    # doing nothing -- and so the reasons the other two decline are written down in
    # one place. See the coalescing section.
    coalesces: bool = False

    def max_stroke_px(self, width_px: float, line_scale: float = 1.0) -> float:
        """The widest stroke this style can lay down on a canvas `width_px` wide."""
        w = (
            self.max_line_px
            if self.ref_px is None
            else self.max_line_px * width_px / self.ref_px
        )
        return w * line_scale


# --- Styles -----------------------------------------------------------------


def _stroke_path(ctx: cairo.Context[cairo.Surface], line: Polyline) -> None:
    """Append one polyline to the current path, vertex by vertex.

    Indexing the shared coordinate buffers rather than unpacking a tuple per vertex,
    which is the whole reason :class:`Polyline` holds indices: cairo wants two floats
    and everything between the database and here now hands it two floats.
    """
    xs, ys = line.xs, line.ys
    it = iter(line.idx)
    first = next(it, None)
    if first is None:
        return
    ctx.move_to(xs[first], ys[first])
    for i in it:
        ctx.line_to(xs[i], ys[i])


def density_halo_width(t: float) -> float:
    """The halo pass's width, in units of DENSITY_REF_PX of canvas.

    It is the widest thing `density` draws, so `STYLES["density"].max_line_px` is its
    value at t=1 and the band collar is sized off that. Named rather than inlined
    among the ramps below so the two cannot drift apart unnoticed.
    """
    return 1.5 + 8.0 * t


def draw_density(
    ctx: cairo.Context[cairo.Surface],
    window: Frame,
    proj: Projection,
    opts: RenderOpts,
) -> None:
    """One hue on a dark ground; busy corridors bloom.

    Two additive strokes -- a wide, almost invisible halo under a narrow bright
    core. ADD is commutative, so overlapping routes accumulate light exactly the
    way a long exposure does and draw order does not matter.

    Both strokes are laid down in *one* walk of the window rather than two. The
    commutativity above is exactly what licenses that: cairo's ADD saturates at
    full brightness, and saturating addition is commutative and associative, so
    halo-then-core per edge and every-halo-then-every-core give the same buffer to
    the byte. It halves the scanning, decoding and projecting a render does.

    The same commutativity is what makes this the one style with the junction
    artefact, and the one that `opts.coalesce` addresses. Every stroke gets a round
    cap at both ends, so where two edges meet the shared node is painted twice and
    ADD makes that a bright dot -- see the coalescing section.
    """
    import cairo

    ctx.set_operator(cairo.Operator.ADD)

    # (width, alpha, saturation) as functions of normalised traffic: first the
    # broad dim halo, then the narrow bright core over it. Widths are in units of
    # DENSITY_REF_PX of canvas, not in pixels -- see below.
    passes: tuple[tuple[Ramp, Ramp, Ramp], ...] = (
        (density_halo_width, lambda t: 0.012 + 0.075 * t, lambda t: 0.95),
        (
            lambda t: 0.25 + 1.8 * t**0.8,
            lambda t: 0.10 + 0.80 * t,
            lambda t: 0.90 - 0.75 * t,
        ),
    )
    # A stroke width fixed in pixels is a different picture at every canvas size:
    # the map shrinks with the canvas and the lines do not, so the same window at
    # 1,600px lays the 4,000px weight over 40% of the road length and the additive
    # passes clip to white in every town centre. That is what made the /art default
    # (1,600px) look nothing like the CLI one (4,000px), and it made a preview a
    # poor guide to the render it stands in for. Scaling with the canvas makes the
    # two the same picture at two resolutions.
    #
    # No floor: a genuinely small canvas *should* draw hairlines, the same way
    # downsampling the big render would. `line_scale` is the knob for overriding
    # any of this.
    weight_scale = opts.line_scale * opts.width_px / DENSITY_REF_PX
    # A sampled preview draws a fraction of the edges, so each survivor carries the
    # light of the ones that were dropped. Linear in the sample rate because ADD is
    # linear: n times the alpha over 1/n of the edges sums to the same brightness.
    #
    # Only until it clips. The core pass already runs at alpha 0.10 to 0.90, so
    # multiplying by 8 pins most of it at 1.0 and the light that would have gone
    # above cannot be recovered -- measured at 62% of the full render's brightness
    # rather than 100%. Widening the lines instead would close the gap and ruin the
    # thing a preview is for, since line weight is one of the knobs being judged. So
    # the preview stays a little dark, says so on the page, and is followed by the
    # real render.
    alpha_scale = opts.alpha_scale * window.alpha_compensation

    # Bound once: this is a property that may run a query on first touch, and it
    # is read once per edge.
    weights = window.weights
    for weight, pts in window.paths(proj, tol=opts.simplify_px, coalesce=opts.coalesce):
        t = weights.of(weight)
        for width_of, alpha_of, sat_of in passes:
            r, g, b = colorsys.hsv_to_rgb(opts.hue, sat_of(t), 1.0)
            ctx.set_source_rgba(r, g, b, min(1.0, alpha_of(t) * alpha_scale))
            ctx.set_line_width(width_of(t) * weight_scale)
            ctx.new_path()
            _stroke_path(ctx, pts)
            ctx.stroke()


def draw_spectrum(
    ctx: cairo.Context[cairo.Surface],
    window: Frame,
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

    This style alone never simplifies its geometry, and the reason is that here a
    vertex is not only shape. Every other style would draw the same line through
    fewer points; this one derives the *colour* from the angle between them, so
    dropping a vertex merges two bearings into their average and repaints that
    stretch of road a different hue. Measured over a million edges, half a pixel of
    tolerance moved 74% of the output bytes -- against 0.05% for `density`. Any
    future style taking colour, width or order from geometry inherits this.
    """
    import cairo

    ctx.set_operator(cairo.Operator.OVER)

    # Quietest first so the busy roads finish on top and stay legible. The ordering
    # is done in SQL rather than by sorting the window in memory -- the weight is
    # monotonic in the trip count, so ordering by one orders by the other.
    weights = window.weights
    for weight, pts in window.paths(proj, tol=0.0, by_weight=True):
        t = weights.of(weight)
        sat = 0.30 + 0.62 * t
        val = 0.52 + 0.48 * t
        alpha = min(1.0, (0.30 + 0.62 * t) * opts.alpha_scale)
        ctx.set_line_width((0.6 + 3.4 * t**0.8) * opts.line_scale)
        for x0, y0, x1, y1 in pts.segments():
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
    window: Frame,
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
    for name, weight, pts in window.group_paths(proj, tol=opts.simplify_px):
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
        _stroke_path(ctx, pts)
    finish()

    if not drew:
        log.warning("no service names on these edges; strands has nothing to draw")


# --- Coalescing ---------------------------------------------------------------
#
# `art` strokes one cairo path per directed edge, and a Valhalla directed edge is
# 4.14 coordinates over tens of metres -- so a road is dozens of short strokes laid
# end to end. Every stroke gets a round cap at both ends, and `density` composites
# with ADD, so a node two edges share is painted twice: a bright dot at every
# junction, and at every point Valhalla happened to split a road. That is an
# artefact of how the geometry is stored, not something in the timetable.
#
# `RenderOpts.coalesce` joins runs of edges that meet head to tail and paint the same
# into a single stroke, which caps the run's two ends and joins everything between.
# `publish` already does this for tiles, and this is deliberately not the same code:
# there the grouping key is the tile attributes and the chaining is undirected, here
# it is the drawn weight and the chaining follows direction. See `_Sql.chain_query`
# for why direction matters and `Window._chained_paths` for why simplification stays
# per edge.
#
# Three things are worth stating outright.
#
# **Banding still holds.** A band computes its chains over its own collar window, so
# it can chain differently from the serial render -- but only at a node outside that
# window, because any edge incident on a node *inside* it has a bounding box that
# overlaps it and is therefore selected too. `_band_pad` is half the widest stroke
# plus slack, so ink from a node outside the collar cannot reach the band's own rows.
# The two renders agree on every pixel that is kept. This is the same argument the
# existing collar rests on, applied to chaining decisions rather than to strokes, and
# it needs no wider collar than the one already there.
#
# **Directed pairs are not collapsed.** An ordinary two-way street is two coincident
# edges and `publish` drops one of them. Doing that here would halve the light on
# every two-way road, which is a different picture rather than a repaired one.
#
# **The two other styles decline, for opposite reasons.** `spectrum` strokes each
# *segment* separately to colour it by its own bearing, so it has a cap at every
# vertex rather than only at shared nodes, and nothing short of changing what it
# means by colour would remove them. `strands` already puts a whole service into one
# cairo path; cairo fills a stroke's outline once with nonzero winding, so caps that
# overlap inside a single stroke do not accumulate, and there is nothing to remove.

STYLES: dict[str, Style] = {
    "density": Style(
        draw=draw_density,
        background=(0.015, 0.018, 0.03),
        blurb="weekly trip volume as light",
        # `density_halo_width` at full traffic, quoted at DENSITY_REF_PX as it is.
        max_line_px=9.5,
        ref_px=DENSITY_REF_PX,
        coalesces=True,
    ),
    "spectrum": Style(
        draw=draw_spectrum,
        background=(0.03, 0.03, 0.04),
        blurb="hue by compass bearing",
        max_line_px=4.0,  # 0.6 + 3.4
    ),
    "strands": Style(
        draw=draw_strands,
        background=(0.04, 0.035, 0.045),
        needs_groups=True,
        blurb="one ribbon per service",
        max_line_px=3.9,  # 0.9 + 3.0
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


def _require_numpy() -> Any:
    """Also lazy, and for the same reason as :func:`_require_cairo`.

    Only :meth:`Projection.batch` needs it, so a caller reading coordinates out of
    the database never pays for the import.
    """
    try:
        import numpy
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError(
            "rendering needs numpy. Install the extra with: pip install -e '.[art]'"
        ) from exc
    return numpy


def _font(ctx: cairo.Context[cairo.Surface], size: float) -> None:
    import cairo

    ctx.select_font_face("sans-serif", cairo.FontSlant.NORMAL, cairo.FontWeight.NORMAL)
    ctx.set_font_size(size)


def _text_width(
    ctx: cairo.Context[cairo.Surface], text: str, size: float, tracking: float
) -> float:
    """How wide `text` will draw, laid out the way :func:`_line` lays it out."""
    _font(ctx, size)
    return sum(ctx.text_extents(ch).x_advance + tracking * size for ch in text)


def _line(
    ctx: cairo.Context[cairo.Surface],
    text: str,
    *,
    x: float,
    y: float,
    size: float,
    alpha: float,
    tracking: float,
) -> None:
    """One line of small, low-contrast text, drawn a glyph at a time.

    The toy text API has no letter-spacing, so the advance is done by hand -- which
    is also what lets :func:`_text_width` predict the result exactly.
    """
    import cairo

    ctx.save()
    ctx.set_operator(cairo.Operator.OVER)
    _font(ctx, size)
    ctx.set_source_rgba(1.0, 1.0, 1.0, alpha)
    for ch in text:
        ctx.move_to(x, y)
        ctx.show_text(ch)
        x += ctx.text_extents(ch).x_advance + tracking * size
    ctx.restore()


# The credit caption's size as a fraction of the canvas width. Smaller than the
# user's own caption because it is a footnote to the picture rather than a title
# for it, and because it is a sentence rather than a phrase.
CREDIT_REF_PX = 220.0
# The size it starts at on a canvas too narrow for the fraction above to give a
# readable one. A starting point and not a floor: fitting the line between the
# margins wins over it, because text running off the edge is a broken picture where
# text too small to read is only a small one.
CREDIT_MIN_PX = 6.5

# The same two for the user's own caption, which is a title rather than a footnote
# and so is drawn larger.
CAPTION_REF_PX = 130.0
CAPTION_MIN_PX = 10.0


def _captions(
    ctx: cairo.Context[cairo.Surface], proj: Projection, opts: RenderOpts
) -> None:
    """Whatever text goes in the bottom-left corner: credit lowest, caption above.

    Drawn once, in the serial parent, after every band has been pasted in -- a
    caption laid down inside :func:`_draw_band` would appear once per band, and each
    band would clip it to its own rows. It is also why the captions are the last
    thing to touch the surface: they composite with OVER, and the additive and
    screening styles would otherwise take the text as light to accumulate.

    Nothing here can perturb the picture beyond the pixels it paints. It reads the
    projection for a canvas size and nothing else -- no weight scale, no window, no
    band collar -- so a credited render draws the same map as an uncredited one.
    """
    size = max(CAPTION_MIN_PX, proj.width / CAPTION_REF_PX)
    x = size * 2.2
    y = proj.height - size * 2.2
    if opts.credit:
        # One line per thing being credited, rather than one long sentence: the
        # break falls where the meaning does, and two short lines fit a canvas that
        # one long one does not.
        lines = licences.lines(config.credit_parts(), links=False)
        c_size = max(CREDIT_MIN_PX, proj.width / CREDIT_REF_PX)
        room = proj.width - 2 * x
        widest = max(_text_width(ctx, line, c_size, 0.0) for line in lines)
        # Shrunk to fit rather than clipped, and with no floor once it comes to
        # that -- the same rule as `density`'s line widths. A thumbnail should look
        # like the render reduced, and text that keeps its point size while the
        # canvas halves is the same mistake as a stroke width that does. Below a few
        # hundred pixels the credit is a grey mark rather than a readable line; the
        # metadata is what carries the obligation at that size.
        if widest > room > 0:
            c_size *= room / widest
        for line in reversed(lines):
            _line(ctx, line, x=x, y=y, size=c_size, alpha=0.45, tracking=0.0)
            y -= c_size * 1.5
        # A gap before the user's own caption, so the two read as separate things.
        y -= c_size * 0.8
    if opts.caption:
        _line(ctx, opts.caption.upper(), x=x, y=y, size=size, alpha=0.40, tracking=0.22)


# --- Provenance -------------------------------------------------------------
#
# Every render carries its credit whether or not anyone asked for one: an image
# served over HTTP leaves this machine, and a picture drawn from timetables under
# attribution and ODbL road geometry that says so nowhere is an uncredited
# derivative work. The timetable's licence varies by region -- OGL v3.0 for BODS
# and Translink, CC BY 4.0 for the Republic's NTA feed -- so the text comes from
# `config.credit_text()` rather than being named here.
# Metadata costs nothing, cannot alter the picture, and needs no flag. The visible
# caption above does alter the picture, so that one is opt-in.
#
# Nothing here may vary between two renders of the same window: no timestamp, no
# hostname, no output path, no version. A render is tested byte for byte, and a
# field that moves would break that for every window rather than for the one it
# was added for. This is why there is no `Creation Time` and why `Software` is the
# bare name -- a version string would be correct, and it would also make every
# stored render's bytes a function of the release that drew it.

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Dublin Core is what an SVG `<metadata>` block conventionally carries, so each PNG
# keyword is mapped to its element rather than invented twice. PNG's registered
# keywords are the keys because a decoder that shows any text at all shows those.
_DC_ELEMENTS = {
    "Title": "dc:title",
    "Description": "dc:description",
    "Software": "dc:creator",
    "Copyright": "dc:rights",
}


def _provenance(bounds: Bounds, bounds_or_name: Bounds | str, style: str) -> dict[str, str]:
    """What a finished render says about itself.

    Four fields, and the argument for each is that it is fixed by the request. The
    credit is the obligation. The style and the window are what the picture *is*,
    and a render that has been through a chat client and back is otherwise a
    picture of somewhere nobody can name -- both are arguments the caller supplied,
    so neither can move under a re-render.

    The feed version is deliberately absent, tempting though it is. It would have to
    be queried, which `render(edges=...)` has no connection to do, and it would make
    the bytes of a render a function of when the timetable was downloaded rather than
    of what was asked for. The database's provenance belongs to the database.
    """
    where = bounds_or_name if isinstance(bounds_or_name, str) else "a window"
    box = ",".join(
        f"{v:g}" for v in (bounds.min_lon, bounds.min_lat, bounds.max_lon, bounds.max_lat)
    )
    return {
        "Title": f"wayfare {style}: {where}",
        "Description": f"Bus routes on the road network, window {box}.",
        "Software": "wayfare",
        "Copyright": licences.text(config.credit_parts()),
    }


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    """Length, type, data, CRC32 of type and data. That is the whole format."""
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data))
    )


def _png_text(keyword: str, value: str) -> bytes:
    """One text chunk: `tEXt` where the value is Latin-1, `iTXt` where it is not.

    `tEXt` is the chunk every decoder reads and it is Latin-1 only, which covers the
    copyright sign and so covers the credit as it stands. An attribution with an
    accent in it is one `config.FEEDS` entry away, though, and the failure would be
    a `UnicodeEncodeError` in the middle of a render -- so the wider chunk is the
    fallback rather than the default.
    """
    try:
        return _png_chunk(
            b"tEXt", keyword.encode("latin-1") + b"\0" + value.encode("latin-1")
        )
    except UnicodeEncodeError:
        # keyword, NUL, uncompressed, method 0, empty language and translated
        # keyword, then UTF-8 text.
        head = keyword.encode("latin-1") + b"\0\0\0" + b"\0" + b"\0"
        return _png_chunk(b"iTXt", head + value.encode("utf-8"))


def _png_with(data: bytes, fields: dict[str, str]) -> bytes:
    """Splice text chunks in after IHDR, which is where a reader expects them.

    pycairo writes no metadata of its own and this is not worth a dependency for:
    a PNG is a signature and a run of chunks, and the only thing to get right is the
    CRC.
    """
    if not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        raise ValueError("cairo did not write a PNG this can annotate")
    end = 8 + 12 + struct.unpack(">I", data[8:12])[0]
    return data[:end] + b"".join(_png_text(k, v) for k, v in fields.items()) + data[end:]


def _svg_with(data: bytes, fields: dict[str, str]) -> bytes:
    """Insert an RDF `<metadata>` block directly after the opening `<svg>` tag.

    Also a post-process, and for the same reason: cairo has no way to write one.
    """
    open_tag = data.find(b"<svg")
    end = data.find(b">", open_tag) + 1
    if open_tag < 0 or end <= 0:
        raise ValueError("cairo did not write an SVG this can annotate")
    body = "".join(
        f"\n   <{_DC_ELEMENTS[k]}>{escape(v)}</{_DC_ELEMENTS[k]}>"
        for k, v in fields.items()
    )
    block = (
        '\n<metadata>\n <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"'
        '\n          xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'\n  <rdf:Description rdf:about="">{body}\n  </rdf:Description>'
        "\n </rdf:RDF>\n</metadata>"
    )
    return data[:end] + block.encode("utf-8") + data[end:]


def _stamped(data: bytes, fmt: str, fields: dict[str, str]) -> bytes:
    return _png_with(data, fields) if fmt == ".png" else _svg_with(data, fields)


FORMATS = (".png", ".svg")


def _fmt(path_or_suffix: Path | str) -> str:
    """The output format, from a path to write or from a bare suffix.

    Both, because the two entry points hold different things: `render` has a filename
    and `render_bytes` has `.png` or `.svg` on its own. A bare suffix has no suffix of
    its own -- `Path(".png").suffix` is empty, since a leading dot names a hidden file
    -- so an empty one means the text was already the answer.
    """
    text = str(path_or_suffix)
    suffix = (Path(text).suffix or text).lower()
    if suffix not in FORMATS:
        raise ValueError(f"unsupported output format {suffix!r}; use .png or .svg")
    return suffix


# Where a finished render goes: a filesystem path, or a buffer for a caller that
# wants the bytes and never the file. cairo takes either interchangeably, which is
# what lets the HTTP endpoint reuse the whole drawing path unchanged.
Sink = Path | BinaryIO


def _surface(
    fmt: str, buf: BinaryIO, w: int, h: int, scale: float
) -> tuple[cairo.Surface, float]:
    """Surface plus the factor drawing should be scaled by.

    SVG is resolution independent, so `scale` is ignored there and the surface is
    sized in points; PNG gets a bigger pixel buffer and a matching context scale,
    which keeps every line width in the styles meaning the same physical thickness.
    """
    import cairo

    if fmt == ".svg":
        # SVG writes as it draws, so the surface owns the buffer from the start.
        return cairo.SVGSurface(buf, w, h), 1.0
    return (
        cairo.ImageSurface(
            cairo.FORMAT_ARGB32, max(1, round(w * scale)), max(1, round(h * scale))
        ),
        scale,
    )


def _canvas(
    surface: Any, draw_scale: float, sty: Style, opts: RenderOpts, dev_origin: int = 0
) -> Any:
    """A context on `surface`, scaled for print and filled with the ground colour.

    `dev_origin` is the device row the surface's first row stands for, which is how a
    band draws its slice of a taller picture in the whole picture's coordinates; zero
    for a whole canvas. The shift is in device space and the print scale is applied
    after it, so `y_device = y_user * scale - origin` and a style still draws in the
    logical units it was written in.

    Typed loosely because a style takes a `Context[Surface]` and cairo's stubs make
    that invariant in the surface type.
    """
    import cairo

    ctx: Any = cairo.Context(surface)
    ctx.translate(0, -dev_origin)
    ctx.scale(draw_scale, draw_scale)
    ctx.set_antialias(cairo.Antialias.BEST)
    ctx.set_line_cap(cairo.LineCap.ROUND)
    ctx.set_line_join(cairo.LineJoin.ROUND)
    r, g, b = opts.background or sty.background
    ctx.set_source_rgb(r, g, b)
    ctx.paint()
    return ctx


def _default_path(bounds_or_name: Bounds | str, style: str) -> Path:
    stem = bounds_or_name if isinstance(bounds_or_name, str) else "custom"
    return config.OUT / f"{stem}-{style}.png"


# --- Banding ----------------------------------------------------------------
#
# A render is CPU-bound cairo on one core, and the box it runs on has eight. The
# canvas splits into horizontal bands, one process each, and the bands are pasted
# back together. Nothing about the picture changes: the bands are disjoint, each is
# clipped to its own rows, so no pixel is painted twice and no compositing operator
# has to be commutative across a cut. Measured byte-identical to the serial render
# on all three styles over the `uk` window.
#
# Two things have to be global rather than per band, and both are scales rather than
# geometry: `Weights` and, for a grouped style, the ribbon weights. A band that took
# its contrast from its own edges would be brighter over the Highlands than over the
# Midlands, and the join would be visible as a step.


def default_workers(workers: int | None = None) -> int:
    """How many bands to draw at once, when a caller has not said.

    Every *physical* core, because the thing being parallelised is the only thing
    running: the render server takes one render at a time by design, and a
    command-line render is what the operator is sitting waiting for.
    `WAYFARE_RENDER_WORKERS` overrides, for a box where that is not true.

    Physical rather than logical, which is measured rather than assumed. On the
    four-core, eight-thread Xeon that serves this, `uk` `density` at 2,000px takes
    26.9s on four workers, 27.2s on six and 28.1s on eight: the second thread of a
    core buys nothing, because tessellating round caps is ALU- and branch-bound and
    there are no memory stalls for it to fill. Eight processes also carry eight
    interpreters and eight DuckDB connections against the render container's memory
    limit, which is the part that actually bites.
    """
    if workers is not None:
        return max(1, workers)
    env = os.environ.get("WAYFARE_RENDER_WORKERS")
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            log.warning("ignoring WAYFARE_RENDER_WORKERS=%r, which is not a number", env)
    logical = os.cpu_count() or 1
    return max(1, min(_physical_cpus() or logical, _cgroup_cpus() or 1_000))


def _physical_cpus() -> int | None:
    """Cores rather than hardware threads, or None if that cannot be established.

    Linux only, and deliberately: it reads the distinct `core_id`/`physical_id`
    pairs out of `/proc/cpuinfo`, which is where the render actually runs. Anywhere
    else there is no reliable physical count, so this returns None and the logical
    count stands -- over-counting costs a few percent, and guessing wrong in the
    other direction would leave half the box idle.
    """
    try:
        text = Path("/proc/cpuinfo").read_text()
    except OSError:
        return None
    cores: set[tuple[str, str]] = set()
    physical = core = None
    for line in text.splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key == "physical id":
            physical = value
        elif key == "core id":
            core = value
        elif not line.strip() and physical is not None and core is not None:
            cores.add((physical, core))
            physical = core = None
    if physical is not None and core is not None:
        cores.add((physical, core))
    return len(cores) or None


def _cgroup_cpus() -> int | None:
    """The container's CPU quota, whole cores, or None outside a limited cgroup.

    `os.cpu_count()` reports the host's cores from inside a container, so the render
    service -- which runs at `cpus: 4` on an eight-core box -- would otherwise start
    eight band processes to share four cores' worth of quota and four gigabytes of
    memory limit. Overcommitting CPU only wastes context switches; overcommitting the
    memory limit gets the container killed.
    """
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
    except (OSError, ValueError):
        return None
    if quota == "max":
        return None
    try:
        return max(1, int(float(quota) / float(period)))
    except (ValueError, ZeroDivisionError):
        return None


def _lat_at(proj: Projection, y_px: float) -> float:
    """Inverse of :meth:`Projection.__call__`'s y, in degrees. Bands cut in pixels."""
    my = proj.y1 - (y_px - proj.oy) / proj.k
    return math.degrees(2.0 * math.atan(math.exp(my)) - math.pi / 2.0)


def _band_window(
    bounds: Bounds, proj: Projection, y0: float, y1: float, pad: float
) -> Bounds:
    """The window a band has to query: its own rows, plus a collar, clamped.

    The collar is what stops a seam. An edge whose geometry sits just outside the
    band still strokes into it, by up to half a line width, so the query has to
    reach that far past the cut.

    Clamped back to `bounds` because the collar must not *add* edges the serial
    render would not have drawn. Serial selects every edge whose bbox overlaps the
    window; an unclamped collar on the outermost band would select edges beyond it,
    and for a grouped style those bring groups the global weight map has never seen.
    Clamping costs nothing, because anything outside the window is clipped away.
    """
    north = min(_lat_at(proj, y0 - pad), bounds.max_lat)
    south = max(_lat_at(proj, y1 + pad), bounds.min_lat)
    # A band can be thinner than the clamp when the window is tiny; keep it legal.
    if north <= south:
        north = min(bounds.max_lat, south + 1e-9)
    return Bounds(bounds.min_lon, south, bounds.max_lon, north)


def band_cuts(
    con: duckdb.DuckDBPyConnection,
    sql: _Sql,
    bounds: Bounds,
    proj: Projection,
    height: float,
    n: int,
) -> tuple[int, list[float]]:
    """How many edges the window holds, and band boundaries splitting them evenly.

    The count comes back with the cuts because it is the same scan and it is what
    decides whether to band at all -- see MIN_BAND_EDGES.

    Equal-height bands do not work here and it is not a close call. Great Britain's
    buses are not spread evenly in latitude: cutting the `uk` window into eight equal
    strips put 1,307,069 of 2,746,261 edges into one of them, so seven cores finished
    in seconds and the render waited on the eighth for 35. Balancing on the edge
    distribution instead took the same render from 37s to 27s.

    Latitude quantiles rather than a count per band, because the cost is per edge and
    the quantiles are one cheap aggregate over a column the zonemaps already prune.
    The cuts land on whole device rows so the paste is a memcpy of exact rows.
    """
    where, params = sql.where(sampled=True)
    row = con.execute(
        "SELECT count(*), quantile_cont((min_lat_e6 + max_lat_e6) / 2.0, ?) "
        f"FROM {EDGES} WHERE {where}",
        [[i / n for i in range(1, n)], *params],
    ).fetchone()
    n_edges = int(row[0]) if row else 0
    cuts = [0.0]
    # North to south, because y grows downward and the bands are listed top first.
    for lat_e6 in sorted(row[1] if row and row[1] else [], reverse=True):
        y = float(round(proj(bounds.min_lon, lat_e6 / 1e6)[1]))
        if cuts[-1] < y < height:
            cuts.append(y)
    cuts.append(float(height))
    return n_edges, cuts


@dataclass(frozen=True, slots=True)
class _BandJob:
    """Everything a worker needs, and nothing it cannot pickle.

    Notably not a connection: DuckDB handles do not cross a process boundary, so a
    band opens the file read-only itself and closes it when it is done. That keeps
    the rule the render server depends on -- no handle outlives a render -- rather
    than working around it.
    """

    db_path: str
    bounds: tuple[float, float, float, float]
    width: int
    height: int
    dev_y0: int
    dev_y1: int
    draw_scale: float
    style: str
    opts: RenderOpts
    query: QuerySpec
    source: Source
    weights: Weights
    group_stats: list[tuple[str, int, float]] | None
    # The parent's chain assignment, as an Arrow table, or None when not coalescing.
    # About 20 bytes an edge and picklable, which is what lets it cross to a worker;
    # see `Source.chains` for why a band must not work one out for itself.
    chains: Any | None


def band_source(con: duckdb.DuckDBPyConnection) -> Path | None:
    """The file a band process should reopen, or None if it cannot.

    Asked of the connection rather than assumed from the config, because they are not
    always the same file: a caller can hand `render` a connection to any database, and
    a band that opened `config.DB_PATH` instead would draw a different picture from the
    one it was asked for -- quietly, and only in the parallel path.

    None means do not band. That covers an in-memory database, which a worker has no
    way to reach, and a file this process cannot open a second time read-only, which
    is what a *writable* handle looks like: DuckDB gives a writer an exclusive lock.
    The probe is an open and a close, so it tests the thing that has to work rather
    than reasoning about it.
    """
    try:
        row = con.execute(
            "SELECT path FROM duckdb_databases() WHERE NOT internal ORDER BY database_oid"
        ).fetchone()
    except Exception:  # an older DuckDB without the view; not worth a version check
        return None
    if not row or not row[0]:
        return None
    path = Path(row[0])
    try:
        db.connect(path, read_only=True).close()
    except Exception as exc:
        log.debug("not banding: %s cannot be reopened read-only (%s)", path, exc)
        return None
    return path


def _stats_table(rows: list[tuple[str, int, float]]) -> Any:
    """The parent's group statistics as an Arrow table, ready to `register`."""
    import pyarrow

    return pyarrow.table(
        {
            "grp": pyarrow.array([r[0] for r in rows], pyarrow.string()),
            "n_edges": pyarrow.array([r[1] for r in rows], pyarrow.int64()),
            "trips": pyarrow.array([r[2] for r in rows], pyarrow.float64()),
        }
    )


def _band_pad(sty: Style, opts: RenderOpts, width_px: float) -> float:
    """How far outside its own rows a band must draw and query, in logical pixels.

    Half the widest stroke, because a stroke is centred on its path, plus two pixels
    of slack for the round caps and joins cairo adds past a vertex. The slack is
    absolute, so it is proportionally thinner the wider the strokes get; it is a
    margin on the arithmetic, not the arithmetic.
    """
    return sty.max_stroke_px(width_px, opts.line_scale) / 2.0 + 2.0


def _draw_band(job: _BandJob) -> tuple[int, int, int, bytes]:
    """One band, drawn into its own surface and handed back as raw ARGB rows."""
    import cairo

    bounds = Bounds(*job.bounds)
    proj = Projection.fit(bounds, job.width, job.height)
    sty = STYLES[job.style]
    s = job.draw_scale

    # The surface is the band plus a margin, and the margin is thrown away. That is
    # what makes a band byte-identical rather than merely indistinguishable: clipping
    # to the band would cut a stroke in half at the boundary, and cairo tessellates in
    # 24.8 fixed point, so the two halves' coverage does not always re-add to what the
    # whole shape rasterised to. It showed up as one row of one Cardiff render off by
    # 1/255. Drawing past the cut and pasting only the middle means no shape is ever
    # split, and the clip that remains is exactly the serial path's.
    #
    # `max_stroke_px` takes the canvas width because a style may quote its widths
    # against a reference canvas rather than in absolute pixels; a collar read as
    # absolute pixels under a style that scales with `width_px` is too narrow above
    # that style's reference canvas, which is a seam, and merely wasteful below it.
    pad = _band_pad(sty, job.opts, job.width)
    dev_pad = math.ceil(pad * s)
    surface = cairo.ImageSurface(
        cairo.FORMAT_ARGB32,
        max(1, round(job.width * s)),
        (job.dev_y1 - job.dev_y0) + 2 * dev_pad,
    )
    ctx = _canvas(surface, s, sty, job.opts, dev_origin=job.dev_y0 - dev_pad)

    ctx.save()
    # Clip to the window and nothing else -- the same rectangle `_render` uses, so an
    # edge in the collar is kept out of the letterbox here too. The band's own extent
    # is not a clip; it is which rows get returned.
    ctx.rectangle(*proj.content_rect(bounds))
    ctx.clip()
    top, bottom = job.dev_y0 / s, job.dev_y1 / s
    con = db.connect(Path(job.db_path), read_only=True)
    try:
        # One thread each. DuckDB defaults to a thread per core *per process*, so
        # eight bands would put sixty-four of them on eight cores; the scan was
        # never the bottleneck here and the contention is real.
        con.execute("SET threads=1")
        source = job.source
        if job.group_stats is not None:
            # Registered rather than inserted: at most MAX_GROUPS rows, and
            # DuckDB takes about 2,700 a second through bound parameters, so
            # 20,000 services would cost seven seconds a band. `register` hands
            # it an Arrow table and costs nothing. The name is ours, so
            # `Source.groups` still only ever holds an identifier this code chose.
            con.register("wf_gstat", _stats_table(job.group_stats))
            source = replace(source, groups="wf_gstat")
        if job.chains is not None:
            con.register(CHAIN_VIEW, job.chains)
            source = replace(source, chains=CHAIN_VIEW)
        window = Window(
            _band_window(bounds, proj, top, bottom, pad),
            con,
            with_groups=sty.needs_groups,
            spec=job.query,
            source=source,
        )
        # The window's scale, injected rather than recomputed. See the section
        # header: a band that scales itself draws a different picture.
        window._weights = job.weights
        sty.draw(ctx, window, proj, job.opts)
    finally:
        con.close()
    ctx.restore()

    surface.flush()
    stride = surface.get_stride()
    rows = job.dev_y1 - job.dev_y0
    data = bytes(surface.get_data())
    return job.dev_y0, rows, stride, data[dev_pad * stride : (dev_pad + rows) * stride]


def _draw_banded(
    surface: Any,
    bounds: Bounds,
    proj: Projection,
    style: str,
    opts: RenderOpts,
    draw_scale: float,
    con: duckdb.DuckDBPyConnection,
    query: QuerySpec,
    workers: int,
) -> bool:
    """Fill `surface` from `workers` processes, one band each; False if it declined.

    Declining rather than raising, because `workers` is a request for speed and a
    small window is simply faster without it. The caller then draws serially, which
    is the only other thing it could sensibly do.

    One band per worker rather than several, because the per-band cost has a floor
    that does not shrink as bands do: `edge_services` carries no bbox column and
    DuckDB pushes no min/max filter through the join, so every band scans all of it
    whatever its height. Twenty-four balanced bands measured *slower* than eight
    (36.7s against 27.0s) for exactly that reason. Balancing the cuts is what buys
    the parallelism; multiplying them only buys more scans.
    """
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    db_path = band_source(con)
    if db_path is None:
        return False
    sty = STYLES[style]
    window = Window(bounds, con, with_groups=sty.needs_groups, spec=query)
    n_edges, cuts = band_cuts(
        con, window.sql, bounds, proj, surface.get_height() / draw_scale, workers
    )
    if n_edges < MIN_BAND_EDGES or len(cuts) < 3:
        return False
    # Worked out once over the whole window, exactly like the group statistics below,
    # and for the reason `Source.chains` records. Doing it per band is both four
    # times the work and the wrong answer.
    chains = window.chain_table() if opts.coalesce and sty.coalesces else None
    jobs = [
        _BandJob(
            db_path=str(db_path),
            bounds=(bounds.min_lon, bounds.min_lat, bounds.max_lon, bounds.max_lat),
            width=opts.width_px,
            height=proj.height,
            dev_y0=round(cuts[i] * draw_scale),
            dev_y1=round(cuts[i + 1] * draw_scale),
            draw_scale=draw_scale,
            style=style,
            opts=opts,
            query=query,
            source=DEFAULT_SOURCE,
            # Resolved here, on the parent's connection, precisely once.
            weights=window.weights,
            group_stats=window.group_stats() if sty.needs_groups else None,
            chains=chains,
        )
        for i in range(len(cuts) - 1)
    ]

    surface.flush()
    dst = surface.get_data()
    dst_stride = surface.get_stride()
    # Spawn, not the Linux default of fork. The parent is holding an open DuckDB
    # connection at this point -- it just read the weights off it -- and DuckDB runs
    # background threads, which a fork does not carry across. The child inherits the
    # connection's state without the threads that maintain it and dies on first use;
    # it presents as BrokenProcessPool with no traceback, because the child is killed
    # rather than raising. Spawn costs an interpreter start and a re-import per band,
    # which against a render measured in tens of seconds is not worth avoiding.
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
        for dev_y0, rows, stride, data in pool.map(_draw_band, jobs):
            for row in range(rows):
                off = (dev_y0 + row) * dst_stride
                dst[off : off + dst_stride] = data[row * stride : row * stride + dst_stride]
    surface.mark_dirty()
    return True


def _render(
    bounds_or_name: Bounds | str,
    style: str,
    fmt: str,
    sink: Sink,
    label: str,
    *,
    opts: RenderOpts | None = None,
    query: QuerySpec = DEFAULT_SPEC,
    con: duckdb.DuckDBPyConnection | None = None,
    edges: Sequence[Edge] | None = None,
    workers: int = 1,
) -> None:
    """Draw into `sink`, which is a path to write or a buffer to fill.

    `label` names the destination in the log line only. Everything else about the
    drawing is identical either way -- there is no separate in-memory code path to
    diverge from the one that writes files.

    `style` and `query` are the two halves: the style decides how an edge is painted,
    the query decides which edges there are, what their weight means, and what a
    group is. Neither knows about the other, which is what lets three styles cover
    the whole product of the two.

    `workers` splits the canvas into that many horizontal bands and draws them in
    separate processes -- see the banding section. It is a speed knob and nothing
    else: the output is byte-identical either way, which is what makes it safe to
    turn on by default rather than something a caller has to reason about.
    """
    # Argument checks come first, and before requiring cairo: a mistyped style
    # should say which styles exist, not tell the caller to install a dependency
    # they would then discover was not the problem.
    try:
        sty = STYLES[style]
    except KeyError:
        known = ", ".join(sorted(STYLES))
        raise KeyError(f"unknown style {style!r}; known styles: {known}") from None

    bounds = resolve(bounds_or_name)
    opts = opts or RenderOpts()
    if opts.coalesce and not sty.coalesces:
        # Not an error -- it is a request for a different picture that this style does
        # not have -- but silence would look like it had been honoured.
        log.warning("%s ignores coalesce; see the coalescing section", style)

    _require_cairo()
    proj = Projection.fit(bounds, opts.width_px, _canvas_height(bounds, opts))

    # cairo draws into a buffer rather than into the sink, because both formats are
    # post-processed before they land: neither PNG nor SVG metadata is something
    # pycairo can write, so the finished bytes have to pass through this process
    # once. It costs one copy of the *encoded* image, against a raster that is
    # already resident and several times larger.
    buf = io.BytesIO()
    surface, draw_scale = _surface(fmt, buf, proj.width, proj.height, opts.scale)
    # Styles draw in logical units and the context is scaled up for print, so a
    # tolerance of half a logical pixel is half a *device* pixel only at 1x. Divide
    # it here, where `draw_scale` is known -- and where SVG's fixed 1.0 keeps a
    # vector output at full detail whatever `scale` was asked for.
    opts = replace(opts, simplify_px=opts.simplify_px / draw_scale)
    ctx = _canvas(surface, draw_scale, sty, opts)

    t0 = time.monotonic()
    _paint(
        surface,
        ctx,
        bounds,
        proj,
        style,
        opts,
        draw_scale,
        fmt,
        con=con,
        edges=edges,
        query=query,
        workers=workers,
    )
    # Last, and in this process whether or not the map was drawn in others: text
    # composites with OVER, and the additive and screening styles would take it as
    # light to accumulate.
    _captions(ctx, proj, opts)

    _emit(surface, buf, fmt, sink, _provenance(bounds, bounds_or_name, style))
    log.info(
        "%s %dx%d %s in %.1fs -> %s",
        style,
        proj.width,
        proj.height,
        f"@{opts.scale:g}x" if opts.scale != 1.0 else "",
        time.monotonic() - t0,
        label,
    )


def _canvas_height(bounds: Bounds, opts: RenderOpts) -> int:
    """The canvas height: the caller's, or the one that fits the window exactly."""
    return opts.height_px or Projection.canvas_height(bounds, opts.width_px)


def _open_window(
    bounds: Bounds,
    sty: Style,
    query: QuerySpec,
    con: duckdb.DuckDBPyConnection | None,
    edges: Sequence[Edge] | None,
) -> Frame:
    """What the style will draw from: the database, or edges the caller holds."""
    if edges is not None:
        return Held(edges, spec=query)
    assert con is not None  # _paint opens one before it asks for a window
    return Window(bounds, con, with_groups=sty.needs_groups, spec=query)


def _paint(
    surface: Any,
    ctx: Any,
    bounds: Bounds,
    proj: Projection,
    style: str,
    opts: RenderOpts,
    draw_scale: float,
    fmt: str,
    *,
    con: duckdb.DuckDBPyConnection | None,
    edges: Sequence[Edge] | None,
    query: QuerySpec,
    workers: int,
) -> None:
    """Draw the map onto `surface`, in bands or serially, and own the connection.

    Whatever this opens, it closes -- which is the render server's whole rule: DuckDB
    gives a writer an exclusive lock, so a handle left alive by a finished render
    stops the next pipeline stage from starting. A connection the caller supplied is
    the caller's to close.

    Banding needs a file to reopen per process and a raster to paste into, so it is
    off for an SVG, for edges the caller already holds, and for a database a worker
    cannot open. Each of those falls back to drawing serially rather than failing:
    `workers` asks for speed, and speed that cannot be had is not an error.
    """
    sty = STYLES[style]
    own_con = con is None and edges is None
    if own_con:
        con = db.connect(read_only=True)
    try:
        if (
            workers > 1
            and fmt == ".png"
            and edges is None
            and con is not None
            and _draw_banded(
                surface, bounds, proj, style, opts, draw_scale, con, query, workers
            )
        ):
            return
        window = _open_window(bounds, sty, query, con, edges)
        ctx.save()
        # Clip to the window rather than the frame: the query returns a collar of
        # edges just outside the bounds, and without this they bleed into the
        # letterbox.
        ctx.rectangle(*proj.content_rect(bounds))
        ctx.clip()
        try:
            sty.draw(ctx, window, proj, opts)
        finally:
            ctx.restore()
    finally:
        if own_con and con is not None:
            con.close()


def _emit(
    surface: Any, buf: io.BytesIO, fmt: str, sink: Sink, fields: dict[str, str]
) -> None:
    """Finish the surface, stamp the provenance in, and hand the bytes to the sink."""
    if fmt == ".png":
        surface.write_to_png(buf)
    # Flushes the SVG writer as well, so the buffer holds a complete document by the
    # time this returns.
    surface.finish()
    data = _stamped(buf.getvalue(), fmt, fields)
    if isinstance(sink, Path):
        sink.write_bytes(data)
    else:
        sink.write(data)


def render(
    bounds_or_name: Bounds | str,
    style: str = "density",
    out_path: str | Path | None = None,
    *,
    opts: RenderOpts | None = None,
    query: QuerySpec = DEFAULT_SPEC,
    con: duckdb.DuckDBPyConnection | None = None,
    edges: Sequence[Edge] | None = None,
    workers: int | None = None,
) -> Path:
    """Draw `bounds_or_name` in `style` and return the path written.

    `out_path` decides the format by suffix (.png or .svg) and defaults to
    ``OUT/<area>-<style>.png``. Pass `edges` to re-render a window you already
    loaded without touching the database again.

    `workers` defaults to every core -- see :func:`default_workers`.
    """
    path = Path(out_path) if out_path else _default_path(bounds_or_name, style)
    fmt = _fmt(path)  # before the query, so a typo'd suffix fails in milliseconds
    config.ensure_dirs()
    path.parent.mkdir(parents=True, exist_ok=True)
    _render(
        bounds_or_name,
        style,
        fmt,
        path,
        str(path),
        opts=opts,
        query=query,
        con=con,
        edges=edges,
        workers=default_workers(workers),
    )
    return path


def render_bytes(
    bounds_or_name: Bounds | str,
    style: str = "density",
    *,
    fmt: str = ".png",
    opts: RenderOpts | None = None,
    query: QuerySpec = DEFAULT_SPEC,
    con: duckdb.DuckDBPyConnection | None = None,
    edges: Sequence[Edge] | None = None,
    workers: int | None = None,
) -> bytes:
    """The same render, returned rather than written.

    What the HTTP endpoint serves. Nothing on a server that answers requests should
    have to invent a filename, and an image the size of a print render has no
    business landing in the output directory on the way to a socket.
    """
    fmt = _fmt(fmt)
    buf = io.BytesIO()
    _render(
        bounds_or_name,
        style,
        fmt,
        buf,
        "memory",
        opts=opts,
        query=query,
        con=con,
        edges=edges,
        workers=default_workers(workers),
    )
    return buf.getvalue()
