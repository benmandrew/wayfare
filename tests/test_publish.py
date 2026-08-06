from __future__ import annotations

import json

import pytest

from wayfare import config, publish


def _row(edge_id, pts, refs, way_id=1, name="Oxford Road", trips=100):
    """One row in the shape export_geojsonl's query returns."""
    return (
        edge_id,
        way_id,
        name,
        [p[0] for p in pts],
        [p[1] for p in pts],
        len(refs),
        list(refs),
        trips,
    )


A, B, C, D = (0, 0), (10, 0), (20, 0), (30, 0)


def test_edges_meeting_end_to_end_become_one_feature():
    out = publish.coalesce([_row(1, [A, B], ["42"]), _row(2, [B, C], ["42"])])
    assert len(out) == 1
    props, coords = out[0]
    # The shared vertex is stored once, not twice.
    assert coords == [[0.0, 0.0], [1e-5, 0.0], [2e-5, 0.0]]
    assert props["id"] == 1
    assert props["refs"] == "42"


def test_a_different_service_set_breaks_the_run():
    out = publish.coalesce([_row(1, [A, B], ["42"]), _row(2, [B, C], ["42", "43"])])
    assert len(out) == 2


def test_opposite_directions_of_the_same_street_collapse():
    """Valhalla edges are directed, so a two-way street arrives twice. Drawing both
    puts a second line exactly under the first, where it cannot be seen."""
    out = publish.coalesce([_row(1, [A, B], ["42"]), _row(2, [B, A], ["42"])])
    assert len(out) == 1
    assert out[0][0]["id"] == 1


def test_a_one_way_pair_with_different_services_stays_two_features():
    """Here the two lines carry information, so they must survive."""
    out = publish.coalesce([_row(1, [A, B], ["42"]), _row(2, [B, A], ["43"])])
    assert len(out) == 2


def test_a_junction_of_three_is_not_merged_through():
    """Picking a continuation at a fork would draw a line that doubles back."""
    spur = (10, 10)
    out = publish.coalesce(
        [
            _row(1, [A, B], ["42"]),
            _row(2, [B, C], ["42"]),
            _row(3, [B, spur], ["42"]),
        ]
    )
    assert len(out) == 3


def test_a_chain_is_walked_end_to_end_whatever_order_it_arrives_in():
    rows = [_row(3, [C, D], ["42"]), _row(1, [A, B], ["42"]), _row(2, [B, C], ["42"])]
    out = publish.coalesce(rows)
    assert len(out) == 1
    props, coords = out[0]
    assert coords[0] == [0.0, 0.0]
    assert coords[-1] == [3e-5, 0.0]
    assert len(coords) == 4
    assert props["id"] == 1  # the lowest edge id in the run names it


def test_a_closed_loop_still_comes_out_once():
    up, over = (0, 10), (10, 10)
    out = publish.coalesce(
        [
            _row(1, [A, B], ["42"]),
            _row(2, [B, over], ["42"]),
            _row(3, [over, up], ["42"]),
            _row(4, [up, A], ["42"]),
        ]
    )
    assert len(out) == 1
    assert len(out[0][1]) == 5  # four edges, first vertex repeated to close


def test_different_ways_are_never_merged():
    out = publish.coalesce(
        [_row(1, [A, B], ["42"], way_id=1), _row(2, [B, C], ["42"], way_id=2)]
    )
    assert len(out) == 2


def _edge(con, edge_id: int, refs: list[str]) -> None:
    con.execute(
        "INSERT INTO edges VALUES (?, ?, 'Oxford Road', 'secondary', 100.0, "
        "[-2245000, -2240000], [53480000, 53480000], "
        "-2245000, 53480000, -2240000, 53480000)",
        [edge_id, edge_id],
    )
    for i, ref in enumerate(refs):
        con.execute(
            "INSERT INTO edge_services VALUES (?, ?, 'OP1', 1, ?)", [edge_id, ref, 100 - i]
        )


