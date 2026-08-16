"""The shared palette, and the two copies of it that must not drift.

`wayfare/map.toml` is the source, `web/palette.js` is generated from it and
committed, and the viewer reads the generated file. Three things can go wrong and
none of them is an error at run time: the generated file goes stale against the
TOML, the page grows a colour of its own again, or the derivation changes what it
produces from unchanged inputs. One test each.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from wayfare import palette

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
GENERATED = WEB / "palette.js"


# Every colour the viewer painted before the derivation moved out of it, dumped
# from the page's own `rampFrom` under node and pasted here. This is the pin: the
# OKLab in `wayfare/palette.py` is a port, and a port that is a shade out on one
# step of one mode is a map nobody would notice had changed.
#
# Read as "the seed colour is the top of its own ramp" -- the last entry of each
# list is the flat colour the legend shows for that mode.
EXPECTED_RAMPS = {
    "light": {
        "tram": ["#fcafd1", "#ef97bf", "#e17fad", "#d3679c", "#c44e8b", "#b5327a"],
        "metro": ["#c595da", "#b67fce", "#a768c2", "#9850b5", "#8937a8", "#7a169b"],
        "rail": ["#c7aa90", "#b89778", "#a88360", "#997049", "#895d31", "#7a4b16"],
        "ferry": ["#c7858f", "#ba6f7b", "#ac5968", "#9e4255", "#8f2a43", "#800732"],
        "cable_tram": ["#bdacf0", "#ab96e5", "#9a81d9", "#8a6ccd", "#7956c1", "#6a3fb5"],
        "funicular": ["#9fceaa", "#86bd93", "#6dac7d", "#539b68", "#378b52", "#0f7a3d"],
        "aerial": ["#dccc9f", "#cbb885", "#bba56a", "#aa924f", "#9a7f31", "#8a6d00"],
        "monorail": ["#929ca1", "#7f8a90", "#6c797f", "#5a686f", "#48575f", "#37474f"],
        "other": ["#c6c6c6", "#b3b3b3", "#a1a1a1", "#8e8e8e", "#7c7c7c", "#6b6b6b"],
    },
    "dark": {
        "tram": ["#88155b", "#9f2e6f", "#b74583", "#cf5a98", "#e770ae", "#ff86c4"],
        "metro": ["#630081", "#78149a", "#8b2dae", "#9e42c2", "#b156d6", "#c56aeb"],
        "rail": ["#6e4100", "#875205", "#9d6624", "#b37b3b", "#c99051", "#e0a566"],
        "ferry": ["#77002c", "#920038", "#af0044", "#ca1152", "#df3163", "#f54874"],
        "cable_tram": ["#543883", "#674b98", "#7a5fad", "#8e73c3", "#a287d9", "#b79cf0"],
        "funicular": ["#00602e", "#00773a", "#258d4d", "#40a362", "#58b977", "#6fd08c"],
        "aerial": ["#675200", "#806600", "#977c19", "#ae9236", "#c5a94f", "#ddc067"],
        "monorail": ["#364850", "#475962", "#586b74", "#6a7d87", "#7d919a", "#90a4ae"],
        "other": ["#505050", "#646464", "#787878", "#8d8d8d", "#a3a3a3", "#b9b9b9"],
    },
}

# `modeMid` is taken at u = 0.5 of a three-step ramp rather than by indexing the
# six-step one, which has no middle step. Pinned separately because that choice is
# easy to "simplify" into `ramp[2]` or `ramp[3]`, and both are wrong by a shade.
EXPECTED_MIDS = {
    "light": {"tram": "#da73a5", "rail": "#a07a55", "other": "#979797"},
    "dark": {"tram": "#c3508e", "rail": "#a87030", "other": "#838383"},
}


def test_the_mode_ramps_are_what_the_viewer_used_to_compute():
    ink = palette.load()
    for theme, ramps in EXPECTED_RAMPS.items():
        assert ink.mode_ramps[theme] == ramps, theme


def test_a_mode_ramp_ends_on_the_colour_the_legend_shows():
    """The seed is the top of the ramp, which is what makes the key truthful: the
    busiest tram is drawn in the colour the key calls tram."""
    ink = palette.load()
    for theme in palette.THEMES:
        for mode, seed in ink.mode_seeds[theme].items():
            assert ink.mode_ramps[theme][mode][-1] == seed, (theme, mode)


def test_the_track_colour_is_the_middle_of_a_three_step_ramp():
    ink = palette.load()
    for theme, mids in EXPECTED_MIDS.items():
        for mode, want in mids.items():
            assert ink.mode_mids[theme][mode] == want, (theme, mode)


def test_the_generated_file_is_current():
    """The one drift this arrangement can still have, and CI runs the same check."""
    generator = ROOT / "scripts" / "palette_js.py"
    result = subprocess.run(
        [sys.executable, str(generator), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_the_viewer_holds_no_colour_of_its_own():
    """A hex literal on the page is a colour that cannot be reached from the TOML.

    This is what the whole arrangement is for: the page used to hold twelve of
    them and derive forty-eight more, and `coverage.draw` had no way to reach any
    of it. `base.css` is deliberately not included -- panel chrome is the page's
    own business and is not painted onto the map.
    """
    source = (WEB / "index.html").read_text()
    # The generated file is the one place a colour is allowed to be written.
    assert re.search(r"#[0-9a-fA-F]{6}\b", GENERATED.read_text())
    stray = re.findall(r"#[0-9a-fA-F]{6}\b", source)
    assert stray == [], f"{len(stray)} colours on the page: {sorted(set(stray))}"


def test_the_viewer_computes_no_colour_of_its_own():
    """The derivation moved to Python whole, so none of its parts may remain.

    A leftover `rampFrom` that nothing calls is not harmless: it is the copy the
    next change edits, leaving the page and the pipeline generating different
    ramps from the same seeds.
    """
    source = (WEB / "index.html").read_text()
    for name in ("rampFrom", "fitChroma", "hexToOklch", "oklchToHex", "RAMP_LOW"):
        assert name not in source, name


def test_the_page_loads_the_generated_palette():
    source = (WEB / "index.html").read_text()
    assert re.search(r'<script src="palette\.js"></script>', source)


@pytest.mark.parametrize("theme", palette.THEMES)
def test_the_road_ramp_runs_from_its_first_colour_to_its_last(theme):
    """Off both ends of the scale the ramp clamps, as MapLibre's `interpolate`
    does, so a road below the first stop is not drawn in nothing."""
    ink = palette.load()
    quiet = ink.road_rgb(theme, 0.0)
    busy = ink.road_rgb(theme, 1_000_000.0)
    assert quiet == palette.hex_to_rgb(ink.road_ramp[theme][0])
    assert busy == palette.hex_to_rgb(ink.road_ramp[theme][-1])


def test_a_road_with_no_trips_is_drawn_in_the_unavailable_grey():
    """Not the first ramp colour: an archive that cannot answer must not read as a
    country with no buses."""
    ink = palette.load()
    for theme in palette.THEMES:
        assert ink.road_rgb(theme, None) == palette.hex_to_rgb(ink.unavailable(theme))


def test_an_unknown_mode_falls_back_rather_than_raising():
    """An archive built by a newer pipeline is the ordinary case here."""
    ink = palette.load()
    got = ink.mode_rgb("dark", "hovercraft", None)
    assert got == palette.hex_to_rgb(ink.mode_mids["dark"][palette.FALLBACK_MODE])


def test_the_ramp_position_is_monotonic_in_journeys():
    ink = palette.load()
    seen = [ink.position(t) for t in (0.0, 7.0, 70.0, 700.0, 7000.0, 70_000.0)]
    assert seen == sorted(seen)
    assert seen[0] == 0.0 and seen[-1] == 1.0


def test_the_generated_file_parses_as_the_object_the_page_expects():
    """A generator that writes valid JavaScript nobody can index is no better than
    one that writes nothing, and the page reads these keys by name."""
    text = GENERATED.read_text()
    body = text[text.index("{") : text.rindex("}") + 1]
    data = json.loads(body)
    for key in (
        "layers",
        "roadRamp",
        "rampSteps",
        "rampAt",
        "tripsPer",
        "rampNeeds",
        "modes",
        "fallbackMode",
        "modeLabel",
        "modeRamp",
        "modeMid",
        "hover",
        "unavailable",
        "trackDefaultMode",
    ):
        assert key in data, key
    # The fallback carries a ramp like any other mode but is not listed as one, or
    # the paint expressions would match it before reaching their default branch.
    assert data["fallbackMode"] not in data["modes"]
    assert data["fallbackMode"] in data["modeRamp"]["dark"]


def test_the_default_archive_is_the_one_publish_writes():
    """One name in one place. It was written once in `publish` and four times
    across the two pages, and renaming it under a running deployment would have
    left them looking for a file nothing writes."""
    from wayfare import publish

    assert palette.load().default_archive == publish.DEFAULT_ARCHIVE


def test_the_head_prefetch_falls_back_to_the_same_archive():
    """The one site that cannot read the palette, so it is pinned instead.

    The prefetch block is first on the page deliberately -- it overlaps a round
    trip that the comments around it are measurements of -- and loading
    `palette.js` ahead of it would give that saving back. So the name is written
    out there, and this is what stops the two drifting.
    """
    head = (WEB / "index.html").read_text().split("<script src=", 1)[0]
    assert f'["./{palette.load().default_archive}"]' in head


def test_the_detail_band_list_is_the_one_publish_strips_by():
    from wayfare import publish

    ink = palette.load()
    assert ink.detail_only == publish._DETAIL_ONLY
    # The sentinel only identifies the detail band if the overview bands lack it.
    assert ink.detail_sentinel in ink.detail_only


def test_a_sentinel_outside_the_stripped_list_is_refused(tmp_path):
    """The failure this arrangement exists to prevent, made loud.

    A sentinel every band carries marks every feature as detail-band, and the
    viewer then reads an edge id as a way id with nothing to show for it. Caught
    when the file is read rather than when a reader hovers.
    """
    source = palette.MAP_TOML.read_text().replace('sentinel = "refs"', 'sentinel = "n"')
    broken = tmp_path / "map.toml"
    broken.write_text(source)
    with pytest.raises(ValueError, match="detail_only"):
        palette.load(broken)


@pytest.mark.parametrize(
    ("swap", "message"),
    [
        # The three relations between one value and another. JSON Schema states
        # the shape of each field and has no way to say any of these, so the
        # schema names `load` as what checks them and this is that check.
        ("at = [3, 10, 30, 100, 300, 1000]||at = [3, 10, 30, 100, 30, 1000]", "ascending"),
        ('default_mode = "rail"||default_mode = "hovercraft"', "default_mode"),
        # The sentinel is the fourth of these and has a test of its own above,
        # because what it costs when it is wrong takes a paragraph to say.
    ],
)
def test_a_file_whose_values_disagree_with_each_other_is_refused(tmp_path, swap, message):
    old, new = swap.split("||")
    source = palette.MAP_TOML.read_text()
    assert old in source, old
    broken = tmp_path / "map.toml"
    broken.write_text(source.replace(old, new, 1))
    with pytest.raises(ValueError, match=message):
        palette.load(broken)


def test_the_roam_box_is_the_one_art_warns_against():
    """`util.js` and `art.ISLES` held the same four numbers, each with a comment
    naming the other as its twin."""
    from wayfare.art import geometry

    west, south, east, north = palette.load().roam
    assert (geometry.ISLES.min_lon, geometry.ISLES.min_lat) == (west, south)
    assert (geometry.ISLES.max_lon, geometry.ISLES.max_lat) == (east, north)


def test_the_committed_coastline_covers_the_roam_box():
    """`docs/coastline.json` is clipped to `map.toml`'s roam box rather than to
    the README map's own frame, which is what lets the frame move without the
    coastline being regenerated. A file clipped to something narrower would draw
    a coast that stops partway for no reason the picture can show."""
    coast = json.loads((ROOT / "docs" / "coastline.json").read_text())
    west, south, east, north = palette.load().roam
    assert coast, "no coastline runs"
    xs = [x for run in coast for x, _y in run]
    ys = [y for run in coast for _x, y in run]
    # Every point inside the box, with the margin a kept segment's far end can
    # reach: a segment is kept when either end is inside, so one end may sit out.
    assert min(xs) > west - 2 and max(xs) < east + 2
    assert min(ys) > south - 2 and max(ys) < north + 2
    # And it holds the land in that box rather than a corner of it. Not the box's
    # own edges: there is open Atlantic between -11.5 and Ireland's west coast, so
    # a coastline reaching the western edge would be a coastline of something that
    # is not there. These are the four extremes of land instead.
    assert min(xs) < -10.0, "no Kerry"
    assert max(xs) > 1.5, "no East Anglia"
    assert min(ys) < 50.2, "no Cornwall"
    assert max(ys) > 60.5, "no Shetland"
    assert all(len(run) > 1 for run in coast), "a one-point run draws nothing"


def test_both_pages_take_their_shared_values_from_the_generated_file():
    """`art.html` held a bare `"source-layer": "bus"` and its own copy of the
    archive fallback, neither behind a constant."""
    for page in ("index.html", "art.html"):
        source = (WEB / page).read_text()
        assert '<script src="palette.js"></script>' in source, page
        assert '"source-layer": "bus"' not in source, page


def test_the_layer_names_are_the_ones_publish_writes():
    """The names tippecanoe is given and the names MapLibre asks for, from one
    file. A source-layer the archive does not carry draws nothing and says
    nothing."""
    from wayfare import publish

    layers = palette.load().layers
    assert layers["road"] == publish.LAYER
    assert layers["segments"] == publish.LAYER_SEGMENTS
    assert layers["track"] == publish.LAYER_TRACK
