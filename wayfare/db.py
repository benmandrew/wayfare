"""DuckDB schema and connection.

One file holds the whole pipeline state. Each stage reads the previous stage's
tables and writes its own, so a stage can be re-run without repeating the ones
before it. DuckDB allows a single writer, so the matching loop keeps all writes on
the main thread and uses its workers only for HTTP.
"""

from __future__ import annotations

import csv
import json
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
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
    n_ways       INTEGER,  -- ways under the cut, not ways of the line it came from
    n_stops      INTEGER,  -- pattern stops matched to relation stop nodes
    worst_stop_m DOUBLE,   -- furthest a matched stop sits from its OSM node
    length_m     DOUBLE,
    detail       VARCHAR,
    traced_at    TIMESTAMP
);

-- What `snap` decided about each pattern it was handed, and why.
--
-- Its own table rather than a status on `trace_status`, because the two stages ask
-- different questions of the same pattern and both answers are worth keeping. A
-- pattern can be refused by the relation fit for having a stop sequence no relation
-- carries and still snap cleanly onto the track under its shape; folding the two
-- into one row would lose whichever ran second. Work is selected by the absence of a
-- row here, so a permanent cache in the sense `match_status` is permanent.
--
-- `covered_pct` is the number that matters and the one a partial refusal is decided
-- on. It is the share of the shape's length that found track within
-- `config.SNAP_MAX_M`, so a refusal reads as "this much of it is mapped" rather than
-- as a bare failure, and a region whose track is thin says so in one column.
--
--   ok             every metre of the shape found track and the ways were stored
--   partial_cover  under `config.SNAP_MIN_COVER` of it did; refused rather than trimmed
--   no_track       nothing fetched came within tolerance of any vertex
--   too_short      fewer than two points to snap
--   error          a bug or malformed geometry; permanent until the code changes
CREATE TABLE IF NOT EXISTS snap_status (
    pattern_id  BIGINT PRIMARY KEY,
    status      VARCHAR,
    n_ways      INTEGER,  -- distinct ways under this shape, in first-appearance order
    covered_pct DOUBLE,   -- share of shape length that found track, 0-100
    worst_m     DOUBLE,   -- furthest a covered vertex sits from the way it took
    length_m    DOUBLE,
    detail      VARCHAR,
    snapped_at  TIMESTAMP
);

-- The geometry that came back, for the patterns where it did.
--
-- Separate from `edges` and deliberately so. These way ids are OpenStreetMap's and
-- are durable, but they carry no Valhalla GraphId -- nothing routed them -- and
-- `edges.edge_id` is that GraphId and the primary key. Minting synthetic values
-- into it would break the one invariant the pipeline rests on. Keyed on pattern_id
-- for the same reason `segments` is: there is exactly one path per pattern.
--
-- `way_ids` is the durable identity of what was drawn, the evidence for the ODbL
-- credit this table makes the archive owe, and what `aggregate.build_track_services`
-- inverts into one row per way.
--
-- `ways_cut` says whether that list is the ways under *this pattern's* geometry or
-- the ways of the whole line it was cut from, and the inversion is only sound on
-- the first. Rows written before `trace` learned to cut carry the whole chain, so a
-- Northern line short working from Edgware to Kennington is stored against every
-- way of the Northern line -- harmless while the column only documented what was
-- drawn, and a lie about half the line once a way is asked which services use it.
-- Nothing stored can tell the two apart after the fact, and nothing can recover the
-- cut either: the way boundaries are gone by the time the polyline is stored. So it
-- is recorded at write time and the two consumers split on it, which is what lets an
-- old row keep being drawn per pattern until `trace` runs again over it.
CREATE TABLE IF NOT EXISTS traces (
    pattern_id  BIGINT PRIMARY KEY,
    relation_id BIGINT,
    way_ids     BIGINT[],
    ways_cut    BOOLEAN,
    -- Micro-degree integer lists, exactly as `edges` and `segments` store geometry.
    lon_e6      INTEGER[],
    lat_e6      INTEGER[]
);

-- Stage 2d: track inverted from per-pattern to per-way ------------------------

