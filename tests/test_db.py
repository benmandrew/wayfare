from __future__ import annotations

from pathlib import Path

import builders
import duckdb
import pytest

from wayfare import aggregate, db, maintenance

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
            db.scalar(con, f"SELECT count(*) FROM patterns p WHERE {db.matchable(con)}")
            == 1
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
        # What the predicate would be if it named the column regardless: it does not
        # bind, which is the failure this degrade exists to stop.
        with pytest.raises(duckdb.Error):
            db.scalar(con, "SELECT count(*) FROM patterns p WHERE p.mode IS NULL")
        # Every stored row counts as matchable instead, which is what an old database
        # means: its loader deleted everything that was not road-going.
        assert (
            db.scalar(con, f"SELECT count(*) FROM patterns p WHERE {db.matchable(con)}")
            == 1
        )
    finally:
        con.close()


def test_the_predicates_cannot_be_called_without_a_connection():
    """The degrade above was optional, and nine of twelve call sites forgot it. The
    parameter is what makes forgetting impossible, so its being required is the fix
    and is pinned here rather than left to a reviewer."""
    with pytest.raises(TypeError):
        db.matchable()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        db.non_road()  # type: ignore[call-arg]


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
    """A database is rarely old on one axis only, and `migrate` runs `MIGRATIONS` in
    order. That order used to carry a contract nothing stated: the renumbering
    rebuilt `patterns` from a column list of its own, which had no `mode` in it, so
    the step that adds `mode` was only sound after it. Swap the two and the column
    was added and then thrown away, on exactly the databases old enough to need
    both."""
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
            db.scalar(con, f"SELECT count(*) FROM patterns p WHERE {db.matchable(con)}")
            == 1
        )
    finally:
        con.close()


def test_the_renumbering_carries_a_column_it_was_never_told_about(legacy_db):
    """The rebuilt `patterns` is derived from the live table, not restated.

    The column list used to be a frozen copy of `SCHEMA`'s, and a frozen copy
    diverges the moment a column is added -- silently, and only on the databases old
    enough to come through here. A column `SCHEMA` does not know about stands in for
    that next addition: it has to survive, or the rewrite is a data loss.
    """
    path = legacy_db(
        db.SCHEMA,
        "ALTER TABLE patterns DROP COLUMN first_seen",
        "ALTER TABLE patterns DROP COLUMN last_seen",
        "ALTER TABLE patterns ADD COLUMN operator_notes VARCHAR",
        "INSERT INTO patterns (pattern_id, route_id, direction, n_stops, mode, "
        "operator_notes) VALUES (7, 'R1', 0, 4, 'bus', 'kept')",
        "INSERT INTO pattern_stops VALUES (7, 0, 'S1'), (7, 1, 'S2')",
        "INSERT INTO meta VALUES ('feed_version', 'F1')",
    )

    con = db.connect(path)
    try:
        assert db.row(con, "SELECT operator_notes, mode FROM patterns") == ("kept", "bus")
        # ...and the key it is rebuilt with is a key, not merely an index.
        with pytest.raises(Exception, match="onstraint|nique"):
            con.execute(
                "INSERT INTO patterns (pattern_id, route_id) "
                "SELECT pattern_id, 'R2' FROM patterns"
            )
    finally:
        con.close()


