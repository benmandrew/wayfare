"""DuckDB schema and connection.

One file holds the whole pipeline state. Each stage reads the previous stage's
tables and writes its own, so a stage can be re-run without repeating the ones
before it. DuckDB allows a single writer, so the matching loop keeps all writes on
the main thread and uses its workers only for HTTP.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from . import config, logs

SCHEMA = """
-- Stage 1: timetable, reduced to distinct route patterns ---------------------

CREATE TABLE IF NOT EXISTS stops (
    stop_id   VARCHAR PRIMARY KEY,
    name      VARCHAR,
    lat       DOUBLE,
    lon       DOUBLE
);

CREATE TABLE IF NOT EXISTS routes (
    route_id    VARCHAR PRIMARY KEY,
    agency_id   VARCHAR,
    short_name  VARCHAR,   -- the bus number the public sees; what we render
    long_name   VARCHAR,
    -- GTFS route_type, as text. Kept because it decides which routes enter the
    -- pipeline at all -- see config.ROAD_ROUTE_TYPES -- and because storing it is
    -- what lets a later run report on a mode it dropped rather than forget it.
    route_type  VARCHAR
);

-- A pattern is one distinct ordered stop sequence. 1.55M trips collapse to a far
-- smaller number of these, and the pattern is the unit we pay Valhalla for.
--
-- pattern_id is a hash of the identity itself -- route, direction and stop
-- sequence -- so the same physical journey keeps the same id across feed
-- versions. That is what makes an incremental rebuild possible: match_status is
-- keyed on it, so a pattern already matched last month is not paid for again.
-- It used to be a popularity rank, which changed under every feed.
--
-- first_seen/last_seen are feed versions. A pattern that leaves the timetable
-- keeps its row and its match results -- seasonal services come back, and when
-- they do they are free -- but only patterns whose last_seen is the loaded feed
-- are matched, aggregated or rendered.
CREATE TABLE IF NOT EXISTS patterns (
    pattern_id  BIGINT PRIMARY KEY,
    route_id    VARCHAR,
    agency_id   VARCHAR,
    short_name  VARCHAR,
    direction   INTEGER,
    shape_id    VARCHAR,   -- NULL for ~52% of trips; see docs/data.md
    n_stops     INTEGER,
    n_trips     INTEGER,   -- how many timetabled trips use this pattern
    span_m      DOUBLE,    -- straight-line length of the stop chain
    -- config.MODES name, denormalised from routes.route_type. It decides how a
    -- pattern gets its geometry -- map-matched for road, drawn from an operator
    -- shape or an OSM relation otherwise -- so every stage after this one needs
    -- it, and a join back to `routes` for it at every read is how that gets
    -- forgotten. NULL means a database written before modes existed, where
    -- everything stored was road-going by construction.
    mode        VARCHAR,
    first_seen  VARCHAR,   -- feed version this pattern first appeared in
    last_seen   VARCHAR    -- feed version it was last present in
);

CREATE TABLE IF NOT EXISTS pattern_stops (
    pattern_id  BIGINT,
    seq         INTEGER,
    stop_id     VARCHAR
);

-- Road geometry supplied by the operator, where they bothered. Used as a better
-- input trace than bare stops, and as a validation set for the matcher.
--
-- One row per shape, not per point. A row per point is the shape shapes.txt
-- arrives in, but nothing ever reads a single point of a shape -- the matcher
-- wants the whole trace at once. Nationally shapes.txt is 2.53 GB, which is on
-- the order of 100M rows; held as a list per shape it is closer to 750k. The
-- coordinates are micro-degrees in INTEGER lists rather than DOUBLEs, which
-- DuckDB bit-packs; 1e-6 of a degree is 11 cm, far below the precision the
-- operator geometry actually carries.
CREATE TABLE IF NOT EXISTS shapes (
    shape_id  VARCHAR PRIMARY KEY,
    lat_e6    INTEGER[],
    lon_e6    INTEGER[]
);

-- Stage 2: map matching ------------------------------------------------------

