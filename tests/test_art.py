from __future__ import annotations

import math

import pytest

from wayfare import art


def test_presets_are_well_formed():
    for name, b in art.PRESETS.items():
        assert b.min_lon < b.max_lon and b.min_lat < b.max_lat, name
        # Everything should sit inside a generous box around these islands.
        assert b.min_lon >= -9.0 and b.max_lon <= 2.5, name
        assert b.min_lat >= 49.0 and b.max_lat <= 61.5, name


def test_degenerate_bounds_are_rejected():
    with pytest.raises(ValueError, match="degenerate"):
        art.Bounds(-2.0, 53.0, -2.0, 53.5)


def test_resolve_names_the_alternatives():
    assert art.resolve("greater_manchester") is art.PRESETS["greater_manchester"]
    with pytest.raises(KeyError, match="greater_manchester"):
        art.resolve("manchester")


def test_resolve_passes_bounds_through():
    b = art.Bounds(-2.0, 53.0, -1.0, 54.0)
    assert art.resolve(b) is b


# --- Projection -------------------------------------------------------------


def test_projection_fills_the_canvas_it_was_fitted_to():
    b = art.Bounds(-2.75, 53.32, -1.90, 53.70)
    h = art.Projection.canvas_height(b, 1000)
    proj = art.Projection.fit(b, 1000, h)

    x_left, y_bottom = proj(b.min_lon, b.min_lat)
    x_right, y_top = proj(b.max_lon, b.max_lat)
    assert (x_left, y_top) == pytest.approx((0.0, 0.0), abs=0.5)
    assert (x_right, y_bottom) == pytest.approx((1000.0, h), abs=0.5)


def test_north_is_up():
    b = art.Bounds(-2.0, 53.0, -1.0, 54.0)
    proj = art.Projection.fit(b, 500, 500)
    _, y_south = proj(-1.5, 53.1)
    _, y_north = proj(-1.5, 53.9)
    assert y_north < y_south


def test_letterboxing_rather_than_stretching():
    """A square canvas over a wide window must centre the content, not shear it.

    Scaling the axes independently is the failure this guards against: it would
    make every road angle wrong, which `spectrum` depends on being right.
    """
    b = art.Bounds(-4.0, 53.0, -1.0, 53.5)  # much wider than tall
    proj = art.Projection.fit(b, 600, 600)
    x, y, w, h = proj.content_rect(b)
    assert w == pytest.approx(600.0, abs=1.0)
    assert h < w
    assert x == pytest.approx(0.0, abs=1.0)
    assert y == pytest.approx((600 - h) / 2, abs=1.0)  # centred


def test_mercator_is_conformal_so_bearings_survive():
    """`spectrum` maps screen angle to hue and calls it a compass bearing. That is
    only true because Mercator preserves angles, so it is worth pinning."""
    b = art.Bounds(-2.30, 53.45, -2.20, 53.50)
    proj = art.Projection.fit(b, 1000, 1000)
    # A due-east segment must come out horizontal, and a due-north one vertical.
    assert proj(-2.28, 53.47)[1] == pytest.approx(proj(-2.22, 53.47)[1], abs=1e-6)
    assert proj(-2.25, 53.46)[0] == pytest.approx(proj(-2.25, 53.49)[0], abs=1e-6)
    # A 45-degree bearing stays 45 degrees on screen.
    lat = 53.475
    d_lat = 0.01
    d_lon = d_lat / math.cos(math.radians(lat))  # equal ground distance east
    (xa, ya), (xb, yb) = proj(-2.25, lat), proj(-2.25 + d_lon, lat + d_lat)
    assert abs(xb - xa) == pytest.approx(abs(yb - ya), rel=1e-3)


# --- Windowing --------------------------------------------------------------


def test_hits_is_a_bbox_overlap_not_containment():
    b = art.Bounds(-2.30, 53.45, -2.20, 53.50)
    # A road crossing the window from outside on both sides still belongs in it.
    assert b.hits([-2.50, -2.10], [53.47, 53.47])
    assert b.hits([-2.25], [53.47])
    assert not b.hits([-2.40, -2.35], [53.47, 53.47])
    assert not b.hits([-2.25], [53.60])


# --- Styles -----------------------------------------------------------------


def test_every_style_is_registered_with_a_drawer():
    assert set(art.STYLES) >= {"density", "spectrum", "strands"}
    for name, spec in art.STYLES.items():
        assert callable(spec.draw), name
        assert spec.blurb, name


def test_unknown_style_lists_the_known_ones(tmp_path):
    with pytest.raises(KeyError, match="density"):
        art.render("cardiff", style="nonesuch", out_path=tmp_path / "x.png", edges=[])


def test_unknown_output_format_fails_before_querying(tmp_path):
    """Rejecting the suffix must not cost a full window query first."""
    with pytest.raises(ValueError):
        art.render("cardiff", style="density", out_path=tmp_path / "x.tiff", edges=[])


# --- Raw windows ------------------------------------------------------------


def test_resolve_accepts_a_raw_window():
    b = art.resolve("-3.32,51.42,-3.08,51.57")
    assert (b.min_lon, b.min_lat, b.max_lon, b.max_lat) == (-3.32, 51.42, -3.08, 51.57)


