from __future__ import annotations

from pathlib import Path

import builders
import duckdb
import pytest

from wayfare import aggregate, db

# `routes` as it stood before the mode filter: no route_type at all.
_OLD_ROUTES = (
    "CREATE TABLE routes (route_id VARCHAR PRIMARY KEY, agency_id VARCHAR, "
    "short_name VARCHAR, long_name VARCHAR)",
    "INSERT INTO routes VALUES ('R1', 'OP1', '42', 'Alpha to Delta')",
)


def test_a_row_per_point_shapes_table_migrates_in_place(legacy_db):
    """A national match run costs a day or two. A schema change it cannot survive
    is a schema change nobody applies, so the old layout is rewritten, not re-read."""
    path = legacy_db(
        "CREATE TABLE shapes (shape_id VARCHAR, seq INTEGER, lat DOUBLE, lon DOUBLE)",
        rows={
            "shapes": [
                ("SH1", 2, 53.48, -2.24),  # deliberately out of order on disk
                ("SH1", 1, 53.48, -2.245),
                ("SH1", 3, 53.48, -2.235),
            ]
        },
    )

    con = db.connect(path)
    lat_e6, lon_e6 = db.row(con, "SELECT lat_e6, lon_e6 FROM shapes")
    assert lon_e6 == [-2245000, -2240000, -2235000]
    assert lat_e6 == [53480000] * 3
    con.close()


def test_routes_gains_route_type_without_losing_its_rows(legacy_db):
    """The column is added rather than the table rebuilt, because nothing already
    stored can supply it -- the old loader never read route_type -- and a database
    that cost a day of matching must open either way."""
    con = db.connect(legacy_db(*_OLD_ROUTES))
    try:
        assert "route_type" in db.columns(con, "routes")
        # Empty until the next `patterns` run, and empty is not road-going, so no
        # stale row can pass the filter by accident.
        assert db.row(con, "SELECT route_type FROM routes") == (None,)
    finally:
        con.close()


def test_patterns_gains_mode_empty_rather_than_backfilled(legacy_db):
    """A database written before modes existed held road patterns only -- that was
    the whole point of the filter -- so the column could be backfilled to 'bus'. It
    is not. Backfilling asserts a mode the feed never told us, where NULL says only
    that nobody recorded one, and `db.matchable` already reads NULL as matchable so
    a national database keeps matching across the upgrade."""
    path = legacy_db(
        db.SCHEMA,
        "ALTER TABLE patterns DROP COLUMN mode",
        "INSERT INTO patterns "
        "(pattern_id, route_id, short_name, n_stops, n_trips, first_seen, last_seen) "
        "VALUES (1, 'R1', '42', 4, 10, 'F1', 'F1')",
    )

    con = db.connect(path)
    try:
        assert "mode" in db.columns(con, "patterns")
        assert db.scalar(con, "SELECT count(*) FROM patterns") == 1
        assert db.row(con, "SELECT mode FROM patterns") == (None,)
        # NULL is matchable, so the upgrade does not quietly stall a match run.
        assert (
            db.scalar(con, f"SELECT count(*) FROM patterns p WHERE {db.matchable()}") == 1
        )
    finally:
        con.close()


def test_traces_gains_ways_cut_from_what_wrote_each_row(legacy_db):
    """Which rows can be inverted per way, decided from what is already stored.

    A relation `osmroutes` built *is* its pattern, so its way list and the ways
    under its geometry are the same list. A trace the tracer cut out of a line is
    only the second if it was written after the tracer learned to cut, and nothing
    stored tells them apart afterwards -- the way boundaries are gone by the time
    the polyline lands. So the old rows read FALSE and keep being drawn per pattern
    until `wayfare trace` runs over them again, which loses nothing and claims
    nothing.
    """
    path = legacy_db(
        db.SCHEMA,
        "ALTER TABLE traces DROP COLUMN ways_cut",
        "INSERT INTO patterns (pattern_id, route_id, short_name, n_stops, n_trips, "
        "first_seen, last_seen) VALUES (1, 'osm:r900', 'V', 2, 10, 'F1', 'F1'), "
        "(2, '43', 'V', 2, 10, 'F1', 'F1')",
        "INSERT INTO traces (pattern_id, relation_id, way_ids, lon_e6, lat_e6) VALUES "
        "(1, 900, [10, 11], [-1000000], [51000000]), "
        "(2, 900, [10, 11], [-1000000], [51000000])",
    )

    con = db.connect(path)
    try:
        got = con.execute(
            "SELECT pattern_id, ways_cut FROM traces ORDER BY pattern_id"
        ).fetchall()
        assert got == [(1, True), (2, False)]
    finally:
        con.close()


def test_track_services_gains_mode_backfilled_to_rail(legacy_db):
    """Every row already in it came from a `route=train` relation, and a NULL mode
    would draw in the fallback grey for as long as it took someone to notice.

    The rebuild is checked here too, because ALTER appends and `SCHEMA` does not:
    `mode` is fourth in a fresh database and last in a migrated one, so an insert
    that named no columns would write the mode into `n_patterns` on precisely the
    databases that already hold rail.
    """
    path = legacy_db(
        db.SCHEMA,
        "ALTER TABLE track_services DROP COLUMN mode",
        "INSERT INTO track_services VALUES (10, 'XC', 'CrossCountry', 1, NULL)",
    )

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