-- One row per pattern, always. This is the checkpoint: the matcher selects
-- patterns with no row here, so killing the process loses at most one batch.
CREATE TABLE IF NOT EXISTS match_status (
    pattern_id  BIGINT PRIMARY KEY,
    -- ok | low_confidence | no_route | error | skipped | transport_error.
    -- All permanent but the last: transport_error means the request never got an
    -- answer, so nothing was learned about the pattern and it is safe to redo.
    status      VARCHAR,
    source      VARCHAR,   -- shape | stops
    confidence  DOUBLE,
    road_m      DOUBLE,
    detour      DOUBLE,    -- road_m / span_m
    n_edges     INTEGER,
    detail      VARCHAR,   -- error text, truncated
    matched_at  TIMESTAMP
);

-- Valhalla graph edges, deduplicated across every pattern that traverses them.
-- edge_id is a Valhalla GraphId: stable within one graph build, meaningless
-- across builds. way_id is the durable OSM identity.
-- Geometry is micro-degree INTEGER lists rather than WKT text: no parsing on read,
-- roughly a third of the bytes, and DuckDB bit-packs the lists. The bbox columns
-- are what make a window query exact and cheap -- see art.load_edges.
CREATE TABLE IF NOT EXISTS edges (
    edge_id     BIGINT PRIMARY KEY,
    way_id      BIGINT,
    road_name   VARCHAR,
    road_class  VARCHAR,
    length_m    DOUBLE,
    lon_e6      INTEGER[],
    lat_e6      INTEGER[],
    min_lon_e6  INTEGER,
    min_lat_e6  INTEGER,
    max_lon_e6  INTEGER,
    max_lat_e6  INTEGER
);

CREATE TABLE IF NOT EXISTS pattern_edges (
    pattern_id  BIGINT,
    seq         INTEGER,
    edge_id     BIGINT
);

-- Stage 3: the rendering dataset ---------------------------------------------

CREATE TABLE IF NOT EXISTS edge_services (
    edge_id     BIGINT,
    short_name  VARCHAR,
    agency_id   VARCHAR,
    n_patterns  INTEGER,
    n_trips     INTEGER    -- timetabled trips per week over this edge
);

-- What a non-road pattern is drawn from, when nothing matched it to a road.
--
-- A tram, metro or ferry has no Valhalla edge and no OSM way id: its geometry is
-- the operator's own trace, copied here from `shapes` so that publishing reads one
-- table rather than reassembling a join, and so a later pruning of `shapes` cannot
-- take the picture with it.
--
-- Keyed on pattern_id and nothing else, because there is exactly one trace per
-- pattern. That is the whole reason this table can exist without inventing an
-- identity space: `edges.edge_id` is a Valhalla GraphId and minting synthetic
-- values into it would collide with the one invariant the pipeline rests on, where
-- `pattern_id` is already an identity this codebase owns.
--
-- The consequence, accepted deliberately: geometry is per pattern rather than
-- shared, so two trams over one street are two coincident lines and there is no
-- edge->services inversion to ask "which services use this track". Getting that
-- back means snapping these traces to OSM way ids, which is a separate decision.
CREATE TABLE IF NOT EXISTS segments (
    pattern_id  BIGINT PRIMARY KEY,
    mode        VARCHAR,
    -- Micro-degree integer lists and a bbox, exactly as `edges` stores geometry,
    -- so a window test is an integer overlap and nothing has to parse WKT.
    lon_e6      INTEGER[],
    lat_e6      INTEGER[],
    min_lon_e6  INTEGER,
    min_lat_e6  INTEGER,
    max_lon_e6  INTEGER,
    max_lat_e6  INTEGER
);

-- Stage 2b: track drawn from OpenStreetMap route relations --------------------

