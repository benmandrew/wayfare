"""Reduce the GTFS timetable to distinct route patterns.

This is the stage that makes the rest affordable. The national feed holds about
1.55M trips, but the vast majority are the same physical journey repeated through
the day. Grouping trips by their ordered stop sequence collapses that to a far
smaller set of *patterns*, and a pattern is what we pay the map matcher for.

The work is done in DuckDB rather than Python because ``stop_times.txt`` is 5.1 GB
and the central operation is a group-by over every row of it. DuckDB does that out
of core; a Python dict of 46M stop references does not fit comfortably in RAM.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb

from . import db, logs

log = logs.get("gtfs")

# Distance between two lat/lon pairs, in metres. Written out rather than pulled
# from the spatial extension so the stage has no extension dependency.
_HAVERSINE = """
    2 * 6371000 * asin(sqrt(
        pow(sin(radians({lat2} - {lat1}) / 2), 2)
      + cos(radians({lat1})) * cos(radians({lat2}))
      * pow(sin(radians({lon2} - {lon1}) / 2), 2)
    ))
"""


def _csv(gtfs_dir: Path, name: str) -> str:
    """A read_csv call that keeps every column as text.

    GTFS ids are strings that frequently look like numbers, and letting the sniffer
    decide turns route "07" into 7 and then fails to join. all_varchar avoids a
    whole class of silent mismatch.
    """
    path = str(gtfs_dir / name)
    return f"read_csv('{path}', all_varchar=true, header=true)"


def build_patterns(
    gtfs_dir: Path,
    con: duckdb.DuckDBPyConnection,
    memory_limit: str | None = None,
) -> None:
    limit = memory_limit or os.environ.get("WAYFARE_MEM", "8GB")
    con.execute(f"SET memory_limit = '{limit}'")
    con.execute(f"SET temp_directory = '{gtfs_dir.parent / 'duckdb_tmp'}'")
    con.execute("SET preserve_insertion_order = false")

    log.info("loading stops, routes, trips")
    con.execute(f"""
        INSERT OR REPLACE INTO stops
        SELECT stop_id, stop_name,
               TRY_CAST(stop_lat AS DOUBLE), TRY_CAST(stop_lon AS DOUBLE)
        FROM {_csv(gtfs_dir, "stops.txt")}
        WHERE TRY_CAST(stop_lat AS DOUBLE) IS NOT NULL
    """)
    con.execute(f"""
        INSERT OR REPLACE INTO routes
        SELECT route_id, agency_id, route_short_name, route_long_name
        FROM {_csv(gtfs_dir, "routes.txt")}
    """)

    # Trips per week, so a pattern's weight reflects how often it actually runs
    # rather than how many rows the operator happened to emit. calendar_dates
    # exceptions are ignored here; they shift individual days, not the shape of the
    # week, and this number is only ever used as a rendering weight.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE service_days AS
        SELECT service_id,
               TRY_CAST(monday AS INT) + TRY_CAST(tuesday AS INT)
             + TRY_CAST(wednesday AS INT) + TRY_CAST(thursday AS INT)
             + TRY_CAST(friday AS INT) + TRY_CAST(saturday AS INT)
             + TRY_CAST(sunday AS INT) AS days_per_week
        FROM {_csv(gtfs_dir, "calendar.txt")}
    """)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE trip AS
        SELECT t.trip_id, t.route_id, t.shape_id,
               TRY_CAST(t.direction_id AS INTEGER) AS direction,
               COALESCE(s.days_per_week, 5) AS days_per_week
        FROM {_csv(gtfs_dir, "trips.txt")} t
        LEFT JOIN service_days s USING (service_id)
    """)
    n_trips = db.scalar(con, "SELECT count(*) FROM trip")
    log.info("%d trips", n_trips)

    # The expensive pass: one group-by over every stop_time row.
    log.info("collapsing trips to stop sequences (one pass over stop_times.txt)")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE trip_seq AS
        SELECT trip_id,
               list(stop_id ORDER BY TRY_CAST(stop_sequence AS INTEGER)) AS stop_seq
        FROM {_csv(gtfs_dir, "stop_times.txt")}
        GROUP BY trip_id
    """)

    log.info("deduplicating to patterns")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE pattern_raw AS
        SELECT row_number() OVER (ORDER BY count(*) DESC) AS pattern_id,
               t.route_id,
               t.direction,
               ts.stop_seq,
               count(*)                       AS n_trips_raw,
               sum(t.days_per_week)           AS n_trips,
               -- Where only some trips on a pattern carry operator geometry, take
               -- the most common shape_id rather than an arbitrary one.
               mode(t.shape_id) FILTER (WHERE t.shape_id IS NOT NULL
                                        AND t.shape_id <> '') AS shape_id
        FROM trip t
        JOIN trip_seq ts USING (trip_id)
        GROUP BY t.route_id, t.direction, ts.stop_seq
    """)

    con.execute("DELETE FROM patterns")
    con.execute("DELETE FROM pattern_stops")

    con.execute("""
        INSERT INTO pattern_stops
        SELECT pattern_id, seq::INTEGER - 1, stop_id
        FROM pattern_raw, unnest(stop_seq) WITH ORDINALITY AS u(stop_id, seq)
    """)

    con.execute("""
        INSERT INTO patterns
        SELECT p.pattern_id, p.route_id, r.agency_id, r.short_name, p.direction,
               p.shape_id, len(p.stop_seq), p.n_trips, 0.0
        FROM pattern_raw p
        LEFT JOIN routes r ON r.route_id = p.route_id
    """)

    _fill_span(con)
    _load_shapes(gtfs_dir, con)
    db.index(con)

    n_patterns, n_with_shape, n_refs = db.row(
        con,
        """
        SELECT count(*),
               sum(CASE WHEN shape_id IS NOT NULL THEN 1 ELSE 0 END),
               sum(n_stops)
        FROM patterns
        """,
    )
    log.info(
        "%d patterns from %d trips (%.1f%% with operator geometry), %d stop references",
        n_patterns,
        n_trips,
        100.0 * (n_with_shape or 0) / max(n_patterns, 1),
        n_refs or 0,
    )


def _fill_span(con: duckdb.DuckDBPyConnection) -> None:
    """Straight-line length of each stop chain.

    Used two ways: to skip patterns whose stops are implausibly far apart, and as
    the denominator for the detour check that catches a matcher that has wandered
    onto the wrong roads.
    """
    log.info("computing stop-chain lengths")
    dist = _HAVERSINE.format(lat1="a.lat", lon1="a.lon", lat2="b.lat", lon2="b.lon")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE span AS
        SELECT ps.pattern_id,
               sum({dist}) AS span_m,
               max({dist}) AS max_gap_m
        FROM pattern_stops ps
        JOIN pattern_stops ps2
          ON ps2.pattern_id = ps.pattern_id AND ps2.seq = ps.seq + 1
        JOIN stops a ON a.stop_id = ps.stop_id
        JOIN stops b ON b.stop_id = ps2.stop_id
        GROUP BY ps.pattern_id
    """)
    con.execute("""
        UPDATE patterns SET span_m = (
            SELECT span_m FROM span WHERE span.pattern_id = patterns.pattern_id
        )
    """)


def _load_shapes(gtfs_dir: Path, con: duckdb.DuckDBPyConnection) -> None:
    """Keep only the shapes some pattern actually refers to.

    shapes.txt is 2.5 GB nationally, but it is written per shape_id and many are
    orphaned once trips collapse into patterns.
    """
    path = gtfs_dir / "shapes.txt"
    if not path.exists():
        log.warning("no shapes.txt in feed; every pattern will match from stops")
        return
    log.info("loading referenced shapes")
    con.execute("DELETE FROM shapes")
    con.execute(f"""
        INSERT INTO shapes
        SELECT s.shape_id,
               TRY_CAST(s.shape_pt_sequence AS INTEGER),
               TRY_CAST(s.shape_pt_lat AS DOUBLE),
               TRY_CAST(s.shape_pt_lon AS DOUBLE)
        FROM {_csv(gtfs_dir, "shapes.txt")} s
        WHERE s.shape_id IN (SELECT DISTINCT shape_id FROM patterns
                             WHERE shape_id IS NOT NULL)
    """)
    log.info(
        "%d referenced shapes",
        db.scalar(con, "SELECT count(DISTINCT shape_id) FROM shapes"),
    )
