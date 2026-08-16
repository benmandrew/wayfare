"""The edges of a window, offered one at a time.

:class:`Frame` is the whole of what a style may ask for, and the two things that
implement it: :class:`Window`, which streams from the database, and :class:`Held`,
which walks a list a caller already has. The weight scale lives here too, because
it is a property of the window rather than of the paint.

Nothing here holds a whole window. What that costs in care is written down on the
methods; what it buys is a national render in a few hundred megabytes.
"""

from __future__ import annotations

import math
import sys
import time
from array import array
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from .. import db, logs
from .deps import _require_numpy
from .geometry import Bounds, Polyline, Projection
from .query import (
    CHAIN_VIEW,
    DEFAULT_SOURCE,
    DEFAULT_SPEC,
    ORDERS,
    QuerySpec,
    Source,
    _Sql,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    import duckdb

log = logs.get("art")


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


def _max_groups() -> int:
    """The cap, read through the package rather than bound at import.

    `MAX_GROUPS` is re-exported, so `art.MAX_GROUPS = n` is what a caller reaches
    for. Binding the value here at import would leave that assignment looking like
    it worked while this reader kept the original -- a cap that silently does not
    apply, which is only visible as a render that never ends.
    """
    return int(sys.modules[__package__].MAX_GROUPS)


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
        weights: Weights | None = None,
    ) -> None:
        self.bounds = bounds
        self.con = con
        self.with_groups = with_groups
        self.spec = spec
        self.sql = _Sql(spec, source, bounds.as_predicate_params())
        # Given rather than computed by a banded render: a band covers a slice of
        # the picture, so a scale worked out from its own rows is a different scale
        # per band, and the seams show. See `band`.
        self._weights: Weights | None = weights

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
        cap = _max_groups()
        if len(rows) > cap:
            raise ValueError(
                f"group={self.spec.group!r} gives {len(rows)} groups in this window, "
                f"over the {cap} limit. Each group is a separate composited "
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
