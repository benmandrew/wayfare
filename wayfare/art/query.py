"""Which edges a render draws, weighted how, grouped by what.

The spec half of a render: the three closed vocabularies, the filters, and the SQL
skeleton they fill. It knows nothing about paint, and nothing about the canvas --
it is handed a window as four micro-degree integers and hands back a query and its
parameters.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

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
        return sql, [*params, lo_q, hi_q]

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
        sql = (
            f"{base}\nSELECT p.grp, win.lon_e6, win.lat_e6\n"
            "FROM pair p JOIN win USING (edge_id) JOIN gstat USING (grp)\n"
            f"{thin}ORDER BY {order}\n"
        )
        return sql, params

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


def _holes(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values)
