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
    long_name   VARCHAR
);

-- A pattern is one distinct ordered stop sequence. 1.55M trips collapse to a far
-- smaller number of these, and the pattern is the unit we pay Valhalla for.
CREATE TABLE IF NOT EXISTS patterns (
    pattern_id  BIGINT PRIMARY KEY,
    route_id    VARCHAR,
    agency_id   VARCHAR,
    short_name  VARCHAR,
    direction   INTEGER,
    shape_id    VARCHAR,   -- NULL for ~52% of trips; see CLAUDE.md
    n_stops     INTEGER,
    n_trips     INTEGER,   -- how many timetabled trips use this pattern
    span_m      DOUBLE     -- straight-line length of the stop chain
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
    status      VARCHAR,   -- ok | low_confidence | no_route | error | skipped
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
CREATE TABLE IF NOT EXISTS edges (
    edge_id     BIGINT PRIMARY KEY,
    way_id      BIGINT,
    road_name   VARCHAR,
    road_class  VARCHAR,
    length_m    DOUBLE,
    geom        VARCHAR    -- WKT LINESTRING, WGS84
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


def prune_shapes(con: duckdb.DuckDBPyConnection) -> int:
    """Drop the operator geometry once every pattern has been matched.

    ``shapes`` is input to ``match`` and nothing else reads it. It is the largest
    table in the file by a wide margin, so on a national run it is worth reclaiming
    -- but only once there is no pending work, because a resumed run needs it.
    """
    pending = scalar(
        con,
        """
        SELECT count(*) FROM patterns p
        WHERE NOT EXISTS (SELECT 1 FROM match_status m WHERE m.pattern_id = p.pattern_id)
        """,
    )
    if pending:
        raise RuntimeError(
            f"{pending} patterns are still unmatched; shapes is still needed. "
            "Finish `wayfare match` first."
        )
    n = scalar(con, "SELECT count(*) FROM shapes")
    con.execute("DELETE FROM shapes")
    con.execute("CHECKPOINT")
    return int(n)


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
