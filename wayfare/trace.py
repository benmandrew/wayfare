"""Draw the non-road patterns that ship no geometry, from OSM route relations.

`match` is the road path and `aggregate.build_segments` is the operator-trace path.
This is the third, and it exists because a large mode falls through both: the
Underground, the DLR, London Trams, West Midlands Metro, Blackpool and the Air-Rail
Link all publish a complete stop sequence and no ``shape_id`` at all. They are not
matchable -- there is no road under a tube tunnel -- and there is nothing in
`shapes` to copy. Nationally that is 1,417 of 1,525 metro patterns and every one of
the 71 DLR patterns.

The shape of the stage is `match`'s, deliberately, because the constraints are the
same ones:

* **`trace_status` is a permanent cache, and failures are recorded rather than
  retried.** Work is selected by the *absence* of a row, so a relation that does not
  resolve is written down once. A tracer that re-fetched every unresolvable pattern
  every run would never finish, which needs "failed" to mean "impossible" --
  `transport_error` is the one retryable status, for a request that never arrived.
* **Bad geometry is worse than missing geometry.** A relation whose ways do not
  chain, or whose stops project out of order along the chain, is refused rather than
  drawn. Both failures produce a confident-looking line down track the service does
  not use -- the second is what a loop branch does -- and neither is visible in the
  picture afterwards.

What it does *not* do is snap anything. A route relation's ``role=""`` members are
already in route order and already join end to end, so a pattern's geometry is a cut
of its line's chain between the first and last stop it calls at. No shortest path,
no Markov model, nothing to disambiguate. `docs/pipeline.md` has the reasoning.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from . import config, db, logs, osm, osmroutes

log = logs.get("trace")

TRANSPORT_ERROR = "transport_error"

# The one status it is safe to clear unattended, and the alias that names it. Same
# vocabulary as `match --retry transient`, because it means the same thing: nothing
# was ever learned about these patterns.
_RETRY_ALIASES = {"transient": (TRANSPORT_ERROR,)}

# How much wider than the patterns' own extent to ask Overpass for, in degrees. A
# relation is only returned if it intersects the window, and a line whose stops sit
# just inside the box still runs out of it -- the Central line reaches Epping, well
# past anything a London window would be drawn around. 0.2 degrees is about 22 km.
_BBOX_PAD_DEG = 0.2

# Results are committed this often, for the reason `config.CHECKPOINT_EVERY` exists.
_CHECKPOINT_EVERY = 500


@dataclass
class Pattern:
    pattern_id: int
    mode: str | None
    short_name: str | None
    names: list[str] = field(default_factory=list)  # normalised, in calling order
    # Every spelling of each stop the other publisher might have used. Only the
    # pattern side carries alternates: OSM writes one name per node, and it is
    # the timetable that qualifies a station by its line -- see `osm.spellings`.
    spellings: list[frozenset[str]] = field(default_factory=list)
    points: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class Outcome:
    pattern_id: int
    status: str
    relation_id: int | None = None
    osm_route: str | None = None
    n_ways: int = 0
    n_stops: int = 0
    worst_stop_m: float = 0.0
    length_m: float = 0.0
    detail: str | None = None
    way_ids: list[int] = field(default_factory=list)
    geom: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class Candidate:
    """One relation, prepared once and tested against many patterns."""

    relation: osm.Relation
    latlon: list[tuple[float, float]]
    metres: list[tuple[float, float]]
    cum: list[float]
    way_ids: list[int]
    # Which way each point of the chain came from, parallel to `latlon`. `way_ids`
    # is the whole line and this is where each way sits along it, which is what
    # turns a cut expressed in metres back into the ways underneath it.
    way_at: list[int]
    names: list[str]  # normalised stop names, in relation order


@dataclass
class Prepared:
    """The fetched relations, chained: the usable ones and what the rest served.

    The second half is only there so a failure can name itself. A relation that does
    not chain is dropped before any pattern is fitted, and without remembering which
    stations it called at, every pattern on a broken line would be filed as though
    nothing were mapped.
    """

    candidates: list[Candidate]
    broken_names: set[str] = field(default_factory=set)


# -- work selection ---------------------------------------------------------


def _pending_sql() -> str:
    """The patterns this stage owes an answer for.

    Three conditions and each is load-bearing. Live, because a departed pattern is
    work spent on a journey nobody runs. Not matchable, because a bus belongs to
    Valhalla and a road relation is not what this draws. And no ``shape_id``,
    because where the operator recorded the course themselves that recording is
    better than anything reassembled from OSM -- it is a survey of where the vehicle
    goes rather than of where the track is.
    """
    return f"""
        FROM patterns p
        WHERE {db.current_feed()}
          AND NOT {db.matchable()}
          AND p.shape_id IS NULL
          AND NOT EXISTS (SELECT 1 FROM trace_status t WHERE t.pattern_id = p.pattern_id)
    """


def pending_count(con: duckdb.DuckDBPyConnection) -> int:
    return int(db.scalar(con, f"SELECT count(*) {_pending_sql()}"))


def bbox(con: duckdb.DuckDBPyConnection) -> tuple[float, float, float, float] | None:
    """The window to ask Overpass for: every pending pattern's stops, padded.

    Computed from the work rather than from the region, because the region is a feed
    slug and this needs a box. A national run asks for the country; a run against
    one city asks for that city, which is the difference between a minute of Overpass
    and most of an hour.

    Clipped to the British Isles for the reason `osmroutes.bbox` is. This window has
    never met a continental stop -- it spans the pending non-road patterns, which are
    urban rail -- but nothing about the construction stops it, and the mode selection
    is the only thing standing in the way.
    """
    row = db.row(
        con,
        f"""
        SELECT min(s.lat), min(s.lon), max(s.lat), max(s.lon)
        FROM pattern_stops ps
        JOIN stops s USING (stop_id)
        WHERE ps.pattern_id IN (SELECT p.pattern_id {_pending_sql()})
          AND {config.british_isles_sql("s.lat", "s.lon")}
        """,
    )
    if row is None or row[0] is None:
        return None
    south, west, north, east = (float(v) for v in row)
    return (
        south - _BBOX_PAD_DEG,
        west - _BBOX_PAD_DEG,
        north + _BBOX_PAD_DEG,
        east + _BBOX_PAD_DEG,
    )


def load_pending(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Pattern]:
    """Every pending pattern with its stops, busiest first.

    Busiest first for `match`'s reason: a run cut short leaves the part of the
    network carrying the most service drawn, rather than a random hole.
    """
    rows = con.execute(
        f"""
        SELECT p.pattern_id, p.mode, p.short_name
        {_pending_sql()}
        ORDER BY p.n_trips DESC, p.pattern_id
        {"LIMIT ?" if limit else ""}
        """,
        [limit] if limit else [],
    ).fetchall()
    if not rows:
        return []

    ids = [r[0] for r in rows]
    stops = con.execute(
        """
        SELECT ps.pattern_id, s.name, s.lat, s.lon
        FROM pattern_stops ps
        JOIN stops s USING (stop_id)
        WHERE ps.pattern_id IN (SELECT unnest(?))
        ORDER BY ps.pattern_id, ps.seq
        """,
        [ids],
    ).fetchall()

    by_id: dict[int, Pattern] = {
        pid: Pattern(pid, mode, short_name) for pid, mode, short_name in rows
    }
    for pattern_id, name, lat, lon in stops:
        p = by_id.get(pattern_id)
        if p is not None and lat is not None and lon is not None:
            p.names.append(osm.normalise(name))
            p.spellings.append(osm.spellings(name))
            p.points.append((float(lat), float(lon)))
    return [by_id[pid] for pid in ids]


def retry(con: duckdb.DuckDBPyConnection, statuses: list[str]) -> int:
    """Forget outcomes with these statuses so the next run redoes them.

    Call this before tracing starts, never during. Work is selected by the absence
    of a status row, exactly as in `match`, so a row deleted while its pattern is
    being worked on is handed out twice.
    """
    wanted: list[str] = []
    for s in statuses:
        wanted.extend(_RETRY_ALIASES.get(s, (s,)))
    ids = [
        r[0]
        for r in con.execute(
            "SELECT pattern_id FROM trace_status WHERE status IN (SELECT unnest(?))",
            [wanted],
        ).fetchall()
    ]
    if not ids:
        return 0
    con.execute("DELETE FROM traces WHERE pattern_id IN (SELECT unnest(?))", [ids])
    con.execute("DELETE FROM trace_status WHERE pattern_id IN (SELECT unnest(?))", [ids])
    log.info("cleared %d outcomes with status in %s", len(ids), wanted)
    return len(ids)


# -- preparing the relations -------------------------------------------------


def prepare(relations: list[osm.Relation]) -> Prepared:
    """Chain each relation once, and keep only the ones that chain cleanly.

    Done once for the whole run rather than per pattern, because 1,417 Underground
    patterns are cuts of eleven lines: the chaining and the projection setup are paid
    for per *line*, and each pattern then costs a subsequence search and a handful of
    projections.
    """
    out: list[Candidate] = []
    broken_names: set[str] = set()
    broken = 0
    for rel in relations:
        ch = osm.chain(rel)
        names = [osm.normalise(s.name) for s in rel.stops]
        if ch.breaks or len(ch.points) < 2 or len(rel.stops) < 2:
            broken += ch.breaks > 0
            # Which stations the unusable relations serve, so that a pattern with no
            # candidate left can say *why* -- "the line is mapped and its chain is
            # broken" and "nothing here is mapped at all" are different problems with
            # different fixes, and collapsing both to `no_relation` hides the first.
            if ch.breaks:
                broken_names.update(n for n in names if n)
            continue
        ref_lat = ch.points[0][0]
        metres = osm.to_metres(ch.points, ref_lat)
        out.append(
            Candidate(
                relation=rel,
                latlon=ch.points,
                metres=metres,
                cum=osm.cumulative(metres),
                way_ids=ch.way_ids,
                way_at=ch.way_at,
                names=names,
            )
        )
    log.info(
        "%d of %d relations chain cleanly (%d broke)", len(out), len(relations), broken
    )
    return Prepared(candidates=out, broken_names=broken_names)


def index_by_name(candidates: list[Candidate]) -> dict[str, list[int]]:
    """Normalised stop name to the candidates carrying it.

    A pattern is tested only against relations that call at its first stop, which
    takes the search from every relation in the country to a handful.
    """
    out: dict[str, list[int]] = {}
    for i, c in enumerate(candidates):
        for name in set(c.names):
            if name:
                out.setdefault(name, []).append(i)
    return out


# -- resolving one pattern ---------------------------------------------------


# How specific a near-miss is, most informative first. A pattern is tested against
# several relations and fails each for its own reason; the one worth writing down is
# the one that got furthest, because that is the one naming the actual obstacle.
# `no_relation` is last: it says only that nothing was tried.
_REASON_RANK = ("not_monotonic", "off_track", "no_sequence", "chain_break", "no_relation")

# Two of those are diagnoses of the same recorded status. Getting a station's name to
# line up and then finding it 3 km from the track is a name collision between lines;
# getting the sequence wrong is a different line entirely. Both are `no_stop_match` in
# the table, and the distinction survives in `detail`.
_STATUS_OF_REASON = {
    "not_monotonic": "not_monotonic",
    "off_track": "no_stop_match",
    "no_sequence": "no_stop_match",
    "chain_break": "chain_break",
    "no_relation": "no_relation",
}

_DETAIL_OF_REASON = {
    "not_monotonic": "its stops run out of order along the relation's chain",
    "off_track": "a matched station sits too far from the relation's track",
    "no_sequence": "a relation shares a terminus but not the stop sequence",
    "chain_break": "the relation serving it does not chain into one path",
    "no_relation": "no relation calls at either of its ends",
}


def resolve(p: Pattern, prepared: Prepared, index: dict[str, list[int]]) -> Outcome:
    """Find this pattern's line, cut its chain to the pattern's stops."""
    if len(p.names) < 2:
        return Outcome(p.pattern_id, "skipped", detail="fewer than two stops")
    if not p.names[0]:
        return Outcome(p.pattern_id, "no_stop_match", detail="first stop has no name")

    ends = p.spellings[0] | p.spellings[-1] if p.spellings else {p.names[0], p.names[-1]}
    seen: set[int] = set()
    for spelling in ends:
        seen.update(index.get(spelling, []))

    best: Outcome | None = None
    reasons: set[str] = set()
    for i in sorted(seen):
        got = _fit(p, prepared.candidates[i])
        if isinstance(got, str):
            reasons.add(got)
            continue
        # Deterministic ranking, and a tiebreak that is unique. The tightest stop fit
        # wins; a relation that draws the same stops in less track wins the tie,
        # because the longer one is going round something; the relation id settles the
        # rest so that two runs over one Overpass body cannot disagree.
        key = (got.worst_stop_m, got.length_m, got.relation_id or 0)
        if best is None or key < (best.worst_stop_m, best.length_m, best.relation_id or 0):
            best = got
    if best is not None:
        return best

    # A line that is mapped and does not chain is a different problem from one nobody
    # has mapped, and only this tells them apart -- the broken relation was dropped
    # before any pattern reached it.
    if ends & prepared.broken_names:
        reasons.add("chain_break")
    reason = next((r for r in _REASON_RANK if r in reasons), "no_relation")
    return Outcome(
        p.pattern_id, _STATUS_OF_REASON[reason], detail=_DETAIL_OF_REASON[reason]
    )


def _fit(p: Pattern, c: Candidate) -> Outcome | str:
    """This pattern against one relation, or the reason it is not that line.

    A pattern is a contiguous run of its line's calling points -- a short working of
    the Northern line still runs on Northern line track -- so the test is a
    subsequence search, and a relation is per direction so the reverse is tried too.

    The reason is returned rather than a bare failure because it is the only thing
    that separates "this relation is some other line" from "this is the right line and
    it loops". Both end as no geometry; only one of them is worth going and looking at.
    """
    worst = "no_sequence"
    needle = p.spellings or [frozenset({n}) for n in p.names]
    for names, stops, reverse in (
        (c.names, c.relation.stops, False),
        (c.names[::-1], c.relation.stops[::-1], True),
    ):
        for at in _occurrences(names, needle):
            matched = list(stops[at : at + len(p.names)])
            out = _cut(p, c, matched, reverse)
            if not isinstance(out, str):
                return out
            if _REASON_RANK.index(out) < _REASON_RANK.index(worst):
                worst = out
    return worst


def _occurrences(haystack: list[str], needle: list[frozenset[str]]) -> list[int]:
    """Every index where the pattern's stop names run contiguously through a line's.

    Every, not the first. A relation that calls at a station twice -- the New
    Addington loop does -- offers more than one placement, and only one of them
    projects in order along the track. Returning the first would refuse the pattern
    on a placement it never had to use.

    Each element of the needle is the set of spellings that stop may go by, and a
    position matches when the relation's single name is any of them.
    """
    n, m = len(haystack), len(needle)
    if m == 0 or m > n or not all(any(s) for s in needle):
        return []
    return [
        i for i in range(n - m + 1) if all(haystack[i + k] in needle[k] for k in range(m))
    ]


def _cut(p: Pattern, c: Candidate, matched: list[osm.Stop], reverse: bool) -> Outcome | str:
    """Project the matched stops onto the chain and slice between the ends.

    The relation's *own* stop nodes are what get projected, not the timetable's
    coordinates. The node is on the track by construction where the feed's point is
    a station entrance -- Highbury & Islington's is 216 m away, on the National Rail
    side -- and projecting the further of the two risks landing on a parallel line.
    The feed's coordinate is used to check the name join instead.
    """
    ref_lat = c.latlon[0][0]
    node_m = osm.to_metres([(s.lat, s.lon) for s in matched], ref_lat)
    along: list[float] = []
    for pt in node_m:
        d, off = osm.project(c.metres, c.cum, pt)
        # A stop node well off its own relation's track means the name matched
        # something on another line that happens to share the name.
        if off > config.TRACE_STOP_MAX_M:
            return "off_track"
        along.append(d)

    # The stops must run *one way* along the chain, and either way will do: a
    # relation is per direction, so half the patterns on a line project from its far
    # end back towards its first way. What is refused is a sequence that turns round
    # partway, which is a loop or a placement that doubles back -- there the slice
    # between the two ends takes the wrong branch and draws confident track the
    # service never runs on.
    slack = config.TRACE_MONOTONIC_SLACK_M
    steps = list(zip(along, along[1:], strict=False))
    forward = all(b >= a - slack for a, b in steps)
    backward = all(a >= b - slack for a, b in steps)
    if not (forward or backward):
        return "not_monotonic"

    worst = 0.0
    for stop, point in zip(matched, p.points, strict=False):
        gap = _haversine_m((stop.lat, stop.lon), point)
        worst = max(worst, gap)
    if worst > config.TRACE_STOP_MAX_M:
        return "off_track"

    geom = osm.slice_between(c.latlon, c.cum, along[0], along[-1])
    if len(geom) < 2:
        return "off_track"
    # The ways under the cut, not the ways of the line. Recording the whole chain
    # documented what was drawn and no more, and it was enough while nothing read
    # the column -- but inverted into `track_services` it says a Northern line
    # short working from Edgware to Kennington runs over every way of the Northern
    # line, which is a confident lie about half of it. `slice_between` answers the
    # same question in geometry; this is the same cut, in identity.
    ways = osm.ways_between(c.way_at, c.cum, along[0], along[-1])
    return Outcome(
        pattern_id=p.pattern_id,
        status="ok",
        relation_id=c.relation.relation_id,
        osm_route=c.relation.route,
        n_ways=len(ways),
        n_stops=len(matched),
        worst_stop_m=worst,
        length_m=abs(along[-1] - along[0]),
        detail=f"{c.relation.name or ''}{' reversed' if reverse else ''}".strip() or None,
        way_ids=ways,
        geom=geom,
    )


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    from math import asin, cos, radians, sin, sqrt

    lat1, lon1 = radians(a[0]), radians(a[1])
    lat2, lon2 = radians(b[0]), radians(b[1])
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * 6_371_000.0 * asin(sqrt(h))


# -- the run ----------------------------------------------------------------


def run(
    con: duckdb.DuckDBPyConnection,
    *,
    relations: list[osm.Relation] | None = None,
    cache: Path | None = None,
    refresh: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    """Trace every pending pattern. Safe to interrupt and re-run."""
    pending = pending_count(con)
    if not pending:
        log.info("nothing to trace")
        return {}
    log.info("%d patterns have no geometry and no road under them", pending)

    if relations is None:
        window = bbox(con)
        if window is None:
            log.info("pending patterns have no stop coordinates; nothing to trace")
            return {}
        try:
            relations = osm.fetch(
                window, cache or config.RAW / "osm_relations.json", refresh=refresh
            )
        except osm.TransportError as exc:
            # Nothing was learned about any pattern, so nothing is written down.
            # A permanent row here would be a lie about every pattern at once.
            raise RuntimeError(f"could not reach Overpass: {exc}") from exc

    prepared = prepare(relations)
    index = index_by_name(prepared.candidates)
    patterns = load_pending(con, limit)
    # Only the relations that chained, because only those can be cut and so only
    # those can end up named by a trace. Keyed by id, which is how an outcome
    # refers back to one.
    by_id = {c.relation.relation_id: c.relation for c in prepared.candidates}

    t0 = time.monotonic()
    counts: dict[str, int] = {}
    batch: list[Outcome] = []
    for n, p in enumerate(patterns, 1):
        got = resolve(p, prepared, index)
        counts[got.status] = counts.get(got.status, 0) + 1
        batch.append(got)
        if len(batch) >= _CHECKPOINT_EVERY:
            write_outcomes(con, batch, by_id)
            batch = []
            log.info("%d/%d traced", n, len(patterns))
    if batch:
        write_outcomes(con, batch, by_id)
    osmroutes.prune_ways(con)

    log.info(
        "traced %d patterns in %.1fs: %s",
        len(patterns),
        time.monotonic() - t0,
        ", ".join(f"{k}={v}" for k, v in sorted(counts.items())),
    )
    return counts


def write_outcomes(
    con: duckdb.DuckDBPyConnection,
    outcomes: list[Outcome],
    relations: dict[int, osm.Relation] | None = None,
) -> None:
    """Record every outcome, and the geometry of the ones that resolved.

    One row in `trace_status` per pattern whatever happened, because that row is
    what stops the pattern being handed out again.

    The ways go in the same transaction as the traces that name them, not once at
    the end of the run. `publish.export_track_geojsonl` joins `ways` inside, so a
    trace whose ways are missing is a service that silently stops being drawn --
    and a batch is the unit of checkpointing here for the reason it is in `match`,
    which means an interrupted run has to leave a state the next one can use.
    """
    if not outcomes:
        return
    now = datetime.now(UTC)
    con.execute("BEGIN")
    try:
        con.executemany(
            """
            INSERT OR REPLACE INTO trace_status
                (pattern_id, status, relation_id, osm_route, n_ways, n_stops,
                 worst_stop_m, length_m, detail, traced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    o.pattern_id,
                    o.status,
                    o.relation_id,
                    o.osm_route,
                    o.n_ways,
                    o.n_stops,
                    o.worst_stop_m,
                    o.length_m,
                    (o.detail or "")[:400] or None,
                    now,
                )
                for o in outcomes
            ],
        )
        drawn = [o for o in outcomes if o.status == "ok" and o.geom]
        if drawn:
            con.executemany(
                """
                INSERT OR REPLACE INTO traces
                    (pattern_id, relation_id, way_ids, ways_cut, lon_e6, lat_e6)
                VALUES (?, ?, ?, TRUE, ?, ?)
                """,
                [
                    (
                        o.pattern_id,
                        o.relation_id,
                        o.way_ids,
                        [round(lon * 1e6) for _, lon in o.geom],
                        [round(lat * 1e6) for lat, _ in o.geom],
                    )
                    for o in drawn
                ],
            )
            if relations:
                used = {
                    o.relation_id: relations[o.relation_id]
                    for o in drawn
                    if o.relation_id in relations
                }
                osmroutes.write_ways(con, list(used.values()))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def summary(con: duckdb.DuckDBPyConnection) -> list[tuple[str, int, float, float]]:
    """What the run produced, per status: count, track drawn, worst stop offset."""
    return [
        (str(r[0]), int(r[1]), float(r[2] or 0.0), float(r[3] or 0.0))
        for r in con.execute(
            """
            SELECT status, count(*), sum(length_m) / 1000.0, max(worst_stop_m)
            FROM trace_status
            GROUP BY status
            ORDER BY count(*) DESC, status
            """
        ).fetchall()
    ]