-- One row per non-road pattern the tracer has considered, always, exactly as
-- `match_status` is one row per matchable pattern. This is the checkpoint and the
-- cache: work is selected by the *absence* of a row here, so a relation that does
-- not resolve is recorded rather than re-fetched every run.
--
-- The statuses are permanent but the last, for the reason `match_status`'s are:
--   ok             the relation chained and its stops matched the pattern's
--   no_relation    nothing in the fetched set carries this pattern's stop sequence
--   chain_break    the relation's ways do not form one continuous path
--   no_stop_match  a relation matched by mode and area, but not stop for stop
--   not_monotonic  the stops matched but project out of order along the chain,
--                  which is what a loop or a doubled-back relation does; the
--                  geometry would be a confident line down the wrong branch
--   skipped        fewer than two stops, so there is nothing to fit
--   error          a bug or a malformed response; permanent until the code changes
--   transport_error  the request never got an answer, so nothing was learned
CREATE TABLE IF NOT EXISTS trace_status (
    pattern_id   BIGINT PRIMARY KEY,
    status       VARCHAR,
    relation_id  BIGINT,   -- the OSM relation it resolved to, where one did
    osm_route    VARCHAR,  -- the relation's `route` tag: subway | train | tram | ...
    n_ways       INTEGER,
    n_stops      INTEGER,  -- pattern stops matched to relation stop nodes
    worst_stop_m DOUBLE,   -- furthest a matched stop sits from its OSM node
    length_m     DOUBLE,
    detail       VARCHAR,
    traced_at    TIMESTAMP
);

-- The geometry that came back, for the patterns where it did.
--
-- Separate from `edges` and deliberately so. These way ids are OpenStreetMap's and
-- are durable, but they carry no Valhalla GraphId -- nothing routed them -- and
-- `edges.edge_id` is that GraphId and the primary key. Minting synthetic values
-- into it would break the one invariant the pipeline rests on. Keyed on pattern_id
-- for the same reason `segments` is: there is exactly one path per pattern.
--
-- `way_ids` is kept although nothing draws it. It is the durable identity of what
-- was drawn, it is what a later decision to invert relation track into
-- edge->services would build on, and it is the evidence for the ODbL credit this
-- table makes the archive owe.
CREATE TABLE IF NOT EXISTS traces (
    pattern_id  BIGINT PRIMARY KEY,
    relation_id BIGINT,
    way_ids     BIGINT[],
    -- Micro-degree integer lists, exactly as `edges` and `segments` store geometry.
    lon_e6      INTEGER[],
    lat_e6      INTEGER[]
);

