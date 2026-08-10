"""Reduce the GTFS timetable to distinct route patterns.

This is the stage that makes the rest affordable. The national feed holds about
1.55M trips, but the vast majority are the same physical journey repeated through
the day. Grouping trips by their ordered stop sequence collapses that to a far
smaller set of *patterns*, and a pattern is what we pay the map matcher for.

Which modes get this far is a choice, and it defaults to road only. A feed carries
ferries, trains and trams as well as buses; keeping one is worthwhile where the
operator supplies its geometry, since a tram with a trace needs no matcher at all.
What must never happen is handing one to the matcher -- a sea crossing snapped by
`bus` costing either fails outright or lands on the nearest coast road, which is
worse. Selecting a mode is ``_drop_unselected_modes``; refusing to match it is
``db.matchable``, and they are deliberately separate decisions.

The work is done in DuckDB rather than Python because ``stop_times.txt`` is 5.1 GB
and the central operation is a group-by over every row of it. DuckDB does that out
of core; a Python dict of 46M stop references does not fit comfortably in RAM.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb

from . import config, db, logs

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


def _kept_modes_sql(col: str, kept: frozenset[int]) -> str:
    """Predicate for a route_type this run was asked to keep.

    The values are integers derived from ``config.MODES`` and nothing else reaches
    the SQL, so the list is a closed vocabulary in the same sense the art query spec
    is.

    TRY_CAST rather than a string comparison because route_type is an enum, not an
    id: "03" and "3" mean the same mode, where route "07" and route "7" are two
    different buses. COALESCE is what makes the *complement* usable -- a trip whose
    route is missing from routes.txt has no mode at all, and ``NOT (NULL IN (...))``
    is NULL, so without it the rows this is meant to catch would be kept.
    """
    if not kept:
        return "false"
    keep = ", ".join(str(t) for t in sorted(kept))
    return f"COALESCE(TRY_CAST({col} AS INTEGER) IN ({keep}), false)"


def _mode_case_sql(col: str) -> str:
    """SQL mapping a route_type to its ``config.MODES`` name, NULL if unknown.

    Built from ``config.MODE_OF_TYPE`` so the vocabulary has one definition. A type
    outside it yields NULL, which is the same thing a missing route yields, and both
    are reported rather than guessed at.
    """
    whens = " ".join(
        f"WHEN {t} THEN '{name}'" for t, name in sorted(config.MODE_OF_TYPE.items())
    )
    return f"CASE TRY_CAST({col} AS INTEGER) {whens} ELSE NULL END"


def _mode_name(route_type: str | None) -> str | None:
    """What a dropped route_type is called, or None if this file cannot say.

    None is the signal that the log line should be a warning: a mode nobody here
    has heard of, or a trip whose route has no row in routes.txt at all.
    """
    if route_type is None:
        return None
    try:
        return config.ROUTE_TYPE_NAMES.get(int(route_type))
    except ValueError:
        return None


def _drop_unselected_modes(con: duckdb.DuckDBPyConnection, modes: frozenset[str]) -> int:
    """Remove trips whose mode this run was not asked for, and say exactly what went.

    Ferries are the reason this exists. Every one of the 52 Valhalla-444 errors in
    the completed GB run was a two-stop sea crossing on the `shape` path, and the
    same crossings routed from bare stops were 63% of all low-confidence trip
    weight -- a matcher being asked to snap water to tarmac. Rail and tram are the
    same problem waiting: the Irish feed carries 2,847 Irish Rail and 2,655 LUAS
    trips, which `bus` costing would happily route down the nearest street.

    Keeping a mode is now a choice rather than a fact about the pipeline, because a
    tram that arrives with an operator trace needs no matcher at all and should be
    drawn rather than deleted. What stops it reaching Valhalla is `db.matchable`,
    not this function -- the two are deliberately separate, so selecting a mode and
    map-matching it are different decisions.

    Nothing here is silent. Every dropped mode is reported with its route and trip
    counts, and a type this file does not recognise is a warning rather than a
    quiet omission, because the way this goes wrong is a future feed publishing
    something road-going in a range nobody thought to keep.
    """
    keep = _kept_modes_sql("route_type", config.route_types(modes))
    dropped = con.execute(f"""
        SELECT nullif(trim(route_type), '') AS rt,
               count(DISTINCT route_id)     AS n_routes,
               count(*)                     AS n_trips
        FROM trip WHERE NOT ({keep})
        GROUP BY rt ORDER BY n_trips DESC, rt
    """).fetchall()
    if not dropped:
        return 0

    for rt, n_routes, n_trips in dropped:
        name = _mode_name(rt)
        (log.info if name else log.warning)(
            "dropping route_type %s (%s): %d routes, %d trips",
            rt if rt is not None else "missing",
            name or "unrecognised",
            n_routes,
            n_trips,
        )
    con.execute(f"DELETE FROM trip WHERE NOT ({keep})")

    # A feed of nothing but ferries is not a thing BODS or the NTA publishes, so
    # this means the join to routes failed rather than that the timetable is all
    # water -- and a run that quietly produces no patterns at all is the worst of
    # the failure modes this stage has.
    if not db.scalar(con, "SELECT count(*) FROM trip"):
        raise RuntimeError(
            "every trip was dropped as an unselected mode; routes.txt either carries "
            f"no route_type in the selected modes ({', '.join(sorted(modes))}) or does "
            "not join to trips.txt"
        )
    return int(sum(r[2] for r in dropped))


def _report_kept_modes(con: duckdb.DuckDBPyConnection) -> None:
    """Say what survived the filter, one line per mode.

    The complement of the dropping lines above. Without it a run reports only what
    it threw away, so a feed that quietly stops publishing its trams looks the same
    as one that never had any -- and now that keeping a mode is a choice, the thing
    worth checking is what came in rather than what went.
    """
    for mode, n_routes, n_trips in con.execute("""
        SELECT COALESCE(mode, '(unrecognised)'),
               count(DISTINCT route_id), count(*)
        FROM trip GROUP BY 1 ORDER BY 3 DESC, 1
    """).fetchall():
        log.info("keeping %s: %d routes, %d trips", mode, n_routes, n_trips)


def stored_modes(con: duckdb.DuckDBPyConnection) -> frozenset[str]:
    """The mode selection this database was last built with, or the default.

    `patterns` rebuilds the table from whatever selection it is handed, so without
    this a bare `wayfare patterns` -- which is exactly what `deploy/refresh.sh` runs,
    every month, unattended -- would quietly narrow a multi-modal database back to
    road on its next refresh. Every tram would disappear from the archive and every
    other number the run reports would look healthy while it happened.

    An unrecognised stored name is left to raise in `config.route_types` rather than
    being dropped. Renaming a mode is a schema change, and a schema change should
    break the timer and be dealt with by a person.
    """
    stored = db.get_meta(con, "modes")
    if not stored:
        return config.DEFAULT_MODES
    return frozenset(m.strip() for m in stored.split(",") if m.strip())


def build_patterns(
    gtfs_dir: Path,
    con: duckdb.DuckDBPyConnection,
    memory_limit: str | None = None,
    feed_version: str | None = None,
    upgrade_shapes: bool = False,
    modes: frozenset[str] | None = None,
) -> None:
    from . import acquire

    kept_modes = modes if modes is not None else stored_modes(con)
    config.route_types(kept_modes)  # reject a bad name before any work is done
    feed = feed_version or acquire.feed_version(gtfs_dir)
    limit = memory_limit or os.environ.get("WAYFARE_MEM", "8GB")
    con.execute(f"SET memory_limit = '{limit}'")
    con.execute(f"SET temp_directory = '{gtfs_dir.parent / 'duckdb_tmp'}'")
    con.execute("SET preserve_insertion_order = false")

    log.info("loading stops, routes, trips")
    # A latitude of exactly zero is the equator, and no feed this pipeline reads
    # runs a bus there. It is worth a clause of its own because it is not a null:
    # Translink's own stop list has shipped 0.0 in both columns, which satisfies
    # every IS NOT NULL test and then drags a pattern's span across two continents.
    con.execute(f"""
        INSERT OR REPLACE INTO stops
        SELECT stop_id, stop_name,
               TRY_CAST(stop_lat AS DOUBLE), TRY_CAST(stop_lon AS DOUBLE)
        FROM {_csv(gtfs_dir, "stops.txt")}
        WHERE TRY_CAST(stop_lat AS DOUBLE) IS NOT NULL
          AND TRY_CAST(stop_lat AS DOUBLE) <> 0
    """)
    con.execute(f"""
        INSERT OR REPLACE INTO routes
        SELECT route_id, agency_id, route_short_name, route_long_name,
               nullif(trim(route_type), '')
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
               COALESCE(s.days_per_week, 5) AS days_per_week,
               r.route_type,
               {_mode_case_sql("r.route_type")} AS mode
        FROM {_csv(gtfs_dir, "trips.txt")} t
        LEFT JOIN service_days s USING (service_id)
        LEFT JOIN routes r USING (route_id)
    """)
    n_dropped = _drop_unselected_modes(con, kept_modes)
    n_trips = db.scalar(con, "SELECT count(*) FROM trip")
    log.info(
        "%d trips on the selected modes (%s), %d on other modes dropped",
        n_trips,
        ", ".join(sorted(kept_modes)),
        n_dropped,
    )
    _report_kept_modes(con)

    _collapse_to_sequences(gtfs_dir, con)

    log.info("deduplicating to patterns")
    pattern_id = db.pattern_id_sql(
        "t.route_id", "t.direction", "array_to_string(ts.stop_seq, '>')"
    )
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE pattern_raw AS
        SELECT {pattern_id}                   AS pattern_id,
               t.route_id,
               t.direction,
               ts.stop_seq,
               count(*)                       AS n_trips_raw,
               sum(t.days_per_week)           AS n_trips,
               -- Where only some trips on a pattern carry operator geometry, take
               -- the most common shape_id rather than an arbitrary one. This is
               -- also what covers Tyne and Wear Metro and Metrolink, which are
               -- 75.2% and 0.8% shapeless by trip and 0% by pattern: every one of
               -- their patterns has a shaped trip to inherit from. Quote pattern
               -- coverage rather than trip coverage, or the gap looks far worse.
               mode(t.shape_id) FILTER (WHERE t.shape_id IS NOT NULL
                                        AND t.shape_id <> '') AS shape_id,
               -- Grouped rather than aggregated: mode is a function of route_id,
               -- which is already in the key, so this cannot split a pattern. It is
               -- `t.mode` and never a bare `mode`, which is the aggregate above.
               t.mode                         AS mode
        FROM trip t
        JOIN trip_seq ts USING (trip_id)
        GROUP BY t.route_id, t.direction, ts.stop_seq, t.mode
    """)
    _check_unique_ids(con)

    # pattern_stops is derived and cheap, so it is rebuilt outright and holds only
    # the current feed. patterns is not: match_status and pattern_edges hang off
    # it, so rows are merged in and departed patterns are left behind rather than
    # deleted.
    con.execute("DELETE FROM pattern_stops")
    con.execute("""
        INSERT INTO pattern_stops
        SELECT pattern_id, seq::INTEGER - 1, stop_id
        FROM pattern_raw, unnest(stop_seq) WITH ORDINALITY AS u(stop_id, seq)
    """)

    con.execute(
        """
        -- Columns are named rather than positional. They used to be positional,
        -- which meant adding one to the middle of the table silently shifted every
        -- value after it into the wrong column.
        INSERT INTO patterns (
            pattern_id, route_id, agency_id, short_name, direction, shape_id,
            n_stops, n_trips, span_m, mode, first_seen, last_seen
        )
        SELECT p.pattern_id, p.route_id, r.agency_id, r.short_name, p.direction,
               p.shape_id, len(p.stop_seq), p.n_trips, 0.0, p.mode,
               ?::VARCHAR, ?::VARCHAR
        FROM pattern_raw p
        LEFT JOIN routes r ON r.route_id = p.route_id
        ON CONFLICT (pattern_id) DO UPDATE SET
            -- route_id, direction and the stop sequence are the identity and
            -- cannot have changed. Everything else is this month's figure.
            agency_id  = excluded.agency_id,
            short_name = excluded.short_name,
            shape_id   = excluded.shape_id,
            n_trips    = excluded.n_trips,
            -- Mode is not in the identity, so a route that changes mode between
            -- feeds keeps its pattern_id and therefore its cached match_status.
            -- Recording the new mode is still right; what it does not do is
            -- re-match, and edges matched as a bus would survive the change. No
            -- feed has been seen doing this -- see the guard in `patterns`.
            mode       = excluded.mode,
            last_seen  = excluded.last_seen
        """,
        [feed, feed],
    )
    # Written only once the merge has succeeded. Every other stage reads this to
    # decide which patterns are live, so a crash part-way through leaves the
    # previous feed's dataset intact and usable rather than half-replaced.
    db.set_meta(con, "feed_version", feed)
    # Beside it, and for the same reason: the next run has to know what this one
    # chose, or it reverts to the default and takes the other modes out with it.
    db.set_meta(con, "modes", ",".join(sorted(kept_modes)))

    _fill_span(con)
    _load_shapes(gtfs_dir, con)
    db.index(con)
    _report_churn(con, feed)
    _upgraded_to_shapes(con, apply=upgrade_shapes)

    n_patterns, n_with_shape, n_refs = db.row(
        con,
        f"""
        SELECT count(*),
               sum(CASE WHEN shape_id IS NOT NULL THEN 1 ELSE 0 END),
               sum(n_stops)
        FROM patterns p WHERE {db.current_feed()}
        """,
    )
    log.info(
        "%d patterns from %d trips (%.1f%% with operator geometry), %d stop references",
        n_patterns,
        n_trips,
        100.0 * (n_with_shape or 0) / max(n_patterns, 1),
        n_refs or 0,
    )


