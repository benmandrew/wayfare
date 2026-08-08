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
import time
from array import array
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

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
    # Whatever `QuerySpec.weight` asked for -- trips per week by default, but possibly
    # a service count or traffic per metre. Named for its role rather than its usual
    # contents, because a field called n_trips holding a count of operators is a lie.
    weight: float
    groups: tuple[str, ...] = ()  # only populated when the style draws grouped paths


# --- The query spec ---------------------------------------------------------
#
# What a style paints is one half of a render; the other half is which edges are in
# frame, what scalar drives the ramps, and what a "group" means. That second half
# used to be hard-coded -- traffic, and groups are services -- and it is the half
# that reaches pictures no paint knob can: the same three styles grouped by operator
# or filtered to one road class are genuinely different maps.
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


@dataclass(frozen=True, slots=True)
class QuerySpec:
    """Which edges, weighted how, grouped by what.

    Defaults reproduce the original hard-coded query exactly, so a render that does
    not ask for anything is byte-identical to one from before this existed.
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
            ],
            separators=(",", ":"),
        )


DEFAULT_SPEC = QuerySpec()


@dataclass(frozen=True, slots=True)
class Source:
    """Where the two tables are read from.

    Normally the database. The point of naming them is that a materialised or
    extracted window can be substituted underneath a render without touching the
    query builder, and the bbox predicate is applied either way, so correctness never
    depends on the substitute being exactly right -- only speed.

    A Parquet extract of the window was the substitution this was built for, and it
    was measured and dropped: against 4.2M edges it moved a Cardiff render from
    2347ms to 2320ms, because the scan is not where the time goes. A density render
    is 75% cairo, and the whole database scan is a quarter of the rest. Anything
    plugged in here can only ever address that quarter.
    """

    edges: str = "edges"
    services: str = "edge_services"


DEFAULT_SOURCE = Source()


class _Sql:
    """The query skeleton, with its holes filled from the spec.

    One builder rather than module-level f-strings, because the holes are no longer
    independent: a filter decides a join type, and the group key decides which table
    the strand queries group on.
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
    def window(self) -> tuple[str, list[Any]]:
        """The bbox filter, plus any edge-plane filter.

        `edges` stores each geometry's bounding box as four micro-degree integers, so
        the window test is an exact integer overlap in SQL and nothing has to look
        inside the geometry. That is worth stating because it used not to be: when
        geom was WKT text there was no numeric column to compare against, and the
        filter matched the *first* vertex only, over-selected a collar of edges
        padded by the longest edge in the table, and re-tested each one in Python.

        There is no spatial index, so a window over `uk` reads the whole table. Four
        integer comparisons a row is a cheap way to pay that, and an edge-plane
        filter here is the one kind of customisation that makes a render *faster*.
        """
        sql = f"""
    SELECT edge_id, way_id, road_name, road_class, length_m, lon_e6, lat_e6
    FROM {self.source.edges}
    WHERE min_lon_e6 <= ? AND max_lon_e6 >= ?
      AND min_lat_e6 <= ? AND max_lat_e6 >= ?
"""
        # The bounds belong to this fragment, so it carries them. Every query puts the
        # window first, but a fragment that owns its own parameters can be moved
        # without a caller having to know where its holes went.
        params: list[Any] = list(self.bbox)
        if self.spec.road_class:
            sql += f"      AND road_class IN ({_holes(self.spec.road_class)})\n"
            params += list(self.spec.road_class)
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
            return f"{self.source.services} s", params
        where = " AND ".join(clauses)
        return f"(SELECT * FROM {self.source.services} WHERE {where}) s", params

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

    # -- whole queries -----------------------------------------------------
    def edges_query(self, *, with_groups: bool, by_weight: bool) -> tuple[str, list[Any]]:
        win, win_p = self.window()
        svc, svc_p = self.services()
        groups = f", list(DISTINCT {self.group}) AS groups" if with_groups else ""
        groups_col = ", svc.groups" if with_groups else ""
        # edge_id breaks the tie, so equally busy roads draw in a fixed order rather
        # than whichever way the scan happened to return them.
        order = "ORDER BY coalesce(svc.weight, 0), win.edge_id" if by_weight else ""
        sql = f"""
WITH win AS ({win}
), svc AS (
    SELECT s.edge_id,
           count(DISTINCT s.short_name) AS n_services,
           {self.weight} AS weight
           {groups}
    FROM {svc} JOIN win USING (edge_id)
    GROUP BY s.edge_id
    {self._having()}
)
SELECT win.edge_id, win.road_class, win.length_m, win.lon_e6, win.lat_e6,
       coalesce(svc.n_services, 0), coalesce(svc.weight, 0){groups_col}
FROM win {self._join()} svc USING (edge_id)
{order}
"""
        return sql, win_p + svc_p

    def weights_query(self) -> tuple[str, list[Any]]:
        """Just the weight per edge, for the percentile pass.

        Eight bytes an edge against the hundreds its geometry costs, which is what
        lets the bounds be known before a single coordinate is read.
        """
        win, win_p = self.window()
        svc, svc_p = self.services()
        sql = f"""
WITH win AS ({win}
), svc AS (
    SELECT s.edge_id, {self.weight} AS weight
    FROM {svc} JOIN win USING (edge_id)
    GROUP BY s.edge_id
    {self._having()}
)
SELECT coalesce(svc.weight, 0) FROM win {self._join()} svc USING (edge_id)
"""
        return sql, win_p + svc_p

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
        `Window.groups()` would then disagree about which network they were drawing,
        from an identical spec, and only the grouped styles would be wrong. It also
        skewed ribbon widths, because `gstat` summed the surviving edges and then the
        whole unfiltered set got stroked. Unfiltered the join is a no-op: `edge_w`
        already holds exactly the edges with a service row in the window.

        The stats CTE is `gstat`, not `grp`: `grp` is the group *column*, and a CTE
        sharing the name makes `JOIN ... USING (grp)` read as a self-reference.
        """
        win, win_p = self.window()
        svc, svc_p = self.services()
        sql = f"""