def test_swapped_lat_lon_warns_because_it_cannot_raise(caplog):
    """A UK latitude near 51 is a valid longitude and a UK longitude near -3 is a
    valid latitude, so lat,lon order parses cleanly. Range checks cannot catch it;
    only noticing that the window is nowhere near the data can."""
    with caplog.at_level("WARNING"):
        b = art.parse_bbox("51.42,-3.32,51.57,-3.08")
    assert b.min_lon == 51.42  # parsed, not rejected
    assert "outside the British Isles" in caplog.text
    assert "lon first" in caplog.text


def test_a_real_uk_window_warns_about_nothing(caplog):
    with caplog.at_level("WARNING"):
        art.parse_bbox("-3.32,51.42,-3.08,51.57")
    assert caplog.text == ""


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("-3.3,51.4,-3.0", "got 3"),
        ("-3.3,51.4,-3.0,51.6,7", "got 5"),
        ("-3.3,51.4,-3.0,north", "not a number"),
        ("-200,51.4,-3.0,51.6", "longitudes run"),
        ("-3.3,100,-3.0,101", "latitudes run"),
        ("-3.3,51.4,-3.0,51.4", "degenerate"),
    ],
)
def test_bad_windows_say_why(text, match):
    with pytest.raises(ValueError, match=match):
        art.parse_bbox(text)


def test_preset_names_still_win():
    assert art.resolve("cardiff") is art.PRESETS["cardiff"]


def test_unknown_name_mentions_the_window_form():
    with pytest.raises(KeyError, match="minlon"):
        art.resolve("swansea")


# --- Streaming ---------------------------------------------------------------


def _art_edge(con, edge_id, lon, trips, services=("42",), agency="OP1"):
    con.execute(
        "INSERT INTO edges VALUES (?, 1, 'R', 'secondary', 100.0, [?, ?], "
        "[51480000, 51480000], ?, 51480000, ?, 51480000)",
        [edge_id, lon, lon + 1000, lon, lon + 1000],
    )
    for s in services:
        con.execute(
            "INSERT INTO edge_services VALUES (?, ?, ?, 1, ?)",
            [edge_id, s, agency, trips],
        )


def test_weights_agree_with_the_list_form():
    """The streaming scale and the list form must be the same scale, or a window
    would be weighted differently depending on which path drew it."""
    values = [0, 1, 5, 40, 900, 3000, 12000]
    w = art.Weights.over(values)
    assert [w.of(v) for v in values] == art._normalise(values)


def test_an_empty_window_has_a_usable_scale():
    w = art.Weights.over([])
    assert w.of(10) == 0.5


def test_window_streams_the_same_edges_the_list_form_returns(con):
    for i, lon in enumerate([-3200000, -3190000, -3180000]):
        _art_edge(con, i + 1, lon, trips=100 * (i + 1))
    bounds = art.Bounds(-3.30, 51.40, -3.10, 51.60)

    streamed = list(art.Window(bounds, con).edges())
    listed = art.load_edges(bounds, con=con)
    assert [e.edge_id for e in streamed] == [e.edge_id for e in listed]
    assert len(streamed) == 3


def test_window_can_be_walked_more_than_once(con):
    """density makes two additive passes, so the stream has to reopen."""
    _art_edge(con, 1, -3200000, trips=100)
    w = art.Window(art.Bounds(-3.30, 51.40, -3.10, 51.60), con)
    assert [e.edge_id for e in w.edges()] == [e.edge_id for e in w.edges()] == [1]


def test_by_weight_orders_quietest_first(con):
    """spectrum draws in this order so busy roads finish on top. It used to sort the
    whole window in memory; the ordering is now the database's job."""
    _art_edge(con, 1, -3200000, trips=900)
    _art_edge(con, 2, -3190000, trips=10)
    _art_edge(con, 3, -3180000, trips=300)
    w = art.Window(art.Bounds(-3.30, 51.40, -3.10, 51.60), con)
    assert [e.edge_id for e in w.edges(by_weight=True)] == [2, 3, 1]


def test_strands_arrive_grouped_by_service_widest_first(con):
    """A ribbon is stroked as one path, so a service's edges must arrive together
    and never be revisited once the next service starts."""
    _art_edge(con, 1, -3200000, trips=100, services=("42", "9A"))
    _art_edge(con, 2, -3190000, trips=100, services=("42",))
    _art_edge(con, 3, -3180000, trips=100, services=("42",))
    w = art.Window(art.Bounds(-3.30, 51.40, -3.10, 51.60), con, with_groups=True)

    names = [name for name, _weight, _coords in w.groups()]
    assert names == ["42", "42", "42", "9A"]  # widest service first, then grouped
    # Once a name is left behind it never comes back, which is what lets the caller
    # stroke and forget.
    assert len(set(names)) == len({n for n in names})
    seen, order = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            order.append(n)
    assert order == ["42", "9A"]


