"""Services drawn from OpenStreetMap route relations, with no timetable at all.

`trace` uses a route relation as *geometry* for a pattern that already exists in a
timetable. This uses one as the pattern itself. A PTv2 relation names its operator
and its line and lists its ways in order, so route identity and route extent are
both already there -- everything a service needs except how often it runs.

That is what makes Great Britain's heavy rail drawable at all. It is absent from
BODS, and every timetable source for it sits behind a login, a licence negotiation
or both. The relations are ODbL, which every wayfare archive already carries and
credits for its matched roads, so a rail layer built this way adds no licence to the
archive and nothing to renegotiate if it is copied.

Measured over the national Overpass body, `route=train`, Great Britain:

* 1,114 relations, of which **981 chain with zero breaks** -- the same gate `trace`
  applies, and the same order of quality as the Underground's 86.9%.
* 911 of those sit in Great Britain rather than Ireland or the continent.
* 96.6% carry `operator`, 99.7% `name`, 62.6% `ref`.
* 55,114 distinct ways, 26,454 km of unique track.

Two things this deliberately does not do:

* **It does not reorder a relation's members.** A greedy endpoint walk rescues 15 of
  the 120 that break, and the rest have between 9 and 36 loose ends -- genuine gaps
  rather than misordering. Chaining in member order stays the gate.
* **It does not invent `n_trips`.** The column is left NULL and `rail.attribute`
  fills it in if a timetable ever arrives. A count of relations is a fact about how
  thoroughly a line has been mapped, and passing one off as a service level would be
  the same mistake as judging a low zoom by feature counts.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import cos, radians, sqrt
from pathlib import Path

import duckdb

from . import config, db, logs, osm

log = logs.get("osmroutes")

# The `route` tag values that become patterns, and the `patterns.mode` each becomes.
# Deliberately narrow. `config.OSM_ROUTE_VALUES` is wide because `trace` discovers
# geometry with it and lets the stop-sequence join decide; here a relation *is* a
# service, so admitting `subway` would put a second Underground beside the one BODS
# already supplies and draw every tube line twice.
ROUTE_MODES: dict[str, str] = {"train": "rail"}

# Padding on the window asked of Overpass, in degrees. A relation is kept when it
# chains, not when it fits, so this only has to be wide enough that a service near
# the edge of the region has its relation returned at all.
BBOX_PAD = 0.05


@dataclass(frozen=True)
class Built:
    """What one run produced, for the CLI to print and the tests to assert on."""

    considered: int
    chained: int
    patterns: int
    ways: int
    skipped_no_stops: int
    skipped_broken: int
    track_km: float


def bbox(con: duckdb.DuckDBPyConnection) -> tuple[float, float, float, float] | None:
    """The window to ask Overpass for: every live pattern's stops, padded.

    Every live pattern rather than the pending non-road ones `trace.bbox` uses, and
    the difference is not cosmetic. `trace`'s window is drawn round the patterns it
    still owes an answer for -- for Great Britain the Underground, the DLR and a few
    tram systems, which is London, Manchester, Blackpool and Birmingham. Asking that
    window for National Rail would return no ScotRail at all.

    Hence a separate cache file too. Two windows sharing one cached body means
    whichever stage ran first silently decides the other's coverage.

    Clipped to the British Isles, which `patterns` has already done to the rows this
    reads. Kept here as well because the failure it prevents is silent and expensive:
    BODS carries coach to Warsaw, and a min/max that meets one of those stops asks
    Overpass for every railway between Ireland and Poland. That is the query that
    ran out of memory before the clip existed, and a database built by an older
    `patterns` would do it again.
    """
    row = db.row(
        con,
        f"""
        SELECT min(s.lat), min(s.lon), max(s.lat), max(s.lon)
        FROM pattern_stops ps
        JOIN stops s USING (stop_id)
        JOIN patterns p USING (pattern_id)
        WHERE {db.current_feed()}
          AND {config.british_isles_sql("s.lat", "s.lon")}
        """,
    )
    if row is None or row[0] is None:
        return None
    south, west, north, east = (float(v) for v in row)
    return (south - BBOX_PAD, west - BBOX_PAD, north + BBOX_PAD, east + BBOX_PAD)


def _span_m(points: list[tuple[float, float]]) -> float:
    """Straight-line length of a chain of (lat, lon), in metres."""
    total = 0.0
    for (y1, x1), (y2, x2) in zip(points, points[1:], strict=False):
        dy = (y2 - y1) * 111_320.0
        dx = (x2 - x1) * 111_320.0 * cos(radians(y1))
        total += sqrt(dy * dy + dx * dx)
    return total


@dataclass(frozen=True)
class Candidate:
    """One relation that passed the gate, ready to become a pattern."""

    relation_id: int
    route_id: str
    agency_id: str | None
    short_name: str
    mode: str
    names: list[str]
    n_stops: int
    span_m: float
    way_ids: list[int]
    lon_e6: list[int]
    lat_e6: list[int]


def candidates(
    relations: list[osm.Relation], routes: dict[str, str] | None = None
) -> tuple[list[Candidate], int, int]:
    """The relations that can stand as a service, and counts of the two refusals.

    A relation qualifies when its ways chain end to end with no break and it names
    at least two stops. Both are the tests `trace` already applies, for the same
    reason: a break draws confident track across a gap, and a relation with no stops
    can be neither identified nor joined to anything later.
    """
    routes = routes or ROUTE_MODES
    out: list[Candidate] = []
    broken = no_stops = 0
    for r in relations:
        mode = routes.get(r.route or "")
        if mode is None or not r.ways:
            continue
        chain = osm.chain(r)
        if chain.breaks:
            broken += 1
            continue
        names = [osm.normalise(s.name) for s in r.stops if s.name]
        names = [n for n in names if n]
        if len(names) < 2:
            no_stops += 1
            continue
        # `ref` first: it is the line's own designation where the mapper gave one,
        # and it is what a reader recognises. The relation name is a description
        # ("CrossCountry: Plymouth -> Aberdeen") and is the fallback rather than the
        # first choice, but 99.7% carry one where only 62.6% carry a ref.
        short = (r.tags.get("ref") or r.name or "").strip() or f"r{r.relation_id}"
        out.append(
            Candidate(
                relation_id=r.relation_id,
                route_id=f"osm:r{r.relation_id}",
                agency_id=(r.tags.get("operator") or "").strip() or None,
                short_name=short,
                mode=mode,
                names=names,
                n_stops=len(names),
                span_m=_span_m([(s.lat, s.lon) for s in r.stops if s.name]),
                way_ids=list(chain.way_ids),
                lon_e6=[round(lon * 1e6) for _, lon in chain.points],
                lat_e6=[round(lat * 1e6) for lat, _ in chain.points],
            )
        )
    return out, broken, no_stops


def write(con: duckdb.DuckDBPyConnection, found: list[Candidate]) -> int:
    """Insert the candidates as patterns, with their geometry and their ways.

    Four tables and each is load-bearing:

    * `patterns`, stamped with the current feed version so `db.current_feed()`
      admits them. They are rebuilt from OpenStreetMap on every run rather than
      carried forward, which is what keeps a line retired in OSM from being drawn
      for ever.
    * `traces`, so `aggregate.build_segments` draws them through the path that
      already exists rather than a second one beside it.
    * `trace_status`, marked ``ok``. Without it `trace._pending_sql` selects every
      one of these -- they are live, not matchable and carry no ``shape_id``, which
      is exactly its definition of pending -- and spends a national Overpass query
      re-deriving geometry this stage already has.
    * `ways`, the per-way geometry `traces` cannot be cut back into.

    The identity hash is computed in SQL by `db.pattern_id_sql` rather than in
    Python, so these ids are minted by the same expression as the timetable's and
    cannot drift from it.
    """
    if not found:
        return 0
    feed = db.get_meta(con, "feed_version")
    if not feed:
        raise RuntimeError("no feed_version in meta; run `wayfare patterns` first")

    con.execute("BEGIN")
    try:
        con.execute("""
            CREATE OR REPLACE TEMP TABLE osm_route_raw (
                relation_id BIGINT, route_id VARCHAR, agency_id VARCHAR,
                short_name VARCHAR, mode VARCHAR, stop_key VARCHAR,
                n_stops INTEGER, span_m DOUBLE,
                way_ids BIGINT[], lon_e6 INTEGER[], lat_e6 INTEGER[]
            )
        """)
        con.executemany(
            "INSERT INTO osm_route_raw VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    c.relation_id,
                    c.route_id,
                    c.agency_id,
                    c.short_name,
                    c.mode,
                    "".join(c.names),
                    c.n_stops,
                    c.span_m,
                    c.way_ids,
                    c.lon_e6,
                    c.lat_e6,
                )
                for c in found
            ],
        )

        pid = db.pattern_id_sql("route_id", "NULL", "stop_key")
        # Retire first, so a relation that stopped chaining -- or stopped existing --
        # leaves the current feed instead of being drawn for ever on a stale row.
        con.execute(
            "UPDATE patterns SET last_seen = NULL "
            "WHERE route_id LIKE 'osm:r%' AND last_seen = ?",
            [feed],
        )
        con.execute(f"""
            INSERT INTO patterns
                (pattern_id, route_id, agency_id, short_name, direction, shape_id,
                 n_stops, n_trips, span_m, mode, first_seen, last_seen)
            SELECT {pid}, route_id, agency_id, short_name, NULL, NULL,
                   n_stops, NULL, span_m, mode, '{feed}', '{feed}'
            FROM osm_route_raw
            ON CONFLICT (pattern_id) DO UPDATE SET
                agency_id = excluded.agency_id,
                short_name = excluded.short_name,
                n_stops = excluded.n_stops,
                span_m = excluded.span_m,
                mode = excluded.mode,
                last_seen = excluded.last_seen
        """)  # noqa: S608 - feed is a meta value this pipeline wrote
        con.execute(f"""
            INSERT OR REPLACE INTO traces (pattern_id, relation_id, way_ids, lon_e6, lat_e6)
            SELECT {pid}, relation_id, way_ids, lon_e6, lat_e6 FROM osm_route_raw
        """)
        con.execute(f"""
            INSERT OR REPLACE INTO trace_status
                (pattern_id, status, relation_id, osm_route, n_ways, n_stops,
                 worst_stop_m, length_m, detail, traced_at)
            SELECT {pid}, 'ok', relation_id, mode, len(way_ids), n_stops,
                   NULL, NULL, 'built from the relation itself', now()
            FROM osm_route_raw
        """)
        n = int(db.scalar(con, "SELECT count(*) FROM osm_route_raw"))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return n


def write_ways(con: duckdb.DuckDBPyConnection, relations: list[osm.Relation]) -> int:
    """Per-way geometry, deduplicated across every relation that uses the way.

    Rebuilt rather than merged, like `segments`: it is derived from a body that was
    just fetched and costs nothing to recompute, and a way that has left the network
    should stop being drawn on the next run.
    """
    seen: dict[int, osm.Way] = {}
    for r in relations:
        for w in r.ways:
            if len(w.points) >= 2:
                seen.setdefault(w.way_id, w)
    con.execute("DELETE FROM ways")
    if not seen:
        return 0
    rows = []
    for way in seen.values():
        lons = [round(lon * 1e6) for _, lon in way.points]
        lats = [round(lat * 1e6) for lat, _ in way.points]
        rows.append((way.way_id, lons, lats, min(lons), min(lats), max(lons), max(lats)))
    con.executemany("INSERT INTO ways VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def run(
    con: duckdb.DuckDBPyConnection,
    cache: Path | None = None,
    *,
    refresh: bool = False,
    routes: dict[str, str] | None = None,
) -> Built:
    """Fetch the relations and turn the ones that qualify into services."""
    window = bbox(con)
    if window is None:
        raise RuntimeError("no live patterns to derive a window from")
    relations = osm.fetch(window, cache or config.RAW / "osm_routes.json", refresh=refresh)
    wanted = routes or ROUTE_MODES
    considered = [r for r in relations if (r.route or "") in wanted]
    found, broken, no_stops = candidates(relations, wanted)
    n_patterns = write(con, found)
    n_ways = write_ways(con, [r for r in relations if (r.route or "") in wanted])
    km = sum(c.span_m for c in found) / 1000.0
    built = Built(
        considered=len(considered),
        chained=len(considered) - broken,
        patterns=n_patterns,
        ways=n_ways,
        skipped_no_stops=no_stops,
        skipped_broken=broken,
        track_km=km,
    )
    log.info(
        "%d relations considered, %d chained, %d became patterns over %d ways "
        "(%d broken, %d with fewer than two named stops)",
        built.considered,
        built.chained,
        built.patterns,
        built.ways,
        built.skipped_broken,
        built.skipped_no_stops,
    )
    return built
