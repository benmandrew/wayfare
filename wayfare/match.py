"""The long-running stage: map-match every pattern onto the road graph.

This is the part that runs for a day or two, so its design is dominated by one
question -- what happens when it stops? It can stop for any reason: the server
reboots, Valhalla runs out of memory, the process is killed. So:

* Every pattern gets a row in ``match_status`` the moment it is resolved, whatever
  the outcome. Work is selected by the absence of that row, so restarting picks up
  where it left off with no bookkeeping of its own.
* Failures are recorded, not retried forever. A pattern whose stops cannot be
  connected by road will never succeed, and a matcher that retries it on every
  restart never finishes. That rule holds only while "failed" means "impossible",
  so a fault that was never about the pattern -- the connection refused, the read
  timed out, Valhalla restarting -- is recorded as ``transport_error`` and is the
  one status a later run is invited to clear. See RETRYABLE.
* One batch is both the unit of concurrency and the unit of checkpointing. Those
  cannot be separated: because work is selected by the absence of a status row, a
  batch still in flight is still selectable, so loading the next batch before
  committing the last would hand the same patterns out twice.

Concurrency is threads, not processes: the work is entirely waiting on Valhalla's
HTTP, and DuckDB takes a single writer. Workers do HTTP, the main thread writes.
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import requests

from . import config, db, logs, valhalla

log = logs.get("match")

# The one outcome that is a statement about the world at that moment rather than
# about the pattern. Everything else in match_status is permanent by design.
TRANSPORT_ERROR = "transport_error"

# What `--retry transient` expands to. Kept as a name rather than spelled out at
# the call site so that adding a status here reaches the CLI, the help text and
# the recovery path together.
RETRYABLE = (TRANSPORT_ERROR,)
_RETRY_ALIASES = {"transient": RETRYABLE}


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
            f"""
            SELECT count(*) FROM patterns p
            WHERE {db.current_feed()}
              AND {db.matchable()}
              AND NOT EXISTS (
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
    wrong too, and this is how they get cleared. ``transient`` is the alias for the
    statuses that are safe to clear unattended, which is ``transport_error`` alone:
    nothing was ever learned about those patterns.

    Call this *before* matching starts, never between batches. Work is selected by
    the absence of a status row, so deleting one while a batch holding that pattern
    is in flight hands the same pattern out twice -- the same trap that makes a
    batch the unit of both concurrency and checkpointing.
    """
    wanted = expand_statuses(statuses)
    ids = [
        r[0]
        for r in con.execute(
            "SELECT pattern_id FROM match_status WHERE status IN (SELECT unnest(?))",
            [wanted],
        ).fetchall()
    ]
    if not ids:
        return 0
    con.execute("DELETE FROM pattern_edges WHERE pattern_id IN (SELECT unnest(?))", [ids])
    con.execute("DELETE FROM match_status WHERE pattern_id IN (SELECT unnest(?))", [ids])
    # `edges` is left alone: it is shared across patterns and re-inserted by
    # ON CONFLICT DO NOTHING, so a stale row costs nothing and deleting one that
    # another pattern still references would lose that pattern's geometry.
    log.info("cleared %d outcomes with status in %s", len(ids), wanted)
    return len(ids)


def expand_statuses(statuses: list[str]) -> list[str]:
    """Resolve the ``transient`` alias, leaving any literal status alone."""
    out: list[str] = []
    for s in statuses:
        out.extend(_RETRY_ALIASES.get(s, (s,)))
    return out


def reclassify_transport_faults(con: duckdb.DuckDBPyConnection) -> int:
    """Repair a database matched before transport faults had a status of their own.

    Until they did, ``match_one`` filed every fault under ``error``, so a Valhalla
    host that was down or restarting left a permanent hole in the map for every
    pattern handed out in that window -- 262 of Great Britain's 462 error rows, and
    nothing that would ever retry them.

    The rows can be told apart after the fact because ``detail`` records which side
    the failure came from. Anything Valhalla answered is stored as ``"<http
    status>: <json body>"``; a fault that never got an answer carries whatever the
    HTTP library said instead. So the test is the shape of the detail this codebase
    itself writes, not the wording of anyone's error message.

    One-off and explicit rather than a migration on connect: it decides what gets
    re-matched, and a match run costs a day or two, so it is the operator's call.
    Run it, then ``wayfare match --retry transient``.
    """
    before = _count(con, TRANSPORT_ERROR)
    con.execute(
        """
        UPDATE match_status SET status = ?
        WHERE status = 'error'
          AND NOT regexp_matches(coalesce(detail, ''), '^[0-9]{3}: ')
          AND coalesce(detail, '') NOT LIKE ?
        """,
        [TRANSPORT_ERROR, f"{valhalla.NO_SCORE_MESSAGE}%"],
    )
    moved = _count(con, TRANSPORT_ERROR) - before
    log.info("reclassified %d error rows as %s", moved, TRANSPORT_ERROR)
    return moved


def _count(con: duckdb.DuckDBPyConnection, status: str) -> int:
    return int(
        db.scalar(con, "SELECT count(*) FROM match_status WHERE status = ?", [status])
    )


def load_batch(con: duckdb.DuckDBPyConnection, limit: int) -> list[Pattern]:
    """Fetch the next unmatched patterns, busiest first.

    Ordering by trip count means that if the run is cut short, what got matched is
    the part of the network that carries the most service -- the output degrades
    gracefully instead of having a random hole in it. That is also what makes the
    queue safe to drain a slice at a time: a nightly run under a time budget spends
    it on the busiest roads first.

    Patterns that have left the timetable are excluded. Their match results stay
    cached against their identity, so if the service returns they cost nothing, but
    matching one now would be work spent on a journey nobody runs.
    """
    rows = con.execute(
        f"""
        SELECT p.pattern_id, p.short_name, p.span_m, p.shape_id
        FROM patterns p
        WHERE {db.current_feed()}
          AND {db.matchable()}
          AND NOT EXISTS (SELECT 1 FROM match_status m WHERE m.pattern_id = p.pattern_id)
        ORDER BY p.n_trips DESC, p.pattern_id
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

    # These are matched rather than skipped, because the geometry is recorded rather
    # than invented. The count is still worth saying out loud: a leg that long is what
    # drops a pattern on the other path, and a trace does not rule out the stop
    # coordinate that produced the leg being wrong.
    traced = sum(1 for p in out if p.shape and p.max_gap_m > config.MAX_STOP_GAP_M)
    if traced:
        log.info(
            "%d of %d patterns carry operator geometry across a leg over %.0f km",
            traced,
            len(out),
            config.MAX_STOP_GAP_M / 1000,
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
    if not p.shape and p.max_gap_m > config.MAX_STOP_GAP_M:
        # The bound guards the `stops` path and only the `stops` path. There, routing
        # through a long leg produces a confident-looking line down a motorway the bus
        # may not use, which is worse than an absent one, and past the limit Valhalla
        # refuses the request outright -- so the choice is a `skipped` row or an
        # `error` one, not a match.
        #
        # With an operator trace there is no route and no guess: map_snap follows
        # geometry the operator recorded, and how far apart two timing points are says
        # nothing about whether that geometry is good. Testing this before the strategy
        # was chosen threw away 153 GB patterns (6,062 trips) that had the road already
        # drawn for them, and 333 of Ireland's 2,853 -- 11.7%, because that feed carries
        # a shape on every trip and so cannot lose the argument on the other path.
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
    except valhalla.TransportError as exc:
        # Nothing was learned about this pattern, so recording it as a permanent
        # failure would be a lie. It stays selectable through `--retry transient`.
        return Outcome(p.pattern_id, TRANSPORT_ERROR, p.source, detail=str(exc)[:400])
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


def pin_graph(
    con: duckdb.DuckDBPyConnection, client: valhalla.Client, force: bool = False
) -> None:
    """Tie the database to one Valhalla graph build, and keep it there.

    Every edge_id already stored belongs to the build that produced it. Matching
    new patterns against a different build mixes two id spaces in one table, and
    the damage is invisible: the geometry renders, it is just attached to the wrong
    roads. Geofabrik rebuilds daily, so this is not a hypothetical.

    The first run records the build. Later runs refuse to add to it against
    anything else -- which is the point, because the alternative is discovering it
    after a match run of several days.
    """
    seen = client.graph_id()
    if seen is None:
        log.warning(
            "Valhalla did not report a tileset timestamp; cannot verify that this "
            "is the graph build the stored edge ids belong to"
        )
        return
    pinned = db.get_meta(con, "graph_id")
    if pinned is None:
        db.set_meta(con, "graph_id", seen)
        log.info("pinned to Valhalla graph %s", seen)
        return
    if pinned == seen or force:
        if pinned != seen:
            log.warning("graph changed %s -> %s; continuing under --force", pinned, seen)
            db.set_meta(con, "graph_id", seen)
        return
    raise RuntimeError(
        f"this database's edge ids belong to Valhalla graph {pinned}, but "
        f"{client.base} is now serving {seen}. Valhalla edge ids are only "
        "meaningful within one build, so matching further patterns would mix two "
        "id spaces. Either point at the original graph, or re-match from scratch "
        "(delete match_status, pattern_edges and edges), or override with --force "
        "if you are certain the graph is unchanged."
    )


def run(
    con: duckdb.DuckDBPyConnection,
    client_: valhalla.Client | None = None,
    workers: int | None = None,
    limit: int | None = None,
    max_seconds: float | None = None,
    force_graph: bool = False,
) -> dict[str, int]:
    """Match every pending pattern. Safe to interrupt and re-run.

    ``limit`` and ``max_seconds`` both bound one invocation without changing what
    it leaves behind, because work is selected by the absence of a status row: a
    run that stops early is indistinguishable from one that was killed. That is
    what lets a national queue be drained by a nightly job with a half-hour budget
    instead of one run that has to survive for days.
    """
    client = client_ or valhalla.Client()
    workers = workers or config.VALHALLA_WORKERS

    if not client.healthy():
        raise RuntimeError(
            f"Valhalla is not responding at {client.base}. "
            "Start it with `docker compose up -d valhalla` and wait for the graph "
            "to finish building (the first build takes 30-90 minutes)."
        )
    pin_graph(con, client, force=force_graph)

    total = pending_count(con)
    if limit:
        total = min(total, limit)
    log.info("%d patterns to match, %d workers", total, workers)

    # Said rather than acted on. Clearing these automatically would be a second
    # source of work selection, and one that fires without anyone asking; saying so
    # keeps a run reproducible while making sure the hole is never silent.
    stalled = _count(con, TRANSPORT_ERROR)
    if stalled:
        log.warning(
            "%d patterns failed on transport in an earlier run and are still "
            "unmatched; `wayfare match --retry transient` puts them back in the queue",
            stalled,
        )

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

        # Checked between batches, never inside one. A batch is the unit of
        # checkpointing, so stopping mid-batch would throw away work already paid
        # for -- the budget is a floor on how long the run takes, not a ceiling.
        if max_seconds is not None and time.monotonic() - started >= max_seconds:
            log.info(
                "time budget of %.0fs reached after %d patterns; %d still pending",
                max_seconds,
                done,
                pending_count(con),
            )
            break

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
        except (valhalla.TransportError, requests.RequestException) as exc:
            # The client turns these into TransportError itself, so reaching here
            # means a code path that talks HTTP by some other route. Either way the
            # pattern was never judged. No traceback: a Valhalla host that goes down
            # produces hundreds of these and they are all the same fault.
            log.warning("pattern %d: %s", p.pattern_id, exc)
            return Outcome(p.pattern_id, TRANSPORT_ERROR, p.source, detail=str(exc)[:400])
        except Exception as exc:  # noqa: BLE001 - one bad pattern must not stop a run of days
            # The genuine last resort: a bug in this code. Permanent on purpose --
            # a defect that retries forever is worse than one that records the
            # traceback and moves on.
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
        _insert_edges(con, list(seen.values()))
        _insert_links(con, link_rows)


# -- bulk insert ------------------------------------------------------------
#
# These two tables are why the match stage looked like it was waiting on Valhalla
# when it was not. Written a row at a time through executemany, DuckDB manages
# about 2,700 rows/s -- it is a columnar engine and every bound-parameter insert
# pays the full per-statement machinery. Wales writes 1,033,886 pattern_edges rows
# and 169,857 edges, so roughly 450 of the run's 983 seconds went on inserting
# rather than matching, with Valhalla sitting at under 1% CPU throughout.
#
# Staging the batch to a file and letting DuckDB read it back columnar is the same
# work at 1.6M rows/s for the flat table and 186k rows/s for the one with list
# columns. Measured: 400k link rows in 0.25s against 150s.
#
# pattern_edges is flat, so CSV. edges carries INTEGER[] geometry and a road_name
# that can hold quotes, commas and newlines, so newline-delimited JSON, with the
# column types stated rather than sniffed -- a batch whose geometry is entirely
# NULL would otherwise infer the wrong type and fail the insert.

_EDGE_COLS: tuple[str, ...] = (
    "edge_id",
    "way_id",
    "road_name",
    "road_class",
    "length_m",
    "lon_e6",
    "lat_e6",
    "min_lon_e6",
    "min_lat_e6",
    "max_lon_e6",
    "max_lat_e6",
)
_EDGE_TYPES = (
    "'edge_id':'BIGINT','way_id':'BIGINT','road_name':'VARCHAR',"
    "'road_class':'VARCHAR','length_m':'DOUBLE',"
    "'lon_e6':'INTEGER[]','lat_e6':'INTEGER[]',"
    "'min_lon_e6':'INTEGER','min_lat_e6':'INTEGER',"
    "'max_lon_e6':'INTEGER','max_lat_e6':'INTEGER'"
)


@contextmanager
def _staged(suffix: str) -> Iterator[Path]:
    """A scratch file beside the database, removed however the block exits.

    Under WORK rather than the system temp directory: a server run points
    WAYFARE_DATA at a volume with room, and /tmp is frequently a small tmpfs.
    """
    d = config.WORK / "stage"
    d.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(suffix=suffix, dir=d)
    os.close(fd)
    path = Path(name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def _insert_edges(con: duckdb.DuckDBPyConnection, rows: list[tuple[Any, ...]]) -> None:
    with _staged(".ndjson") as p:
        with p.open("w") as fh:
            for r in rows:
                fh.write(json.dumps(dict(zip(_EDGE_COLS, r, strict=True))))
                fh.write("\n")
        con.execute(
            f"INSERT INTO edges SELECT {', '.join(_EDGE_COLS)} "
            f"FROM read_json('{p}', format='newline_delimited', columns={{{_EDGE_TYPES}}}) "
            "ON CONFLICT (edge_id) DO NOTHING"
        )


def _insert_links(con: duckdb.DuckDBPyConnection, rows: list[tuple[int, int, int]]) -> None:
    with _staged(".csv") as p:
        with p.open("w", newline="") as fh:
            csv.writer(fh).writerows(rows)
        con.execute(
            f"INSERT INTO pattern_edges SELECT * FROM read_csv('{p}', header=false, "
            "columns={'pattern_id':'BIGINT','seq':'INTEGER','edge_id':'BIGINT'})"
        )


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
