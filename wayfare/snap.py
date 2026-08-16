"""Give an operator's own rail shape the way ids it does not carry.

`trace` fits a pattern to a route relation by station sequence, which needs the
relation to list the pattern's calling points in order. Against the Republic's rail
that resolved 50 of 319 patterns: OpenStreetMap models an intercity line with sparse
stop members -- `Cork - Dublin` carries four, Cork Kent, Mallow, Limerick Junction and
Dublin Heuston -- while the timetable's patterns are stopping services over branches,
so a branch's calling points are a subsequence of nothing fetched.

This stage takes the other side of the same problem. The operator already published
where the train goes, so nothing has to be inferred about the route; what a shape
cannot do is say *which* way it is on, and a way id is the whole of what
`aggregate.build_track_services` inverts into shared track. So the shape is the
evidence and OpenStreetMap is only the identity: every vertex is snapped to the
nearest piece of running track, and the ordered ways under it are what gets stored.

**The threshold is wide because there is nothing near it.** Over 3,000.6 km of Irish
rail shape the covered share is 99.5% at 5 m, 99.8% at 10 m and 100.0% at 25 m and at
50 m. A survey either follows the track or is somewhere else, so `SNAP_MAX_M` is a
margin rather than a tuned knob, and a run at half or double it returns the same
answer.

**A partial cover is refused rather than trimmed**, which is the one way this stage
can lie. Attributing the half of a shape that found track and dropping the half that
did not reports a short working over a line the service runs the length of, and
nothing downstream could tell. `SNAP_MIN_COVER` refuses the pattern instead, which
leaves it drawn from its own shape exactly as before -- so a region whose track is
unmapped loses the sharing and never the line.

**Parallel track is where a nearest-vertex answer flaps.** Four tracks through a
station throat sit within a few metres of each other, and taking the nearest way at
every vertex independently hops between them, turning one line into a shredded list
of ways each carrying a fragment of the service. `_snap_one` holds the way the run is
already on, and the bound on that hold is `SNAP_HOLD_M` against the nearest way rather
than `SNAP_MAX_M` against nothing: held to the tolerance instead, a way that has
already diverged keeps the shape for another 25 m, which the first run showed as all
319 patterns reporting a worst vertex in the 20-25 m band over track that has
something within 5 m of 99.5% of it.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from math import cos, radians, sqrt
from pathlib import Path

import duckdb

from . import config, db, logs, osm

log = logs.get("snap")

_M_PER_DEG_LAT = 111_320.0

# Padding on the window handed to Overpass, in degrees. A shape runs to the edge of
# the box its own vertices fit in, and track a few metres outside it is still what
# the last vertex must snap to.
_BBOX_PAD_DEG = 0.05


@dataclass(frozen=True)
class Shaped:
    """One pattern's published geometry, with the identity it is missing."""

    pattern_id: int
    route_id: str
    lat: tuple[float, ...]
    lon: tuple[float, ...]


@dataclass(frozen=True)
class Outcome:
    pattern_id: int
    status: str
    way_ids: list[int]
    n_ways: int
    covered: float
    worst_m: float
    length_m: float
    detail: str | None = None


# -- work selection ---------------------------------------------------------


def _pending_where(con: duckdb.DuckDBPyConnection) -> str:
    """The patterns this stage owes an answer for.

    A shape is the input, so `shape_id IS NOT NULL` is a requirement here where it is
    a disqualification in `trace`. `config.TRACE_OVER_SHAPE_MODES` is reused rather
    than given a list of its own: it already names the modes whose shape is worth
    trading for way ids, and the two stages disagreeing about that would put a
    pattern in both layers.

    Work is selected by the absence of a `snap_status` row, which is `match`'s rule
    and `trace`'s. A pattern `trace` already resolved is left alone, because a
    relation fitted by stop sequence is the stronger evidence of the two and
    `traces` holds one row per pattern.

    Takes the connection because `db.matchable` does: a data root that predates
    `patterns.mode` needs the predicate to degrade rather than fail to bind.
    """
    modes = ", ".join(f"'{m}'" for m in sorted(config.TRACE_OVER_SHAPE_MODES))
    return f"""
        WHERE {db.current_feed()}
          AND NOT {db.matchable(con)}
          AND p.shape_id IS NOT NULL
          AND p.mode IN ({modes})
          AND NOT EXISTS (SELECT 1 FROM snap_status s WHERE s.pattern_id = p.pattern_id)
          AND NOT EXISTS (SELECT 1 FROM traces t WHERE t.pattern_id = p.pattern_id)
    """