def test_the_renumbering_moves_every_table_keyed_on_the_old_id(legacy_db):
    """`traces`, `trace_status` and `segments` are keyed on pattern_id too.

    All three post-date hash ids, so no real database can reach this state -- which
    is exactly why it is pinned. Left behind, they would point at ids nothing holds
    any more, and the way that surfaces is a service drawn onto track it never
    reaches, years later, with nothing to trace it back to.
    """
    path = legacy_db(
        db.SCHEMA,
        "ALTER TABLE patterns DROP COLUMN first_seen",
        "ALTER TABLE patterns DROP COLUMN last_seen",
        "INSERT INTO patterns (pattern_id, route_id, direction, n_stops, mode) "
        "VALUES (7, 'R1', 0, 2, 'rail')",
        "INSERT INTO pattern_stops VALUES (7, 0, 'S1'), (7, 1, 'S2')",
        "INSERT INTO traces (pattern_id, relation_id, way_ids, ways_cut, lon_e6, "
        "lat_e6) VALUES (7, 900, [10], TRUE, [-1000000], [51000000])",
        "INSERT INTO trace_status (pattern_id, status) VALUES (7, 'ok')",
        "INSERT INTO segments VALUES (7, 'rail', [-1000000], [51000000], "
        "-1000000, 51000000, -1000000, 51000000)",
        "INSERT INTO meta VALUES ('feed_version', 'F1')",
    )

    con = db.connect(path)
    try:
        new_id = db.scalar(con, "SELECT pattern_id FROM patterns")
        assert new_id != 7
        for table in ("traces", "trace_status", "segments"):
            assert db.scalar(con, f"SELECT pattern_id FROM {table}") == new_id, table
    finally:
        con.close()


def test_the_catalog_is_read_from_this_database_alone(con, tmp_path):
    """`maintenance.cluster` attaches a second database, and `information_schema`
    spans every one of them. Unfiltered, a table in the file being copied into
    answers "does `patterns` have a `mode` column" -- which is every migration gate
    and every `matchable` degrade decision."""
    other = tmp_path / "other.duckdb"
    o = duckdb.connect(str(other))
    o.execute("CREATE TABLE patterns (pattern_id BIGINT, ghost VARCHAR)")
    o.execute("CREATE TABLE nowhere (x INTEGER)")
    o.close()

    con.execute(f"ATTACH '{other}' AS other")
    try:
        assert "ghost" not in db.columns(con, "patterns")
        assert not db.table_exists(con, "nowhere")
        # A staging table is ours, though, and `insert_via_file` asks the catalog for
        # the types to read its file back as.
        con.execute("CREATE TEMP TABLE staged (way_id BIGINT)")
        assert db.columns(con, "staged") == {"way_id"}
    finally:
        con.execute("DETACH other")


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
        maintenance.prune_shapes(con)
    assert db.scalar(con, "SELECT count(*) FROM shapes") == 1


def test_prune_drops_shapes_once_every_pattern_is_resolved(con):
    _one_live_pattern(con)
    con.execute("INSERT INTO shapes VALUES ('SH1', [53480000], [-2245000])")
    con.execute(
        "INSERT INTO match_status "
        "VALUES (1, 'ok', 'shape', 0.9, 100.0, 1.0, 2, NULL, now())"
    )

    assert maintenance.prune_shapes(con) == 1
    assert db.scalar(con, "SELECT count(*) FROM shapes") == 0


# --- Spatial clustering ------------------------------------------------------


def test_morton_interleaves_the_two_axes(con):
    """The code is two 16-bit axes woven together, so the low bit is longitude and
    the next is latitude. Getting the order wrong still clusters, just along the
    wrong diagonal, which is why this is pinned rather than eyeballed."""
    sql = f"SELECT {maintenance.morton_sql('?::DOUBLE', '?::DOUBLE')}"
    lo_lon, lo_lat = maintenance.CLUSTER_BOX[0], maintenance.CLUSTER_BOX[1]
    # Aimed at the middle of a cell rather than its edge: one cell width lands
    # exactly on the boundary, where floating point decides which side by a hair.
    dx = (maintenance.CLUSTER_BOX[2] - maintenance.CLUSTER_BOX[0]) / 65535.0
    dy = (maintenance.CLUSTER_BOX[3] - maintenance.CLUSTER_BOX[1]) / 65535.0

    assert db.scalar(con, sql, [lo_lon + dx / 2, lo_lat + dy / 2]) == 0
    assert db.scalar(con, sql, [lo_lon + dx * 1.5, lo_lat + dy / 2]) == 1
    assert db.scalar(con, sql, [lo_lon + dx / 2, lo_lat + dy * 1.5]) == 2
    assert db.scalar(con, sql, [lo_lon + dx * 1.5, lo_lat + dy * 1.5]) == 3


