"""Where a render is, and where that lands on the canvas.

The window (:class:`Bounds` and the presets that name one), the projection from
lon/lat to pixels, and the polyline a projected edge becomes. Nothing here knows
what an edge carries or how it is painted, so it is also what a caller wanting
coordinates rather than a picture can import on its own.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from .. import logs, palette
from .deps import _require_numpy

log = logs.get("art")


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
#
# From `map.toml`, because the viewer holds the same box as the one it will not let
# a reader pan outside of. The two were written out separately, in the same
# minlon,minlat,maxlon,maxlat order, each with a comment naming the other.
ISLES = Bounds(*palette.load().roam)


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

    # Unhashable on purpose, and spelled out rather than left to the default that a
    # custom `__eq__` implies. A hash agreeing with that equality would have to
    # materialise the points, which is the allocation this class exists to avoid,
    # and nothing keys a set or a dict on a path.
    __hash__ = None  # type: ignore[assignment]

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