-- The geometry of one OpenStreetMap way, so track can be drawn once rather than
-- once per service that runs over it.
--
-- `traces` holds a whole pattern's path flattened into one polyline, which cannot
-- be cut back into ways -- the way boundaries are gone by the time it is stored.
-- Measured over Great Britain's 911 chaining `route=train` relations: drawn per
-- pattern that is 1,569,495 vertices, drawn per way 443,126, because 75.8% of ways
-- carry two or more relations. The reduction is the reason this table exists.
--
-- Deliberately not `edges`. That table's key is a Valhalla GraphId, and nothing
-- routed these; minting a synthetic id into it would break the one identity the
-- pipeline rests on. `way_id` is OpenStreetMap's own and is durable across graph
-- rebuilds, which `edge_id` is not.
CREATE TABLE IF NOT EXISTS ways (
    way_id      BIGINT PRIMARY KEY,
    -- Micro-degree integer lists and a bbox, exactly as `edges` and `segments`
    -- store geometry, so a window test is an integer overlap.
    lon_e6      INTEGER[],
    lat_e6      INTEGER[],
    min_lon_e6  INTEGER,
    min_lat_e6  INTEGER,
    max_lon_e6  INTEGER,
    max_lat_e6  INTEGER
);

-- Which services use one piece of track: the `edge_services` inversion, for the
-- modes drawn from route relations rather than matched onto roads.
--
-- `n_trips` is nullable and that is the point. A relation names its operator and
-- its line and lists its ways in order, so geometry and service identity are both
-- available with no timetable at all, under a licence the archive already carries.
-- A timetable, where there is one, fills this column in and changes nothing else.
--
-- `n_patterns` counts relations for the rail this pipeline built *from* relations,
-- which is not the quantity `edge_services.n_patterns` counts. A way carrying eight
-- relations is a fact about how thoroughly its line has been mapped rather than how
-- busy it is, so the two must never share a colour ramp -- which is why this is
-- published as its own layer. Where a timetable supplied the pattern and `trace`
-- only supplied its geometry, it counts patterns and means what `edge_services`
-- means.
--
-- `mode` is in the key because this table stopped being heavy rail alone once the
-- traced modes joined it: the Underground, the DLR and the trams are drawn from
-- relations too, and a way is painted by its mode. A way carrying both a tube line
-- and a National Rail service is two rows and two features, which is the one place
-- coincident track is drawn twice on purpose -- they are different networks.
CREATE TABLE IF NOT EXISTS track_services (
    way_id      BIGINT,
    short_name  VARCHAR,
    agency_id   VARCHAR,
    mode        VARCHAR,
    n_patterns  INTEGER,
    n_trips     INTEGER    -- NULL until a timetable has been attributed
);

