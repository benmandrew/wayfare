from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from wayfare import aggregate, db


def test_a_row_per_point_shapes_table_migrates_in_place(tmp_path: Path):
    """A national match run costs a day or two. A schema change it cannot survive
    is a schema change nobody applies, so the old layout is rewritten, not re-read."""
    path = tmp_path / "old.duckdb"
    old = duckdb.connect(str(path))
    old.execute(
        "CREATE TABLE shapes (shape_id VARCHAR, seq INTEGER, lat DOUBLE, lon DOUBLE)"
    )
    old.executemany(
        "INSERT INTO shapes VALUES (?, ?, ?, ?)",
        [
            ("SH1", 2, 53.48, -2.24),  # deliberately out of order on disk
            ("SH1", 1, 53.48, -2.245),
            ("SH1", 3, 53.48, -2.235),
        ],
    )
    old.close()

    con = db.connect(path)
    lat_e6, lon_e6 = con.execute("SELECT lat_e6, lon_e6 FROM shapes").fetchone()
    assert lon_e6 == [-2245000, -2240000, -2235000]
    assert lat_e6 == [53480000] * 3
    con.close()


def _old_routes_table(path: Path) -> None:
    """`routes` as it stood before the mode filter: no route_type at all."""
    old = duckdb.connect(str(path))
    old.execute(
        "CREATE TABLE routes (route_id VARCHAR PRIMARY KEY, agency_id VARCHAR, "
        "short_name VARCHAR, long_name VARCHAR)"
    )
    old.execute("INSERT INTO routes VALUES ('R1', 'OP1', '42', 'Alpha to Delta')")
    old.close()


def test_routes_gains_route_type_without_losing_its_rows(tmp_path: Path):
    """The column is added rather than the table rebuilt, because nothing already
    stored can supply it -- the old loader never read route_type -- and a database
    that cost a day of matching must open either way."""
    path = tmp_path / "old.duckdb"
    _old_routes_table(path)

    con = db.connect(path)
    try:
        assert "route_type" in db.columns(con, "routes")
        # Empty until the next `patterns` run, and empty is not road-going, so no
        # stale row can pass the filter by accident.
        assert con.execute("SELECT route_type FROM routes").fetchone() == (None,)
    finally:
        con.close()


def test_patterns_gains_mode_empty_rather_than_backfilled(tmp_path: Path):
    """A database written before modes existed held road patterns only -- that was
    the whole point of the filter -- so the column could be backfilled to 'bus'. It
    is not. Backfilling asserts a mode the feed never told us, where NULL says only
    that nobody recorded one, and `db.matchable` already reads NULL as matchable so
    a national database keeps matching across the upgrade."""
    path = tmp_path / "old.duckdb"
    old = duckdb.connect(str(path))
    old.execute(db.SCHEMA)
    old.execute("ALTER TABLE patterns DROP COLUMN mode")
    old.execute(
        "INSERT INTO patterns "
        "(pattern_id, route_id, short_name, n_stops, n_trips, first_seen, last_seen) "
        "VALUES (1, 'R1', '42', 4, 10, 'F1', 'F1')"
    )
    old.close()

    con = db.connect(path)
    try:
        assert "mode" in db.columns(con, "patterns")
        assert db.scalar(con, "SELECT count(*) FROM patterns") == 1
        assert con.execute("SELECT mode FROM patterns").fetchone() == (None,)
        # NULL is matchable, so the upgrade does not quietly stall a match run.
        assert (
            db.scalar(con, f"SELECT count(*) FROM patterns p WHERE {db.matchable()}") == 1
        )
    finally:
        con.close()


def test_traces_gains_ways_cut_from_what_wrote_each_row(tmp_path: Path):
    """Which rows can be inverted per way, decided from what is already stored.

    A relation `osmroutes` built *is* its pattern, so its way list and the ways
    under its geometry are the same list. A trace the tracer cut out of a line is
    only the second if it was written after the tracer learned to cut, and nothing
    stored tells them apart afterwards -- the way boundaries are gone by the time
    the polyline lands. So the old rows read FALSE and keep being drawn per pattern
    until `wayfare trace` runs over them again, which loses nothing and claims
    nothing.
    """
    path = tmp_path / "old.duckdb"
    old = duckdb.connect(str(path))
    old.execute(db.SCHEMA)
    old.execute("ALTER TABLE traces DROP COLUMN ways_cut")
    old.executemany(
        "INSERT INTO patterns (pattern_id, route_id, short_name, n_stops, n_trips, "
        "first_seen, last_seen) VALUES (?, ?, 'V', 2, 10, 'F1', 'F1')",
        [(1, "osm:r900"), (2, "43")],
    )
    old.executemany(
        "INSERT INTO traces (pattern_id, relation_id, way_ids, lon_e6, lat_e6) "
        "VALUES (?, 900, [10, 11], [-1000000], [51000000])",
        [(1,), (2,)],
    )
    old.close()

    con = db.connect(path)
    try:
        got = con.execute(
            "SELECT pattern_id, ways_cut FROM traces ORDER BY pattern_id"
        ).fetchall()
        assert got == [(1, True), (2, False)]
    finally:
        con.close()