def _check_unique_ids(con: duckdb.DuckDBPyConnection) -> None:
    """Refuse to build on a hash collision rather than merging two patterns.

    Two distinct journeys sharing an id would have their edges silently pooled and
    one of them would never be matched. The odds are around one in a million at
    national scale, which over a monthly rebuild is not small enough to assume
    away.
    """
    n, distinct = db.row(
        con, "SELECT count(*), count(DISTINCT pattern_id) FROM pattern_raw"
    )
    if n != distinct:
        raise RuntimeError(
            f"{n - distinct} pattern id collisions across {n} patterns; "
            "the identity hash needs widening before this feed can be built"
        )


def _report_churn(con: duckdb.DuckDBPyConnection, feed: str) -> None:
    """How much of the timetable actually changed since the last feed.

    This is the number the incremental rebuild lives or dies on: only patterns new
    to this feed cost a Valhalla call. Printed every run so the monthly cost is
    visible rather than inferred.
    """
    new, returning, departed = db.row(
        con,
        """
        SELECT
          (SELECT count(*) FROM patterns WHERE last_seen = ? AND first_seen = ?),
          (SELECT count(*) FROM patterns p WHERE p.last_seen = ? AND p.first_seen <> ?
             AND NOT EXISTS (SELECT 1 FROM match_status m
                             WHERE m.pattern_id = p.pattern_id)),
          (SELECT count(*) FROM patterns WHERE last_seen <> ?)
        """,
        [feed, feed, feed, feed, feed],
    )
    log.info(
        "feed %s: %d new patterns, %d carried over still unmatched, %d departed",
        feed,
        new,
        returning,
        departed,
    )