def test_morton_clamps_outside_the_box(con):
    """A window off West Africa is a real thing this has to survive -- see
    art.parse_bbox. Anything outside the grid pins to its edge rather than
    producing a negative code that would sort in front of Great Britain."""
    sql = f"SELECT {maintenance.morton_sql('?::DOUBLE', '?::DOUBLE')}"
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

    assert maintenance.cluster_edges(con) == 20

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
    maintenance.cluster_edges(con)

    order = [r[0] for r in con.execute("SELECT edge_id FROM edges").fetchall()]
    assert abs(order.index(1) - order.index(3)) == 1


def test_clustering_reinstates_the_unique_edge_id(con):
    """CTAS does not carry the PRIMARY KEY over, so the rewrite has to put it back
    or the next `match` could double-insert an edge in silence."""
    builders.insert_edge(con, 1, lon_e6=-3200000, lat_e6=51480000)
    maintenance.cluster_edges(con)
    with pytest.raises(Exception, match="onstraint|nique"):
        builders.insert_edge(con, 1, lon_e6=-3200000, lat_e6=51480000)


def test_clustering_is_idempotent(con):
    for i in range(20):
        builders.insert_edge(
            con, i + 1, lon_e6=-3200000 + i * 40000, lat_e6=51480000 + i * 30000
        )
    maintenance.cluster_edges(con)
    once = [r[0] for r in con.execute("SELECT edge_id FROM edges").fetchall()]
    maintenance.cluster_edges(con)
    assert [r[0] for r in con.execute("SELECT edge_id FROM edges").fetchall()] == once


def test_clustering_an_empty_table_is_not_an_error(con):
    assert maintenance.cluster_edges(con) == 0
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

    n, before, after = maintenance.cluster(path)

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
    n, before, after = maintenance.cluster(path)
    assert n == 0 and before == after
    assert not (tmp_path / "wayfare.duckdb.compacting").exists()


def test_status_reports_clustering_going_stale(con):
    """Clustering degrades rather than switching off: a later `match` appends
    unsorted rows on the end, and the count is what makes that visible.

    Read through `coverage`, which is what `wayfare status` prints, so the wording
    is free to change while the three states it has to distinguish are not."""
    builders.insert_edge(con, 1, lon_e6=-3200000, lat_e6=51480000)
    assert aggregate.funnel(con)["edges_clustered"] == "no"

    maintenance.cluster_edges(con)
    assert aggregate.funnel(con)["edges_clustered"] == "yes"

    builders.insert_edge(con, 2, lon_e6=-100000, lat_e6=51500000)
    stale = aggregate.funnel(con)["edges_clustered"]
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
    assert maintenance.prune_shapes(con) == 0
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
    assert maintenance.prune_shapes(con) == 1
    assert [r[0] for r in con.execute("SELECT shape_id FROM shapes").fetchall()] == ["SH2"]


# --- Bulk insert -------------------------------------------------------------


def test_insert_via_file_loads_flat_rows(con, staging):
    """The shape `pattern_edges` grows in: three numbers, hundreds of millions of
    rows, and the only reason this helper exists instead of an insert loop."""
    n = db.insert_via_file(
        con,
        "pattern_edges",
        ("pattern_id", "seq", "edge_id"),
        [(1, 0, 1001), (1, 1, 1002), (2, 0, 1001)],
    )
    assert n == 3
    assert con.execute(
        "SELECT pattern_id, seq, edge_id FROM pattern_edges ORDER BY pattern_id, seq"
    ).fetchall() == [(1, 0, 1001), (1, 1, 1002), (2, 0, 1001)]