-- How many trains a week run over one way, attributed from a timetable.
--
-- Separate from `track_services` because the two answer different questions and are
-- filled from different sources. That table says which lines use a piece of track
-- and comes from OpenStreetMap; this says how busy it is and comes from a timetable
-- that may not exist. Keeping them apart is what lets the track layer ship, and
-- stay correct, with no timetable at all.
--
-- Attributed per *leg* rather than per pattern, which is the whole reason it is
-- keyed on the way. Measured against the April 2024 national extract: matching a
-- whole calling sequence onto a relation covers 23.9% of GB rail trips, and
-- matching each consecutive pair of calls covers 82.0%. A fast service does not need
-- a relation with its exact stopping pattern; each of its legs runs on track some
-- relation covers.
CREATE TABLE IF NOT EXISTS way_trips (
    way_id   BIGINT PRIMARY KEY,
    n_trips  INTEGER   -- trips per week, the unit `edge_services.n_trips` uses
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


def matchable(con: duckdb.DuckDBPyConnection, alias: str = "p") -> str:
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

    `con` is first and has no default because it is what makes the predicate safe
    against a database it has not written to. `connect` runs `migrate` only when it
    is not read-only, so a read-only data root may hold no `mode` column at all, and
    a predicate naming a column that is not there fails to bind rather than
    degrading -- which is what `wayfare status` did against Great Britain three days
    after the column landed. With `con` in hand it degrades to TRUE, which is the
    answer a NULL mode gets and for the same reason. Optional, it was a safety every
    call site had to remember, and nine of twelve did not.
    """
    if "mode" not in columns(con, "patterns"):
        return "TRUE"
    keep = ", ".join(f"'{m}'" for m in sorted(config.ROAD_MODES))
    return f"({alias}.mode IS NULL OR {alias}.mode IN ({keep}))"


def non_road(con: duckdb.DuckDBPyConnection, alias: str = "p") -> str:
    """Predicate for the live patterns whose geometry has to come from a relation.

    Three conditions and each is load-bearing. Live, because a departed pattern is
    work spent on a journey nobody runs. Not matchable, because a bus belongs to
    Valhalla and a road is not what these draw. And no ``shape_id``, because where
    the operator recorded the course themselves that recording is better than
    anything reassembled from OpenStreetMap -- it is a survey of where the vehicle
    goes rather than of where the track is.

    This is the predicate that decides what `build_segments` *draws*. What `trace` is
    *offered* is :func:`traceable`, which is this with the last condition widened, and
    the two differ by exactly the modes fitted against OpenStreetMap anyway.

    `con` is required for the reason :func:`matchable` requires one.
    """
    return (
        f"{current_feed(alias)} AND NOT {matchable(con, alias)} "
        f"AND {alias}.shape_id IS NULL"
    )


# Great-circle distance between two lat/lon expressions, in metres. A format string
# rather than a function because it is composed into a group-by over pattern_stops
# that DuckDB has to run out of core -- the stop chain is far too big to walk in
# Python. The mean earth radius is the same 6,371 km `valhalla` measures with, so a
# span computed here and a road length computed there are comparable, which is what
# the detour ratio rests on.
HAVERSINE_SQL = """
    2 * 6371000 * asin(sqrt(
        pow(sin(radians({lat2} - {lat1}) / 2), 2)
      + cos(radians({lat1})) * cos(radians({lat2}))
      * pow(sin(radians({lon2} - {lon1}) / 2), 2)
    ))
"""


def traceable(con: duckdb.DuckDBPyConnection, alias: str = "p") -> str:
    """The live patterns `trace` is offered, which is :func:`non_road` widened.

    The widening is the whole of the difference, and it is the last condition alone.
    A `shape_id` normally settles the matter, for the reason `non_road` gives. The
    exception is `config.TRACE_OVER_SHAPE_MODES`, where the relation's chain is worth
    fitting even against a shape, because only the relation carries way ids and only
    way ids can be inverted into shared track. The shape is not thrown away:
    `build_segments` falls back to it for every pattern the tracer does not resolve,
    so a line whose relation is unmapped draws exactly as it always did.

    On a database with no `mode` column this *is* `non_road`, and not by accident:
    without the column no pattern can be in the set, so there is nothing to widen by.
    """
    if "mode" not in columns(con, "patterns"):
        return non_road(con, alias)
    over = ", ".join(f"'{m}'" for m in sorted(config.TRACE_OVER_SHAPE_MODES))
    return (
        f"{current_feed(alias)} AND NOT {matchable(con, alias)} "
        f"AND ({alias}.shape_id IS NULL OR {alias}.mode IN ({over}))"
    )


def connect(path: Path | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    p = path or config.DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(p), read_only=read_only)
    if not read_only:
        con.execute(SCHEMA)
        migrate(con)
    return con


def create_indices(con: duckdb.DuckDBPyConnection) -> None:
    for stmt in INDICES:
        con.execute(stmt)


# `information_schema` spans every attached database, and `maintenance.cluster`
# attaches one whose tables have the same names as ours. Without this every question
# about our own schema -- which migration is outstanding, whether `patterns.mode`
# exists -- can be answered by a table in a file that is only there to be copied
# into. Every catalog read below carries it.
#
# `temp` is ours too, and has to be here: a staging table lives in that catalog, and
# `insert_via_file` asks this for the types to read a file back as. An attached
# database is the thing being excluded, not a scratch table.
_THIS_DATABASE = "table_catalog IN (current_database(), 'temp')"


def table_exists(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    """Whether a table is in this database at all.

    `segments` post-dates Great Britain's database and `prune` reclaims tables once
    matching is done, so a data root that is missing one is a normal thing to be
    handed rather than a corrupt one.
    """
    return bool(
        scalar(
            con,
            "SELECT count(*) FROM information_schema.tables "
            f"WHERE {_THIS_DATABASE} AND table_name = ?",
            [table],
        )
    )


def columns(con: duckdb.DuckDBPyConnection, table: str) -> set[str]:
    return {
        r[0]
        for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE {_THIS_DATABASE} AND table_name = ?",
            [table],
        ).fetchall()
    }


@dataclass(frozen=True)
class _Migration:
    """One in-place rewrite, and the column whose presence says it is still owed.

    Every gate this file has ever needed is a column that is either there or not, so
    the whole set is decided from one read of the catalog rather than one read each --
    which every `connect` on an already-current database pays.
    """

    name: str
    table: str
    column: str
    # Whether the column being *present* is what says the rewrite is outstanding.
    # Two of these replace a column that is gone afterwards; the rest add one.
    present: bool
    apply: Callable[[duckdb.DuckDBPyConnection], None]

    def outstanding(self, schema: Mapping[str, frozenset[str]]) -> bool:
        return (self.column in schema.get(self.table, frozenset())) is self.present


def _schema_snapshot(con: duckdb.DuckDBPyConnection) -> dict[str, frozenset[str]]:
    """Every column of every table of *this* database, in one query."""
    out: dict[str, set[str]] = {}
    for table, column in con.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        f"WHERE {_THIS_DATABASE}"
    ).fetchall():
        out.setdefault(table, set()).add(column)
    return {t: frozenset(c) for t, c in out.items()}


def _migrate_shapes_to_one_row_per_shape(con: duckdb.DuckDBPyConnection) -> None:
    """shapes was a row per point; collapse to a row per shape."""
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


def _migrate_edges_to_micro_degrees(con: duckdb.DuckDBPyConnection) -> None:
    """edges.geom was a WKT LINESTRING.

    Split it into micro-degree lists and precompute the bbox the window query now
    filters on, so nothing parses text on read.
    """
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


def _add_routes_route_type(con: duckdb.DuckDBPyConnection) -> None:
    """The mode filter's column.

    Nothing already stored can supply it -- it comes from routes.txt and nowhere
    else -- so it is added empty and the next `patterns` run fills it in. Until then
    it is NULL, which no query treats as road-going, and no matched pattern is
    touched: a departed ferry keeps its rows and its match_status exactly like any
    other departed pattern.
    """
    con.execute("ALTER TABLE routes ADD COLUMN route_type VARCHAR")
    logs.get("db").info("added routes.route_type; run `wayfare patterns` to fill it")


def _add_traces_ways_cut(con: duckdb.DuckDBPyConnection) -> None:
    """What a stored `way_ids` means.

    Everything written before this column existed holds the whole line's chain,
    except the rows `osmroutes` wrote: there the relation *is* the pattern, so its
    ways and the ways under its geometry are the same list by construction. Both are
    derivable from what is already stored, so this is a rewrite and not a re-run --
    and the rows left FALSE keep being drawn per pattern, unchanged, until `trace`
    runs over them again and cuts them.
    """
    con.execute("ALTER TABLE traces ADD COLUMN ways_cut BOOLEAN")
    con.execute("""
        UPDATE traces SET ways_cut = EXISTS (
            SELECT 1 FROM patterns p
            WHERE p.pattern_id = traces.pattern_id
              AND p.route_id LIKE 'osm:r%'
        )
    """)
    cut = scalar(con, "SELECT count(*) FROM traces WHERE ways_cut")
    whole = scalar(con, "SELECT count(*) FROM traces WHERE NOT ways_cut")
    logs.get("db").info(
        "added traces.ways_cut: %d rows already cut to their pattern, %d hold "
        "the whole line and are drawn per pattern until `wayfare trace` reruns",
        cut,
        whole,
    )


def _add_track_services_mode(con: duckdb.DuckDBPyConnection) -> None:
    """Rebuilt outright by every `aggregate`, so this only has to exist before the
    next one inserts into it. Backfilled rather than left NULL because every row
    already there came from `route=train`, and a NULL mode would draw in the fallback
    grey for exactly as long as it took to notice."""
    con.execute("ALTER TABLE track_services ADD COLUMN mode VARCHAR")
    con.execute("UPDATE track_services SET mode = 'rail'")


def _add_patterns_mode(con: duckdb.DuckDBPyConnection) -> None:
    """Added empty for the reason route_type was: nothing already stored can supply
    it. It is left NULL rather than backfilled to 'bus', because a database written
    before this column existed held road modes only -- that was the whole point of
    the filter -- and `matchable` reads NULL as matchable for exactly that reason.
    Backfilling would assert a mode the feed never told us."""
    con.execute("ALTER TABLE patterns ADD COLUMN mode VARCHAR")
    logs.get("db").info("added patterns.mode; run `wayfare patterns` to fill it")


# Every table keyed on `pattern_id`, and whether one row is one pattern -- a CTAS
# carries the rows but not the key, so the tables that have a unique one get it back
# as the index the WKT rewrite above already established as this file's idiom.
#
# `traces`, `trace_status` and `segments` all post-date hash ids, so nothing can be
# holding rows in them by the time this runs. They are remapped anyway rather than
# asserted empty: they are keyed on the id being rewritten, the remap is the same
# operation the other four get, and a table left pointing at ids nothing holds any
# more would surface years later as a service attributed to track it never reaches.
# Refusing to open the database instead would strand one that cost a day of matching.
_KEYED_ON_PATTERN = {
    "pattern_stops": False,
    "pattern_edges": False,
    "match_status": True,
    "traces": True,
    "trace_status": True,
    "segments": True,
}


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

    Everything keyed on the old id moves with it -- see `_KEYED_ON_PATTERN`, which is
    the list, and says why the three tables that cannot yet hold a row are on it.
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

    # Derived from the live table rather than restated. The column list used to be a
    # frozen copy of `SCHEMA`'s, which is a copy that diverges in silence the next
    # time a column is added -- and what it would do is drop that column off every
    # database old enough to come through here.
    con.execute(
        """
        CREATE TABLE patterns_new AS
        SELECT r.new_id AS pattern_id, p.* EXCLUDE (pattern_id),
               ?::VARCHAR AS first_seen, ?::VARCHAR AS last_seen
        FROM patterns p JOIN pattern_remap r ON r.old_id = p.pattern_id
        """,
        [feed, feed],
    )
    con.execute("DROP TABLE patterns")
    con.execute("ALTER TABLE patterns_new RENAME TO patterns")
    con.execute("ALTER TABLE patterns ADD PRIMARY KEY (pattern_id)")

    for table, one_per_pattern in _KEYED_ON_PATTERN.items():
        if not scalar(con, f"SELECT count(*) FROM {table}"):  # noqa: S608
            # Nothing to remap, and rebuilding would trade the key `SCHEMA` has just
            # declared on an empty table for an index, to no end.
            continue
        con.execute(f"""
            CREATE OR REPLACE TABLE {table}_new AS
            SELECT r.new_id AS pattern_id, t.* EXCLUDE (pattern_id)
            FROM {table} t JOIN pattern_remap r ON r.old_id = t.pattern_id
        """)  # noqa: S608
        con.execute(f"DROP TABLE {table}")
        con.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
        if one_per_pattern:
            con.execute(f"ALTER TABLE {table} ADD PRIMARY KEY (pattern_id)")

    con.execute("DROP TABLE pattern_remap")
    create_indices(con)
    log.info(
        "migrated %d patterns from rank ids to identity hashes, feed %s",
        n_patterns,
        feed,
    )


# The rewrites, in the order they have to run.
#
# The order used to be whatever order the `if`s happened to be written in, and one
# pair depended on it: `patterns.mode` is added by ALTER, and the renumbering
# rebuilds `patterns`. That rebuild now derives its columns from the live table, so
# the two would survive being swapped -- but the shape of the hazard does not go
# away. A step that rebuilds a table is only ever safe before the steps that add
# columns to it, and adding one here means deciding where it goes.
MIGRATIONS: tuple[_Migration, ...] = (
    _Migration(
        "shapes to one row per shape",
        "shapes",
        "seq",
        True,
        _migrate_shapes_to_one_row_per_shape,
    ),
    _Migration(
        "edges from WKT to micro-degrees",
        "edges",
        "geom",
        True,
        _migrate_edges_to_micro_degrees,
    ),
    # Rebuilds `patterns` and everything else keyed on pattern_id, so it precedes
    # every step that adds a column to any of them.
    _Migration(
        "pattern ids from rank to identity hash",
        "patterns",
        "last_seen",
        False,
        _migrate_pattern_ids,
    ),
    _Migration("routes.route_type", "routes", "route_type", False, _add_routes_route_type),
    _Migration("traces.ways_cut", "traces", "ways_cut", False, _add_traces_ways_cut),
    _Migration(
        "track_services.mode", "track_services", "mode", False, _add_track_services_mode
    ),
    _Migration("patterns.mode", "patterns", "mode", False, _add_patterns_mode),
)


def migrate(con: duckdb.DuckDBPyConnection) -> None:
    """Bring an older database up to the current schema, in place.

    A national ``match`` run costs a day or two, so a schema change that forced it
    to be redone would be a change nobody applies. Every migration here is derivable
    from what is already stored, so it is a rewrite rather than a re-run.
    """
    schema = _schema_snapshot(con)
    for step in MIGRATIONS:
        if not step.outstanding(schema):
            continue
        step.apply(con)
        # Retaken rather than patched: a step may rebuild a table, and a snapshot
        # describing what that table looked like beforehand is how the next gate
        # reads the wrong answer. Only an open that does work pays for this.
        schema = _schema_snapshot(con)


# --- Bulk insert -------------------------------------------------------------

# The types a CSV round-trip cannot change the meaning of. Everything else stages as
# newline-delimited JSON, and the two exclusions are the whole reason this list
# exists: a list column has no CSV spelling at all, and an empty CSV field comes back
# as NULL, so a VARCHAR staged through CSV loses the difference between "" and NULL
# without a word. Measured on a million three-column rows, CSV loads in 0.53 s
# against JSON's 1.86 s, which is why the safe case is not simply given up.
_CSV_SAFE = re.compile(
    r"^(BIGINT|INTEGER|SMALLINT|TINYINT|HUGEINT|UBIGINT|UINTEGER|USMALLINT|UTINYINT"
    r"|DOUBLE|FLOAT|REAL|BOOLEAN|DATE|TIME|TIMESTAMP.*|DECIMAL\(.*\))$"
)

# What `on_conflict` accepts, and what each spells in SQL. A closed vocabulary rather
# than a clause the caller supplies: this builds SQL by interpolation, so the only
# safe caller-controlled parts are the ones enumerated here.
_CONFLICT = {None: "INSERT", "ignore": "INSERT OR IGNORE", "replace": "INSERT OR REPLACE"}


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


def insert_via_file(
    con: duckdb.DuckDBPyConnection,
    table: str,
    cols: Sequence[str],
    rows: Iterable[Sequence[Any]],
    types: Mapping[str, str] | None = None,
    *,
    on_conflict: str | None = None,
) -> int:
    """Insert rows by staging them to a file and having DuckDB read it back.

    The only way to fill a table that grows with the network. ``executemany`` moves
    about 2,700 rows a second; a staged file moves 1.6M, so on a national run the
    difference is the stage finishing overnight or not at all.

    ``types`` names the DuckDB type of each column, which is what stops the reader
    guessing. Where it is not given the destination table is asked, so the file is
    read back as exactly what the table already declares -- a column that arrives all
    NULL, or a GTFS id that looks like a number, cannot be inferred into something
    else on the way in.

    ``on_conflict`` is ``None``, ``"ignore"`` or ``"replace"``: the first for a table
    with no key to collide on, the second where another pattern may already have
    inserted the same edge, the third where two stages fill one row and the later one
    wins.

    Returns the number of rows staged, which is not the number inserted when a
    conflict rule discards some.

    ``rows`` is consumed once, while the file is being written. It must not be a
    generator that queries ``con``: a connection holds one result at a time, so a
    query started mid-stream abandons whatever this is iterating and the truncated
    result looks complete.
    """
    if on_conflict not in _CONFLICT:
        raise ValueError(f"on_conflict must be one of {sorted(_CONFLICT, key=str)}")
    resolved = dict(types) if types is not None else _column_types(con, table)
    missing = [c for c in cols if c not in resolved]
    if missing:
        raise ValueError(f"no type for {missing} of {table}; pass `types`")

    spec = ",".join(f"'{c}':'{resolved[c]}'" for c in cols)
    named = ", ".join(cols)
    as_json = any(not _CSV_SAFE.match(resolved[c]) for c in cols)
    with _staged(".ndjson" if as_json else ".csv") as path:
        n = _write_json(path, cols, rows) if as_json else _write_csv(path, rows)
        if not n:
            return 0
        # The path is ours, but the data root is the operator's and an apostrophe in
        # it would otherwise end the string literal early -- `cluster` doubles it for
        # the same reason.
        quoted = str(path).replace("'", "''")
        read = (
            f"read_json('{quoted}', format='newline_delimited', columns={{{spec}}})"
            if as_json
            else f"read_csv('{quoted}', header=false, columns={{{spec}}})"
        )
        con.execute(
            f"{_CONFLICT[on_conflict]} INTO {table} ({named}) "  # noqa: S608
            f"SELECT {named} FROM {read}"
        )
    return n


def _column_types(con: duckdb.DuckDBPyConnection, table: str) -> dict[str, str]:
    return {
        r[0]: r[1]
        for r in con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            f"WHERE {_THIS_DATABASE} AND table_name = ?",
            [table],
        ).fetchall()
    }


def _write_csv(path: Path, rows: Iterable[Sequence[Any]]) -> int:
    n = 0
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        for r in rows:
            writer.writerow(r)
            n += 1
    return n


def _write_json(path: Path, cols: Sequence[str], rows: Iterable[Sequence[Any]]) -> int:
    n = 0
    with path.open("w") as fh:
        for r in rows:
            # `default=str` so a timestamp or a Decimal stages as the text DuckDB
            # parses back into the column's own type, rather than raising here.
            fh.write(json.dumps(dict(zip(cols, r, strict=True)), default=str))
            fh.write("\n")
            n += 1
    return n


# --- Status caches -----------------------------------------------------------

# The one outcome of `match_status` and `trace_status` that is a statement about the
# world at that moment rather than about the pattern. Everything else in either table
# is permanent by design: a pattern that cannot be routed will never route, and a
# stage that retries the impossible on every restart never finishes.
TRANSPORT_ERROR = "transport_error"

# What `--retry transient` expands to. Kept as a name rather than spelled out at the
# call site so that adding a status here reaches the CLI, the help text and the
# recovery path together.
RETRYABLE = (TRANSPORT_ERROR,)
_RETRY_ALIASES: dict[str, tuple[str, ...]] = {"transient": RETRYABLE}


def expand_statuses(statuses: Sequence[str]) -> list[str]:
    """Resolve the ``transient`` alias, leaving any literal status alone."""
    out: list[str] = []
    for s in statuses:
        out.extend(_RETRY_ALIASES.get(s, (s,)))
    return out


def retry_statuses(
    con: duckdb.DuckDBPyConnection,
    status_table: str,
    dependents: Sequence[str],
    statuses: Sequence[str],
    *,
    key: str = "pattern_id",
) -> int:
    """Forget outcomes with these statuses so the next run redoes them.

    Failures are deliberately never retried automatically. But when the stage itself
    was wrong, the recorded failures are wrong too, and this is how they get cleared.
    ``transient`` is the alias for the statuses that are safe to clear unattended,
    which is ``transport_error`` alone: nothing was ever learned about those patterns.

    ``dependents`` are the tables whose rows only exist because of the status row --
    a cleared trace has to lose its geometry with it, or the next run writes a second
    one. Tables shared across patterns are not dependents: `edges` is re-inserted on
    conflict, and deleting a row another pattern still references would take that
    pattern's geometry with it.

    Call this *before* the stage starts, never between batches. Work is selected by
    the absence of a status row, so deleting one while a batch holding that pattern
    is in flight hands the same pattern out twice -- the same trap that makes a batch
    the unit of both concurrency and checkpointing.
    """
    wanted = expand_statuses(statuses)
    ids = [
        r[0]
        for r in con.execute(
            f"SELECT {key} FROM {status_table} WHERE status IN (SELECT unnest(?))",  # noqa: S608
            [wanted],
        ).fetchall()
    ]
    if not ids:
        return 0
    for table in (*dependents, status_table):
        con.execute(f"DELETE FROM {table} WHERE {key} IN (SELECT unnest(?))", [ids])  # noqa: S608
    logs.get("db").info("cleared %d outcomes with status in %s", len(ids), wanted)
    return len(ids)


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