def test_track_services_gains_mode_backfilled_to_rail(tmp_path: Path):
    """Every row already in it came from a `route=train` relation, and a NULL mode
    would draw in the fallback grey for as long as it took someone to notice.

    The rebuild is checked here too, because ALTER appends and `SCHEMA` does not:
    `mode` is fourth in a fresh database and last in a migrated one, so an insert
    that named no columns would write the mode into `n_patterns` on precisely the
    databases that already hold rail.
    """
    path = tmp_path / "old.duckdb"
    old = duckdb.connect(str(path))
    old.execute(db.SCHEMA)
    old.execute("ALTER TABLE track_services DROP COLUMN mode")
    old.execute("INSERT INTO track_services VALUES (10, 'XC', 'CrossCountry', 1, NULL)")
    old.close()

    con = db.connect(path)
    try:
        assert db.scalar(con, "SELECT mode FROM track_services") == "rail"
        db.set_meta(con, "feed_version", "F1")
        con.execute(
            "INSERT INTO patterns (pattern_id, route_id, short_name, mode, n_trips, "
            "first_seen, last_seen) VALUES (1, 'osm:r9', 'XC', 'rail', NULL, 'F1', 'F1')"
        )
        con.execute(
            "INSERT INTO traces (pattern_id, relation_id, way_ids, ways_cut, "
            "lon_e6, lat_e6) VALUES (1, 9, [10], TRUE, [-1000000], [51000000])"
        )
        aggregate.build_track_services(con)
        assert db.row(con, "SELECT mode, n_patterns FROM track_services") == ("rail", 1)
    finally:
        con.close()


def test_a_read_only_connection_never_migrates_so_the_predicate_must_degrade(
    tmp_path: Path,
):
    """`connect` migrates only when it can write, so a data root that has not been
    opened for writing since `patterns.mode` landed still has the old schema. Great
    Britain's did, three days later, and `wayfare status` failed to bind against it --
    which is the number `deploy/refresh.sh` gates a publish on."""
    path = tmp_path / "old.duckdb"
    old = duckdb.connect(str(path))
    old.execute(db.SCHEMA)
    old.execute("ALTER TABLE patterns DROP COLUMN mode")
    old.execute(
        "INSERT INTO patterns "
        "(pattern_id, route_id, short_name, n_stops, n_trips, first_seen, last_seen) "
        "VALUES (1, 'R1', '42', 4, 10, 'F1', 'F1')"
    )
    old.close()

    con = db.connect(path, read_only=True)
    try:
        assert "mode" not in db.columns(con, "patterns")
        # Without the connection the predicate names a column that is not there.
        with pytest.raises(duckdb.Error):
            db.scalar(con, f"SELECT count(*) FROM patterns p WHERE {db.matchable()}")
        # With it, every stored row counts as matchable, which is what an old database
        # means: its loader deleted everything that was not road-going.
        assert (
            db.scalar(
                con, f"SELECT count(*) FROM patterns p WHERE {db.matchable('p', con)}"
            )
            == 1
        )
    finally:
        con.close()


def test_an_old_database_migrates_and_then_builds(gtfs_dir: Path, tmp_path: Path):
    """The migration on its own proves nothing; what matters is that the next run
    against the migrated file fills the column in and filters on it."""
    from wayfare import gtfs

    path = tmp_path / "old.duckdb"
    _old_routes_table(path)

    con = db.connect(path)
    try:
        gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
        assert db.scalar(con, "SELECT route_type FROM routes WHERE route_id = 'R1'") == "3"
        assert {
            r[0] for r in con.execute("SELECT DISTINCT route_id FROM patterns").fetchall()
        } == {"R1"}
    finally:
        con.close()


def _one_live_pattern(con) -> None:
    db.set_meta(con, "feed_version", "F1")
    # Columns named rather than positional, so adding one to `patterns` does not
    # silently shift every value here into the wrong place.
    con.execute(
        "INSERT INTO patterns "
        "(pattern_id, route_id, agency_id, short_name, direction, shape_id, "
        " n_stops, n_trips, span_m, mode, first_seen, last_seen) "
        "VALUES (1, 'R1', 'OP1', '42', 0, 'SH1', 4, 10, 1.0, 'bus', 'F1', 'F1')"
    )