def test_export_writes_one_feature_per_line(con, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT", tmp_path)
    _edge(con, 1, ["42", "43"])
    _edge(con, 2, ["X57"])

    path = publish.export_geojsonl(con, tmp_path / "edges.geojsonl")
    features = [json.loads(line) for line in path.read_text().splitlines()]

    assert len(features) == 2
    props = {f["properties"]["id"]: f["properties"] for f in features}
    assert props[1]["n"] == 2
    assert props[1]["refs"] == "42,43"
    assert props[2]["refs"] == "X57"
    assert features[0]["geometry"]["type"] == "LineString"


def test_refs_are_ordered_by_service_frequency(con, tmp_path, monkeypatch):
    """The capped list should keep the buses that run most, not an arbitrary set."""
    monkeypatch.setattr(config, "OUT", tmp_path)
    _edge(con, 1, ["busiest", "quieter", "quietest"])
    path = publish.export_geojsonl(con, tmp_path / "edges.geojsonl")
    props = json.loads(path.read_text().splitlines()[0])["properties"]
    assert props["refs"].split(",") == ["busiest", "quieter", "quietest"]


def test_a_capped_edge_still_reports_its_true_count(con, tmp_path, monkeypatch):
    """The cap is a backstop, not a routine truncation, and there is no sidecar to
    fall back on -- so a capped feature must say how many services it really has."""
    monkeypatch.setattr(config, "OUT", tmp_path)
    monkeypatch.setattr(config, "MAX_REFS_IN_TILE", 3)
    _edge(con, 1, [str(i) for i in range(10)])
    _edge(con, 2, ["42"])

    path = publish.export_geojsonl(con, tmp_path / "edges.geojsonl")
    props = {
        json.loads(line)["properties"]["id"]: json.loads(line)["properties"]
        for line in path.read_text().splitlines()
    }
    assert len(props[1]["refs"].split(",")) == 3
    assert props[1]["n"] == 10
    assert not (tmp_path / "overflow.json").exists()


def test_wales_fits_under_the_cap(con, tmp_path, monkeypatch):
    """53 services was the busiest edge in the Wales run. The default cap clears it,
    so the common case carries a complete list."""
    monkeypatch.setattr(config, "OUT", tmp_path)
    _edge(con, 1, [str(i) for i in range(53)])
    path = publish.export_geojsonl(con, tmp_path / "edges.geojsonl")
    props = json.loads(path.read_text().splitlines()[0])["properties"]
    assert len(props["refs"].split(",")) == 53


def test_edges_without_geometry_are_skipped(con, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT", tmp_path)
    con.execute(
        "INSERT INTO edges VALUES "
        "(1, 1, 'X', 'residential', 100.0, NULL, NULL, NULL, NULL, NULL, NULL)"
    )
    con.execute("INSERT INTO edge_services VALUES (1, '42', 'OP1', 1, 10)")
    path = publish.export_geojsonl(con, tmp_path / "edges.geojsonl")
    assert path.read_text() == ""


def test_missing_tippecanoe_says_which_fork(monkeypatch, tmp_path):
    monkeypatch.setattr(publish.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="felt/tippecanoe"):
        publish.build_tiles(tmp_path / "edges.geojsonl")


def test_dropping_is_reported_not_silent(caplog):
    """--drop-densest-as-needed sheds features to fit a tile. A build that kept a
    quarter of the network at low zoom must not read as full coverage."""
    stderr = (
        "Going to try keeping the sparsest 42.26% of the features to make it fit\n"
        "Going to try keeping the sparsest 27.47% of the features to make it fit\n"
    )
    with caplog.at_level("INFO"):
        publish._report_dropping(stderr)
    assert "27.5%" in caplog.text
    assert "thinned 2 tiles" in caplog.text


def test_no_dropping_says_so(caplog):
    with caplog.at_level("INFO"):
        publish._report_dropping("tile 6/31/21 written\n")
    assert "no features dropped" in caplog.text


def test_tippecanoe_failure_surfaces_stderr(monkeypatch, tmp_path):
    import subprocess

    monkeypatch.setattr(publish.shutil, "which", lambda _: "/usr/local/bin/tippecanoe")
    monkeypatch.setattr(
        publish.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "boom: out of memory"),
    )
    with pytest.raises(subprocess.CalledProcessError):
        publish.build_tiles(tmp_path / "edges.geojsonl", tmp_path / "o.pmtiles")