def test_a_read_only_connection_never_migrates_so_the_predicate_must_degrade(legacy_db):
    """`connect` migrates only when it can write, so a data root that has not been
    opened for writing since `patterns.mode` landed still has the old schema. Great
    Britain's did, three days later, and `wayfare status` failed to bind against it --
    which is the number `deploy/refresh.sh` gates a publish on."""
    path = legacy_db(
        db.SCHEMA,
        "ALTER TABLE patterns DROP COLUMN mode",
        "INSERT INTO patterns "
        "(pattern_id, route_id, short_name, n_stops, n_trips, first_seen, last_seen) "
        "VALUES (1, 'R1', '42', 4, 10, 'F1', 'F1')",
    )

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


def test_an_old_database_migrates_and_then_builds(gtfs_dir: Path, legacy_db):
    """The migration on its own proves nothing; what matters is that the next run
    against the migrated file fills the column in and filters on it."""
    from wayfare import gtfs

    con = db.connect(legacy_db(*_OLD_ROUTES))
    try:
        gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
        assert db.scalar(con, "SELECT route_type FROM routes WHERE route_id = 'R1'") == "3"
        assert {
            r[0] for r in con.execute("SELECT DISTINCT route_id FROM patterns").fetchall()
        } == {"R1"}
    finally:
        con.close()


def test_edges_migrate_from_wkt_text_to_micro_degree_lists(legacy_db):
    """The largest of the rewrites: `edges.geom` was a WKT LINESTRING parsed on every
    read, and is now two integer lists and the bbox the window query filters on. It
    has to carry the whole table, an edge whose geometry was never recorded included,
    because the alternative is re-matching a national run to change a storage format.
    """
    path = legacy_db(
        "CREATE TABLE edges (edge_id BIGINT PRIMARY KEY, way_id BIGINT, "
        "road_name VARCHAR, road_class VARCHAR, length_m DOUBLE, geom VARCHAR)",
        rows={
            "edges": [
                (
                    1,
                    10,
                    "Oxford Road",
                    "secondary",
                    100.0,
                    "LINESTRING(-2.245 53.48, -2.24 53.481)",
                ),
                # Too few points to be a line, so it never had geometry to convert.
                (2, 20, None, None, 0.0, None),
            ]
        },
    )

    con = db.connect(path)
    try:
        assert "geom" not in db.columns(con, "edges")
        assert db.row(
            con,
            "SELECT way_id, road_name, lon_e6, lat_e6, min_lon_e6, min_lat_e6, "
            "max_lon_e6, max_lat_e6 FROM edges WHERE edge_id = 1",
        ) == (
            10,
            "Oxford Road",
            [-2245000, -2240000],
            [53480000, 53481000],
            -2245000,
            53480000,
            -2240000,
            53481000,
        )
        # Kept rather than dropped: an edge with no geometry is filtered on read like
        # any other, and losing the row would lose what points at it.
        assert db.row(con, "SELECT lon_e6, lat_e6 FROM edges WHERE edge_id = 2") == (
            None,
            None,
        )
        # CTAS does not carry a PRIMARY KEY over, so the rewrite reinstates it.
        with pytest.raises(Exception, match="onstraint|nique"):
            builders.insert_edge(con, 1)
    finally:
        con.close()


def test_two_migrations_apply_in_one_open(legacy_db):
    """A database is rarely old on one axis only, and `migrate` runs its steps in
    the order they are written. That order carries a contract nothing states: the
    renumbering rebuilds `patterns` from a column list of its own, which has no
    `mode` in it, so the step that adds `mode` is only sound after it. Swap the two
    and the column is added and then thrown away, on exactly the databases old
    enough to need both."""
    path = legacy_db(
        db.SCHEMA,
        "ALTER TABLE patterns DROP COLUMN first_seen",
        "ALTER TABLE patterns DROP COLUMN last_seen",
        "ALTER TABLE patterns DROP COLUMN mode",
        "INSERT INTO patterns VALUES (7, 'R1', 'OP1', '42', 0, 'SH1', 4, 10, 1000.0)",
        "INSERT INTO pattern_stops VALUES (7, 0, 'S1'), (7, 1, 'S2'), (7, 2, 'S3'), "
        "(7, 3, 'S4')",
        "INSERT INTO meta VALUES ('feed_version', 'F1')",
    )

    con = db.connect(path)
    try:
        assert {"first_seen", "last_seen", "mode"} <= db.columns(con, "patterns")
        assert db.row(con, "SELECT first_seen, last_seen, mode FROM patterns") == (
            "F1",
            "F1",
            None,
        )
        # Both rewrites landed on the same row: it is renumbered to its identity
        # hash and still counts as matchable, which is what a NULL mode means.
        expected = db.scalar(
            con, "SELECT " + db.pattern_id_sql("'R1'", "0", "'S1>S2>S3>S4'")
        )
        assert db.scalar(con, "SELECT pattern_id FROM patterns") == expected
        assert (
            db.scalar(con, f"SELECT count(*) FROM patterns p WHERE {db.matchable()}") == 1
        )
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
    assert db.scalar(con, "SELECT count(*) FROM shapes") == 1