def _upgraded_to_shapes(con: duckdb.DuckDBPyConnection, apply: bool) -> int:
    """Patterns matched from bare stops that now carry operator geometry.

    An operator switching on TrackPoints turns a guess about which roads the bus
    takes into an observation of it, so these are worth re-matching -- but they are
    a quality improvement, not a correctness fix, and re-matching them adds work to
    a queue that is meant to be predictable. So the count is always reported and
    the clearing is opt-in.
    """
    ids = [
        r[0]
        for r in con.execute(f"""
            SELECT p.pattern_id
            FROM patterns p JOIN match_status m USING (pattern_id)
            WHERE {db.current_feed()}
              AND m.source = 'stops' AND p.shape_id IS NOT NULL
        """).fetchall()
    ]
    if not ids:
        return 0
    if not apply:
        log.info(
            "%d matched patterns have gained operator geometry; "
            "re-run with --upgrade-shapes to redo them",
            len(ids),
        )
        return len(ids)
    con.execute("DELETE FROM pattern_edges WHERE pattern_id IN (SELECT unnest(?))", [ids])
    con.execute("DELETE FROM match_status WHERE pattern_id IN (SELECT unnest(?))", [ids])
    log.info("cleared %d patterns that gained operator geometry", len(ids))
    return len(ids)


