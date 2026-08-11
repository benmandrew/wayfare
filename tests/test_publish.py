from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from wayfare import cli, config, licences, publish


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

# Stands in for a DuckDB connection where the test stubs out everything that would
# touch it. `build` refuses a publish with neither a connection nor an export, so
# passing None here would be testing that refusal rather than what the test means.
_A_CONNECTION: Any = object()


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


def write_geojsonl(path, trips, at=(0.0, 0.0)):
    """A GeoJSONL file in the shape `export_geojsonl` writes, one feature per count.

    `trips` may be a flat list, which puts every feature at `at`, or a mapping of
    position to counts, which is how a test says two places rather than one.
    """
    if not isinstance(trips, dict):
        trips = {at: trips}
    lines, i = [], 0
    for (lon, lat), counts in trips.items():
        for t in counts:
            lines.append(
                json.dumps(
                    {
                        "type": "Feature",
                        "properties": {"id": i, "way": i, "n": 1, "trips": t},
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[lon, lat], [lon + 1e-4, lat + 1e-4]],
                        },
                    },
                    separators=(",", ":"),
                )
            )
            i += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


def kept_trips(path):
    return sorted(
        json.loads(line)["properties"]["trips"] for line in path.read_text().splitlines()
    )


def test_missing_tippecanoe_says_which_fork(monkeypatch, tmp_path):
    monkeypatch.setattr(publish.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="felt/tippecanoe"):
        publish.build_tiles(write_geojsonl(tmp_path / "edges.geojsonl", [1]))


def test_a_file_under_its_cap_is_not_filtered_at_all(tmp_path):
    """Both parts of Ireland are here, and a cap that thinned them would be throwing
    away roads to solve a problem they do not have."""
    src = write_geojsonl(tmp_path / "edges.geojsonl", [5, 900, 12, 40])
    assert publish._cell_floors(src, 10, config.OVERVIEW_WEIGHT) == {}
    assert publish._hold_back(src, tmp_path / "far.geojsonl", {}) is src
    assert not (tmp_path / "far.geojsonl").exists()


def test_the_floor_is_the_count_that_keeps_a_cell_its_share(tmp_path):
    src = write_geojsonl(tmp_path / "edges.geojsonl", [10, 20, 30, 40, 50])
    # One cell holding everything, so the weighting has nothing to weigh: a cap of 2
    # of 5 is 2 features whatever it is set to.
    assert publish._cell_floors(src, 2, config.OVERVIEW_WEIGHT) == {(0, 0): 40}
    assert publish._cell_floors(src, 4, config.OVERVIEW_WEIGHT) == {(0, 0): 20}


def test_ties_at_the_floor_are_kept_rather_than_cut(tmp_path):
    """Overshooting hands tippecanoe a few more features than asked for. Undershooting
    throws away roads that would have fitted."""
    src = write_geojsonl(tmp_path / "edges.geojsonl", [7, 7, 7, 1])
    floors = publish._cell_floors(src, 2, config.OVERVIEW_WEIGHT)
    assert floors == {(0, 0): 7}
    assert kept_trips(publish._hold_back(src, tmp_path / "far.geojsonl", floors)) == [
        7,
        7,
        7,
    ]


def test_a_quiet_place_is_not_ranked_against_a_busy_one(tmp_path):
    """The whole point. `trips` is an absolute count, so one national floor ranks the
    map by how urban it is: a 703-trip floor took every feature from 310 of Great
    Britain's 655 populated cells. Each cell gets its own floor instead."""
    src = write_geojsonl(
        tmp_path / "edges.geojsonl",
        {(0.0, 0.0): [900, 800, 700, 600], (10.0, 10.0): [9, 8, 7, 6]},
    )
    floors = publish._cell_floors(src, 4, config.OVERVIEW_WEIGHT)
    # A floor for each place -- the two cells are the same size, so the weighting
    # gives them the same quota -- and the quiet one's is nowhere near the busy one's.
    assert len(floors) == 2
    assert floors[(0, 0)] == 800
    assert floors[(40, 40)] == 8
    # Both places still draw something, which a single national floor would not do.
    assert kept_trips(publish._hold_back(src, tmp_path / "far.geojsonl", floors)) == [
        8,
        9,
        800,
        900,
    ]