def test_held_window_matches_the_streaming_one(con):
    """`render(edges=...)` re-renders a window a caller already has; it must draw
    the same picture as the streaming path."""
    _art_edge(con, 1, -3200000, trips=900, services=("42", "9A"))
    _art_edge(con, 2, -3190000, trips=10, services=("42",))
    bounds = art.Bounds(-3.30, 51.40, -3.10, 51.60)

    streamed = art.Window(bounds, con, with_groups=True)
    held = art.Held(art.load_edges(bounds, with_groups=True, con=con))

    assert held.weights == streamed.weights
    assert [e.edge_id for e in held.edges(by_weight=True)] == [
        e.edge_id for e in streamed.edges(by_weight=True)
    ]
    assert [(n, round(w, 9)) for n, w, _ in held.groups()] == [
        (n, round(w, 9)) for n, w, _ in streamed.groups()
    ]


def test_held_window_carries_the_groups_the_query_asked_for(con):
    """`with_groups=` populates Edge.groups, and `Held` draws from that field alone --
    so a rename that left one of the two behind would show up as an empty ribbon."""
    _art_edge(con, 1, -3200000, trips=900, services=("42", "9A"))
    bounds = art.Bounds(-3.30, 51.40, -3.10, 51.60)
    assert [e.groups for e in art.load_edges(bounds, with_groups=True, con=con)] == [
        ("42", "9A")
    ]
    assert art.load_edges(bounds, con=con)[0].groups == ()


def test_a_spec_is_recorded_on_a_held_window_even_though_it_cannot_honour_it(con):
    """`Held` was handed edges somebody else already weighted and grouped. It keeps
    the spec so a caller can see which one that was, and ignores it otherwise."""
    spec = art.QuerySpec(weight="services", group="operator")
    assert art.Held([], spec=spec).spec is spec


# --- Rendering the spec -------------------------------------------------------

RENDER_BOUNDS = art.Bounds(-3.30, 51.40, -3.10, 51.60)
RENDER_OPTS = art.RenderOpts(width_px=300)


@pytest.fixture
def drawable(con):
    """Enough overlap that every style has something to composite."""
    _art_edge(con, 1, -3200000, trips=900, services=("42", "9A"))
    _art_edge(con, 2, -3190000, trips=40, services=("42",), agency="FIRST")
    _art_edge(con, 3, -3180000, trips=300, services=("9A", "7"))
    return con


@pytest.mark.parametrize("style", sorted(art.STYLES))
@pytest.mark.parametrize("fmt", [".png", ".svg"])
def test_a_render_is_byte_identical_across_two_calls(drawable, style, fmt):
    """SVG is the format that can tell: it records the strokes in the order they were
    issued, where a PNG of `strands` hides an arbitrary order because SCREEN
    compositing is commutative. Two runs once differed in 180,365 of 293,842 bytes."""
    first = art.render_bytes(RENDER_BOUNDS, style, fmt=fmt, opts=RENDER_OPTS, con=drawable)
    second = art.render_bytes(RENDER_BOUNDS, style, fmt=fmt, opts=RENDER_OPTS, con=drawable)
    assert first == second


@pytest.mark.parametrize(
    "query",
    [
        art.QuerySpec(weight="services"),
        art.QuerySpec(group="operator", order="busiest"),
        art.QuerySpec(operator=("FIRST",)),
    ],
    ids=lambda q: q.key,
)
def test_a_spec_renders_deterministically_too(drawable, query):
    kwargs = {"fmt": ".svg", "opts": RENDER_OPTS, "con": drawable, "query": query}
    assert art.render_bytes(RENDER_BOUNDS, "strands", **kwargs) == art.render_bytes(
        RENDER_BOUNDS, "strands", **kwargs
    )


def test_two_specs_do_not_draw_the_same_picture(drawable):
    """If they did, `QuerySpec.key` would be pointless and the cache could not tell
    them apart in the first place."""
    kwargs = {"fmt": ".svg", "opts": RENDER_OPTS, "con": drawable}
    plain = art.render_bytes(RENDER_BOUNDS, "strands", **kwargs)
    filtered = art.render_bytes(
        RENDER_BOUNDS, "strands", query=art.QuerySpec(operator=("FIRST",)), **kwargs
    )
    assert plain != filtered


# --- Cached windows -----------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        art.DEFAULT_SPEC,
        art.QuerySpec(weight="density", group="way"),
        art.QuerySpec(min_trips=100),
    ],
    ids=lambda q: q.key,
)
@pytest.mark.parametrize("style", sorted(art.STYLES))
def test_substituting_the_source_never_changes_the_picture(drawable, style, query):
    """`Source` names where the two tables are read from, so that a materialised or
    extracted window can be swapped in underneath a render. Whatever is swapped in,
    the picture has to be the same one -- a source that changed the output would make
    every design iterated against it a picture of something other than the database.
    """
    kwargs = {"fmt": ".svg", "opts": RENDER_OPTS, "con": drawable, "query": query}
    direct = art.render_bytes(RENDER_BOUNDS, style, **kwargs)
    wrapped = art.Source(
        edges="(SELECT * FROM edges)", services="(SELECT * FROM edge_services)"
    )
    assert art.render_bytes(RENDER_BOUNDS, style, source=wrapped, **kwargs) == direct