def _collapse_to_sequences(gtfs_dir: Path, con: duckdb.DuckDBPyConnection) -> None:
    """Reduce stop_times to one ordered stop list per trip.

    This is the expensive pass, and the one place the stage runs out of memory.

    ``list(stop_id ORDER BY stop_sequence)`` is the natural way to write it, but an
    ordered aggregate holds its per-group sort state pinned, so DuckDB cannot spill
    it and the buffer manager fails however high the limit is set. Measured on
    London: 480,412 trips over a 1.5 GB stop_times.txt died identically at a 7.4 GB
    limit and at 10.2 GB. Raising the limit is not a fix, it just moves the wall.

    So bound the state instead. Project the three columns that matter into a table
    once -- a streaming scan that spills cleanly -- then aggregate it in partitions
    of trip_id, so at most 1/N of the groups are in flight at a time. The partition
    key is a hash of trip_id, which keeps every row of a trip in the same pass.
    """
    log.info("projecting stop_times (one pass over the CSV)")
    con.execute(f"""
        CREATE OR REPLACE TABLE stop_time_raw AS
        SELECT trip_id, stop_id, TRY_CAST(stop_sequence AS INTEGER) AS seq
        FROM {_csv(gtfs_dir, "stop_times.txt")}
    """)
    n_rows = db.scalar(con, "SELECT count(*) FROM stop_time_raw")

    con.execute("""
        CREATE OR REPLACE TEMP TABLE trip_seq (trip_id VARCHAR, stop_seq VARCHAR[])
    """)
    log.info(
        "collapsing %d stop times to sequences in %d partitions",
        n_rows,
        config.SEQ_PARTITIONS,
    )
    for k in range(config.SEQ_PARTITIONS):
        con.execute(
            f"""
            INSERT INTO trip_seq
            SELECT trip_id, list(stop_id ORDER BY seq)
            FROM stop_time_raw
            WHERE hash(trip_id) % {config.SEQ_PARTITIONS} = ?
            GROUP BY trip_id
            """,
            [k],
        )

    con.execute("DROP TABLE stop_time_raw")


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
    # Only patterns in this feed have rows in span, and a departed pattern's span
    # must not be nulled: it is the denominator of the detour check that its
    # cached match result was accepted on.
    con.execute("""
        UPDATE patterns SET span_m = (
            SELECT span_m FROM span WHERE span.pattern_id = patterns.pattern_id
        )
        WHERE EXISTS (SELECT 1 FROM span WHERE span.pattern_id = patterns.pattern_id)
    """)