-- Free-form key/value for provenance: feed version, OSM extract date, the Valhalla
-- graph build id that edge_id values belong to.
CREATE TABLE IF NOT EXISTS meta (
    key    VARCHAR PRIMARY KEY,
    value  VARCHAR
);
"""

# Created after bulk load, not before -- DuckDB inserts are much faster without
# them and every stage here is a bulk write.
INDICES = [
    "CREATE INDEX IF NOT EXISTS pattern_stops_pid ON pattern_stops (pattern_id)",
    "CREATE INDEX IF NOT EXISTS pattern_edges_pid ON pattern_edges (pattern_id)",
    "CREATE INDEX IF NOT EXISTS pattern_edges_eid ON pattern_edges (edge_id)",
    "CREATE INDEX IF NOT EXISTS edge_services_eid ON edge_services (edge_id)",
]


def pattern_id_sql(route_id: str, direction: str, stop_key: str) -> str:
    """SQL for a pattern's identity hash, from expressions for its three parts.

    The identity of a pattern is exactly what defines it: the route it belongs to,
    the direction it runs in, and the ordered stops it calls at. Nothing that
    varies between feeds -- trip counts, shape ids, operator -- may enter it, or
    the id moves and the match cache misses.

    ``stop_key`` is the stop sequence already joined into one string. The two call
    sites reach it differently (a list column when patterns are built, a group-by
    over pattern_stops when an old database is migrated), so it is a parameter.

    hash() is a 64-bit UBIGINT; shifting right by one keeps it inside a signed
    BIGINT without a cast that could overflow. The lost bit doubles the collision
    rate, which at a few million patterns is still around one in a million -- and
    build_patterns asserts uniqueness rather than trusting the arithmetic.
    """
    return (
        f"(hash({route_id} || '|' || COALESCE(({direction})::VARCHAR, '') "
        f"|| '|' || {stop_key}) >> 1)::BIGINT"
    )


def current_feed(alias: str = "p") -> str:
    """Predicate restricting `patterns` to the feed version currently loaded.

    Departed patterns keep their rows so that their match results stay cached, so
    every consumer of `patterns` has to say which ones it means. Written against
    meta rather than a parameter so the answer cannot drift between stages.
    """
    return f"{alias}.last_seen = (SELECT value FROM meta WHERE key = 'feed_version')"


def matchable(alias: str = "p", con: duckdb.DuckDBPyConnection | None = None) -> str:
    """Predicate restricting `patterns` to the modes Valhalla can be asked about.

    `patterns` may now hold trams, ferries and metros, which have no road under them
    and must never reach the matcher -- a sea crossing handed to `bus` costing either
    fails outright or snaps to the nearest coast road, which is worse. Their geometry
    comes from an operator shape or an OpenStreetMap relation instead.

    A NULL mode is matchable. That is not a default, it is what an older database
    means: before `patterns.mode` existed the loader kept road modes and deleted
    everything else, so every row already stored is road-going by construction. The
    migration therefore leaves the column empty rather than asserting a mode nobody
    recorded, and this predicate is what makes that safe.

    Pass `con` from anywhere that reads a database it has not written to, and a
    database with no `mode` column at all gets the same answer for the same reason.
    `connect` runs `migrate` only when it is not read-only, so a data root that has not
    been opened for writing since the column landed still has the old schema -- Great
    Britain's had, three days later -- and every read-only consumer of this predicate
    failed to bind against it rather than degrading.
    """
    if con is not None and "mode" not in columns(con, "patterns"):
        return "TRUE"
    keep = ", ".join(f"'{m}'" for m in sorted(config.ROAD_MODES))
    return f"({alias}.mode IS NULL OR {alias}.mode IN ({keep}))"


def connect(path: Path | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    p = path or config.DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(p), read_only=read_only)
    if not read_only:
        con.execute(SCHEMA)
        migrate(con)
    return con


def index(con: duckdb.DuckDBPyConnection) -> None:
    for stmt in INDICES:
        con.execute(stmt)


def table_exists(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    """Whether a table is in this database at all.

    `segments` post-dates Great Britain's database and `prune` reclaims tables once
    matching is done, so a data root that is missing one is a normal thing to be
    handed rather than a corrupt one.
    """
    row = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
        [table],
    ).fetchone()
    return bool(row and row[0])


def columns(con: duckdb.DuckDBPyConnection, table: str) -> set[str]:
    return {
        r[0]
        for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            [table],
        ).fetchall()
    }


def migrate(con: duckdb.DuckDBPyConnection) -> None:
    """Bring an older database up to the current schema, in place.

    A national ``match`` run costs a day or two, so a schema change that forced it
    to be redone would be a change nobody applies. Every migration here is derivable
    from what is already stored, so it is a rewrite rather than a re-run.
    """
    if "seq" in columns(con, "shapes"):
        # shapes was a row per point; collapse to a row per shape.
        con.execute("""
            CREATE OR REPLACE TABLE shapes_new AS
            SELECT shape_id,
                   list(round(lat * 1e6)::INTEGER ORDER BY seq) AS lat_e6,
                   list(round(lon * 1e6)::INTEGER ORDER BY seq) AS lon_e6
            FROM shapes
            WHERE lat IS NOT NULL AND lon IS NOT NULL
            GROUP BY shape_id
        """)
        con.execute("DROP TABLE shapes")
        con.execute("ALTER TABLE shapes_new RENAME TO shapes")
        log_rows = scalar(con, "SELECT count(*) FROM shapes")
        logs.get("db").info("migrated shapes to one row per shape (%d rows)", log_rows)

    if "geom" in columns(con, "edges"):
        # edges.geom was a WKT LINESTRING; split it into micro-degree lists and
        # precompute the bbox that the window query now filters on.
        con.execute("""
            CREATE OR REPLACE TABLE edges_new AS
            WITH pt AS (
                SELECT e.edge_id, u.i,
                       round(split_part(trim(u.p), ' ', 1)::DOUBLE * 1e6)::INTEGER
                           AS lon_e6,
                       round(split_part(trim(u.p), ' ', 2)::DOUBLE * 1e6)::INTEGER
                           AS lat_e6
                FROM edges e,
                     unnest(string_split(
                         substr(e.geom,
                                position('(' IN e.geom) + 1,
                                length(e.geom) - position('(' IN e.geom) - 1),
                         ',')) WITH ORDINALITY AS u(p, i)
                WHERE e.geom IS NOT NULL
            ), g AS (
                SELECT edge_id,
                       list(lon_e6 ORDER BY i) AS lon_e6, list(lat_e6 ORDER BY i) AS lat_e6,
                       min(lon_e6) AS min_lon_e6, min(lat_e6) AS min_lat_e6,
                       max(lon_e6) AS max_lon_e6, max(lat_e6) AS max_lat_e6
                FROM pt GROUP BY edge_id
            )
            SELECT e.edge_id, e.way_id, e.road_name, e.road_class, e.length_m,
                   g.lon_e6, g.lat_e6,
                   g.min_lon_e6, g.min_lat_e6, g.max_lon_e6, g.max_lat_e6
            FROM edges e LEFT JOIN g USING (edge_id)
        """)
        con.execute("DROP TABLE edges")
        con.execute("ALTER TABLE edges_new RENAME TO edges")
        con.execute("CREATE UNIQUE INDEX edges_pk ON edges (edge_id)")
        logs.get("db").info(
            "migrated %d edges from WKT to micro-degree lists",
            scalar(con, "SELECT count(*) FROM edges"),
        )

    if "last_seen" not in columns(con, "patterns"):
        _migrate_pattern_ids(con)

    if "route_type" not in columns(con, "routes"):
        # The mode filter's column. Nothing already stored can supply it -- the old
        # loader never read it -- so it is added empty and the next `patterns` run
        # fills it in from routes.txt. Until then it is NULL, which no query treats
        # as road-going, and no matched pattern is touched: a departed ferry keeps
        # its rows and its match_status exactly like any other departed pattern.
        con.execute("ALTER TABLE routes ADD COLUMN route_type VARCHAR")
        logs.get("db").info("added routes.route_type; run `wayfare patterns` to fill it")

    if "mode" not in columns(con, "patterns"):
        # Added empty for the same reason route_type was: nothing already stored can
        # supply it. It is left NULL rather than backfilled to 'bus', because a
        # database written before this column existed held road modes only -- that
        # was the whole point of the filter -- and `matchable` reads NULL as
        # matchable for exactly that reason. Backfilling would assert a mode the
        # feed never told us.
        con.execute("ALTER TABLE patterns ADD COLUMN mode VARCHAR")
        logs.get("db").info("added patterns.mode; run `wayfare patterns` to fill it")


def _migrate_pattern_ids(con: duckdb.DuckDBPyConnection) -> None:
    """Renumber patterns from popularity rank to identity hash, in place.

    pattern_id used to be ``row_number() OVER (ORDER BY count(*) DESC)`` -- a rank
    recomputed from scratch on every run, so the same journey got a different id
    whenever some other route gained a trip. Every match result is keyed on it, so
    a second run against an existing database would have silently re-pointed
    matched edges at the wrong patterns.

    The new id is derivable from what is already stored, so this is a rewrite
    rather than a re-run: the stop sequence that produced a pattern is still in
    pattern_stops, and hashing it recovers the id the pattern would get today. A
    national match run costs a day or two; it must survive this.
    """
    log = logs.get("db")
    if not scalar(con, "SELECT count(*) FROM patterns"):
        con.execute("ALTER TABLE patterns ADD COLUMN first_seen VARCHAR")
        con.execute("ALTER TABLE patterns ADD COLUMN last_seen VARCHAR")
        return

    feed = get_meta(con, "feed_version") or "unknown"
    new_id = pattern_id_sql("p.route_id", "p.direction", "k.stop_key")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE pattern_remap AS
        SELECT p.pattern_id AS old_id, {new_id} AS new_id
        FROM patterns p
        JOIN (
            SELECT pattern_id, array_to_string(list(stop_id ORDER BY seq), '>') AS stop_key
            FROM pattern_stops GROUP BY pattern_id
        ) k ON k.pattern_id = p.pattern_id
    """)

    n_patterns = scalar(con, "SELECT count(*) FROM patterns")
    n_mapped, n_distinct = row(
        con, "SELECT count(*), count(DISTINCT new_id) FROM pattern_remap"
    )
    # Both of these mean the rewrite would lose match work, and losing it silently
    # is far worse than refusing to open the database.
    if n_mapped != n_patterns:
        raise RuntimeError(
            f"cannot migrate pattern ids: {n_patterns - n_mapped} of {n_patterns} "
            "patterns have no rows in pattern_stops, so their identity cannot be "
            "recovered. Rebuild with `wayfare patterns`."
        )
    if n_distinct != n_mapped:
        raise RuntimeError(
            f"cannot migrate pattern ids: {n_mapped - n_distinct} hash collisions "
            f"across {n_mapped} patterns. Report this -- the identity hash needs "
            "widening."
        )

    con.execute("""
        CREATE TABLE patterns_new (
            pattern_id BIGINT PRIMARY KEY, route_id VARCHAR, agency_id VARCHAR,
            short_name VARCHAR, direction INTEGER, shape_id VARCHAR,
            n_stops INTEGER, n_trips INTEGER, span_m DOUBLE,
            first_seen VARCHAR, last_seen VARCHAR
        )
    """)
    con.execute(
        """
        INSERT INTO patterns_new
        SELECT r.new_id, p.* EXCLUDE (pattern_id), ?::VARCHAR, ?::VARCHAR
        FROM patterns p JOIN pattern_remap r ON r.old_id = p.pattern_id
        """,
        [feed, feed],
    )
    con.execute("DROP TABLE patterns")
    con.execute("ALTER TABLE patterns_new RENAME TO patterns")

    con.execute("""
        CREATE TABLE match_status_new (
            pattern_id BIGINT PRIMARY KEY, status VARCHAR, source VARCHAR,
            confidence DOUBLE, road_m DOUBLE, detour DOUBLE, n_edges INTEGER,
            detail VARCHAR, matched_at TIMESTAMP
        );
        INSERT INTO match_status_new
        SELECT r.new_id, m.* EXCLUDE (pattern_id)
        FROM match_status m JOIN pattern_remap r ON r.old_id = m.pattern_id;
        DROP TABLE match_status;
        ALTER TABLE match_status_new RENAME TO match_status;
    """)

    for table in ("pattern_stops", "pattern_edges"):
        con.execute(f"""
            CREATE OR REPLACE TABLE {table}_new AS
            SELECT r.new_id AS pattern_id, t.* EXCLUDE (pattern_id)
            FROM {table} t JOIN pattern_remap r ON r.old_id = t.pattern_id;
            DROP TABLE {table};
            ALTER TABLE {table}_new RENAME TO {table};
        """)

    con.execute("DROP TABLE pattern_remap")
    index(con)
    log.info(
        "migrated %d patterns from rank ids to identity hashes, feed %s",
        n_patterns,
        feed,
    )


