from __future__ import annotations

import json

import pytest

from wayfare import config, publish


def test_micro_degrees_come_back_as_lon_lat():
    coords = publish._coords([-2245000, -2240000], [53480000, 53480000])
    assert coords == [[-2.245, 53.48], [-2.24, 53.48]]


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


def test_overflow_goes_to_the_sidecar(con, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT", tmp_path)
    monkeypatch.setattr(config, "MAX_REFS_IN_TILE", 3)
    _edge(con, 1, [str(i) for i in range(10)])
    _edge(con, 2, ["42"])

    path = publish.export_geojsonl(con, tmp_path / "edges.geojsonl")
    props = {
        json.loads(line)["properties"]["id"]: json.loads(line)["properties"]
        for line in path.read_text().splitlines()
    }
    # The tile carries a truncated list but the honest count, so the viewer knows.
    assert len(props[1]["refs"].split(",")) == 3
    assert props[1]["n"] == 10

    overflow = json.loads((tmp_path / "overflow.json").read_text())
    assert len(overflow["1"]) == 10
    assert "2" not in overflow  # edges under the cap stay out of the sidecar


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
