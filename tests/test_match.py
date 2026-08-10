from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
import requests
from conftest import FakeClient

from wayfare import aggregate, config, gtfs, match, valhalla


@pytest.fixture
def loaded(gtfs_dir: Path, con):
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
    return con


def test_run_matches_every_pattern(loaded):
    tally = match.run(loaded, client_=FakeClient())
    assert tally == {"ok": 2}
    assert match.pending_count(loaded) == 0
    assert loaded.execute("SELECT count(*) FROM edges").fetchone()[0] == 2
    assert loaded.execute("SELECT count(*) FROM pattern_edges").fetchone()[0] == 4


def test_patterns_with_operator_geometry_use_the_shape_path(loaded):
    client = FakeClient()
    match.run(loaded, client_=client)
    # One pattern has SH1, the other has no shape_id.
    assert sorted(client.calls) == ["shape", "stops"]


def test_rerun_does_no_work(loaded):
    match.run(loaded, client_=FakeClient())
    second = FakeClient()
    tally = match.run(loaded, client_=second)
    assert tally == {}
    assert second.calls == []


def test_interruption_keeps_completed_work(loaded):
    """The central resumption guarantee: matching half the patterns and stopping
    leaves those results in place, and a re-run only does the remainder."""
    match.run(loaded, client_=FakeClient(), limit=1)
    assert match.pending_count(loaded) == 1

    client = FakeClient()
    match.run(loaded, client_=client)
    assert match.pending_count(loaded) == 0
    assert len(client.calls) == 1  # only the leftover pattern


def test_absurd_detour_is_recorded_but_its_edges_are_dropped(loaded):
    # The mini feed's stop chain is about 1 km; claim 100 km of road.
    match.run(loaded, client_=FakeClient(road_m=100_000))
    statuses = {r[0] for r in loaded.execute("SELECT status FROM match_status").fetchall()}
    assert statuses == {"low_confidence"}
    # Recorded, so it is never retried...
    assert match.pending_count(loaded) == 0
    # ...but the bad geometry never reaches the map.
    assert loaded.execute("SELECT count(*) FROM pattern_edges").fetchone()[0] == 0


def test_non_road_modes_are_never_handed_to_the_matcher(gtfs_dir: Path, con):
    """Keeping a ferry and map-matching it are different decisions, and only the
    first is `--modes`. A sea crossing given to `bus` costing either fails outright
    or snaps to the nearest coast road, which is worse -- it was the largest single
    error class in the GB run. Their geometry comes from the operator instead.
    """
    gtfs.build_patterns(
        gtfs_dir, con, memory_limit="1GB", modes=frozenset({"bus", "ferry", "rail"})
    )
    assert con.execute("SELECT count(*) FROM patterns").fetchone()[0] == 4

    assert match.pending_count(con) == 2  # the two bus patterns, not the four
    match.run(con, client_=FakeClient())
    matched = {
        r[0]
        for r in con.execute("""
            SELECT p.mode FROM patterns p JOIN match_status m USING (pattern_id)
        """).fetchall()
    }
    assert matched == {"bus"}


def test_an_older_database_has_no_mode_and_is_still_matched(loaded):
    """A NULL mode is what a database written before the column existed means. It
    held road modes only -- that was the point of the filter -- so reading NULL as
    unmatchable would silently stop a national database dead."""
    loaded.execute("UPDATE patterns SET mode = NULL")
    assert match.pending_count(loaded) == 2


def test_unroutable_patterns_are_not_retried_forever(loaded):
    match.run(loaded, client_=FakeClient(fail=valhalla.NoRoute("no route")))
    assert match.pending_count(loaded) == 0
    assert {r[0] for r in loaded.execute("SELECT status FROM match_status").fetchall()} == {
        "no_route"
    }