# --- Spatial clustering ------------------------------------------------------

# The box the Z-order grid is quantised over: Great Britain with room to spare,
# matching `art.PRESETS["uk"]`. Deliberately wider than the data and written here
# rather than imported, because `art` imports this module and the constant is four
# numbers.
#
# It is a fixed grid rather than the data's own extent so that a region's layout
# does not depend on which region it is. Changing these numbers is harmless -- the
# code is a physical row order, never an identity -- but it does mean re-running
# `wayfare cluster` to get the benefit back.
CLUSTER_BOX = (-8.75, 49.85, 1.95, 60.90)
CLUSTER_BITS = 16  # per axis, so a cell is about 165 m across at this latitude

# The classic bit spread: four rounds of shift-and-mask turn 16 packed bits into 16
# bits with a zero between each, which is what interleaving two axes needs.
_SPREAD = ((8, 0x00FF00FF), (4, 0x0F0F0F0F), (2, 0x33333333), (1, 0x55555555))


def morton_sql(lon: str, lat: str) -> str:
    """SQL for a Z-order code over a lon/lat expression pair, in degrees.

    Two 16-bit axes interleaved into 32 bits, which fits a signed BIGINT with room
    to spare. Z-order rather than Hilbert because Hilbert needs the `spatial`
    extension and only beat Morton on the smallest window measured -- see
    `scripts/bench_window.py`, which is where the numbers behind this come from.

    The quantisation is a subquery so that each spreading round can name its input
    twice without the expression doubling in length four times over.
    """
    span_lon = CLUSTER_BOX[2] - CLUSTER_BOX[0]
    span_lat = CLUSTER_BOX[3] - CLUSTER_BOX[1]
    top = (1 << CLUSTER_BITS) - 1

    def spread(col: str) -> str:
        e = col
        for shift, mask in _SPREAD:
            e = f"(({e} | ({e} << {shift})) & {mask})"
        return e

    qx = (
        f"least({top}, greatest(0, floor((({lon}) - {CLUSTER_BOX[0]})"
        f" * {top}.0 / {span_lon})))::BIGINT AS qx"
    )
    qy = (
        f"least({top}, greatest(0, floor((({lat}) - {CLUSTER_BOX[1]})"
        f" * {top}.0 / {span_lat})))::BIGINT AS qy"
    )
    code = f"({spread('qx')} | ({spread('qy')} << 1))"
    return f"(SELECT {code} FROM (SELECT {qx}, {qy}))"