def test_every_populated_cell_keeps_at_least_one_feature(tmp_path):
    """Rounding alone empties the five sparsest cells in Great Britain, and an empty
    cell is a hole in the map rather than a thinner patch of it."""
    src = write_geojsonl(
        tmp_path / "edges.geojsonl",
        {(0.0, 0.0): [100] * 500, (10.0, 10.0): [3]},
    )
    floors = publish._cell_floors(src, 50, config.OVERVIEW_WEIGHT)
    assert 3 in kept_trips(publish._hold_back(src, tmp_path / "far.geojsonl", floors))


def test_a_sparse_place_is_kept_whole_and_a_dense_one_pays_for_it(tmp_path):
    """The failure this weighting exists for. A quarter of a city is still a city; a
    quarter of a country lane is nothing. Under the proportional weight Great
    Britain's rural cells drew 15 features at z6 where Ireland's, holding the same
    number of roads, drew 53."""
    src = write_geojsonl(
        tmp_path / "edges.geojsonl",
        {(0.0, 0.0): list(range(1, 101)), (10.0, 10.0): [4, 3, 2, 1]},
    )
    floors = publish._cell_floors(src, 52, config.OVERVIEW_WEIGHT)
    # The sparse cell has no floor at all, which is how a cell says "all of it".
    assert (40, 40) not in floors
    assert (0, 0) in floors
    kept = kept_trips(publish._hold_back(src, tmp_path / "far.geojsonl", floors))
    assert [t for t in kept if t <= 4] == [1, 2, 3, 4]

    # The same input at the proportional weight, which is what was deployed: the
    # sparse cell is cut in half to buy the dense one a couple of dozen more roads it
    # cannot show.
    proportional = publish._cell_floors(src, 52, 1.0)
    assert proportional[(40, 40)] == 3


def test_a_cell_that_cannot_use_its_quota_hands_it_back():
    """Otherwise the cap undershoots by whatever the countryside had no roads to
    spend it on, and the cities are thinned to pay for features that do not exist."""
    sizes = {(0, 0): 1000, (1, 1): 2}
    quotas = publish._quotas(sizes, 500, 0.5)
    assert quotas[(1, 1)] == 2
    assert quotas[(0, 0)] == 498
    assert sum(quotas.values()) == 500


def test_a_road_on_the_prime_meridian_is_still_read(tmp_path):
    """A longitude within about a kilometre of Greenwich is written in scientific
    notation. Great Britain has 63 of them, and a number pattern that cannot read an
    exponent takes them off the map without saying so."""
    src = write_geojsonl(tmp_path / "edges.geojsonl", {(-1.1e-05, 52.219691): [500, 400]})
    assert b"e-05" in src.read_bytes()
    assert publish._cell_floors(src, 1, config.OVERVIEW_WEIGHT) == {(0, 209): 500}


def test_an_unreadable_export_raises_rather_than_filtering_everything_out(tmp_path):
    """A filter that quietly matches nothing builds an empty band, and an empty band
    reads as a region that lost its buses rather than as a bug here."""
    src = tmp_path / "edges.geojsonl"
    src.write_text('{"type":"Feature","properties":{"n":1}}\n')
    with pytest.raises(RuntimeError, match="diverged"):
        publish._cell_floors(src, 1, config.OVERVIEW_WEIGHT)


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

    src = write_geojsonl(tmp_path / "edges.geojsonl", [1, 2, 3])
    publish.build_tiles(src, tmp_path / "bus.pmtiles")

    far, mid, near, detail, join = calls
    assert far[far.index("-Z") + 1] == str(config.MIN_ZOOM)
    assert far[far.index("-z") + 1] == str(config.FAR_ZOOM - 1)
    assert mid[mid.index("-Z") + 1] == str(config.FAR_ZOOM)
    assert mid[mid.index("-z") + 1] == str(config.MID_ZOOM - 1)
    assert near[near.index("-Z") + 1] == str(config.MID_ZOOM)
    assert near[near.index("-z") + 1] == str(config.DETAIL_ZOOM - 1)
    assert detail[detail.index("-Z") + 1] == str(config.DETAIL_ZOOM)
    for name in publish._DETAIL_ONLY:
        assert name in far
        assert name in mid
        assert name in near
        assert name not in detail
    assert join[0] == "tile-join"