def test_insert_via_file_keeps_list_columns_whole(con, staging):
    """`ways` and `edges` store geometry as micro-degree lists, so a staging format
    that cannot carry a list carries nothing this pipeline needs."""
    db.insert_via_file(
        con,
        "ways",
        (
            "way_id",
            "lon_e6",
            "lat_e6",
            "min_lon_e6",
            "min_lat_e6",
            "max_lon_e6",
            "max_lat_e6",
        ),
        [
            (
                7,
                [-2245000, -2240000],
                [53480000, 53481000],
                -2245000,
                53480000,
                -2240000,
                53481000,
            )
        ],
    )
    assert db.row(con, "SELECT lon_e6, lat_e6 FROM ways") == (
        [-2245000, -2240000],
        [53480000, 53481000],
    )


def test_insert_via_file_tells_an_empty_string_from_a_null(con, staging):
    """A CSV round-trip reads an empty field back as NULL, so text stages as JSON.
    The two mean different things -- a road with no name and a road named "" -- and
    the difference disappearing is the kind of failure nothing downstream reports."""
    db.insert_via_file(
        con,
        "edges",
        ("edge_id", "way_id", "road_name", "road_class"),
        [
            (1, 10, "", "residential"),
            (2, 11, None, "residential"),
            (3, 12, 'High Street, "the" one\nsecond line', "residential"),
        ],
    )
    assert con.execute(
        "SELECT edge_id, road_name FROM edges ORDER BY edge_id"
    ).fetchall() == [
        (1, ""),
        (2, None),
        (3, 'High Street, "the" one\nsecond line'),
    ]


def test_insert_via_file_keeps_a_gtfs_id_a_string(con, staging):
    """Route "07" must not become 7. The destination column's declared type is what
    the file is read back as, so nothing is left for a sniffer to decide."""
    db.set_meta(con, "feed_version", "F1")
    db.insert_via_file(
        con,
        "routes",
        ("route_id", "short_name", "route_type"),
        [("07", "07", "3")],
    )
    assert db.row(con, "SELECT route_id, short_name FROM routes") == ("07", "07")


def test_insert_via_file_stages_nothing_for_no_rows(con, staging):
    assert db.insert_via_file(con, "pattern_edges", ("pattern_id",), []) == 0
    assert db.scalar(con, "SELECT count(*) FROM pattern_edges") == 0


def test_insert_via_file_ignores_a_row_another_pattern_already_wrote(con, staging):
    """`edges` is shared across every pattern that traverses it, so the second
    arrival keeps the geometry the first one stored rather than replacing it."""
    cols = ("edge_id", "way_id", "road_name")
    db.insert_via_file(con, "edges", cols, [(1, 10, "first")], on_conflict="ignore")
    db.insert_via_file(con, "edges", cols, [(1, 99, "second")], on_conflict="ignore")
    assert db.row(con, "SELECT way_id, road_name FROM edges") == (10, "first")


def test_insert_via_file_replaces_where_the_later_writer_wins(con, staging):
    """`ways` is filled by two stages that never see each other's relations."""
    cols = ("way_id", "lon_e6", "lat_e6")
    db.insert_via_file(con, "ways", cols, [(7, [1], [2])], on_conflict="replace")
    db.insert_via_file(con, "ways", cols, [(7, [3], [4])], on_conflict="replace")
    assert db.row(con, "SELECT lon_e6, lat_e6 FROM ways") == ([3], [4])


def test_insert_via_file_takes_explicit_types_for_a_table_it_cannot_ask(con, staging):
    """A staging table the caller creates itself is still a table this can fill."""
    con.execute("CREATE TEMP TABLE staged (way_id BIGINT, way_ids BIGINT[])")
    db.insert_via_file(
        con,
        "staged",
        ("way_id", "way_ids"),
        [(1, [10, 11])],
        types={"way_id": "BIGINT", "way_ids": "BIGINT[]"},
    )
    assert db.row(con, "SELECT way_id, way_ids FROM staged") == (1, [10, 11])