# The centre of the stored bbox. Both the window query and the curve are asking
# about where an edge *is*, and this is the one point an edge has that is already
# four plain integer columns.
_EDGE_CX = "(min_lon_e6 + max_lon_e6) / 2e6"
_EDGE_CY = "(min_lat_e6 + max_lat_e6) / 2e6"


def cluster_edges(con: duckdb.DuckDBPyConnection) -> int:
    """Rewrite `edges` in Z-order so its row-group zonemaps can prune a window.

    DuckDB keeps a min/max zonemap per row group of 122,880 rows and skips a group
    whose zonemap cannot satisfy a filter. `match` inserts edges as their patterns
    complete, and a batch of patterns is a national sample, so insertion order
    carries no geography at all: every group's bbox spans most of the country and
    none can ever be skipped. A city window reads 100% of the table.

    Ordering the rows by a Z-order code over the bbox centre fixes that. Measured on
    a synthetic 4.2M-edge database, Cardiff went from reading 100% of `edges` to
    11.7%, 22 ms to 4.4 ms, and London to 26.3%.

    Two things to keep in proportion. The scan is about a quarter of a render, so
    this is a large improvement to a small share; and `edge_services` carries no
    bbox column, so the weights pass reads all of it under any layout. Wales, at
    barely two row groups, cannot show the effect at all.

    This leaves the file *larger*: the old table's blocks are not reclaimed. Callers
    want :func:`cluster`, which follows it with the compaction that gets the size
    win too. This half is separate only so it can be tested against a connection.
    """
    n = int(scalar(con, "SELECT count(*) FROM edges"))
    if not n:
        return 0

    # CTAS preserves the order it was handed, which is what puts the rows on disk in
    # curve order; the PRIMARY KEY does not survive it, so it is reinstated as the
    # unique index the WKT migration above already established as this file's idiom.
    con.execute(f"""
        CREATE OR REPLACE TABLE edges_clustered AS
        SELECT * FROM edges
        ORDER BY {morton_sql(_EDGE_CX, _EDGE_CY)}, edge_id
    """)
    con.execute("DROP TABLE edges")
    con.execute("ALTER TABLE edges_clustered RENAME TO edges")
    con.execute("CREATE UNIQUE INDEX edges_pk ON edges (edge_id)")
    # The row count at the time of clustering, so `wayfare status` can say whether
    # a later `match` has appended unsorted rows on the end.
    set_meta(con, "edges_clustered", n)
    con.execute("CHECKPOINT")
    return n


