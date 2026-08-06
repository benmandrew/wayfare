"""The long-running stage: map-match every pattern onto the road graph.

This is the part that runs for a day or two, so its design is dominated by one
question -- what happens when it stops? It can stop for any reason: the server
reboots, Valhalla runs out of memory, the process is killed. So:

* Every pattern gets a row in ``match_status`` the moment it is resolved, whatever
  the outcome. Work is selected by the absence of that row, so restarting picks up
  where it left off with no bookkeeping of its own.
* Failures are recorded, not retried forever. A pattern whose stops cannot be
  connected by road will never succeed, and a matcher that retries it on every
  restart never finishes.
* One batch is both the unit of concurrency and the unit of checkpointing. Those
  cannot be separated: because work is selected by the absence of a status row, a
  batch still in flight is still selectable, so loading the next batch before
  committing the last would hand the same patterns out twice.

Concurrency is threads, not processes: the work is entirely waiting on Valhalla's
HTTP, and DuckDB takes a single writer. Workers do HTTP, the main thread writes.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import duckdb

from . import config, db, logs, valhalla

log = logs.get("match")


@dataclass
class Pattern:
    pattern_id: int
    short_name: str | None
    span_m: float
    max_gap_m: float
    shape_id: str | None
    points: list[tuple[float, float]] = field(default_factory=list)
    shape: list[tuple[float, float]] = field(default_factory=list)

    @property
    def source(self) -> str:
        return "shape" if self.shape else "stops"


@dataclass
class Outcome:
    pattern_id: int
    status: str
    source: str
    confidence: float = 0.0
    road_m: float = 0.0
    detour: float = 0.0
    detail: str | None = None
    edges: list[valhalla.Edge] = field(default_factory=list)


# -- work selection ---------------------------------------------------------


def pending_count(con: duckdb.DuckDBPyConnection) -> int:
    return int(
        db.scalar(
            con,
            """
            SELECT count(*) FROM patterns p
            WHERE NOT EXISTS (
                SELECT 1 FROM match_status m WHERE m.pattern_id = p.pattern_id
            )
            """,
        )
    )


def retry(con: duckdb.DuckDBPyConnection, statuses: list[str]) -> int:
    """Forget outcomes with these statuses so the next run redoes them.

    Failures are deliberately never retried automatically -- a pattern that cannot
    be routed will never route, and retrying it every restart means never
    finishing. But when the matcher itself was wrong, the recorded failures are
    wrong too, and this is how they get cleared.
    """
    ids = [
        r[0]
        for r in con.execute(
            "SELECT pattern_id FROM match_status WHERE status IN (SELECT unnest(?))",
            [statuses],
        ).fetchall()
    ]
    if not ids:
        return 0
    con.execute("DELETE FROM pattern_edges WHERE pattern_id IN (SELECT unnest(?))", [ids])
    con.execute("DELETE FROM match_status WHERE pattern_id IN (SELECT unnest(?))", [ids])
    # `edges` is left alone: it is shared across patterns and re-inserted by
    # ON CONFLICT DO NOTHING, so a stale row costs nothing and deleting one that
    # another pattern still references would lose that pattern's geometry.
    log.info("cleared %d outcomes with status in %s", len(ids), statuses)
    return len(ids)


def load_batch(con: duckdb.DuckDBPyConnection, limit: int) -> list[Pattern]:
    """Fetch the next unmatched patterns, busiest first.

    Ordering by trip count means that if the run is cut short, what got matched is
    the part of the network that carries the most service -- the output degrades
    gracefully instead of having a random hole in it.
    """
    rows = con.execute(
        """
        SELECT p.pattern_id, p.short_name, p.span_m, p.shape_id
        FROM patterns p
        WHERE NOT EXISTS (SELECT 1 FROM match_status m WHERE m.pattern_id = p.pattern_id)
        ORDER BY p.n_trips DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    if not rows:
        return []

    ids = [r[0] for r in rows]
    stops = _fetch_points(con, ids)
    shape_ids = [r[3] for r in rows if r[3]]
    shapes = _fetch_shapes(con, shape_ids) if shape_ids else {}
    gaps = _fetch_max_gap(con, ids)

    out = []
    for pattern_id, short_name, span_m, shape_id in rows:
        out.append(
            Pattern(
                pattern_id=pattern_id,
                short_name=short_name,
                span_m=span_m or 0.0,
                max_gap_m=gaps.get(pattern_id, 0.0),
                shape_id=shape_id,
                points=stops.get(pattern_id, []),
                shape=shapes.get(shape_id, []) if shape_id else [],
            )
        )
    return out


