from __future__ import annotations

from pathlib import Path

import pytest

from wayfare import aggregate, config, gtfs, match, valhalla


class FakeClient:
    """Stands in for Valhalla. Returns two edges for anything it is asked to match,
    which is enough to exercise checkpointing, resumption and aggregation."""

    def __init__(self, road_m: float = 1000.0, fail: Exception | None = None):
        self.road_m = road_m
        self.fail = fail
        self.calls: list[str] = []

    def healthy(self) -> bool:
        return True

    def _match(self, source: str) -> valhalla.Match:
        self.calls.append(source)
        if self.fail:
            raise self.fail
        edges = [
            valhalla.Edge(1001, 44556677, self.road_m / 2, "Oxford Road", "secondary",
                          [(53.48, -2.245), (53.48, -2.240)]),
            valhalla.Edge(1002, 44556678, self.road_m / 2, "Oxford Road", "secondary",
                          [(53.48, -2.240), (53.48, -2.235)]),
        ]
        return valhalla.Match(edges, confidence=0.9, road_m=self.road_m, source=source)

    def match_shape(self, shape):
        return self._match("shape")

    def match_stops(self, stops):
        return self._match("stops")


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


def test_unroutable_patterns_are_not_retried_forever(loaded):
    match.run(loaded, client_=FakeClient(fail=valhalla.NoRoute("no route")))
    assert match.pending_count(loaded) == 0
    assert {r[0] for r in loaded.execute("SELECT status FROM match_status").fetchall()} == {
        "no_route"
    }


def test_transient_errors_are_also_recorded(loaded):
    match.run(loaded, client_=FakeClient(fail=valhalla.ValhallaError("503")))
    assert {r[0] for r in loaded.execute("SELECT status FROM match_status").fetchall()} == {
        "error"
    }


def test_huge_stop_gaps_are_skipped(loaded, monkeypatch):
    monkeypatch.setattr(config, "MAX_STOP_GAP_M", 10)  # every hop is ~330 m
    client = FakeClient()
    match.run(loaded, client_=client)
    assert client.calls == []
    assert {r[0] for r in loaded.execute("SELECT status FROM match_status").fetchall()} == {
        "skipped"
    }


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


def test_wkt_needs_two_points():
    assert match._wkt([(53.0, -2.0)]) is None
    assert match._wkt([(53.0, -2.0), (53.1, -2.1)]) == (
        "LINESTRING(-2.000000 53.000000, -2.100000 53.100000)"
    )


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