def test_the_bands_cover_every_zoom_once_with_no_gap(monkeypatch, tmp_path):
    """A gap between two bands is a zoom the archive simply does not answer, and a
    viewer showing an empty map at one zoom looks like missing data, not a config
    slip."""
    import subprocess

    calls = []

    def fake_run(cmd, **_):
        calls.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(publish.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(publish.subprocess, "run", fake_run)
    src = write_geojsonl(tmp_path / "edges.geojsonl", [1, 2, 3])
    publish.build_tiles(src, tmp_path / "bus.pmtiles")

    spans = [
        (int(c[c.index("-Z") + 1]), int(c[c.index("-z") + 1]))
        for c in calls
        if c[0] == "tippecanoe"
    ]
    covered = [z for lo, hi in spans for z in range(lo, hi + 1)]
    assert covered == list(range(config.MIN_ZOOM, config.MAX_ZOOM + 1))


def test_a_publish_never_shows_a_half_built_archive(monkeypatch, tmp_path):
    """The output directory is served. `server.archives` globs `*.pmtiles` there and
    the viewer loads every archive it is offered, so a band built beside the archive
    was advertised as a region for the length of a publish -- and the archive itself
    was rewritten under clients reading it in byte ranges.

    The glob is spelled out rather than imported so this stays a test of what is on
    disk; it is the same one `server.archives` runs.
    """
    import subprocess

    out = tmp_path / "out" / "great_britain.pmtiles"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"the previous archive")
    seen = []

    def fake_run(cmd, **_):
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"the new one")
        seen.append(
            (sorted(p.name for p in out.parent.glob("*.pmtiles")), out.read_bytes())
        )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(publish.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(publish.subprocess, "run", fake_run)
    src = write_geojsonl(tmp_path / "edges.geojsonl", [1, 2, 3])
    publish.build_tiles(src, out)

    # All four tippecanoe passes and the join: one archive on offer throughout, and
    # it is the one that was there before, whole.
    assert seen == [(["great_britain.pmtiles"], b"the previous archive")] * 5
    # Swapped only at the end, and the scratch directory taken away with it.
    assert out.read_bytes() == b"the new one"
    assert [p.name for p in out.parent.iterdir()] == ["great_britain.pmtiles"]


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
    src = write_geojsonl(tmp_path / "edges.geojsonl", [1, 2, 3])
    publish.build_tiles(src, tmp_path / "bus.pmtiles", **kwargs)
    return calls


def _attribution(cmd):
    return next(a.split("=", 1)[1] for a in cmd if a.startswith("--attribution="))


def test_every_licence_names_where_to_find_it():
    """CC BY 4.0 requires the licence to be identified, which means a URI. A source
    added with a licence nobody wrote a URL for must fail loudly at publish rather
    than ship an archive crediting a licence it does not link to."""
    for region in ("all", *config.FEEDS):
        assert licences.URLS[config.feed(region).licence].startswith("https://")


def test_the_credit_names_the_publisher_the_licence_and_openstreetmap():
    """Two obligations, and the second is the one that is easy to miss: every edge is
    an OSM way, which makes the archive a derived database under ODbL whatever the
    timetable's licence says."""
    credit = config.credit_html("wales")
    assert "Department for Transport" in credit
    assert licences.OGL in credit
    assert licences.URLS[licences.OGL] in credit
    assert "OpenStreetMap" in credit
    assert licences.URLS[licences.ODBL] in credit
    # And it says which half OSM covers, since the viewer's existing OSM line is
    # about the backdrop and this one is about the lines drawn on it.
    assert "Road geometry" in credit


def test_a_region_on_a_different_licence_gets_a_different_credit():
    """The Republic is CC BY 4.0 where every BODS slug is OGL. One string for both
    would be wrong for whichever region is not showing."""
    assert config.credit_html("ireland") != config.credit_html("all")
    assert "National Transport Authority" in config.credit_html("ireland")
    assert licences.URLS[licences.CC_BY_4] in config.credit_html("ireland")
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
    assert licences.URLS[licences.CC_BY_4] in text
    assert licences.OSM_COPYRIGHT in text
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
    assert licences.CC_BY_4 in joined
    assert "OpenStreetMap contributors" in joined and licences.ODBL in joined
    assert " ".join(config.credit_lines("ireland")) == config.credit_text("ireland")


def test_every_zoom_band_is_stamped_with_the_credit(monkeypatch, tmp_path):
    *bands, join = _tippecanoe_calls(monkeypatch, tmp_path)
    assert len(bands) == 4
    for band in bands:
        assert _attribution(band) == config.credit_html()
    # tile-join carries an input's attribution through to the joined archive --
    # measured against tippecanoe 2.79.0, including where only one input has one --
    # so it needs no flag of its own.
    assert join[0] == "tile-join"


def test_the_credit_follows_the_region_rather_than_the_call_site(monkeypatch, tmp_path):
    """Derived from `config.Feed`, so it cannot drift from what `acquire` fetched."""
    far, *_ = _tippecanoe_calls(
        monkeypatch, tmp_path, attribution=config.credit_html("ireland")
    )
    assert "National Transport Authority" in _attribution(far)
    assert licences.OGL not in _attribution(far)


def test_publish_stamps_the_region_it_was_given(monkeypatch, tmp_path):
    """`wayfare publish --region ireland` has to reach tippecanoe, because the
    licence is a property of the feed and not of the machine running the build."""
    seen = {}

    def fake_build_tiles(path, out=None, attribution=None, segments=None):
        seen["credit"] = attribution
        return tmp_path / "bus.pmtiles"

    monkeypatch.setattr(config, "OUT", tmp_path)
    monkeypatch.setattr(publish, "export_geojsonl", lambda con: tmp_path / "e.geojsonl")
    monkeypatch.setattr(publish, "export_segments_geojsonl", lambda con: None)
    monkeypatch.setattr(publish, "contents", lambda con: {"road": True, "operator": False})
    monkeypatch.setattr(publish, "build_tiles", fake_build_tiles)
    publish.build(_A_CONNECTION, region="ireland")
    assert seen["credit"] == config.credit_html("ireland")


# --- where the archive goes ---------------------------------------------------
#
# Three regions are served from one directory, and the viewer labels each of them
# from its filename. So a name is a label as well as a destination, and a publish
# that writes the wrong one is a publish that looks like it worked.


def _built_out(monkeypatch, tmp_path, **kwargs) -> Path:
    """Run `build` against a stubbed tippecanoe and report the path it chose."""
    seen = {}

    def fake_build_tiles(path, out=None, attribution=None, segments=None):
        seen["out"] = out
        return out

    monkeypatch.setattr(publish, "export_geojsonl", lambda con: tmp_path / "e.geojsonl")
    # Stubbed alongside the edge export: this helper is about which path `build`
    # chooses, and it hands `build` a None connection to prove it never reads one.
    monkeypatch.setattr(publish, "export_segments_geojsonl", lambda con: None)
    monkeypatch.setattr(publish, "contents", lambda con: {"road": True, "operator": False})
    monkeypatch.setattr(publish, "build_tiles", fake_build_tiles)
    publish.build(_A_CONNECTION, **kwargs)
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

    def fake_build(con, region=None, out=None, from_export=None):
        seen.update(region=region, out=out, from_export=from_export)
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
    src = write_geojsonl(tmp_path / "edges.geojsonl", [1, 2, 3])
    with pytest.raises(subprocess.CalledProcessError):
        publish.build_tiles(src, tmp_path / "o.pmtiles")


def test_only_the_last_band_may_extend_past_its_top_zoom(monkeypatch, tmp_path):
    """`-z` is a ceiling --extend-zooms-if-still-dropping is allowed to raise. Above
    the detail band there is nothing to collide with; below it, a band that grew into
    the next one's zooms would have tile-join merge both copies of every road."""
    import subprocess

    calls = []

    def fake_run(cmd, **_):
        calls.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(publish.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(publish.subprocess, "run", fake_run)
    src = write_geojsonl(tmp_path / "edges.geojsonl", [1, 2, 3])
    publish.build_tiles(src, tmp_path / "bus.pmtiles")

    *overview, detail, _ = calls
    for band in overview:
        assert "--extend-zooms-if-still-dropping" not in band
    assert "--extend-zooms-if-still-dropping" in detail


def test_a_database_without_a_segments_table_still_publishes():
    """`segments` post-dates Great Britain's database, and `prune` reclaims tables once
    matching is done. A missing one used to raise out of the credit calculation, which
    failed the publish over a mode the region has none of."""
    import duckdb

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE edge_services (edge_id BIGINT)")
    con.execute("INSERT INTO edge_services VALUES (1)")

    assert publish.contents(con) == {"road": True, "operator": False}
    assert publish.export_segments_geojsonl(con) is None


def test_only_the_bands_with_a_cap_read_a_filtered_file(monkeypatch, tmp_path):
    """z10 has never troubled the size limit, and neither has the detail band. Banding
    them in with z8 would cap the loosest zoom at whatever the tightest one needs --
    which is how a cap of 381,000 once took z10 from 943,040 features to 411,255."""
    import subprocess

    calls = []

    def fake_run(cmd, **_):
        calls.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(publish.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(publish.subprocess, "run", fake_run)
    monkeypatch.setattr(config, "OVERVIEW_CAP_FAR", 1)
    monkeypatch.setattr(config, "OVERVIEW_CAP_MID", 2)

    src = write_geojsonl(tmp_path / "edges.geojsonl", [1, 2, 3, 4])
    publish.build_tiles(src, tmp_path / "bus.pmtiles")

    far, mid, near, detail, _ = calls
    assert Path(far[-1]).name == "far.geojsonl"
    assert Path(mid[-1]).name == "mid.geojsonl"
    # The same file object the caller passed, not a copy of it.
    assert near[-1] == str(src)
    assert detail[-1] == str(src)


def test_an_uncapped_band_is_handed_the_export_itself(monkeypatch, tmp_path):
    """None means no cap, which is not the same as a cap nothing reaches: it must not
    cost a pass over the 1.6 GB national export either."""
    import subprocess

    calls = []

    def fake_run(cmd, **_):
        calls.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(publish.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(publish.subprocess, "run", fake_run)
    monkeypatch.setattr(config, "OVERVIEW_CAP_MID", None)

    src = write_geojsonl(tmp_path / "edges.geojsonl", [1, 2, 3, 4])
    publish.build_tiles(src, tmp_path / "bus.pmtiles")

    assert calls[1][-1] == str(src)
    assert not (tmp_path / "mid.geojsonl").exists()


def test_building_from_an_existing_export_needs_no_connection(monkeypatch, tmp_path):
    """A `prune` reclaims the tables `match` needed, so a data root can be left with
    its export and nothing else. Northern Ireland's is exactly that."""
    import subprocess

    calls = []

    def fake_run(cmd, **_):
        calls.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(publish.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(publish.subprocess, "run", fake_run)
    monkeypatch.setattr(config, "OUT", tmp_path / "out")

    def explode(*_a, **_k):
        raise AssertionError("export_geojsonl was called with no connection to use")

    monkeypatch.setattr(publish, "export_geojsonl", explode)

    src = write_geojsonl(tmp_path / "edges.geojsonl", [1, 2, 3])
    out = publish.build(None, region="northern_ireland", from_export=src)
    assert out.exists()
    assert _attribution(calls[0]) == config.credit_html("northern_ireland")


def test_a_missing_export_is_named_rather_than_silently_exported(monkeypatch, tmp_path):
    monkeypatch.setattr(publish.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(config, "OUT", tmp_path / "out")
    with pytest.raises(RuntimeError, match="not there"):
        publish.build(None, from_export=tmp_path / "gone.geojsonl")


def test_publishing_without_a_connection_or_an_export_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT", tmp_path / "out")
    with pytest.raises(ValueError, match="needs a connection"):
        publish.build(None)


def test_from_export_publishes_without_touching_the_database(monkeypatch, tmp_path):
    """The one publish that reaches for a data root whose database is gone must not
    be stopped by the check that every other publish needs."""
    monkeypatch.setattr(config, "WORK", tmp_path)
    write_geojsonl(tmp_path / "edges.geojsonl", [1, 2, 3])

    def no_db():
        raise AssertionError("_require_db ran for a publish that needs no database")

    monkeypatch.setattr(cli, "_require_db", no_db)
    monkeypatch.setattr(
        cli.db, "connect", lambda **_: pytest.fail("a connection was opened")
    )
    seen = {}

    def fake_build(con, region=None, out=None, from_export=None):
        seen.update(con=con, from_export=from_export)
        return tmp_path / "bus.pmtiles"

    monkeypatch.setattr(cli.publish, "build", fake_build)
    assert cli.main(["publish", "--from-export"]) == 0
    assert seen["con"] is None
    assert seen["from_export"] == tmp_path / "edges.geojsonl"


# --- non-road segments --------------------------------------------------------


def _tram(con, pattern_id, lon_e6, lat_e6, ref="T1", trips=50, mode="tram"):
    con.execute(
        "INSERT INTO patterns (pattern_id, route_id, short_name, n_stops, n_trips, "
        "mode, first_seen, last_seen) VALUES (?, 'R9', ?, 2, ?, ?, 'F1', 'F1')",
        [pattern_id, ref, trips, mode],
    )
    con.execute(
        "INSERT INTO segments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            pattern_id,
            mode,
            lon_e6,
            lat_e6,
            min(lon_e6),
            min(lat_e6),
            max(lon_e6),
            max(lat_e6),
        ],
    )


def test_a_region_with_no_segments_writes_no_file(con, tmp_path):
    """None rather than an empty file, so a bus-only region skips the extra
    tippecanoe pass instead of joining an empty layer into every archive."""
    assert publish.export_segments_geojsonl(con, tmp_path / "s.geojsonl") is None
    assert not (tmp_path / "s.geojsonl").exists()


def test_a_segment_becomes_one_feature_carrying_its_mode(con, tmp_path):
    """A segment is a whole pattern's trace, so it is one feature and there is
    nothing to coalesce it with. The mode rides along because the viewer styles a
    tram differently from a ferry."""
    from wayfare import db

    db.set_meta(con, "feed_version", "F1")
    _tram(con, 1, [-2245000, -2240000], [53480000, 53481000], ref="Metrolink")

    path = publish.export_segments_geojsonl(con, tmp_path / "s.geojsonl")
    assert path is not None
    features = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(features) == 1
    assert features[0]["properties"] == {
        "id": 1,
        "mode": "tram",
        "ref": "Metrolink",
        "trips": 50,
    }
    assert features[0]["geometry"]["coordinates"] == [[-2.245, 53.48], [-2.24, 53.481]]


def test_the_segment_export_is_deterministic(con, tmp_path):
    """Two runs must be byte-identical, for the same reason the edge export must:
    it is what makes tiles cacheable and two builds comparable."""
    from wayfare import db

    db.set_meta(con, "feed_version", "F1")
    _tram(con, 7, [-2245000, -2240000], [53480000, 53481000])
    _tram(con, 3, [-2240000, -2235000], [53481000, 53482000], mode="ferry")

    a = publish.export_segments_geojsonl(con, tmp_path / "a.geojsonl")
    b = publish.export_segments_geojsonl(con, tmp_path / "b.geojsonl")
    assert a is not None and b is not None
    assert a.read_bytes() == b.read_bytes()
    # Ordered by pattern_id, so the order is defined rather than incidentally equal.
    assert [json.loads(x)["properties"]["id"] for x in a.read_text().splitlines()] == [3, 7]


def test_segments_are_one_more_pass_joined_into_the_same_archive(monkeypatch, tmp_path):
    """A layer of its own rather than an attribute on `bus`: tile-join keeps
    distinct layer names, so the whole cost is one more tippecanoe pass. One pass
    over the full zoom range, not banded like the roads -- the bands thin the
    quietest roads out of the far view, and a tram line thinned out of its own layer
    would just be missing."""
    import subprocess

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(publish.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(publish.subprocess, "run", fake_run)

    publish.build_tiles(
        write_geojsonl(tmp_path / "edges.geojsonl", [1, 2, 3]),
        tmp_path / "bus.pmtiles",
        segments=tmp_path / "segments.geojsonl",
    )

    far, *_roads, seg, join = calls
    assert seg[seg.index("-l") + 1] == publish.LAYER_SEGMENTS
    assert far[far.index("-l") + 1] == publish.LAYER
    assert seg[seg.index("-Z") + 1] == str(config.MIN_ZOOM)
    assert seg[seg.index("-z") + 1] == str(config.MAX_ZOOM)
    # Not extended: the band already reaches MAX_ZOOM, and only the last road band
    # may grow past its own ceiling.
    assert "--extend-zooms-if-still-dropping" not in seg
    # Every input carries the credit, so a band inspected on its own still says
    # where it came from.
    assert join[0] == "tile-join"
    assert sum(1 for a in seg if a.startswith("--attribution=")) == 1


def test_a_bus_only_region_gets_no_segments_pass(monkeypatch, tmp_path):
    """Passing None must skip the pass rather than join an empty layer into every
    archive that has no trams."""
    import subprocess

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(publish.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(publish.subprocess, "run", fake_run)

    publish.build_tiles(
        write_geojsonl(tmp_path / "edges.geojsonl", [1, 2, 3]), tmp_path / "bus.pmtiles"
    )
    assert len(calls) == 5  # far, mid, near, detail, join -- and nothing else
    assert not any(publish.LAYER_SEGMENTS in c for c in calls)


# --- what the credit claims, and what it must not ------------------------------


def test_the_noun_is_not_bus_once_the_archive_holds_other_modes():
    """ "Bus routes" was accurate while a bus was all there was. An archive holding
    trams and ferries credited as bus routes misdescribes what it contains."""
    assert "Bus routes" not in config.credit_html("wales")
    assert "Routes and timetables" in config.credit_html("wales")


def test_operator_geometry_is_named_in_the_publishers_credit_not_a_third_line():
    """The trace arrives in the same bundle as the timetable and is covered by the
    same licence, so it needs naming rather than crediting separately."""
    credit = config.credit_html("wales", operator=True)
    assert "Routes, timetables and operator geometry" in credit
    # Still two parts, not three.
    assert len(config.credit_parts("wales", operator=True)) == 2


def test_an_archive_with_no_matched_edges_makes_no_odbl_claim():
    """Claiming ODbL over an operator's own survey is wrong in the opposite
    direction from omitting it: it asserts a share-alike condition on data whose
    publisher never imposed one. No OSM way was involved, so no OSM credit."""
    credit = config.credit_html("ireland", road=False, operator=True)
    assert "OpenStreetMap" not in credit
    assert licences.URLS[licences.ODBL] not in credit
    # The publisher is still credited, and CC BY 4.0 makes that a condition.
    assert "National Transport Authority" in credit
    assert licences.URLS[licences.CC_BY_4] in credit


def test_the_default_is_unchanged_for_a_road_only_archive():
    """Every existing caller -- `art`, `/art/meta`, a bare `build_tiles` -- draws
    matched edges and nothing else, so the flags default to exactly that."""
    assert config.credit_parts("wales") == config.credit_parts(
        "wales", road=True, operator=False
    )
    assert [c.what for c in config.credit_parts("wales")] == [
        "Routes and timetables",
        "Road geometry",
    ]


def test_the_credit_follows_what_the_archive_actually_holds(con, tmp_path, monkeypatch):
    """The flags are read from the database rather than assumed, because getting
    either wrong is a licence statement that is false and invisible in the picture."""
    from wayfare import db

    assert publish.contents(con) == {"road": False, "operator": False}

    db.set_meta(con, "feed_version", "F1")
    _tram(con, 1, [-2245000, -2240000], [53480000, 53481000])
    assert publish.contents(con) == {"road": False, "operator": True}

    _edge(con, 5, ["42"])
    assert publish.contents(con) == {"road": True, "operator": True}


def _stub_tippecanoe(monkeypatch, calls):
    import subprocess

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(publish.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(publish.subprocess, "run", fake_run)


def test_a_region_with_no_matched_edges_skips_the_road_bands(monkeypatch, tmp_path):
    """Irish Rail on its own is 331 patterns and not one of them is a road.
    tippecanoe exits 110 on an empty input rather than writing an empty archive, so
    the road bands are skipped -- the same rule the segments pass already follows,
    in the other direction."""
    calls = []
    _stub_tippecanoe(monkeypatch, calls)
    (tmp_path / "edges.geojsonl").write_text("")  # exported, but no features
    (tmp_path / "segments.geojsonl").write_text('{"type":"Feature"}\n')

    publish.build_tiles(
        tmp_path / "edges.geojsonl",
        tmp_path / "ireland.pmtiles",
        segments=tmp_path / "segments.geojsonl",
    )

    seg, join = calls
    assert seg[seg.index("-l") + 1] == publish.LAYER_SEGMENTS
    assert join[0] == "tile-join"
    # No `bus` layer at all, rather than an empty one.
    assert not any(publish.LAYER in c and "-l" in c for c in [seg])


def test_publishing_nothing_at_all_is_refused(monkeypatch, tmp_path):
    """Louder than an empty archive, which loads without complaint and shows a
    blank map -- that reads as a broken viewer rather than as a stage with nothing
    to write."""
    _stub_tippecanoe(monkeypatch, [])
    (tmp_path / "edges.geojsonl").write_text("")

    with pytest.raises(RuntimeError, match="nothing to publish"):
        publish.build_tiles(tmp_path / "edges.geojsonl", tmp_path / "o.pmtiles")