def _fetch_points(
    con: duckdb.DuckDBPyConnection, ids: list[int]
) -> dict[int, list[tuple[float, float]]]:
    rows = con.execute(
        """
        SELECT ps.pattern_id, ps.seq, s.lat, s.lon
        FROM pattern_stops ps
        JOIN stops s USING (stop_id)
        WHERE ps.pattern_id IN (SELECT unnest(?))
        ORDER BY ps.pattern_id, ps.seq
        """,
        [ids],
    ).fetchall()
    out: dict[int, list[tuple[float, float]]] = {}
    for pattern_id, _seq, lat, lon in rows:
        out.setdefault(pattern_id, []).append((lat, lon))
    return out


def _fetch_shapes(
    con: duckdb.DuckDBPyConnection, shape_ids: list[str]
) -> dict[str, list[tuple[float, float]]]:
    rows = con.execute(
        "SELECT shape_id, lat_e6, lon_e6 FROM shapes WHERE shape_id IN (SELECT unnest(?))",
        [shape_ids],
    ).fetchall()
    return {
        shape_id: [(la / 1e6, lo / 1e6) for la, lo in zip(lat_e6, lon_e6, strict=True)]
        for shape_id, lat_e6, lon_e6 in rows
    }


def _fetch_max_gap(con: duckdb.DuckDBPyConnection, ids: list[int]) -> dict[int, float]:
    from .gtfs import _HAVERSINE

    dist = _HAVERSINE.format(lat1="a.lat", lon1="a.lon", lat2="b.lat", lon2="b.lon")
    rows = con.execute(
        f"""
        SELECT ps.pattern_id, max({dist})
        FROM pattern_stops ps
        JOIN pattern_stops ps2
          ON ps2.pattern_id = ps.pattern_id AND ps2.seq = ps.seq + 1
        JOIN stops a ON a.stop_id = ps.stop_id
        JOIN stops b ON b.stop_id = ps2.stop_id
        WHERE ps.pattern_id IN (SELECT unnest(?))
        GROUP BY ps.pattern_id
        """,
        [ids],
    ).fetchall()
    return {r[0]: r[1] or 0.0 for r in rows}


# -- matching ---------------------------------------------------------------


def match_one(client: valhalla.Client, p: Pattern) -> Outcome:
    if len(p.points) < 2:
        return Outcome(p.pattern_id, "skipped", p.source, detail="fewer than two stops")
    if p.max_gap_m > config.MAX_STOP_GAP_M:
        # Usually a long-distance coach leg or a stop with bad coordinates. Routing
        # through it produces a confident-looking line down a motorway that the bus
        # may not use, which is worse than an absent one.
        return Outcome(
            p.pattern_id,
            "skipped",
            p.source,
            detail=f"stop gap {p.max_gap_m / 1000:.0f} km exceeds limit",
        )

    try:
        m = client.match_shape(p.shape) if p.shape else client.match_stops(p.points)
    except valhalla.NoRoute as exc:
        return Outcome(p.pattern_id, "no_route", p.source, detail=str(exc)[:400])
    except valhalla.ValhallaError as exc:
        return Outcome(p.pattern_id, "error", p.source, detail=str(exc)[:400])

    if not m.edges:
        return Outcome(p.pattern_id, "no_route", m.source, detail="matched zero edges")

    detour = m.road_m / p.span_m if p.span_m > 0 else 0.0
    status = "ok"
    detail = None

    # A road distance far above the straight-line stop chain means the matcher went
    # somewhere the bus did not. Keep the row so it is never retried, but drop the
    # edges so the bad geometry never reaches the map.
    if m.road_m > p.span_m * config.MAX_DETOUR_RATIO + config.DETOUR_SLACK_M:
        status = "low_confidence"
        detail = f"detour ratio {detour:.1f}"
    elif m.source == "shape" and m.confidence < config.MIN_MATCH_CONFIDENCE:
        status = "low_confidence"
        detail = f"confidence {m.confidence:.2f}"

    return Outcome(
        pattern_id=p.pattern_id,
        status=status,
        source=m.source,
        confidence=m.confidence,
        road_m=m.road_m,
        detour=detour,
        detail=detail,
        edges=m.edges if status == "ok" else [],
    )


# -- the run ----------------------------------------------------------------


