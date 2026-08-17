"""Corridor building and corridor-at-a-time thinning.

Everything here is on synthetic geometry laid out on the equator, where a degree of
longitude and a degree of latitude are the same distance, so a bearing in the test is
the bearing the code sees.
"""

from __future__ import annotations

import json
import math

import pytest

from wayfare import corridors

# 0.004 degrees is about 445 m at the equator, comfortably past `REACH_M`, so an
# end's direction is read off the segment the test drew rather than off the far end.
STEP = 0.004


def write(path, features):
    """A GeoJSONL in the shape `export_edges_geojsonl` writes.

    `features` is a list of `(trips, [(lon, lat), ...])`, optionally with an `n` and a
    way id after it. Every property the real export carries is here in the order it
    writes them, because both filters read them back off the wire by pattern and the
    order is what makes the leftmost match the right one.
    """
    lines = []
    for i, feature in enumerate(features):
        trips, points = feature[0], feature[1]
        n = feature[2] if len(feature) > 2 else 1
        way = feature[3] if len(feature) > 3 else i
        lines.append(
            json.dumps(
                {
                    "type": "Feature",
                    "properties": {
                        "id": i,
                        "way": way,
                        "n": n,
                        "refs": ",".join(str(k) for k in range(n)),
                        "trips": trips,
                        "name": "Cross Street",
                    },
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[lon, lat] for lon, lat in points],
                    },
                },
                separators=(",", ":"),
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


def run(lon, lat, bearing_deg, pieces, trips=10, step=STEP):
    """A straight road as `pieces` separate features, meeting end to end."""
    dx = step * math.cos(math.radians(bearing_deg))
    dy = step * math.sin(math.radians(bearing_deg))
    return [
        (trips, [(lon + i * dx, lat + i * dy), (lon + (i + 1) * dx, lat + (i + 1) * dy)])
        for i in range(pieces)
    ]


def groups(built):
    """The corridors as sets of feature indices, so a test can name them by content."""
    out = {}
    for i, cid in enumerate(built.of_feature):
        out.setdefault(cid, set()).add(i)
    return sorted(out.values(), key=min)


def kept_ids(path):
    return sorted(
        json.loads(line)["properties"]["id"] for line in path.read_text().splitlines()
    )


def test_a_road_cut_into_features_comes_back_as_one_corridor(tmp_path):
    """The whole premise. A road a reader sees as one line is a dozen features end to
    end, and a thinner that ranks features individually keeps some and drops the
    rest, which draws a dashed road."""
    src = write(tmp_path / "edges.geojsonl", run(0.0, 0.0, 0, 6))
    built = corridors.build(src, 0.02)
    assert groups(built) == [{0, 1, 2, 3, 4, 5}]
    assert built.length_m[0] == pytest.approx(6 * STEP * 111_320, rel=1e-3)


def test_a_side_street_does_not_capture_the_road_it_joins(tmp_path):
    """Mutual best fit. Were the side street allowed to claim the through road, a
    trunk would be ranked as two halves and could lose one of them."""
    src = write(
        tmp_path / "edges.geojsonl",
        run(0.0, 0.0, 0, 2) + run(STEP, 0.0, 90, 1),
    )
    built = corridors.build(src, 0.02)
    assert groups(built) == [{0, 1}, {2}]


def test_a_gentle_bend_is_one_road_and_a_sharp_one_is_two(tmp_path):
    """`MAX_TURN_DEG` is where a reader stops following a line through a junction."""
    gentle = write(
        tmp_path / "gentle.geojsonl",
        run(0.0, 0.0, 0, 1) + run(STEP, 0.0, 30, 1),
    )
    assert groups(corridors.build(gentle, 0.02)) == [{0, 1}]

    sharp = write(
        tmp_path / "sharp.geojsonl",
        run(0.0, 0.0, 0, 1) + run(STEP, 0.0, 89, 1),
    )
    assert groups(corridors.build(sharp, 0.02)) == [{0}, {1}]


def test_a_ring_road_is_one_corridor_and_the_walk_terminates(tmp_path):
    """A closed loop has no end to start from. Walking back from the seed has to stop
    when it arrives back at the seed, or building the corridors never returns."""
    ring = []
    for i in range(8):
        a, b = math.radians(45 * i), math.radians(45 * (i + 1))
        ring.append(
            (
                10,
                [
                    (math.cos(a) * 0.01, math.sin(a) * 0.01),
                    (math.cos(b) * 0.01, math.sin(b) * 0.01),
                ],
            )
        )
    built = corridors.build(write(tmp_path / "ring.geojsonl", ring), 0.02)
    assert groups(built) == [set(range(8))]


