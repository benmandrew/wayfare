"""The two housekeeping stages: cluster the edge table, and prune what it fed.

Neither belongs in `db`. That module owns the schema, the identity SQL, the
predicates and the migrations -- the things every other stage reads -- and these are
two commands an operator runs, once matching is done, against a database that is
already correct. Both rewrite storage rather than meaning: `cluster` changes the
physical row order and the file's size, `prune` drops operator geometry nothing needs
any more. Neither changes what a query answers.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from . import config, db, logs

# --- Spatial clustering ------------------------------------------------------

# The box the Z-order grid is quantised over: Great Britain with room to spare,
# matching `art.PRESETS["uk"]`. Deliberately wider than the data and written out here
# rather than imported from `art`, which would pull cairo and numpy into a command
# that reorders rows, for four numbers.
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
    n = int(db.scalar(con, "SELECT count(*) FROM edges"))
    if not n:
        return 0

    # CTAS preserves the order it was handed, which is what puts the rows on disk in
    # curve order; the PRIMARY KEY does not survive it, so it is reinstated as the
    # unique index `db`'s rewrites use for the same reason.
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
    db.set_meta(con, "edges_clustered", n)
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

    con = db.connect(path)
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
    con = db.connect(path)
    try:
        # ATTACH takes a literal, not a bound parameter. The path comes from config
        # rather than a request, but doubling the quote costs nothing and means a
        # data directory with an apostrophe in it is not a broken command.
        con.execute(f"ATTACH '{str(tmp).replace(chr(39), chr(39) * 2)}' AS compacted")
        source = db.scalar(con, "SELECT current_database()")
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
    check = db.connect(tmp)
    try:
        copied = int(db.scalar(check, "SELECT count(*) FROM edges"))
        if copied != n:
            raise RuntimeError(
                f"compacted copy has {copied} edges, expected {n}; leaving {path} alone"
            )
        check.execute("CREATE UNIQUE INDEX IF NOT EXISTS edges_pk ON edges (edge_id)")
        db.create_indices(check)
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
    pending = db.scalar(
        con,
        f"""
        SELECT count(*) FROM patterns p
        WHERE {db.current_feed()}
          AND {db.matchable(con)}
          AND NOT EXISTS (SELECT 1 FROM match_status m WHERE m.pattern_id = p.pattern_id)
        """,
    )
    if pending:
        raise RuntimeError(
            f"{pending} patterns are still unmatched; shapes is still needed. "
            "Finish `wayfare match` first."
        )
    before = db.scalar(con, "SELECT count(*) FROM shapes")
    con.execute(f"""
        DELETE FROM shapes WHERE shape_id NOT IN (
            SELECT p.shape_id FROM patterns p
            WHERE {db.current_feed()} AND NOT {db.matchable(con)} AND p.shape_id IS NOT NULL
        )
    """)
    kept = db.scalar(con, "SELECT count(*) FROM shapes")
    if kept:
        logs.get("maintenance").info(
            "kept %d shapes drawn directly by non-road patterns", kept
        )
    con.execute("CHECKPOINT")
    return int(before - kept)