def test_prune_drops_shapes_once_every_pattern_is_resolved(con):
    _one_live_pattern(con)
    con.execute("INSERT INTO shapes VALUES ('SH1', [53480000], [-2245000])")
    con.execute(
        "INSERT INTO match_status "
        "VALUES (1, 'ok', 'shape', 0.9, 100.0, 1.0, 2, NULL, now())"
    )

    assert db.prune_shapes(con) == 1
    assert db.scalar(con, "SELECT count(*) FROM shapes") == 0


# --- Spatial clustering ------------------------------------------------------


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

    assert db.scalar(con, sql, [lo_lon + dx / 2, lo_lat + dy / 2]) == 0
    assert db.scalar(con, sql, [lo_lon + dx * 1.5, lo_lat + dy / 2]) == 1
    assert db.scalar(con, sql, [lo_lon + dx / 2, lo_lat + dy * 1.5]) == 2
    assert db.scalar(con, sql, [lo_lon + dx * 1.5, lo_lat + dy * 1.5]) == 3


def test_morton_clamps_outside_the_box(con):
    """A window off West Africa is a real thing this has to survive -- see
    art.parse_bbox. Anything outside the grid pins to its edge rather than
    producing a negative code that would sort in front of Great Britain."""
    sql = f"SELECT {db.morton_sql('?::DOUBLE', '?::DOUBLE')}"
    below = db.scalar(con, sql, [-180.0, -90.0])
    above = db.scalar(con, sql, [180.0, 90.0])
    assert below == 0
    assert above == (1 << 32) - 1


def test_clustering_keeps_every_edge_and_its_geometry(con):
    """The whole table is rewritten, so the thing to prove first is that nothing is
    lost or altered on the way through."""
    for i in range(20):
        builders.insert_edge(
            con, i + 1, lon_e6=-3200000 + i * 40000, lat_e6=51480000 + i * 30000
        )
    before = con.execute("SELECT * FROM edges ORDER BY edge_id").fetchall()

    assert db.cluster_edges(con) == 20

    after = con.execute("SELECT * FROM edges ORDER BY edge_id").fetchall()
    assert after == before


def test_clustering_puts_neighbours_together(con):
    """Two edges in the same place must end up adjacent on disk however far apart
    they were inserted -- that adjacency is the entire mechanism."""
    builders.insert_edge(con, 1, lon_e6=-3200000, lat_e6=51480000)  # Cardiff
    builders.insert_edge(con, 2, lon_e6=-100000, lat_e6=51500000)  # London
    builders.insert_edge(
        con, 3, lon_e6=-3200100, lat_e6=51480100
    )  # Cardiff again, inserted last
    db.cluster_edges(con)

    order = [r[0] for r in con.execute("SELECT edge_id FROM edges").fetchall()]
    assert abs(order.index(1) - order.index(3)) == 1


def test_clustering_reinstates_the_unique_edge_id(con):
    """CTAS does not carry the PRIMARY KEY over, so the rewrite has to put it back
    or the next `match` could double-insert an edge in silence."""
    builders.insert_edge(con, 1, lon_e6=-3200000, lat_e6=51480000)
    db.cluster_edges(con)
    with pytest.raises(Exception, match="onstraint|nique"):
        builders.insert_edge(con, 1, lon_e6=-3200000, lat_e6=51480000)


def test_clustering_is_idempotent(con):
    for i in range(20):
        builders.insert_edge(
            con, i + 1, lon_e6=-3200000 + i * 40000, lat_e6=51480000 + i * 30000
        )
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
        builders.insert_edge(
            con, i + 1, lon_e6=-3200000 + i * 40000, lat_e6=51480000 + i * 30000
        )
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
    unsorted rows on the end, and the count is what makes that visible.

    Read through `coverage`, which is what `wayfare status` prints, so the wording
    is free to change while the three states it has to distinguish are not."""
    builders.insert_edge(con, 1, lon_e6=-3200000, lat_e6=51480000)
    assert aggregate.coverage(con)["edges_clustered"] == "no"

    db.cluster_edges(con)
    assert aggregate.coverage(con)["edges_clustered"] == "yes"

    builders.insert_edge(con, 2, lon_e6=-100000, lat_e6=51500000)
    stale = aggregate.coverage(con)["edges_clustered"]
    assert isinstance(stale, str)
    # The shortfall itself, rather than the sentence it is reported in: one of the
    # two edges is sorted and the other is not.
    assert stale.startswith("stale") and "1 of 2" in stale


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

    # Nothing to drop -- the bus's own shape was never inserted -- and the tram's
    # is spared, so the count is the proof that the refusal did not fire either.
    assert db.prune_shapes(con) == 0
    assert db.scalar(con, "SELECT count(*) FROM shapes") == 1


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