def test_prune_refuses_while_patterns_are_still_pending(con):
    _one_live_pattern(con)
    con.execute("INSERT INTO shapes VALUES ('SH1', [53480000], [-2245000])")

    with pytest.raises(RuntimeError, match="still unmatched"):
        db.prune_shapes(con)
    assert con.execute("SELECT count(*) FROM shapes").fetchone()[0] == 1


def test_prune_drops_shapes_once_every_pattern_is_resolved(con):
    _one_live_pattern(con)
    con.execute("INSERT INTO shapes VALUES ('SH1', [53480000], [-2245000])")
    con.execute(
        "INSERT INTO match_status "
        "VALUES (1, 'ok', 'shape', 0.9, 100.0, 1.0, 2, NULL, now())"
    )

    assert db.prune_shapes(con) == 1
    assert con.execute("SELECT count(*) FROM shapes").fetchone()[0] == 0


# --- Spatial clustering ------------------------------------------------------


def _edge(con, edge_id, lon_e6, lat_e6):
    """One edge whose bbox is a point, so its Z-order cell is unambiguous."""
    con.execute(
        "INSERT INTO edges VALUES (?, 1, 'R', 'secondary', 100.0, [?], [?], ?, ?, ?, ?)",
        [edge_id, lon_e6, lat_e6, lon_e6, lat_e6, lon_e6, lat_e6],
    )


def test_morton_interleaves_the_two_axes(con):
    """The code is two 16-bit axes woven together, so the low bit is longitude and
    the next is latitude. Getting the order wrong still clusters, just along the
    wrong diagonal, which is why this is pinned rather than eyeballed."""
    sql = f"SELECT {db.morton_sql('?::DOUBLE', '?::DOUBLE')}"
    lo_lon, lo_lat = db.CLUSTER_BOX[0], db.CLUSTER_BOX[1]
    # Aimed at the middle of a cell rather than its edge: one cell width lands
    # exactly on the boundary, where floating point decides which side by a hair.
    dx = (db.CLUSTER_BOX[2] - db.CLUSTER_BOX[0]) / 65535.0
    dy = (db.CLUSTER_BOX[3] - db.CLUSTER_BOX[1]) / 65535.0

    assert con.execute(sql, [lo_lon + dx / 2, lo_lat + dy / 2]).fetchone()[0] == 0
    assert con.execute(sql, [lo_lon + dx * 1.5, lo_lat + dy / 2]).fetchone()[0] == 1
    assert con.execute(sql, [lo_lon + dx / 2, lo_lat + dy * 1.5]).fetchone()[0] == 2
    assert con.execute(sql, [lo_lon + dx * 1.5, lo_lat + dy * 1.5]).fetchone()[0] == 3


def test_morton_clamps_outside_the_box(con):
    """A window off West Africa is a real thing this has to survive -- see
    art.parse_bbox. Anything outside the grid pins to its edge rather than
    producing a negative code that would sort in front of Great Britain."""
    sql = f"SELECT {db.morton_sql('?::DOUBLE', '?::DOUBLE')}"
    below = con.execute(sql, [-180.0, -90.0]).fetchone()[0]
    above = con.execute(sql, [180.0, 90.0]).fetchone()[0]
    assert below == 0
    assert above == (1 << 32) - 1


def test_clustering_keeps_every_edge_and_its_geometry(con):
    """The whole table is rewritten, so the thing to prove first is that nothing is
    lost or altered on the way through."""
    for i in range(20):
        _edge(con, i + 1, -3200000 + i * 40000, 51480000 + i * 30000)
    before = con.execute("SELECT * FROM edges ORDER BY edge_id").fetchall()

    assert db.cluster_edges(con) == 20

    after = con.execute("SELECT * FROM edges ORDER BY edge_id").fetchall()
    assert after == before


def test_clustering_puts_neighbours_together(con):
    """Two edges in the same place must end up adjacent on disk however far apart
    they were inserted -- that adjacency is the entire mechanism."""
    _edge(con, 1, -3200000, 51480000)  # Cardiff
    _edge(con, 2, -100000, 51500000)  # London
    _edge(con, 3, -3200100, 51480100)  # Cardiff again, inserted last
    db.cluster_edges(con)

    order = [r[0] for r in con.execute("SELECT edge_id FROM edges").fetchall()]
    assert abs(order.index(1) - order.index(3)) == 1


def test_clustering_reinstates_the_unique_edge_id(con):
    """CTAS does not carry the PRIMARY KEY over, so the rewrite has to put it back
    or the next `match` could double-insert an edge in silence."""
    _edge(con, 1, -3200000, 51480000)
    db.cluster_edges(con)
    with pytest.raises(Exception, match="onstraint|nique"):
        _edge(con, 1, -3200000, 51480000)