def test_two_ends_of_one_loop_meeting_at_a_node_are_not_joined_to_each_other(tmp_path):
    """A lollipop: a stem into a loop drawn as a single feature that starts and ends
    at the same point. Joining that feature's two ends closes it onto itself and
    leaves the stem out of the corridor it plainly continues into."""
    loop = [
        (math.cos(math.radians(a)) * 0.005 + 0.005, math.sin(math.radians(a)) * 0.005)
        for a in range(0, 361, 45)
    ]
    src = write(tmp_path / "edges.geojsonl", [(10, [(-STEP, 0.0), (0.0, 0.0)]), (10, loop)])
    built = corridors.build(src, 0.02)
    # Two corridors, and neither is the loop joined to itself: the stem and the loop
    # meet at an angle no reader would follow, so they stay apart.
    assert len(groups(built)) == 2


def test_an_export_under_the_cap_is_handed_back_untouched(tmp_path):
    """Both parts of Ireland are here. A cap that thinned them would be throwing away
    roads to solve a problem they do not have."""
    src = write(tmp_path / "edges.geojsonl", run(0.0, 0.0, 0, 4))
    out = tmp_path / "thin.geojsonl"
    assert corridors.thin(src, out, 100, 0.7, 0.02) is src
    assert not out.exists()


def test_what_is_dropped_is_dropped_whole(tmp_path):
    """The claim the whole approach rests on. Whatever the cap costs, it never costs
    part of a road: the kept set is a union of complete corridors."""
    # One long road and five busy stubs beside it, sharing no node with it and with
    # each other, all inside the one cell so the cap has nowhere else to go.
    features = run(0.0, 0.0, 0, 5, step=0.001)
    for i in range(5):
        features += run(0.006 + i * 0.0005, 0.001, 90, 1, trips=900, step=0.0002)
    src = write(tmp_path / "edges.geojsonl", features)
    built = corridors.build(src, 0.02)
    assert len(groups(built)) == 6

    out = corridors.thin(src, tmp_path / "thin.geojsonl", 5, 0.7, 0.02)
    kept = set(kept_ids(out))
    for group in groups(built):
        assert group <= kept or not (group & kept)
    # And the road that won is the long one, not the busy stubs: `trips` is what the
    # four recorded failures ranked on, and it is why they kept city centres and lost
    # the roads between towns.
    assert kept == {0, 1, 2, 3, 4}


def test_a_quiet_place_is_not_ranked_against_a_busy_one(tmp_path):
    """The same rule the `trips` cap needed. Length is absolute too, so one national
    ranking would take the countryside off the map to spare the cities."""
    features = run(0.0, 0.0, 0, 20, trips=900, step=0.0005)
    features += run(10.0, 0.0, 0, 1, trips=3, step=0.0002)
    src = write(tmp_path / "edges.geojsonl", features)
    out = corridors.thin(src, tmp_path / "thin.geojsonl", 4, 0.7, 0.02)
    # The remote lane is a fortieth the length of anything in the city and still
    # draws, because its own cell ranks it first.
    assert 20 in kept_ids(out)


def test_a_corridor_that_leaves_a_cell_keeps_its_far_end(tmp_path):
    """A trunk road survives on being the best thing in a quiet cell, and it then
    draws over the city it ends in as well. That is what keeps the roads between
    towns whole, and it is why the cap is soft rather than exact."""
    trunk = run(0.0, 0.0, 0, 30, trips=5, step=0.002)
    city = [
        (900, [(0.0605 + i * 0.0002, 0.001), (0.0605 + i * 0.0002, 0.0012)])
        for i in range(40)
    ]
    src = write(tmp_path / "edges.geojsonl", trunk + city)
    out = corridors.thin(src, tmp_path / "thin.geojsonl", 20, 0.7, 0.02)
    kept = set(kept_ids(out))
    assert set(range(30)) <= kept


def test_the_same_export_thins_to_the_same_bytes(tmp_path):
    """A rebuild has to be byte-identical, and a set of corridor ids is a thing that
    can come out ordered differently for nothing."""
    features = run(0.0, 0.0, 0, 6) + run(0.001, 0.001, 90, 6, trips=11)
    src = write(tmp_path / "edges.geojsonl", features)
    first = corridors.thin(src, tmp_path / "a.geojsonl", 6, 0.7, 0.02).read_bytes()
    second = corridors.thin(src, tmp_path / "b.geojsonl", 6, 0.7, 0.02).read_bytes()
    assert first == second


def test_an_unreadable_export_raises_rather_than_thinning_everything_out(tmp_path):
    """`thin` writes back by line number, so a line this cannot read is not one it may
    step over: the kept features would come out carrying somebody else's geometry."""
    src = tmp_path / "edges.geojsonl"
    src.write_text('{"type":"Feature","properties":{"n":1}}\n')
    with pytest.raises(RuntimeError, match="diverged"):
        corridors.build(src, 0.02)


def test_a_road_on_the_prime_meridian_is_still_read(tmp_path):
    """A longitude within about a kilometre of Greenwich is written in scientific
    notation, and Great Britain has 63 of them. The geometry is read as JSON here
    rather than by a number pattern, so this is a guard rather than a fix."""
    src = write(tmp_path / "edges.geojsonl", run(-1.1e-05, 52.219691, 0, 3))
    assert b"e-05" in src.read_bytes()
    assert groups(corridors.build(src, 0.02)) == [{0, 1, 2}]