def test_insert_via_file_refuses_a_column_it_has_no_type_for(con, staging):
    with pytest.raises(ValueError, match="no type for"):
        db.insert_via_file(con, "edges", ("edge_id", "nonesuch"), [(1, 2)])


def test_insert_via_file_refuses_a_conflict_rule_it_does_not_know(con, staging):
    with pytest.raises(ValueError, match="on_conflict"):
        db.insert_via_file(
            con, "edges", ("edge_id",), [(1,)], on_conflict="do nothing at all"
        )


# --- The non-road predicate --------------------------------------------------


def _non_road_ids(con) -> list[int]:
    return [
        r[0]
        for r in con.execute(
            f"SELECT p.pattern_id FROM patterns p WHERE {db.non_road(con)} ORDER BY 1"
        ).fetchall()
    ]


def test_non_road_selects_only_what_a_relation_has_to_draw(con):
    """Live, not matchable, and no operator shape. Each condition drops something
    that would otherwise be drawn twice or drawn from the wrong source."""
    db.set_meta(con, "feed_version", "F1")
    builders.insert_pattern(con, 1, mode="bus")  # matchable: Valhalla's
    builders.insert_pattern(con, 2, mode="tram")  # nothing else draws it
    builders.insert_pattern(con, 3, mode="tram", last_seen=None)  # departed
    con.execute("UPDATE patterns SET shape_id = 'SH1' WHERE pattern_id = 2")
    builders.insert_pattern(con, 4, mode="metro")
    builders.insert_pattern(con, 5, mode="tram")

    assert _non_road_ids(con) == [4, 5]


def test_non_road_binds_against_a_database_with_no_mode_column(legacy_db):
    """`status` connects read-only, so `migrate` never runs and the column may not
    be there. A predicate that names it fails to bind rather than degrading."""
    path = legacy_db(
        "CREATE TABLE patterns (pattern_id BIGINT, shape_id VARCHAR, last_seen VARCHAR)",
        "CREATE TABLE meta (key VARCHAR, value VARCHAR)",
        "INSERT INTO patterns VALUES (1, NULL, 'F1')",
        "INSERT INTO meta VALUES ('feed_version', 'F1')",
    )
    con = duckdb.connect(str(path), read_only=True)
    try:
        sql = f"SELECT count(*) FROM patterns p WHERE {db.non_road(con)}"
        # Everything stored in such a database is road-going by construction, so
        # nothing in it is owed geometry from a relation.
        assert db.scalar(con, sql) == 0
    finally:
        con.close()


# --- Distance in SQL ---------------------------------------------------------


def test_haversine_sql_agrees_with_the_python_one(con):
    """`patterns` measures a stop chain in SQL because it is far too big to walk in
    Python, and `art` and `trace` measure the same geometry in Python. The two have
    to be the same distance or a detour ratio compares one against the other."""
    from wayfare import osm

    a, b = (51.5074, -0.1278), (53.4808, -2.2426)
    sql = db.HAVERSINE_SQL.format(lat1=a[0], lon1=a[1], lat2=b[0], lon2=b[1])
    assert db.scalar(con, f"SELECT {sql}") == pytest.approx(osm.haversine_m(a, b))


def test_haversine_sql_composes_into_a_group_by(con):
    """The shape both callers use it in: a max over consecutive stops of a pattern."""
    con.execute(
        "INSERT INTO stops VALUES ('S1', 'Alpha', 53.48, -2.245), "
        "('S2', 'Bravo', 53.48, -2.240), ('S3', 'Charlie', 53.48, -2.200)"
    )
    con.execute("INSERT INTO pattern_stops VALUES (1, 1, 'S1'), (1, 2, 'S2'), (1, 3, 'S3')")
    dist = db.HAVERSINE_SQL.format(lat1="a.lat", lon1="a.lon", lat2="b.lat", lon2="b.lon")
    gap = db.scalar(
        con,
        f"""
        SELECT max({dist}) FROM pattern_stops ps
        JOIN pattern_stops ps2
          ON ps2.pattern_id = ps.pattern_id AND ps2.seq = ps.seq + 1
        JOIN stops a ON a.stop_id = ps.stop_id
        JOIN stops b ON b.stop_id = ps2.stop_id
        GROUP BY ps.pattern_id
        """,
    )
    assert gap == pytest.approx(2646.0, abs=1.0)


