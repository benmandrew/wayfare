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
    con.execute("""
        INSERT INTO edge_services
        SELECT edge_id, short_name, agency_id,
               count(DISTINCT pattern_id) AS n_patterns,
               sum(n_trips)               AS n_trips
        FROM (
            SELECT pe.edge_id, pe.pattern_id, p.agency_id, p.n_trips,
                   COALESCE(NULLIF(trim(p.short_name), ''), p.route_id) AS short_name
            FROM (SELECT DISTINCT pattern_id, edge_id FROM pattern_edges) pe
            JOIN patterns p USING (pattern_id)
        )
        GROUP BY edge_id, short_name, agency_id
    """)
    db.index(con)

    n_edges, n_rows = db.row(
        con, "SELECT count(DISTINCT edge_id), count(*) FROM edge_services"
    )
    log.info("%d edges carry %d edge-service pairs", n_edges, n_rows)


def coverage(con: duckdb.DuckDBPyConnection) -> dict[str, object]:
    """How much of the network came out, and how much was lost on the way.

    Reported as a funnel rather than a single number: a run that matched 95% of
    patterns but dropped the busiest ones is worse than the percentage suggests.
    """
    matched, total = db.row(
        con,
        """
        SELECT
          (SELECT count(*) FROM match_status WHERE status = 'ok'),
          (SELECT count(*) FROM patterns)
        """,
    )

    trips_ok, trips_all = db.row(
        con,
        """
        SELECT
          (SELECT COALESCE(sum(n_trips), 0) FROM patterns p
             JOIN match_status m USING (pattern_id) WHERE m.status = 'ok'),
          (SELECT COALESCE(sum(n_trips), 0) FROM patterns)
        """,
    )

    return {
        "patterns_total": total,
        "patterns_matched": matched,
        "patterns_pct": round(100.0 * matched / max(total, 1), 1),
        # The number that actually matters: share of timetabled service represented.
        "trips_pct": round(100.0 * trips_ok / max(trips_all, 1), 1),
        "edges": db.scalar(con, "SELECT count(*) FROM edges"),
        "services": db.scalar(con, "SELECT count(DISTINCT short_name) FROM edge_services"),
        "by_status": {
            row[0]: row[1]
            for row in con.execute(
                "SELECT status, count(*) FROM match_status GROUP BY status"
            ).fetchall()
        },
    }
