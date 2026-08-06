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

from . import config

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
CREATE TABLE IF NOT EXISTS shapes (
    shape_id  VARCHAR,
    seq       INTEGER,
    lat       DOUBLE,
    lon       DOUBLE
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
    "CREATE INDEX IF NOT EXISTS shapes_sid ON shapes (shape_id)",
    "CREATE INDEX IF NOT EXISTS edge_services_eid ON edge_services (edge_id)",
]


def connect(path: Path | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    p = path or config.DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(p), read_only=read_only)
    if not read_only:
        con.execute(SCHEMA)
    return con


def index(con: duckdb.DuckDBPyConnection) -> None:
    for stmt in INDICES:
        con.execute(stmt)


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