def test_clustering_is_idempotent(con):
    for i in range(20):
        _edge(con, i + 1, -3200000 + i * 40000, 51480000 + i * 30000)
    db.cluster_edges(con)
    once = [r[0] for r in con.execute("SELECT edge_id FROM edges").fetchall()]
    db.cluster_edges(con)
    assert [r[0] for r in con.execute("SELECT edge_id FROM edges").fetchall()] == once


def test_clustering_an_empty_table_is_not_an_error(con):
    assert db.cluster_edges(con) == 0
    # ...and it does not claim to have clustered anything, so status stays honest.
    assert db.get_meta(con, "edges_clustered") is None


def test_cluster_rewrites_the_file_and_keeps_everything(tmp_path):
    """`cluster` swaps in a freshly written file, so the thing to prove is that
    every table survives the trip -- not just `edges`. The database it replaces is
    worth a day or two of matching."""
    path = tmp_path / "wayfare.duckdb"
    con = db.connect(path)
    for i in range(30):
        _edge(con, i + 1, -3200000 + i * 40000, 51480000 + i * 30000)
    con.execute("INSERT INTO edge_services VALUES (1, '42', 'OP1', 1, 100)")
    con.execute("INSERT INTO shapes VALUES ('SH1', [53480000], [-2245000])")
    db.set_meta(con, "feed_version", "20260806_022608")
    con.close()

    n, before, after = db.cluster(path)

    assert n == 30
    assert before > 0 and after > 0
    con = db.connect(path)
    try:
        assert db.scalar(con, "SELECT count(*) FROM edges") == 30
        assert db.scalar(con, "SELECT count(*) FROM edge_services") == 1
        assert db.scalar(con, "SELECT count(*) FROM shapes") == 1
        assert db.get_meta(con, "feed_version") == "20260806_022608"
        # The clustering flag has to survive its own compaction, or status would
        # report a freshly clustered database as unclustered.
        assert db.get_meta(con, "edges_clustered") == "30"
    finally:
        con.close()
    # No temporary file left beside it.
    assert [p.name for p in tmp_path.iterdir()] == ["wayfare.duckdb"]


def test_cluster_leaves_an_empty_database_alone(tmp_path):
    path = tmp_path / "wayfare.duckdb"
    db.connect(path).close()
    n, before, after = db.cluster(path)
    assert n == 0 and before == after
    assert not (tmp_path / "wayfare.duckdb.compacting").exists()


def test_status_reports_clustering_going_stale(con):
    """Clustering degrades rather than switching off: a later `match` appends
    unsorted rows on the end, and the count is what makes that visible."""
    from wayfare import aggregate

    _edge(con, 1, -3200000, 51480000)
    assert aggregate._clustered(con) == "no"

    db.cluster_edges(con)
    assert aggregate._clustered(con) == "yes"

    _edge(con, 2, -100000, 51500000)
    assert aggregate._clustered(con).startswith("stale (1 of 2 edges")


# --- pruning with non-road patterns present -----------------------------------


def _tram_pattern(con, pattern_id=2, shape_id="SH2") -> None:
    con.execute(
        "INSERT INTO patterns (pattern_id, route_id, short_name, shape_id, n_stops, "
        "n_trips, mode, first_seen, last_seen) "
        "VALUES (?, 'R2', 'T1', ?, 2, 5, 'tram', 'F1', 'F1')",
        [pattern_id, shape_id],
    )
    con.execute(
        "INSERT INTO shapes VALUES (?, [53480000, 53481000], [-2245000, -2240000])",
        [shape_id],
    )


def test_prune_is_not_blocked_by_a_pattern_that_is_never_matched(con):
    """A tram gets no match_status row, ever. Counting it as pending would make
    `prune` refuse for good on any database that keeps one."""
    _one_live_pattern(con)
    con.execute(
        "INSERT INTO match_status "
        "VALUES (1, 'ok', 'shape', 0.9, 100.0, 1.0, 2, NULL, now())"
    )
    _tram_pattern(con)

    db.prune_shapes(con)  # does not raise


def test_prune_keeps_the_geometry_a_non_road_pattern_is_drawn_from(con):
    """For a non-road pattern such as a tram, `shapes` is the drawn geometry itself
    rather than matcher input, so pruning it would blank the mode silently at the
    next publish."""
    _one_live_pattern(con)
    con.execute(
        "INSERT INTO match_status "
        "VALUES (1, 'ok', 'shape', 0.9, 100.0, 1.0, 2, NULL, now())"
    )
    con.execute("INSERT INTO shapes VALUES ('SH1', [53480000], [-2245000])")
    _tram_pattern(con)

    # The bus's shape goes; the tram's stays.
    assert db.prune_shapes(con) == 1
    assert [r[0] for r in con.execute("SELECT shape_id FROM shapes").fetchall()] == ["SH2"]
