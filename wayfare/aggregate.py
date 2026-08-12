"""Invert pattern-to-edges into edge-to-services.

Matching answers "which roads does this bus take". Both the map and the art want
the opposite: "which buses take this road". That is one group-by, but two details
matter.

First, a service is identified by its public number, not by its GTFS route_id. The
same number is often registered several times -- once per operator, per licensing
area, or per revision -- and a user hovering a road wants to see "43" once, not
"43, 43, 43". Grouping by short_name is what makes the output legible.

Second, a pattern can traverse the same edge more than once, and several patterns
of the same service overlap heavily. Counting distinct patterns per edge would
overstate how much service a road carries, so trips are counted from distinct
(pattern, edge) pairs.
"""

from __future__ import annotations

import duckdb

from . import db, logs

log = logs.get("aggregate")


def build(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("DELETE FROM edge_services")
    # short_name is what the public calls the bus. It is occasionally blank in the
    # feed, in which case route_id is the only handle we have; a subquery gives the
    # fallback a name that GROUP BY can refer to without repeating the expression.
    # The join to patterns is what drops departed services. pattern_edges keeps the
    # matched geometry of a pattern that has left the timetable -- it is a cache, and
    # a seasonal service that returns should not be paid for twice -- but a road
    # nobody runs on any more must not still be drawn as if buses used it.
    #
    # `matchable` is the same rule in the other direction, and it is about the past
    # rather than the future. A database matched before the mode filter existed holds
    # `pattern_edges` for patterns that should never have reached the matcher: Great
    # Britain's held 1,726,822 of them for the Underground alone, plus ferries snapped
    # to coast roads, and 16,833 edges were reachable from nothing else -- roads drawn
    # as though buses used them, weighted by trips no bus runs. Those patterns are
    # drawn from operator geometry by `build_segments` now, and drawing them twice, one
    # of the copies wrong, is worse than either.
    con.execute(f"""
        INSERT INTO edge_services
        SELECT edge_id, short_name, agency_id,
               count(DISTINCT pattern_id) AS n_patterns,
               sum(n_trips)               AS n_trips
        FROM (
            SELECT pe.edge_id, pe.pattern_id, p.agency_id, p.n_trips,
                   COALESCE(NULLIF(trim(p.short_name), ''), p.route_id) AS short_name
            FROM (SELECT DISTINCT pattern_id, edge_id FROM pattern_edges) pe
            JOIN patterns p USING (pattern_id)
            WHERE {db.current_feed()} AND {db.matchable("p", con)}
        )
        GROUP BY edge_id, short_name, agency_id
    """)
    db.index(con)

    n_edges, n_rows = db.row(
        con, "SELECT count(DISTINCT edge_id), count(*) FROM edge_services"
    )
    log.info("%d edges carry %d edge-service pairs", n_edges, n_rows)
    build_segments(con)
    build_track_services(con)


def build_segments(con: duckdb.DuckDBPyConnection) -> None:
    """Copy the operator trace of every live non-road pattern into `segments`.

    This is the whole of "drawing" a tram. There is no matcher, no routing and no
    snapping: the operator recorded where the vehicle goes, and for a mode with no
    road under it that recording is the best geometry available and the only one.
    Metrolink's traces run to a median 474 points, which is the same order as the
    bus feed's 849 -- a survey rather than a schematic.

    Rebuilt outright rather than merged. It is derived from `patterns` and `shapes`
    and costs nothing to recompute, so it holds the current feed only, exactly like
    `pattern_stops`. A departed tram stops being drawn on the next run, which is the
    same rule `edge_services` follows.

    Two sources, and the operator's own wins. Where the feed carries a trace that
    recording is of where the *vehicle* goes; an OSM relation is a survey of where
    the track is, which is the same line for a tram and not quite the same line for
    anything with a depot or a turnback. The `wayfare trace` arm therefore only
    covers the patterns with no `shape_id` at all -- which is what keeps the two arms
    disjoint, and what lets `segments` keep its primary key on `pattern_id`.

    A non-road pattern with neither gets no row and is simply not drawn. That is the
    "bad geometry is worse than missing geometry" rule applied to the one case where
    inventing would be easy: the stops are known, and a straight line between them
    would render perfectly happily down the wrong side of a river.
    """
    con.execute("DELETE FROM segments")
    con.execute(f"""
        INSERT INTO segments
        WITH geom AS (
            SELECT p.pattern_id, p.mode, s.lon_e6, s.lat_e6
            FROM patterns p
            JOIN shapes s ON s.shape_id = p.shape_id
            WHERE {db.current_feed()} AND NOT {db.matchable()}
            UNION ALL
            SELECT p.pattern_id, p.mode, t.lon_e6, t.lat_e6
            FROM patterns p
            JOIN traces t ON t.pattern_id = p.pattern_id
            WHERE {db.current_feed()} AND NOT {db.matchable()}
              AND p.shape_id IS NULL
        )
        SELECT pattern_id, mode, lon_e6, lat_e6,
               list_min(lon_e6), list_min(lat_e6),
               list_max(lon_e6), list_max(lat_e6)
        FROM geom
    """)
    drawn, traced, missing = db.row(
        con,
        f"""
        SELECT (SELECT count(*) FROM segments),
               (SELECT count(*) FROM patterns p
                JOIN traces t ON t.pattern_id = p.pattern_id
                WHERE {db.current_feed()} AND NOT {db.matchable()}
                  AND p.shape_id IS NULL),
               (SELECT count(*) FROM patterns p
                WHERE {db.current_feed()} AND NOT {db.matchable()}
                  AND p.shape_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM traces t WHERE t.pattern_id = p.pattern_id))
        """,
    )
    if drawn or missing:
        log.info(
            "%d non-road patterns drawn (%d of them from OSM route relations), "
            "%d have no geometry from either source and are not drawn",
            drawn,
            traced,
            missing,
        )


def _clustered(con: duckdb.DuckDBPyConnection) -> str:
    """Whether `edges` is still in the Z-order `wayfare cluster` left it in.

    A count rather than a flag, because clustering goes stale rather than off: the
    rows that were sorted stay sorted, and anything `match` adds afterwards sits
    unsorted on the end where no zonemap can help.
    """
    at = db.get_meta(con, "edges_clustered")
    if at is None:
        return "no"
    now = int(db.scalar(con, "SELECT count(*) FROM edges"))
    if int(at) == now:
        return "yes"
    return f"stale ({at} of {now} edges sorted; re-run `wayfare cluster`)"


def coverage(con: duckdb.DuckDBPyConnection) -> dict[str, object]:
    """How much of the network came out, and how much was lost on the way.

    Reported as a funnel rather than a single number: a run that matched 95% of
    patterns but dropped the busiest ones is worse than the percentage suggests.

    Every number in that funnel counts *matchable* patterns only. A tram will never
    hold a `match_status` row, so counting it as unmatched would put a floor under
    `patterns_pending` that no amount of matching could lift -- and `patterns_pending`
    is half of what `deploy/refresh.sh` gates a publish on, so that floor would stop a
    scheduled region publishing again, permanently. What the other modes are doing is
    `patterns_by_mode` instead.
    """
    live = db.current_feed()
    owed = f"{live} AND {db.matchable('p', con)}"
    matched, total = db.row(
        con,
        f"""
        SELECT
          (SELECT count(*) FROM patterns p JOIN match_status m USING (pattern_id)
             WHERE m.status = 'ok' AND {owed}),
          (SELECT count(*) FROM patterns p WHERE {owed})
        """,
    )

    trips_ok, trips_all = db.row(
        con,
        f"""
        SELECT
          (SELECT COALESCE(sum(p.n_trips), 0) FROM patterns p
             JOIN match_status m USING (pattern_id)
             WHERE m.status = 'ok' AND {owed}),
          (SELECT COALESCE(sum(p.n_trips), 0) FROM patterns p WHERE {owed})
        """,
    )

    return {
        "feed_version": db.get_meta(con, "feed_version"),
        "graph_id": db.get_meta(con, "graph_id"),
        "modes": db.get_meta(con, "modes"),
        "patterns_total": total,
        "patterns_matched": matched,
        "patterns_pct": round(100.0 * matched / max(total, 1), 1),
        # Live patterns per mode, matchable or not. This is the only place a mode
        # going missing is visible: `patterns` rebuilds from whatever selection it
        # was given, so a refresh that lost its `--modes` drops every tram silently
        # and every other number here stays healthy while it does.
        # A database from before the column reports one bucket rather than nothing, so
        # the total still reconciles with the rest of the funnel.
        "patterns_by_mode": {
            row[0] or "unknown": row[1]
            for row in con.execute(
                f"SELECT p.mode, count(*) FROM patterns p WHERE {live} "
                "GROUP BY p.mode ORDER BY p.mode"
                if "mode" in db.columns(con, "patterns")
                else f"SELECT 'unknown', count(*) FROM patterns p WHERE {live}"
            ).fetchall()
        },
        # What a scheduled run needs to know: how much of this feed is still owed
        # to the matcher, and therefore whether the tiles are complete yet.
        "patterns_pending": db.scalar(
            con,
            f"""
            SELECT count(*) FROM patterns p WHERE {owed}
              AND NOT EXISTS (SELECT 1 FROM match_status m
                              WHERE m.pattern_id = p.pattern_id)
            """,
        ),
        "patterns_departed": db.scalar(
            con, f"SELECT count(*) FROM patterns p WHERE NOT ({live})"
        ),
        # The number that actually matters: share of timetabled service represented.
        "trips_pct": round(100.0 * trips_ok / max(trips_all, 1), 1),
        "edges": db.scalar(con, "SELECT count(*) FROM edges"),
        # Not just whether `wayfare cluster` has ever run, but whether it still
        # holds: it records the row count it sorted, so edges appended by a later
        # `match` land on the end out of order and show up here as a shortfall.
        "edges_clustered": _clustered(con),
        "services": db.scalar(con, "SELECT count(DISTINCT short_name) FROM edge_services"),
        "by_status": {
            row[0]: row[1]
            for row in con.execute(
                f"""
                SELECT m.status, count(*) FROM match_status m
                JOIN patterns p USING (pattern_id) WHERE {live}
                GROUP BY m.status
                """
            ).fetchall()
        },
        "traced": _traced(con, live),
    }


def _traced(con: duckdb.DuckDBPyConnection, live: str) -> dict[str, object]:
    """What `wayfare trace` owes and what it has drawn, kept out of the funnel.

    Out of it deliberately, and for the reason `patterns_by_mode` is out of it: the
    funnel counts matchable patterns, and every pattern counted here is one Valhalla
    will never be asked about. Folding an unresolvable relation into
    `patterns_pending` would put a permanent floor under the number
    `deploy/refresh.sh` gates a publish on, which is exactly the mistake the mode
    filter already made once.

    A database with no `trace_status` table reports nothing rather than raising.
    `status` connects read-only, so a data root that has not been opened for writing
    since this stage landed still has the old schema -- the same trap `db.matchable`
    carries a connection to survive.
    """
    if not db.table_exists(con, "trace_status"):
        return {}
    owed = f"{live} AND NOT {db.matchable('p', con)} AND p.shape_id IS NULL"
    pending = db.scalar(
        con,
        f"""
        SELECT count(*) FROM patterns p WHERE {owed}
          AND NOT EXISTS (SELECT 1 FROM trace_status t
                          WHERE t.pattern_id = p.pattern_id)
        """,
    )
    return {
        "patterns_owed": db.scalar(con, f"SELECT count(*) FROM patterns p WHERE {owed}"),
        "patterns_pending": pending,
        "by_status": {
            row[0]: row[1]
            for row in con.execute(
                f"""
                SELECT t.status, count(*) FROM trace_status t
                JOIN patterns p USING (pattern_id) WHERE {live}
                GROUP BY t.status
                """
            ).fetchall()
        },
    }


def build_track_services(con: duckdb.DuckDBPyConnection) -> int:
    """Invert relation track from per-pattern into per-way: which lines use this rail.

    `segments` draws one polyline per pattern, so two services over one stretch of
    the West Coast Main Line are two coincident lines and nothing can be asked which
    services use a given piece of track. This is the same inversion `edge_services`
    performs for roads, keyed one level up the identifier stack: straight on
    `way_id`, because nothing routed this track and there is no Valhalla GraphId to
    key on. `way_id` is also the more durable of the two -- an `edge_id` is valid
    only within one graph build.

    **Only patterns this pipeline built from a relation are inverted**, which is
    what `route_id LIKE 'osm:r%'` is doing, and leaving it out would ship a
    confident lie. `trace` records `way_ids` as the whole candidate chain rather
    than the ways inside the slice it cut, so a Northern line short working from
    Edgware to Kennington is stored against every way of the Northern line. That is
    harmless while the column only documents what was drawn; inverted, it attributes
    a service to track it never runs on. An `osmroutes` pattern *is* its whole
    relation, so for those the two are the same thing by construction.

    `n_trips` sums to NULL rather than to zero where no timetable has been
    attributed, because zero trips a week and an unknown number of trips a week are
    different claims and only one of them is true.
    """
    con.execute("DELETE FROM track_services")
    con.execute(f"""
        INSERT INTO track_services
        SELECT way_id, short_name, agency_id,
               count(DISTINCT pattern_id)                  AS n_patterns,
               CASE WHEN count(n_trips) = 0 THEN NULL
                    ELSE sum(n_trips) END                  AS n_trips
        FROM (
            -- The subquery is what gives the fallback a name GROUP BY can refer
            -- to without repeating the expression, exactly as `build` does above.
            SELECT u.way_id, p.pattern_id, p.agency_id, p.n_trips,
                   COALESCE(NULLIF(trim(p.short_name), ''), p.route_id) AS short_name
            FROM traces t
            JOIN patterns p USING (pattern_id)
            CROSS JOIN unnest(t.way_ids) AS u(way_id)
            WHERE {db.current_feed()} AND NOT {db.matchable()}
              AND p.route_id LIKE 'osm:r%'
        )
        GROUP BY way_id, short_name, agency_id
    """)
    n_ways, n_rows = db.row(
        con, "SELECT count(DISTINCT way_id), count(*) FROM track_services"
    )
    log.info("%d ways carry %d track-service pairs", n_ways, n_rows)
    return int(n_rows)
