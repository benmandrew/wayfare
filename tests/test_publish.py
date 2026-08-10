from __future__ import annotations

import json
from pathlib import Path

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


def test_the_same_service_set_merges_whatever_order_it_is_listed_in():
    """Identity is the service set, not the busiest-first order it happens to be
    listed in. Two services running the same number of trips can come out of the
    aggregate either way round, and splitting the geometry over that splits it over
    nothing -- it also made the export differ from one run to the next."""
    out = publish.coalesce([_row(1, [A, B], ["42", "43"]), _row(2, [B, C], ["43", "42"])])
    assert len(out) == 1
    assert len(out[0][1]) == 3
    # The list shown comes from the lowest edge id, so it does not depend on order.
    assert out[0][0]["refs"] == "42,43"
    assert (
        publish.coalesce([_row(2, [B, C], ["43", "42"]), _row(1, [A, B], ["42", "43"])])[0][
            0
        ]["refs"]
        == "42,43"
    )


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


def test_streaming_gives_the_same_result_as_collapsing_everything_at_once(
    con, tmp_path, monkeypatch
):
    """The export coalesces one way at a time so the whole table is never resident.
    That is only sound because a segment can never span two ways."""
    monkeypatch.setattr(config, "OUT", tmp_path)
    # Two ways, interleaved edge ids, and a fetch small enough to split a way across
    # two chunks -- the buffer must survive the chunk boundary.
    monkeypatch.setattr(publish, "FETCH_ROWS", 2)
    for eid, way, x in [(1, 10, 0), (2, 10, 10), (3, 20, 0), (4, 10, 20), (5, 20, 10)]:
        con.execute(
            "INSERT INTO edges VALUES (?, ?, 'R', 'secondary', 100.0, [?, ?], "
            "[0, 0], ?, 0, ?, 0)",
            [eid, way, x, x + 10, x, x + 10],
        )
        con.execute("INSERT INTO edge_services VALUES (?, '42', 'OP1', 1, 100)", [eid])

    path = publish.export_geojsonl(con, tmp_path / "edges.geojsonl")
    feats = [json.loads(line) for line in path.read_text().splitlines()]

    # Way 10 chains into one 4-point line, way 20 into one 3-point line. Never one
    # feature spanning both, however the rows were chunked.
    assert sorted(len(f["geometry"]["coordinates"]) for f in feats) == [3, 4]
    assert sorted(f["properties"]["way"] for f in feats) == [10, 20]


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