WITH win AS ({win}
), edge_w AS (
    SELECT s.edge_id, {self.weight} AS weight
    FROM {svc} JOIN win USING (edge_id)
    GROUP BY s.edge_id
    {self._having()}
), pair AS (
    SELECT DISTINCT {self.group} AS grp, s.edge_id
    FROM {svc} JOIN win USING (edge_id) JOIN edge_w USING (edge_id)
), gstat AS (
    SELECT p.grp, count(*) AS n_edges, sum(w.weight) AS trips
    FROM pair p JOIN edge_w w USING (edge_id)
    GROUP BY p.grp
)
"""
        # The window CTE is declared once; the services fragment appears twice, in
        # edge_w and again in pair. Bound parameters follow textual order, so this
        # list has to match that exactly -- one win, then two svc.
        return sql, win_p + svc_p + svc_p

    def group_query(self) -> tuple[str, list[Any]]:
        base, params = self._grouped_base()
        order = _order_sql(self.spec.order, grouped=False)
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
        return (
            f"{base}\nSELECT p.grp, win.lon_e6, win.lat_e6\n"
            "FROM pair p JOIN win USING (edge_id) JOIN gstat USING (grp)\n"
            f"ORDER BY {order}\n",
            params,
        )

    def cardinality_query(self) -> tuple[str, list[Any]]:
        """How many groups this spec would produce, before anything is drawn."""
        win, win_p = self.window()
        svc, svc_p = self.services()
        return (
            f"WITH win AS ({win})\n"
            f"SELECT count(DISTINCT {self.group}) FROM {svc} JOIN win USING (edge_id)",
            win_p + svc_p,
        )


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


def _holes(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values)


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
        with_groups: bool = False,
        spec: QuerySpec = DEFAULT_SPEC,
        source: Source = DEFAULT_SOURCE,
    ) -> None:
        self.bounds = bounds
        self.con = con
        self.with_groups = with_groups
        self.spec = spec
        self.sql = _Sql(
            spec,
            source,
            [
                round(bounds.max_lon * 1e6),
                round(bounds.min_lon * 1e6),
                round(bounds.max_lat * 1e6),
                round(bounds.min_lat * 1e6),
            ],
        )
        query, params = self.sql.weights_query()
        # "d" rather than "q": a weight is not necessarily an integer any more --
        # `density` divides by length. Still eight bytes an edge either way.
        self.weights = Weights.over(
            array("d", (r[0] for r in con.execute(query, params).fetchall()))
        )

    def edges(self, *, by_weight: bool = False) -> Iterator[Edge]:
        """Every edge whose bbox overlaps the window.

        `by_weight` orders quietest first in SQL, which is what `spectrum` used to
        get by sorting the whole list in memory. The weight is monotonic in the trip
        count, so ordering by one orders by the other.
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

    def groups(self) -> Iterator[tuple[str, float, list[tuple[float, float]]]]:
        """(group, its weight, one edge's coordinates), grouped.

        Consecutive tuples sharing a group belong to the same ribbon, widest first by
        default. The caller strokes a group's geometry as one path and moves on.

        The group listing is materialised because it is small -- hundreds of services
        for a city, thousands nationally. That assumption is the spec's to break:
        `group=way` over a city is one group per OSM way, so the count is checked
        against MAX_GROUPS here rather than discovered as a render that never ends.
        """
        query, params = self.sql.group_query()
        rows = self.con.execute(query, params).fetchall()
        if not rows:
            return
        if len(rows) > MAX_GROUPS:
            raise ValueError(
                f"group={self.spec.group!r} gives {len(rows)} groups in this window, "
                f"over the {MAX_GROUPS} limit. Each group is a separate composited "
                "stroke, so this would draw slowly and read as noise. Narrow the "
                "window, or group by something coarser."
            )
        weights = Weights.over(array("d", (float(r[2] or 0.0) for r in rows)))
        weight_of = {r[0]: weights.of(float(r[2] or 0.0)) for r in rows}

        query, params = self.sql.grouped_query()
        cur = self.con.execute(query, params)
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

    It cannot honour a spec: the edges it was handed were already weighted, grouped
    and filtered by whatever produced them. `spec` is recorded so a caller can see
    which one that was, and ignored otherwise.
    """

    def __init__(self, edges: Sequence[Edge], *, spec: QuerySpec = DEFAULT_SPEC) -> None:
        self._edges = list(edges)
        self.spec = spec
        self.weights = Weights.over([e.weight for e in self._edges])

    def edges(self, *, by_weight: bool = False) -> Iterator[Edge]:
        if by_weight:
            return iter(sorted(self._edges, key=lambda e: (e.weight, e.edge_id)))
        return iter(self._edges)

    def groups(self) -> Iterator[tuple[str, float, list[tuple[float, float]]]]:
        by_group: dict[str, list[Edge]] = {}
        for e in self._edges:
            for name in e.groups:
                by_group.setdefault(name, []).append(e)
        if not by_group:
            return
        trips = {n: sum(e.weight for e in es) for n, es in by_group.items()}
        weights = Weights.over(list(trips.values()))
        for name in sorted(by_group, key=lambda n: (-len(by_group[n]), n)):
            for e in by_group[name]:
                yield name, weights.of(trips[name]), e.coords


def _to_edge(row: tuple[Any, ...], *, with_groups: bool) -> Edge | None:
    lon_e6, lat_e6 = row[3], row[4]
    if not lon_e6 or len(lon_e6) < 2:
        return None
    return Edge(
        edge_id=int(row[0]),
        road_class=row[1],
        length_m=float(row[2] or 0.0),
        coords=[(x / 1e6, y / 1e6) for x, y in zip(lon_e6, lat_e6, strict=True)],
        n_services=int(row[5]),
        weight=float(row[6]),
        groups=tuple(sorted(row[7])) if with_groups and row[7] else (),
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
    needs_groups: bool = False
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
            t = window.weights.of(e.weight)
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
        t = window.weights.of(e.weight)
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
    for name, weight, coords in window.groups():
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
        needs_groups=True,
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


FORMATS = (".png", ".svg")


def _fmt(path: Path | str) -> str:
    suffix = (Path(path).suffix if isinstance(path, Path) else path).lower()
    if suffix not in FORMATS:
        raise ValueError(f"unsupported output format {suffix!r}; use .png or .svg")
    return suffix


# Where a finished render goes: a filesystem path, or a buffer for a caller that
# wants the bytes and never the file. cairo takes either interchangeably, which is
# what lets the HTTP endpoint reuse the whole drawing path unchanged.
Sink = Path | BinaryIO


def _surface(
    fmt: str, sink: Sink, w: int, h: int, scale: float
) -> tuple[cairo.Surface, float]:
    """Surface plus the factor drawing should be scaled by.

    SVG is resolution independent, so `scale` is ignored there and the surface is
    sized in points; PNG gets a bigger pixel buffer and a matching context scale,
    which keeps every line width in the styles meaning the same physical thickness.
    """
    import cairo

    if fmt == ".svg":
        # SVG writes as it draws, so the surface owns the sink from the start.
        return cairo.SVGSurface(str(sink) if isinstance(sink, Path) else sink, w, h), 1.0
    return (
        cairo.ImageSurface(
            cairo.FORMAT_ARGB32, max(1, round(w * scale)), max(1, round(h * scale))
        ),
        scale,
    )


def _default_path(bounds_or_name: Bounds | str, style: str) -> Path:
    stem = bounds_or_name if isinstance(bounds_or_name, str) else "custom"
    return config.OUT / f"{stem}-{style}.png"


def _render(
    bounds_or_name: Bounds | str,
    style: str,
    fmt: str,
    sink: Sink,
    label: str,
    *,
    opts: RenderOpts | None = None,
    query: QuerySpec = DEFAULT_SPEC,
    source: Source = DEFAULT_SOURCE,
    con: duckdb.DuckDBPyConnection | None = None,
    edges: Sequence[Edge] | None = None,
) -> None:
    """Draw into `sink`, which is a path to write or a buffer to fill.

    `label` names the destination in the log line only. Everything else about the
    drawing is identical either way -- there is no separate in-memory code path to
    diverge from the one that writes files.

    `style` and `query` are the two halves: the style decides how an edge is painted,
    the query decides which edges there are, what their weight means, and what a
    group is. Neither knows about the other, which is what lets three styles cover
    the whole product of the two.
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

    cairo = _require_cairo()

    # Streamed from the database unless the caller handed over edges it already
    # holds; either way the styles see the same interface.
    own_con = con is None and edges is None
    if edges is not None:
        window: Window = Held(edges, spec=query)
    else:
        con = con or db.connect(read_only=True)
        window = Window(
            bounds, con, with_groups=sty.needs_groups, spec=query, source=source
        )

    width = opts.width_px
    height = opts.height_px or Projection.canvas_height(bounds, width)
    proj = Projection.fit(bounds, width, height)

    surface, draw_scale = _surface(fmt, sink, width, height, opts.scale)
    ctx = cairo.Context(surface)
    ctx.scale(draw_scale, draw_scale)
    ctx.set_antialias(cairo.Antialias.BEST)
    ctx.set_line_cap(cairo.LineCap.ROUND)
    ctx.set_line_join(cairo.LineJoin.ROUND)

    r, g, b = opts.background or sty.background
    ctx.set_source_rgb(r, g, b)
    ctx.paint()

    ctx.save()
    # Clip to the window rather than the frame: the query returns a collar of edges
    # just outside the bounds, and without this they bleed into the letterbox.
    ctx.rectangle(*proj.content_rect(bounds))
    ctx.clip()
    t0 = time.monotonic()
    try:
        sty.draw(ctx, window, proj, opts)
    finally:
        if own_con and con is not None:
            con.close()
    ctx.restore()

    if opts.caption:
        _caption(ctx, opts.caption, proj)

    if fmt == ".png":
        surface.write_to_png(str(sink) if isinstance(sink, Path) else sink)
    # Flushes the SVG writer as well, so a buffer holds a complete document by the
    # time this returns.
    surface.finish()
    log.info(
        "%s %dx%d %s in %.1fs -> %s",
        style,
        width,
        height,
        f"@{opts.scale:g}x" if opts.scale != 1.0 else "",
        time.monotonic() - t0,
        label,
    )


def render(
    bounds_or_name: Bounds | str,
    style: str = "density",
    out_path: str | Path | None = None,
    *,
    opts: RenderOpts | None = None,
    query: QuerySpec = DEFAULT_SPEC,
    source: Source = DEFAULT_SOURCE,
    con: duckdb.DuckDBPyConnection | None = None,
    edges: Sequence[Edge] | None = None,
) -> Path:
    """Draw `bounds_or_name` in `style` and return the path written.

    `out_path` decides the format by suffix (.png or .svg) and defaults to
    ``OUT/<area>-<style>.png``. Pass `edges` to re-render a window you already
    loaded without touching the database again.
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
        source=source,
        con=con,
        edges=edges,
    )
    return path


def render_bytes(
    bounds_or_name: Bounds | str,
    style: str = "density",
    *,
    fmt: str = ".png",
    opts: RenderOpts | None = None,
    query: QuerySpec = DEFAULT_SPEC,
    source: Source = DEFAULT_SOURCE,
    con: duckdb.DuckDBPyConnection | None = None,
    edges: Sequence[Edge] | None = None,
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
        source=source,
        con=con,
        edges=edges,
    )
    return buf.getvalue()
