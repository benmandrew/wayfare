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
            WHERE {db.current_feed()}
        )
        GROUP BY edge_id, short_name, agency_id
    """)
    db.index(con)

    n_edges, n_rows = db.row(
        con, "SELECT count(DISTINCT edge_id), count(*) FROM edge_services"
    )
    log.info("%d edges carry %d edge-service pairs", n_edges, n_rows)
    build_segments(con)


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

    A non-road pattern with no shape gets no row and is simply not drawn. That is
    the "bad geometry is worse than missing geometry" rule applied to the one case
    where inventing would be easy: the stops are known, and a straight line between
    them would render perfectly happily down the wrong side of a river.
    """
    con.execute("DELETE FROM segments")
    con.execute(f"""
        INSERT INTO segments
        SELECT p.pattern_id, p.mode, s.lon_e6, s.lat_e6,
               list_min(s.lon_e6), list_min(s.lat_e6),
               list_max(s.lon_e6), list_max(s.lat_e6)
        FROM patterns p
        JOIN shapes s ON s.shape_id = p.shape_id
        WHERE {db.current_feed()} AND NOT {db.matchable()}
    """)
    drawn, missing = db.row(
        con,
        f"""
        SELECT (SELECT count(*) FROM segments),
               (SELECT count(*) FROM patterns p
                WHERE {db.current_feed()} AND NOT {db.matchable()}
                  AND p.shape_id IS NULL)
        """,
    )
    if drawn or missing:
        log.info(
            "%d non-road patterns drawn from operator geometry, "
            "%d have none and are not drawn",
            drawn,
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
    """
    live = db.current_feed()
    matched, total = db.row(
        con,
        f"""
        SELECT
          (SELECT count(*) FROM patterns p JOIN match_status m USING (pattern_id)
             WHERE m.status = 'ok' AND {live}),
          (SELECT count(*) FROM patterns p WHERE {live})
        """,
    )

    trips_ok, trips_all = db.row(
        con,
        f"""
        SELECT
          (SELECT COALESCE(sum(p.n_trips), 0) FROM patterns p
             JOIN match_status m USING (pattern_id)
             WHERE m.status = 'ok' AND {live}),
          (SELECT COALESCE(sum(p.n_trips), 0) FROM patterns p WHERE {live})
        """,
    )

    return {
        "feed_version": db.get_meta(con, "feed_version"),
        "graph_id": db.get_meta(con, "graph_id"),
        "patterns_total": total,
        "patterns_matched": matched,
        "patterns_pct": round(100.0 * matched / max(total, 1), 1),
        # What a scheduled run needs to know: how much of this feed is still owed
        # to the matcher, and therefore whether the tiles are complete yet.
        "patterns_pending": db.scalar(
            con,
            f"""
            SELECT count(*) FROM patterns p WHERE {live}
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
    }