def test_the_overview_band_drops_the_card_only_attributes(monkeypatch, tmp_path):
    """Attributes are stored per feature per zoom, and nothing reads a road name
    when the whole country is a few hundred pixels across."""
    import subprocess

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(publish.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(publish.subprocess, "run", fake_run)

    publish.build_tiles(tmp_path / "edges.geojsonl", tmp_path / "bus.pmtiles")

    overview, detail, join = calls
    assert overview[overview.index("-z") + 1] == str(config.DETAIL_ZOOM - 1)
    assert detail[detail.index("-Z") + 1] == str(config.DETAIL_ZOOM)
    for name in publish._DETAIL_ONLY:
        assert name in overview
        assert name not in detail
    assert join[0] == "tile-join"


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


def test_no_dropping_reports_only_the_size_limit(caplog):
    """It must not claim every zoom holds every road. Sub-pixel geometry is
    discarded at low zoom regardless -- z5 carries a sixth of the features -- and
    saying "nothing was dropped" made a generalised map read as a complete one."""
    with caplog.at_level("INFO"):
        publish._report_dropping("tile 6/31/21 written\n")
    assert "no tile hit the size limit" in caplog.text
    assert "full network" not in caplog.text


# --- attribution -------------------------------------------------------------
#
# A licence condition rather than a label, so these test what reaches the archive
# and not what a docstring says. The credit is stamped into the tiles because that
# is the one place it travels with the data: an archive copied to a bucket keeps
# it, where a line in the viewer or a field in /archives.json would not.


def _tippecanoe_calls(monkeypatch, tmp_path, **kwargs):
    """Run build_tiles against a fake tippecanoe and hand back the argv it built."""
    import subprocess

    calls = []

    def fake_run(cmd, **_):
        calls.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(publish.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(publish.subprocess, "run", fake_run)
    publish.build_tiles(tmp_path / "edges.geojsonl", tmp_path / "bus.pmtiles", **kwargs)
    return calls


def _attribution(cmd):
    return next(a.split("=", 1)[1] for a in cmd if a.startswith("--attribution="))


def test_every_licence_names_where_to_find_it():
    """CC BY 4.0 requires the licence to be identified, which means a URI. A source
    added with a licence nobody wrote a URL for must fail loudly at publish rather
    than ship an archive crediting a licence it does not link to."""
    for region in ("all", *config.FEEDS):
        assert config.LICENCE_URLS[config.feed(region).licence].startswith("https://")


def test_the_credit_names_the_publisher_the_licence_and_openstreetmap():
    """Two obligations, and the second is the one that is easy to miss: every edge is
    an OSM way, which makes the archive a derived database under ODbL whatever the
    timetable's licence says."""
    credit = config.credit_html("wales")
    assert "Department for Transport" in credit
    assert config.OGL in credit
    assert config.LICENCE_URLS[config.OGL] in credit
    assert "OpenStreetMap" in credit
    assert config.LICENCE_URLS[config.ODBL] in credit
    # And it says which half OSM covers, since the viewer's existing OSM line is
    # about the backdrop and this one is about the lines drawn on it.
    assert "Road geometry" in credit


def test_a_region_on_a_different_licence_gets_a_different_credit():
    """The Republic is CC BY 4.0 where every BODS slug is OGL. One string for both
    would be wrong for whichever region is not showing."""
    assert config.credit_html("ireland") != config.credit_html("all")
    assert "National Transport Authority" in config.credit_html("ireland")
    assert config.LICENCE_URLS[config.CC_BY_4] in config.credit_html("ireland")
    assert "Department for Transport" not in config.credit_html("ireland")
    # Northern Ireland is OGL like GB but a different publisher, so the licence
    # alone does not decide the string.
    assert "Translink" in config.credit_html("northern_ireland")


def test_the_plain_text_credit_says_the_same_thing_without_markup():
    """What a PNG tEXt chunk or an SVG <metadata> block can carry. The links have to
    survive, since a licence identified by name alone is not identified."""
    text = config.credit_text("ireland")
    assert "<a href" not in text and "&copy;" not in text
    assert "National Transport Authority" in text
    assert config.LICENCE_URLS[config.CC_BY_4] in text
    assert config.OSM_COPYRIGHT in text
    # tEXt is Latin-1, so the sign has to be one of the 256 characters it allows.
    text.encode("latin-1")


def test_dropping_the_links_keeps_every_name_and_still_credits_both():
    """What `art` burns into a corner, where a URI is unclickable and twice the
    length of the line that has to fit. It is the same credit, shortened -- not a
    second one, which is the failure this whole arrangement exists to prevent."""
    lines = config.credit_lines("ireland", links=False)
    assert len(lines) == len(config.credit_parts("ireland")) == 2
    joined = " ".join(lines)
    assert "http" not in joined
    assert "National Transport Authority" in joined
    assert config.CC_BY_4 in joined
    assert "OpenStreetMap contributors" in joined and config.ODBL in joined
    assert " ".join(config.credit_lines("ireland")) == config.credit_text("ireland")


def test_both_zoom_bands_are_stamped_with_the_credit(monkeypatch, tmp_path):
    overview, detail, join = _tippecanoe_calls(monkeypatch, tmp_path)
    assert _attribution(overview) == config.credit_html()
    assert _attribution(detail) == config.credit_html()
    # tile-join carries an input's attribution through to the joined archive --
    # measured against tippecanoe 2.79.0, including where only one input has one --
    # so it needs no flag of its own.
    assert join[0] == "tile-join"


def test_the_credit_follows_the_region_rather_than_the_call_site(monkeypatch, tmp_path):
    """Derived from `config.Feed`, so it cannot drift from what `acquire` fetched."""
    overview, _, _ = _tippecanoe_calls(
        monkeypatch, tmp_path, attribution=config.credit_html("ireland")
    )
    assert "National Transport Authority" in _attribution(overview)
    assert config.OGL not in _attribution(overview)


def test_publish_stamps_the_region_it_was_given(monkeypatch, tmp_path):
    """`wayfare publish --region ireland` has to reach tippecanoe, because the
    licence is a property of the feed and not of the machine running the build."""
    seen = {}

    def fake_build_tiles(path, out=None, attribution=None):
        seen["credit"] = attribution
        return tmp_path / "bus.pmtiles"

    monkeypatch.setattr(config, "OUT", tmp_path)
    monkeypatch.setattr(publish, "export_geojsonl", lambda con: tmp_path / "e.geojsonl")
    monkeypatch.setattr(publish, "build_tiles", fake_build_tiles)
    publish.build(None, region="ireland")
    assert seen["credit"] == config.credit_html("ireland")


# --- where the archive goes ---------------------------------------------------
#
# Three regions are served from one directory, and the viewer labels each of them
# from its filename. So a name is a label as well as a destination, and a publish
# that writes the wrong one is a publish that looks like it worked.


def _built_out(monkeypatch, tmp_path, **kwargs) -> Path:
    """Run `build` against a stubbed tippecanoe and report the path it chose."""
    seen = {}

    def fake_build_tiles(path, out=None, attribution=None):
        seen["out"] = out
        return out

    monkeypatch.setattr(publish, "export_geojsonl", lambda con: tmp_path / "e.geojsonl")
    monkeypatch.setattr(publish, "build_tiles", fake_build_tiles)
    publish.build(None, **kwargs)
    return seen["out"]


def test_an_archive_is_named_after_its_region():
    assert config.archive_name("ireland") == "ireland.pmtiles"
    assert config.archive_name("northern_ireland") == "northern_ireland.pmtiles"
    # `all` is the BODS scope for the whole of Great Britain rather than a place,
    # and the viewer builds a region's label from the filename, so the archive is
    # named for the place. An `all.pmtiles` would label a map "all".
    assert config.archive_name("all") == "great_britain.pmtiles"


def test_a_region_that_would_escape_the_output_directory_names_no_archive():
    """The only place a region slug reaches the filesystem."""
    for bad in ("../served", "out/wales", ".", ".."):
        with pytest.raises(ValueError):
            config.archive_name(bad)


def test_the_default_archive_name_has_not_moved(monkeypatch, tmp_path):
    """A filename is a deployment's contract -- a mount, an object key, the viewer's
    own fallback. Deriving it from the region silently would leave whatever is being
    served stale while the publish still reported success."""
    monkeypatch.setattr(config, "OUT", tmp_path)
    assert _built_out(monkeypatch, tmp_path, region="ireland") == tmp_path / "bus.pmtiles"


def test_publish_writes_where_it_is_told(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OUT", tmp_path)
    named = tmp_path / "ireland.pmtiles"
    assert _built_out(monkeypatch, tmp_path, region="ireland", out=named) == named


def test_the_default_refuses_to_leave_a_region_archive_stale(monkeypatch, tmp_path):
    """An archive already named for this region says the caller has published it by
    name before and has left the flag off this time. Writing bus.pmtiles beside it
    would update nothing anyone serves."""
    monkeypatch.setattr(config, "OUT", tmp_path)
    served = tmp_path / "ireland.pmtiles"
    served.write_bytes(b"the archive being served")

    with pytest.raises(RuntimeError, match="--name-by-region"):
        _built_out(monkeypatch, tmp_path, region="ireland")

    assert served.read_bytes() == b"the archive being served"
    assert not (tmp_path / "bus.pmtiles").exists()


def test_naming_a_region_replaces_that_regions_archive(monkeypatch, tmp_path):
    """The guard is about publishing one region past another, not about republishing.
    Asking for the region by name is asking for the file that region owns."""
    monkeypatch.setattr(config, "OUT", tmp_path)
    served = tmp_path / "ireland.pmtiles"
    served.write_bytes(b"last month")
    assert _built_out(monkeypatch, tmp_path, region="ireland", out=served) == served


def test_another_regions_archive_does_not_block_the_default(monkeypatch, tmp_path):
    """One data root holds one region, so a neighbour's archive sitting in the served
    directory is not this region's business."""
    monkeypatch.setattr(config, "OUT", tmp_path)
    (tmp_path / "great_britain.pmtiles").write_bytes(b"")
    assert _built_out(monkeypatch, tmp_path, region="ireland") == tmp_path / "bus.pmtiles"


# --- the publish subcommand ----------------------------------------------------


def _run_publish(monkeypatch, tmp_path, argv, build=None):
    """Run the CLI with the database and the tile build stubbed out."""
    import types

    from wayfare import cli

    for name in ("DATA", "RAW", "WORK", "OUT"):
        monkeypatch.setattr(config, name, tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "wayfare.duckdb")
    (tmp_path / "wayfare.duckdb").write_bytes(b"")
    monkeypatch.setattr(
        cli.db, "connect", lambda **_: types.SimpleNamespace(close=lambda: None)
    )

    seen = {}

    def fake_build(con, region=None, out=None):
        seen.update(region=region, out=out)
        if build is not None:
            return build(con, region=region, out=out)
        return out or tmp_path / "bus.pmtiles"

    monkeypatch.setattr(cli.publish, "build", fake_build)
    return cli.main(argv), seen


def test_the_publish_command_can_name_the_archive_after_the_region(monkeypatch, tmp_path):
    """Which is what makes republishing three regions three commands and no renaming.
    --region keeps its licence meaning; this adds the filename to it."""
    code, seen = _run_publish(
        monkeypatch, tmp_path, ["publish", "--region", "ireland", "--name-by-region"]
    )
    assert code == 0
    assert seen["out"] == tmp_path / "ireland.pmtiles"
    assert seen["region"] == "ireland"


def test_the_publish_command_takes_an_explicit_path(monkeypatch, tmp_path):
    where = tmp_path / "served" / "roi.pmtiles"
    code, seen = _run_publish(monkeypatch, tmp_path, ["publish", "--out", str(where)])
    assert code == 0
    assert seen["out"] == where


def test_the_publish_command_still_defaults_to_the_old_name(monkeypatch, tmp_path):
    """No flag, no derivation: the path is left for `build` to decide, which is
    bus.pmtiles and its guard."""
    code, seen = _run_publish(monkeypatch, tmp_path, ["publish"])
    assert code == 0
    assert seen["out"] is None


def test_a_path_and_a_region_name_are_not_given_together(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        _run_publish(
            monkeypatch, tmp_path, ["publish", "--out", "x.pmtiles", "--name-by-region"]
        )


def test_a_refused_name_is_an_error_not_a_traceback(monkeypatch, tmp_path):
    """`build` raises when the default would leave a region archive stale, and the
    command reports it the way every other stage reports a refusal."""

    def refuse(con, region=None, out=None):
        raise RuntimeError("ireland.pmtiles would be left stale")

    code, _ = _run_publish(
        monkeypatch, tmp_path, ["publish", "--region", "ireland"], build=refuse
    )
    assert code == 1


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