def run(
    con: duckdb.DuckDBPyConnection,
    client_: valhalla.Client | None = None,
    workers: int | None = None,
    limit: int | None = None,
) -> dict[str, int]:
    """Match every pending pattern. Safe to interrupt and re-run."""
    client = client_ or valhalla.Client()
    workers = workers or config.VALHALLA_WORKERS

    if not client.healthy():
        raise RuntimeError(
            f"Valhalla is not responding at {client.base}. "
            "Start it with `docker compose up -d valhalla` and wait for the graph "
            "to finish building (the first build takes 30-90 minutes)."
        )

    total = pending_count(con)
    if limit:
        total = min(total, limit)
    log.info("%d patterns to match, %d workers", total, workers)

    tally: dict[str, int] = {}
    done = 0
    started = time.monotonic()
    remaining = limit

    # One batch is both the unit of concurrency and the unit of checkpointing, and
    # those two must not be separated. Work is selected by the *absence* of a
    # match_status row, so a batch that is still in flight is still selectable --
    # loading the next batch before committing the last one hands the same patterns
    # out twice. Committing at the end of every batch is what makes selection sound.
    while True:
        size = config.CHECKPOINT_EVERY
        if remaining is not None:
            size = min(size, remaining)
            if size <= 0:
                break

        batch = load_batch(con, size)
        if not batch:
            break
        if remaining is not None:
            remaining -= len(batch)

        outcomes = _match_batch(client, batch, workers)
        _commit(con, outcomes)

        for o in outcomes:
            tally[o.status] = tally.get(o.status, 0) + 1
        done += len(outcomes)
        _progress(done, total, started, tally)

    return tally


def _match_batch(
    client: valhalla.Client, batch: list[Pattern], workers: int
) -> list[Outcome]:
    """Match one batch across a thread pool.

    Threads rather than processes because every worker spends its time waiting on
    Valhalla's HTTP, and DuckDB accepts a single writer anyway.
    """

    def one(p: Pattern) -> Outcome:
        try:
            return match_one(client, p)
        except Exception as exc:  # noqa: BLE001 - one bad pattern must not stop a run of days
            log.exception("pattern %d raised", p.pattern_id)
            return Outcome(p.pattern_id, "error", p.source, detail=str(exc)[:400])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, batch))


def _commit(con: duckdb.DuckDBPyConnection, batch: list[Outcome]) -> None:
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())

    con.executemany(
        """
        INSERT OR REPLACE INTO match_status
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                o.pattern_id,
                o.status,
                o.source,
                o.confidence,
                o.road_m,
                o.detour,
                len(o.edges),
                o.detail,
                now,
            )
            for o in batch
        ],
    )

    edge_rows = []
    link_rows = []
    for o in batch:
        for seq, e in enumerate(o.edges):
            link_rows.append((o.pattern_id, seq, e.edge_id))
            edge_rows.append(
                (e.edge_id, e.way_id, e.road_name, e.road_class, e.length_m, *_geom(e.geom))
            )

    if link_rows:
        # A pattern may traverse an edge twice (a loop, or an out-and-back spur), so
        # de-duplicate within the batch before insert -- ON CONFLICT cannot resolve
        # two conflicting rows in the same statement.
        seen: dict[int, tuple[Any, ...]] = {}
        for row in edge_rows:
            seen.setdefault(row[0], row)
        con.executemany(
            "INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (edge_id) DO NOTHING",
            list(seen.values()),
        )
        con.executemany("INSERT INTO pattern_edges VALUES (?, ?, ?)", link_rows)


# lon_e6, lat_e6, then the bounding box. Empty for an edge with too few points, so
# a degenerate edge still gets its row and is filtered on read like any other.
_NO_GEOM: tuple[Any, ...] = (None, None, None, None, None, None)


def _geom(points: list[tuple[float, float]]) -> tuple[Any, ...]:
    """Valhalla's (lat, lon) floats to the micro-degree lists the table stores.

    Rounding here rather than at read time is what lets the bbox be stored: the
    window query compares integers against integers and never has to look inside
    the geometry.
    """
    if len(points) < 2:
        return _NO_GEOM
    lats = [round(lat * 1e6) for lat, _ in points]
    lons = [round(lon * 1e6) for _, lon in points]
    return (lons, lats, min(lons), min(lats), max(lons), max(lats))


def _progress(done: int, total: int, started: float, tally: dict[str, int]) -> None:
    if not done:
        return
    elapsed = time.monotonic() - started
    rate = done / elapsed
    left = (total - done) / rate if rate > 0 else 0
    summary = " ".join(f"{k}={v}" for k, v in sorted(tally.items()))
    log.info(
        "%d/%d (%.1f%%) %.1f/s eta %s | %s",
        done,
        total,
        100.0 * done / max(total, 1),
        rate,
        _hms(left),
        summary,
    )


def _hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:d}h{(s % 3600) // 60:02d}m"


def summary(con: duckdb.DuckDBPyConnection) -> list[tuple[Any, ...]]:
    return con.execute("""
        SELECT status, source, count(*), round(avg(n_edges), 1), round(avg(detour), 2)
        FROM match_status GROUP BY status, source ORDER BY count(*) DESC
    """).fetchall()
