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

# The patterns the track layer answers for, and so the ones `segments` must leave
# alone. One expression rather than two, because the two arms have to partition:
# written twice they can drift, and a pattern in both layers is the same track
# painted over itself while a pattern in neither is a line that vanishes.
#
# The question is what `traces.way_ids` means for the row. Cut to this pattern, the
# inversion is sound and the track layer draws it per way; the whole line's chain,
# and inverting would attribute a short working to track it never reaches, so it
# stays a polyline of its own. A trace this pipeline has not rewritten since the
# tracer learned to cut is the second kind, and COALESCE is what reads a column
# added to rows that predate it.
_DRAWN_AS_TRACK = "COALESCE(t.ways_cut, FALSE)"


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

    **A pattern the track layer draws must not be drawn here as well**, which is
    what `_DRAWN_AS_TRACK` partitions. Every relation-built pattern was drawn in
    both, from the day `osmroutes` landed until the per-way inversion followed it:
    an `osm:r` pattern is live, not matchable and carries no `shape_id`, so this arm
    took it, and each one came out as a polyline lying over the very ways
    `build_track_services` had just collapsed it into. Two coats of the same track
    is the visible half of that; the hover is the worse half, because the viewer
    asks the segments layer first and got one relation's own card where the track
    layer would have answered for every service on the way.

    What is left here is the operator's own recording plus the traces this pipeline
    has not re-cut yet, and both of those are per pattern by nature.

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
              AND NOT {_DRAWN_AS_TRACK}
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
                  AND p.shape_id IS NULL
                  AND NOT {_DRAWN_AS_TRACK}),
               -- No `_DRAWN_AS_TRACK` on this one and none is wanted: a pattern
               -- with no trace at all is drawn by neither layer, which is what
               -- this counts.
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
        # Negated through COALESCE, because `osmroutes` withdraws a rail pattern by
        # setting `last_seen` to NULL: `live` is then NULL rather than false, and
        # `NOT NULL` is not true, so every retired relation would leave the count
        # while still being outside the current feed.
        "patterns_departed": db.scalar(
            con, f"SELECT count(*) FROM patterns p WHERE NOT COALESCE({live}, FALSE)"
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
    `deploy/refresh.sh` gates a publish on, and stop a scheduled region publishing
    again for good.

    A database with no `trace_status` table reports nothing rather than raising.
    `status` connects read-only and `connect` runs `migrate` only when it is not, so
    the table may be absent from a data root this stage has never written to -- the
    same trap `db.matchable` carries a connection to survive.
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

    **Only a trace whose `way_ids` are cut to its own pattern is inverted**, which
    is what `_DRAWN_AS_TRACK` is doing. A trace written before `trace` learned to
    cut holds the whole line's chain, so a Northern line short working from Edgware
    to Kennington is stored against every way of the Northern line -- harmless while
    the column only documented what was drawn, and a confident lie once a way is
    asked which services use it. Those rows keep being drawn per pattern in
    `segments` until the tracer runs over them again, which is the one place the two
    layers divide anything other than by source.

    Both kinds of pattern are inverted now. An `osmroutes` pattern is its whole
    relation, so its ways are cut by construction; a timetable's pattern is cut by
    `osm.ways_between` at the same distances `osm.slice_between` cuts its geometry.
    What that buys is the Underground: 1,040 metro patterns over eleven lines were
    1,040 coincident polylines, and a way of the Victoria line now carries its
    service list the way a road carries its buses.

    `mode` is in the key because the layer carries several modes and a way is
    painted by its mode. It also keeps two networks over one alignment apart --
    the Elizabeth line and the National Rail service beside it are two rows, drawn
    twice, which is the one coincidence worth keeping.

    `n_trips` sums to NULL rather than to zero where no timetable has been
    attributed, because zero trips a week and an unknown number of trips a week are
    different claims and only one of them is true. Where there is a timetable it
    sums over distinct patterns of one service, exactly as `edge_services` does.
    """
    con.execute("DELETE FROM track_services")
    con.execute(f"""
        -- Named columns, not positional. `mode` sits fourth in `SCHEMA` and lands
        -- last on a database that gained it by ALTER, so a positional insert writes
        -- the mode into `n_patterns` on exactly the databases that have been
        -- through a migration -- which is every one already carrying rail.
        INSERT INTO track_services
            (way_id, short_name, agency_id, mode, n_patterns, n_trips)
        SELECT way_id, short_name, agency_id, mode,
               count(DISTINCT pattern_id)                  AS n_patterns,
               CASE WHEN count(n_trips) = 0 THEN NULL
                    ELSE sum(n_trips) END                  AS n_trips
        FROM (
            -- The subquery is what gives the fallback a name GROUP BY can refer
            -- to without repeating the expression, exactly as `build` does above.
            --
            -- DISTINCT for `build`'s reason as well: a line that runs over one way
            -- twice -- a loop, a reversal at a terminus -- lists it twice, and
            -- summing trips over both would say the service runs twice as often
            -- there. Everything but `way_id` follows from `pattern_id`, so this is
            -- distinct pairs and nothing else.
            SELECT DISTINCT u.way_id, p.pattern_id, p.agency_id, p.n_trips, p.mode,
                   COALESCE(NULLIF(trim(p.short_name), ''), p.route_id) AS short_name
            FROM traces t
            JOIN patterns p USING (pattern_id)
            CROSS JOIN unnest(t.way_ids) AS u(way_id)
            WHERE {db.current_feed()} AND NOT {db.matchable()}
              AND {_DRAWN_AS_TRACK}
        )
        GROUP BY way_id, short_name, agency_id, mode
    """)
    n_ways, n_rows = db.row(
        con, "SELECT count(DISTINCT way_id), count(*) FROM track_services"
    )
    log.info("%d ways carry %d track-service pairs", n_ways, n_rows)
    return int(n_rows)