# --- Clearing a status cache -------------------------------------------------


def _status(con, table, pattern_id, status) -> None:
    con.execute(
        f"INSERT INTO {table} (pattern_id, status) VALUES (?, ?)", [pattern_id, status]
    )


def test_transport_error_is_the_one_status_both_caches_call_retryable():
    """Two stages record it and one alias clears it, so the spelling is shared."""
    from wayfare import match, trace

    assert db.TRANSPORT_ERROR == match.TRANSPORT_ERROR == trace.TRANSPORT_ERROR
    assert db.expand_statuses(["transient"]) == [db.TRANSPORT_ERROR]
    # Anything else is a literal status, passed through untouched.
    assert db.expand_statuses(["no_route", "transient"]) == [
        "no_route",
        db.TRANSPORT_ERROR,
    ]


def test_retry_statuses_clears_the_transport_faults_and_their_edges(con):
    """Nothing was ever learned about those patterns, so the row is a lie about
    them -- and the edges written under it have to go with it."""
    _status(con, "match_status", 1, "ok")
    _status(con, "match_status", 2, db.TRANSPORT_ERROR)
    _status(con, "match_status", 3, "no_route")
    con.execute("INSERT INTO pattern_edges VALUES (2, 0, 1001), (1, 0, 1002)")

    assert db.retry_statuses(con, "match_status", ["pattern_edges"], ["transient"]) == 1
    assert [
        r[0]
        for r in con.execute("SELECT pattern_id FROM match_status ORDER BY 1").fetchall()
    ] == [1, 3]
    # The dependent row goes with the status; the other pattern's is untouched.
    assert [
        r[0] for r in con.execute("SELECT pattern_id FROM pattern_edges").fetchall()
    ] == [1]


def test_retry_statuses_leaves_a_permanent_failure_alone(con):
    """A pattern that cannot be routed will never route. Retrying it every restart
    is how a national run stops finishing."""
    _status(con, "match_status", 1, "no_route")
    _status(con, "match_status", 2, "low_confidence")

    assert db.retry_statuses(con, "match_status", ["pattern_edges"], ["transient"]) == 0
    assert db.scalar(con, "SELECT count(*) FROM match_status") == 2


def test_retry_statuses_takes_a_literal_status_too(con):
    """The escape hatch for when the stage itself was wrong: the recorded failures
    are wrong with it, and only an operator can say so."""
    _status(con, "match_status", 1, "low_confidence")
    _status(con, "match_status", 2, "ok")

    assert db.retry_statuses(con, "match_status", [], ["low_confidence"]) == 1
    assert db.scalar(con, "SELECT count(*) FROM match_status") == 1


def test_retry_statuses_clears_a_trace_and_its_geometry(con):
    """The same call against the other cache: a trace_status row cleared without its
    `traces` row leaves geometry no status admits to, and the next run writes a
    second one over it."""
    _status(con, "trace_status", 1, db.TRANSPORT_ERROR)
    _status(con, "trace_status", 2, "no_relation")
    con.execute("INSERT INTO traces (pattern_id, relation_id) VALUES (1, 900), (2, 901)")

    assert db.retry_statuses(con, "trace_status", ["traces"], ["transient"]) == 1
    assert db.scalar(con, "SELECT count(*) FROM trace_status") == 1
    assert [r[0] for r in con.execute("SELECT pattern_id FROM traces").fetchall()] == [2]