def cluster(path: Path | None = None) -> tuple[int, int, int]:
    """Cluster `edges` and compact the file. Returns (edges, bytes before, after).

    Two steps, because neither alone is the thing wanted. The reorder is what makes
    the zonemaps prune; the compaction is what collects the *other* half of the win,
    which is that sorted neighbours compress better -- 528 MB to 453 MB on the
    benchmark's 4.2M edges.

    The compaction has to write a new file. DuckDB never returns space below a
    file's high-water mark: dropping the old table leaves its blocks allocated, and
    neither CHECKPOINT nor VACUUM gives them back, so reordering in place ends up
    *bigger* than it started -- measured at 505 MB going to 730. `COPY FROM
    DATABASE` into a fresh file is what actually reclaims them, and it preserves row
    order, so the curve survives the copy.

    The original is replaced only after the copy has been reopened and checked, and
    the replace itself is atomic, so an interruption leaves the database that cost a
    day of matching exactly as it was. It does need room for a second copy while it
    runs.
    """
    path = path or config.DB_PATH
    before = path.stat().st_size

    con = connect(path)
    try:
        n = cluster_edges(con)
    finally:
        con.close()
    if not n:
        return 0, before, before

    # Alongside the original rather than in a temp directory, so the rename at the
    # end is within one filesystem and therefore atomic.
    tmp = path.with_suffix(path.suffix + ".compacting")
    tmp.unlink(missing_ok=True)
    con = connect(path)
    try:
        # ATTACH takes a literal, not a bound parameter. The path comes from config
        # rather than a request, but doubling the quote costs nothing and means a
        # data directory with an apostrophe in it is not a broken command.
        con.execute(f"ATTACH '{str(tmp).replace(chr(39), chr(39) * 2)}' AS compacted")
        source = scalar(con, "SELECT current_database()")
        con.execute(f'COPY FROM DATABASE "{source}" TO compacted')
        con.execute("DETACH compacted")
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        con.close()

    # Reopen the copy and count it before trusting it with the original's place.
    # `COPY FROM DATABASE` carries data, not necessarily every index, so the
    # pipeline's own are re-asserted here -- all of them `IF NOT EXISTS`.
    check = connect(tmp)
    try:
        copied = int(scalar(check, "SELECT count(*) FROM edges"))
        if copied != n:
            raise RuntimeError(
                f"compacted copy has {copied} edges, expected {n}; leaving {path} alone"
            )
        check.execute("CREATE UNIQUE INDEX IF NOT EXISTS edges_pk ON edges (edge_id)")
        index(check)
        check.execute("CHECKPOINT")
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        check.close()

    after = tmp.stat().st_size
    tmp.replace(path)
    return n, before, after