def _load_shapes(gtfs_dir: Path, con: duckdb.DuckDBPyConnection) -> None:
    """Keep only the shapes some pattern actually refers to, one row per shape.

    shapes.txt is 2.5 GB nationally, but it is written per shape_id and many are
    orphaned once trips collapse into patterns.

    The aggregation happens here rather than on read because nothing downstream
    wants a single point of a shape -- the matcher takes the whole trace. Collapsing
    at load turns roughly 100M national rows into roughly 750k, and the group-by
    costs one pass that the CSV read is already paying for.
    """
    path = gtfs_dir / "shapes.txt"
    if not path.exists():
        log.warning("no shapes.txt in feed; every pattern will match from stops")
        return
    log.info("loading referenced shapes")
    con.execute("DELETE FROM shapes")
    con.execute(f"""
        INSERT INTO shapes
        SELECT shape_id,
               list(lat_e6 ORDER BY seq),
               list(lon_e6 ORDER BY seq)
        FROM (
            SELECT s.shape_id,
                   TRY_CAST(s.shape_pt_sequence AS INTEGER)               AS seq,
                   round(TRY_CAST(s.shape_pt_lat AS DOUBLE) * 1e6)::INTEGER AS lat_e6,
                   round(TRY_CAST(s.shape_pt_lon AS DOUBLE) * 1e6)::INTEGER AS lon_e6
            FROM {_csv(gtfs_dir, "shapes.txt")} s
            WHERE s.shape_id IN (SELECT DISTINCT p.shape_id FROM patterns p
                                 WHERE p.shape_id IS NOT NULL
                                   AND {db.current_feed()})
        )
        WHERE lat_e6 IS NOT NULL AND lon_e6 IS NOT NULL
        GROUP BY shape_id
    """)
    log.info("%d referenced shapes", db.scalar(con, "SELECT count(*) FROM shapes"))