def pending_count(con: duckdb.DuckDBPyConnection) -> int:
    return int(db.scalar(con, f"SELECT count(*) FROM patterns p {_pending_where(con)}"))


def load_pending(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Shaped]:
    """Pending patterns, busiest first, so an interrupted run has done the work that
    carries the most service."""
    cap = f"LIMIT {int(limit)}" if limit else ""
    rows = con.execute(f"""
        SELECT p.pattern_id, p.route_id, sh.lat_e6, sh.lon_e6
        FROM patterns p JOIN shapes sh ON sh.shape_id = p.shape_id
        {_pending_where(con)}
        ORDER BY p.n_trips DESC NULLS LAST, p.pattern_id
        {cap}
    """).fetchall()
    return [
        Shaped(
            pattern_id=int(r[0]),
            route_id=str(r[1]),
            lat=tuple(v / 1e6 for v in r[2]),
            lon=tuple(v / 1e6 for v in r[3]),
        )
        for r in rows
    ]


def bbox(
    con: duckdb.DuckDBPyConnection, region: str | None = None
) -> tuple[float, float, float, float] | None:
    """The window to ask Overpass for: the pending shapes' own extent, padded.

    Off the shapes rather than off the stops, unlike `trace`'s. A shape is the thing
    being snapped and it already runs past the stops it calls at, so a window sized
    on stops leaves the approach to a terminus with no track under it.

    Every vertex is tested against the British Isles rather than only the corners of
    the box, for the reason `config.british_isles_sql` exists: a feed that carries
    international coach puts correct coordinates in Warsaw, and one of them in the
    min/max is an Overpass query for every railway between here and Poland. A vertex
    outside the bounds is dropped from the *window* and not from the pattern -- the
    snap still runs over the whole shape, and track it cannot reach shows up as a
    partial cover, which is already refused.

    Then padded and clipped to `config.Feed.bounds` by `config.pad_and_clip`, which
    `trace.bbox` and `osmroutes.bbox` also run, and for the same reason: a border is
    not a box, so Northern Ireland's rail shapes reach Dublin and a window round them
    fetches the Republic's own track for this region to snap onto.
    """
    row = db.row(
        con,
        f"""
        SELECT min(v.lat), min(v.lon), max(v.lat), max(v.lon)
        FROM (
            SELECT unnest(sh.lat_e6) / 1e6 AS lat, unnest(sh.lon_e6) / 1e6 AS lon
            FROM patterns p JOIN shapes sh ON sh.shape_id = p.shape_id
            {_pending_where(con)}
        ) v
        WHERE {config.british_isles_sql("v.lat", "v.lon")}
        """,
    )
    if row is None or row[0] is None:
        return None
    return config.pad_and_clip(
        (float(row[0]), float(row[1]), float(row[2]), float(row[3])),
        pad=_BBOX_PAD_DEG,
        region=region,
        what="pending shapes",
    )


# -- the index over the target track -----------------------------------------