def prune_shapes(con: duckdb.DuckDBPyConnection) -> int:
    """Drop the operator geometry that nothing needs any more.

    ``shapes`` was once input to ``match`` and nothing else, so it could go whole
    once matching finished. It is now also the *only* geometry a non-road pattern
    has: a tram is drawn from its operator trace rather than matched, so its shape
    is the picture and not an input to making one. Both clauses below exist because
    of that, and getting either wrong is silent.

    The pending test counts only matchable patterns. A tram never gets a
    ``match_status`` row, so counting it as pending would make this refuse for ever
    on any database that keeps one.

    The delete then spares every shape a live non-matchable pattern still points at.
    Those rows are worth keeping even after ``segments`` has copied them, because
    the copy is derived and this is the source.
    """
    pending = scalar(
        con,
        f"""
        SELECT count(*) FROM patterns p
        WHERE {current_feed()}
          AND {matchable()}
          AND NOT EXISTS (SELECT 1 FROM match_status m WHERE m.pattern_id = p.pattern_id)
        """,
    )
    if pending:
        raise RuntimeError(
            f"{pending} patterns are still unmatched; shapes is still needed. "
            "Finish `wayfare match` first."
        )
    before = scalar(con, "SELECT count(*) FROM shapes")
    con.execute(f"""
        DELETE FROM shapes WHERE shape_id NOT IN (
            SELECT p.shape_id FROM patterns p
            WHERE {current_feed()} AND NOT {matchable()} AND p.shape_id IS NOT NULL
        )
    """)
    kept = scalar(con, "SELECT count(*) FROM shapes")
    if kept:
        logs.get("db").info("kept %d shapes drawn directly by non-road patterns", kept)
    con.execute("CHECKPOINT")
    return int(before - kept)


def row(
    con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None
) -> tuple[Any, ...]:
    """First row of a query that must return one.

    Every aggregate query here is a count or a sum, which always yields a row --
    but fetchone() is typed as optional, and sprinkling asserts through the stages
    is worse than one helper that fails loudly with the query that surprised us.
    """
    r = con.execute(sql, params or []).fetchone()
    if r is None:
        raise RuntimeError(f"expected a row from: {' '.join(sql.split())[:120]}")
    return r


def scalar(
    con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None
) -> Any:
    return row(con, sql, params)[0]


def set_meta(con: duckdb.DuckDBPyConnection, key: str, value: Any) -> None:
    con.execute(
        "INSERT INTO meta VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        [key, str(value)],
    )


def get_meta(con: duckdb.DuckDBPyConnection, key: str) -> str | None:
    r = con.execute("SELECT value FROM meta WHERE key = ?", [key]).fetchone()
    return str(r[0]) if r else None