def _statuses(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT status FROM match_status").fetchall()}


def test_a_valhalla_error_is_recorded_as_permanent(loaded):
    match.run(loaded, client_=FakeClient(fail=valhalla.ValhallaError("400: {}")))
    assert _statuses(loaded) == {"error"}


@pytest.mark.parametrize(
    "fault",
    [
        valhalla.TransportError("ConnectionError: Connection refused"),
        valhalla.TransportError("Timeout: Read timed out (read timeout=120.0)"),
    ],
)
def test_transport_faults_are_not_recorded_as_permanent(loaded, fault):
    """A refused connection says nothing about the pattern. Recording it as `error`
    is what put 262 recoverable patterns beyond the reach of any retry."""
    match.run(loaded, client_=FakeClient(fail=fault))
    assert _statuses(loaded) == {match.TRANSPORT_ERROR}
    # Still recorded, so the run terminates rather than spinning on them.
    assert match.pending_count(loaded) == 0


def test_a_raw_requests_fault_is_also_transport(loaded):
    """The client converts these itself; this is the belt-and-braces catch for any
    other path that talks HTTP."""
    match.run(loaded, client_=FakeClient(fail=requests.ConnectionError("refused")))
    assert _statuses(loaded) == {match.TRANSPORT_ERROR}


def test_a_bug_in_our_own_code_stays_permanent(loaded):
    """The bare except is the last resort and must not become retryable: a defect
    that retries forever is worse than one that records and moves on."""
    match.run(loaded, client_=FakeClient(fail=KeyError("shape")))
    assert _statuses(loaded) == {"error"}
    assert match.retry(loaded, ["transient"]) == 0


def test_retry_transient_selects_transport_faults_and_nothing_else(loaded):
    match.run(loaded, client_=FakeClient(fail=valhalla.TransportError("refused")))
    assert match.pending_count(loaded) == 0

    assert match.retry(loaded, ["transient"]) == 2
    assert match.pending_count(loaded) == 2

    match.run(loaded, client_=FakeClient())
    assert _statuses(loaded) == {"ok"}


def test_retry_transient_leaves_permanent_failures_alone(loaded):
    match.run(loaded, client_=FakeClient(fail=valhalla.NoRoute("400: {}")))
    assert _statuses(loaded) == {"no_route"}
    assert match.retry(loaded, ["transient"]) == 0
    assert match.pending_count(loaded) == 0


def test_reclassifying_an_old_database_moves_only_the_transport_rows(loaded):
    """What an existing database needs. Every fault used to be `error`, and the two
    kinds are told apart by the shape of the detail this codebase writes: a reply
    from Valhalla is "<http status>: <json body>", anything else never got one."""
    match.run(loaded, client_=FakeClient())
    ids = [r[0] for r in loaded.execute("SELECT pattern_id FROM match_status").fetchall()]
    details = [
        '400: {"error_code": 442, "error": "No path could be found for input"}',
        "ConnectionError: HTTPConnectionPool(host='valhalla', port=8002): "
        "Max retries exceeded (Caused by NewConnectionError: Connection refused)",
    ]
    for pattern_id, detail in zip(ids, details, strict=True):
        loaded.execute(
            "UPDATE match_status SET status = 'error', detail = ? WHERE pattern_id = ?",
            [detail, pattern_id],
        )

    assert match.reclassify_transport_faults(loaded) == 1
    assert _statuses(loaded) == {"error", match.TRANSPORT_ERROR}
    # And the reply from Valhalla stays permanent.
    kept = loaded.execute(
        "SELECT detail FROM match_status WHERE status = 'error'"
    ).fetchone()
    assert kept is not None and kept[0].startswith("400:")


def test_reclassifying_leaves_our_own_configuration_error_alone(loaded):
    """The one ValhallaError raised without a reply to quote. It is a bad request,
    not a bad network, and re-matching it would fail the same way."""
    match.run(loaded, client_=FakeClient())
    loaded.execute(
        "UPDATE match_status SET status = 'error', detail = ?",
        [f"{valhalla.NO_SCORE_MESSAGE}; 'confidence_score' must be listed"],
    )
    assert match.reclassify_transport_faults(loaded) == 0


def test_huge_stop_gaps_are_skipped_only_without_operator_geometry(loaded, monkeypatch):
    """The gap bound guards guesswork, so it must not touch a recorded trace.

    The mini feed has one pattern with a shape and one without. Both blow the limit;
    only the one whose road would have to be invented is skipped.
    """
    monkeypatch.setattr(config, "MAX_STOP_GAP_M", 10)  # every hop is ~330 m
    client = FakeClient()
    match.run(loaded, client_=client)
    assert client.calls == ["shape"]
    rows = loaded.execute("SELECT source, status FROM match_status").fetchall()
    assert sorted(rows) == [("shape", "ok"), ("stops", "skipped")]


def test_aggregate_inverts_to_services(loaded):
    match.run(loaded, client_=FakeClient())
    aggregate.build(loaded)

    rows = loaded.execute(
        "SELECT edge_id, short_name, n_patterns, n_trips "
        "FROM edge_services ORDER BY edge_id"
    ).fetchall()
    assert len(rows) == 2  # two edges, one service each
    for _eid, short_name, n_patterns, n_trips in rows:
        assert short_name == "42"
        # Both patterns of route 42 traverse both edges; weekly trips sum across them.
        assert n_patterns == 2
        assert n_trips == 15


def test_coverage_reports_service_weighted_share(loaded):
    match.run(loaded, client_=FakeClient())
    aggregate.build(loaded)
    cov = aggregate.coverage(loaded)
    assert cov["patterns_total"] == 2
    assert cov["patterns_matched"] == 2
    assert cov["trips_pct"] == 100.0
    assert cov["services"] == 1


def test_bulk_insert_survives_awkward_text_and_missing_geometry(con, tmp_path, monkeypatch):
    """Rows are staged through a file, so anything the file format could mangle has
    to survive the round trip: quotes, commas and newlines in a road name, and an
    edge with no geometry at all."""
    monkeypatch.setattr(config, "WORK", tmp_path)
    nasty = 'A "B", C\nD\treal name'
    rows = [
        (1, 10, nasty, "secondary", 100.0, [1, 2], [3, 4], 1, 3, 2, 4),
        (2, 20, None, None, 0.0, None, None, None, None, None, None),
    ]
    match._insert_edges(con, rows)

    got = con.execute(
        "SELECT edge_id, road_name, lon_e6 FROM edges ORDER BY edge_id"
    ).fetchall()
    assert got == [(1, nasty, [1, 2]), (2, None, None)]
    # Nothing is left behind in the staging directory.
    assert list((tmp_path / "stage").iterdir()) == []


def test_bulk_insert_ignores_an_edge_another_batch_wrote(con, tmp_path, monkeypatch):
    """edges is shared across patterns, so the same edge arrives in many batches."""
    monkeypatch.setattr(config, "WORK", tmp_path)
    first = (1, 10, "Oxford Road", "secondary", 100.0, [1, 2], [3, 4], 1, 3, 2, 4)
    match._insert_edges(con, [first])
    match._insert_edges(con, [(1, 10, "Renamed", "primary", 999.0, [9], [9], 9, 9, 9, 9)])

    assert con.execute("SELECT count(*) FROM edges").fetchone()[0] == 1
    assert con.execute("SELECT road_name FROM edges").fetchone()[0] == "Oxford Road"


def test_staging_file_is_removed_even_when_the_insert_fails(con, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORK", tmp_path)
    bad = [("not a number", 10, "x", "y", 1.0, [1], [1], 1, 1, 1, 1)]
    with pytest.raises(duckdb.Error):
        match._insert_edges(con, bad)  # read_json refuses text where a BIGINT belongs
    assert list((tmp_path / "stage").iterdir()) == []


def test_geometry_needs_two_points():
    assert match._geom([(53.0, -2.0)]) == (None,) * 6


def test_geometry_carries_its_own_bounding_box():
    """The window query in `art` compares integers against integers and never looks
    inside the geometry, which only works if the bbox is stored alongside it."""
    lon_e6, lat_e6, min_lon, min_lat, max_lon, max_lat = match._geom(
        [(53.0, -2.0), (53.1, -2.1), (53.05, -1.9)]
    )
    assert lon_e6 == [-2_000_000, -2_100_000, -1_900_000]
    assert lat_e6 == [53_000_000, 53_100_000, 53_050_000]
    assert (min_lon, max_lon) == (-2_100_000, -1_900_000)
    assert (min_lat, max_lat) == (53_000_000, 53_100_000)


def test_retry_clears_only_the_named_statuses(loaded):
    match.run(loaded, client_=FakeClient(road_m=100_000))  # all low_confidence
    assert match.pending_count(loaded) == 0

    assert match.retry(loaded, ["error"]) == 0  # nothing of that status
    assert match.pending_count(loaded) == 0

    assert match.retry(loaded, ["low_confidence"]) == 2
    assert match.pending_count(loaded) == 2

    # And a re-run with a working client now succeeds.
    match.run(loaded, client_=FakeClient())
    statuses = {r[0] for r in loaded.execute("SELECT status FROM match_status").fetchall()}
    assert statuses == {"ok"}


def test_retry_leaves_shared_edges_alone(loaded):
    """`edges` is shared across patterns and re-inserted idempotently, so clearing
    one pattern's outcome must not delete geometry another still points at."""
    match.run(loaded, client_=FakeClient())
    match.retry(loaded, ["ok"])
    assert loaded.execute("SELECT count(*) FROM edges").fetchone()[0] == 2
    assert loaded.execute("SELECT count(*) FROM pattern_edges").fetchone()[0] == 0


# --- drawing the modes that are never matched ---------------------------------


def _with_ferry_geometry(con) -> None:
    """Give the mini feed's ferry an operator trace, as two thirds of GB's real
    ferry trips have. The fixture ships it without one, and the shape is the whole
    input to drawing a non-road mode."""
    con.execute(
        "INSERT INTO shapes VALUES ('SHF', [53405000, 53320000], [-2996000, -3180000])"
    )
    con.execute("UPDATE patterns SET shape_id = 'SHF' WHERE mode = 'ferry'")


def test_segments_are_built_from_operator_geometry_only(gtfs_dir: Path, con):
    """The whole of "drawing" a tram or a ferry: copy the trace, run no matcher."""
    gtfs.build_patterns(
        gtfs_dir, con, memory_limit="1GB", modes=frozenset({"bus", "ferry", "rail"})
    )
    _with_ferry_geometry(con)
    aggregate.build_segments(con)

    rows = con.execute(
        "SELECT mode, lon_e6, min_lon_e6, max_lat_e6 FROM segments"
    ).fetchall()
    assert len(rows) == 1
    mode, lon_e6, min_lon, max_lat = rows[0]
    assert mode == "ferry"
    assert lon_e6 == [-2996000, -3180000]
    # The bbox is computed from the trace rather than copied from anywhere.
    assert (min_lon, max_lat) == (-3180000, 53405000)


def test_a_non_road_pattern_with_no_shape_is_not_drawn(gtfs_dir: Path, con):
    """ "Bad geometry is worse than missing geometry", applied to the case where
    inventing would be easy: the stops are known, and a straight line between them
    renders perfectly happily down the wrong side of a river."""
    gtfs.build_patterns(
        gtfs_dir, con, memory_limit="1GB", modes=frozenset({"bus", "ferry", "rail"})
    )
    aggregate.build_segments(con)
    assert con.execute("SELECT count(*) FROM segments").fetchone()[0] == 0


def test_no_matched_pattern_is_ever_a_segment(gtfs_dir: Path, con):
    """A bus is drawn from its matched edges. Drawing it from its shape as well
    would put a second line under the first, off the road network."""
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
    aggregate.build_segments(con)
    assert con.execute("SELECT count(*) FROM segments").fetchone()[0] == 0


def test_segments_hold_the_current_feed_only(gtfs_dir: Path, con):
    """Derived and cheap, so it is rebuilt outright like pattern_stops rather than
    merged like patterns. A departed ferry stops being drawn on the next run."""
    gtfs.build_patterns(
        gtfs_dir, con, memory_limit="1GB", modes=frozenset({"bus", "ferry"})
    )
    _with_ferry_geometry(con)
    aggregate.build_segments(con)
    assert con.execute("SELECT count(*) FROM segments").fetchone()[0] == 1

    con.execute("UPDATE patterns SET last_seen = 'GONE'")
    aggregate.build_segments(con)
    assert con.execute("SELECT count(*) FROM segments").fetchone()[0] == 0