class Track:
    """Every fetched way's segments, in metres, under a uniform grid.

    A grid rather than a tree because the query is always "what is within 25 m of
    this point" over a target that is built once and asked hundreds of thousands of
    times, and a dict of cells answers that in constant time with no build cost worth
    measuring. The cell size changes how many candidates a lookup scans and nothing
    about the answer.
    """

    def __init__(self, ways: Iterable[osm.Way], ref_lat: float) -> None:
        self.scale = _M_PER_DEG_LAT * cos(radians(ref_lat))
        self.ax: list[float] = []
        self.ay: list[float] = []
        self.bx: list[float] = []
        self.by: list[float] = []
        self.way_of: list[int] = []
        self.cells: dict[tuple[int, int], list[int]] = {}
        cell = config.SNAP_GRID_M
        for w in ways:
            pts = [(lon * self.scale, lat * _M_PER_DEG_LAT) for lat, lon in w.points]
            for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
                i = len(self.way_of)
                self.ax.append(x0)
                self.ay.append(y0)
                self.bx.append(x1)
                self.by.append(y1)
                self.way_of.append(w.way_id)
                # Every cell the segment's bounding box touches, so a lookup only
                # ever has to read its own cell and the eight around it.
                for cx in range(int(min(x0, x1) // cell), int(max(x0, x1) // cell) + 1):
                    for cy in range(int(min(y0, y1) // cell), int(max(y0, y1) // cell) + 1):
                        self.cells.setdefault((cx, cy), []).append(i)

    def __len__(self) -> int:
        return len(self.way_of)

    def to_metres(self, lat: float, lon: float) -> tuple[float, float]:
        return (lon * self.scale, lat * _M_PER_DEG_LAT)

    def near(self, x: float, y: float, limit: float) -> list[tuple[float, int]]:
        """(distance, way_id) for every segment within `limit`, nearest first."""
        cell = config.SNAP_GRID_M
        cx, cy = int(x // cell), int(y // cell)
        seen: set[int] = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                seen.update(self.cells.get((cx + dx, cy + dy), ()))
        out: list[tuple[float, int]] = []
        for i in seen:
            d = _point_to_segment(x, y, self.ax[i], self.ay[i], self.bx[i], self.by[i])
            if d <= limit:
                out.append((d, self.way_of[i]))
        out.sort()
        return out


def _point_to_segment(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> float:
    dx, dy = bx - ax, by - ay
    den = dx * dx + dy * dy
    t = 0.0 if den == 0.0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / den))
    qx, qy = ax + t * dx, ay + t * dy
    return sqrt((px - qx) ** 2 + (py - qy) ** 2)


# -- the fit ------------------------------------------------------------------


def _snap_one(shape: Shaped, track: Track) -> Outcome:
    """One shape onto the track under it."""
    pts = [track.to_metres(la, lo) for la, lo in zip(shape.lat, shape.lon, strict=False)]
    if len(pts) < 2:
        return Outcome(shape.pattern_id, "too_short", [], 0, 0.0, 0.0, 0.0)

    chosen: list[int | None] = []
    worst = 0.0
    held: int | None = None
    for x, y in pts:
        cands = track.near(x, y, config.SNAP_MAX_M)
        if not cands:
            chosen.append(None)
            held = None
            continue
        # Hysteresis, bounded against the nearest rather than against the tolerance.
        # Stay on the way the run is already on while it is no more than
        # `SNAP_HOLD_M` further off than the best on offer: through a four-track
        # throat the nearest answer alternates between parallel ways and shreds one
        # line into fragments, and holding fixes that. Holding all the way out to
        # `SNAP_MAX_M` fixes it too and breaks something worse -- a way that has
        # diverged keeps the shape for another 25 m of track it does not carry.
        best = cands[0][0]
        keep = next((d for d, w in cands if w == held), None)
        if keep is None or keep > best + config.SNAP_HOLD_M:
            keep, held = cands[0][0], cands[0][1]
        chosen.append(held)
        worst = max(worst, keep)

    total = 0.0
    covered = 0.0
    for i, ((x0, y0), (x1, y1)) in enumerate(zip(pts, pts[1:], strict=False)):
        seg = sqrt((x1 - x0) ** 2 + (y1 - y0) ** 2)
        total += seg
        if chosen[i] is not None and chosen[i + 1] is not None:
            covered += seg
    frac = covered / total if total else 0.0

    ways = _ordered_unique(w for w in chosen if w is not None)
    if not ways:
        return Outcome(shape.pattern_id, "no_track", [], 0, frac, worst, total)
    if frac < config.SNAP_MIN_COVER:
        return Outcome(
            shape.pattern_id,
            "partial_cover",
            [],
            len(ways),
            frac,
            worst,
            total,
            detail=f"{100 * frac:.1f}% of {total / 1000:.1f} km found track",
        )
    return Outcome(shape.pattern_id, "ok", ways, len(ways), frac, worst, total)


def _ordered_unique(ids: Iterable[int]) -> list[int]:
    """First appearance order, which is `osm.ways_between`'s rule and for its reason:
    a line that doubles back over a way must record that way once."""
    seen: set[int] = set()
    out: list[int] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


# -- writing ------------------------------------------------------------------


def commit(
    con: duckdb.DuckDBPyConnection,
    outcomes: Sequence[Outcome],
    shapes: dict[int, Shaped],
    ways: Iterable[osm.Way],
) -> int:
    """Write the run's three tables together, or write none of them.

    The transaction is the whole point of this function existing. Work is selected by
    the absence of a `snap_status` row, so a status committed without its geometry is
    a pattern marked resolved that will never be asked about again -- and the failure
    is invisible, because the pattern simply stops being drawn. Killed between the two
    writes on a real run, that left 48 of 319 patterns `ok` with no trace, and the
    `ways` write is a third statement so 2,436 way ids referenced geometry that was
    never stored, which `publish.export_track_geojsonl` joins away without a word.

    `match` gets away with two statements because a `match_status` row and its edges
    are written per batch and a lost batch is reselected. Nothing reselects here.
    """
    con.execute("BEGIN TRANSACTION")
    try:
        write_outcomes(con, outcomes, shapes)
        n = write_ways(con, ways)
    except Exception:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")
    return n


def write_outcomes(
    con: duckdb.DuckDBPyConnection,
    outcomes: Sequence[Outcome],
    shapes: dict[int, Shaped],
) -> None:
    """Every outcome gets a `snap_status` row; only `ok` writes geometry.

    Called inside `commit`'s transaction and not on its own: on its own it is half a
    write, and the half it leaves behind is the half that stops the other from ever
    being retried.

    `traces` is written with `ways_cut` TRUE, because the ways stored are the ones
    under this pattern's own shape and nothing wider -- the same claim `trace` makes
    about a cut chain, arrived at by a different route. `relation_id` is NULL, which
    is what tells a snapped trace from a fitted one afterwards.

    The polyline kept is the operator's shape rather than the track it snapped to.
    Neither is drawn -- a `ways_cut` row is drawn per way out of `ways` -- so what
    this stores is the evidence, and the operator's own survey is the honest record
    of what was snapped.
    """
    con.executemany(
        """
        INSERT OR REPLACE INTO snap_status
            (pattern_id, status, n_ways, covered_pct, worst_m, length_m, detail, snapped_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, now())
        """,
        [
            (
                o.pattern_id,
                o.status,
                o.n_ways,
                round(100.0 * o.covered, 2),
                round(o.worst_m, 1),
                round(o.length_m, 1),
                o.detail,
            )
            for o in outcomes
        ],
    )
    good = [o for o in outcomes if o.status == "ok"]
    if not good:
        return
    con.executemany(
        """
        INSERT OR REPLACE INTO traces
            (pattern_id, relation_id, way_ids, ways_cut, lon_e6, lat_e6)
        VALUES (?, NULL, ?, TRUE, ?, ?)
        """,
        [
            (
                o.pattern_id,
                o.way_ids,
                [round(v * 1e6) for v in shapes[o.pattern_id].lon],
                [round(v * 1e6) for v in shapes[o.pattern_id].lat],
            )
            for o in good
        ],
    )


def write_ways(con: duckdb.DuckDBPyConnection, ways: Iterable[osm.Way]) -> int:
    """Store the geometry of every way something snapped onto.

    Upserts rather than clearing, because `trace` and `routes` write into this table
    too and a blanket delete would take their track out of the archive.
    `osmroutes.prune_ways` is what drops the ways no `traces` row runs over.
    """
    rows = []
    for w in ways:
        lon = [round(lo * 1e6) for _, lo in w.points]
        lat = [round(la * 1e6) for la, _ in w.points]
        rows.append((w.way_id, lon, lat, min(lon), min(lat), max(lon), max(lat)))
    if not rows:
        return 0
    con.executemany(
        """
        INSERT OR REPLACE INTO ways
            (way_id, lon_e6, lat_e6, min_lon_e6, min_lat_e6, max_lon_e6, max_lat_e6)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return len(rows)


# -- the stage ----------------------------------------------------------------


def retry(con: duckdb.DuckDBPyConnection, statuses: Sequence[str]) -> int:
    """Clear cached outcomes so the next run asks again.

    `transport_error` is never written -- a request that got no answer taught nothing
    about any pattern -- so this exists for the outcomes that are permanent until the
    code or the map moves, which is all of them.
    """
    wanted = list(statuses)
    holes = ",".join("?" * len(wanted))
    n = int(
        db.scalar(
            con, f"SELECT count(*) FROM snap_status WHERE status IN ({holes})", wanted
        )
    )
    con.execute(f"DELETE FROM snap_status WHERE status IN ({holes})", wanted)
    return n


def summary(con: duckdb.DuckDBPyConnection) -> list[tuple[str, int, float, float]]:
    """What the run produced, per status: count, shape length, mean covered share."""
    return [
        (str(r[0]), int(r[1]), float(r[2] or 0.0), float(r[3] or 0.0))
        for r in con.execute(
            """
            SELECT status, count(*), sum(length_m) / 1000.0, avg(covered_pct)
            FROM snap_status
            GROUP BY status
            ORDER BY count(*) DESC, status
            """
        ).fetchall()
    ]


def run(
    con: duckdb.DuckDBPyConnection,
    *,
    ways: Sequence[osm.Way] | None = None,
    cache: Path | None = None,
    refresh: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    """Snap every pending shaped pattern onto OSM track. Returns counts by status."""
    pending = load_pending(con, limit)
    if not pending:
        log.info("nothing to snap")
        return {}
    log.info("%d shaped patterns have no way ids", len(pending))

    if ways is None:
        window = bbox(con)
        if window is None:
            log.warning("no window to query: no pending shape sits in the British Isles")
            return {}
        ways = osm.fetch_ways(
            window, cache or config.RAW / "osm_track.json", refresh=refresh
        )
    if not ways:
        log.warning("no track fetched; nothing can be snapped")
        return {}

    ref = sum(la for s in pending for la in s.lat) / sum(len(s.lat) for s in pending)
    started = time.monotonic()
    track = Track(ways, ref)
    log.info("indexed %d track segments from %d ways", len(track), len(ways))

    shapes = {s.pattern_id: s for s in pending}
    outcomes = [_snap_one(s, track) for s in pending]
    used = {w for o in outcomes if o.status == "ok" for w in o.way_ids}
    commit(con, outcomes, shapes, [w for w in ways if w.way_id in used])

    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
    ok = [o for o in outcomes if o.status == "ok"]
    log.info(
        "snapped %d patterns in %.1fs: %s",
        len(outcomes),
        time.monotonic() - started,
        " ".join(f"{k}={v}" for k, v in sorted(counts.items())),
    )
    if ok:
        log.info(
            "%d ways carry %.1f km of shape, worst vertex %.1f m off track",
            len(used),
            sum(o.length_m for o in ok) / 1000.0,
            max(o.worst_m for o in ok),
        )
    return counts
