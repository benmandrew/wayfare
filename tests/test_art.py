from __future__ import annotations

import io
import itertools
import math
import re
import struct
import zlib
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree

import builders
import pytest

from wayfare import art, config, db, licences

# One window over Cardiff, shared by every test here that needs somewhere to draw.
BOUNDS = art.Bounds(*builders.WINDOW)


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
    assert art.resolve("manchester") is art.PRESETS["manchester"]
    with pytest.raises(KeyError, match="manchester"):
        art.resolve("greater_manchester")


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


class _Bare:
    """Exactly :class:`art.Frame` and nothing else -- no spec, no connection.

    Anything a style reaches for beyond the four members raises AttributeError here,
    which is the point: the protocol is the whole of what paint may ask of data.
    """

    weights = art.Weights.over([1.0, 100.0, 900.0])
    alpha_compensation = 2.0

    def paths(self, proj, *, tol=0.0, by_weight=False, coalesce=False):
        yield 100.0, art.Polyline.of([(10.0, 10.0), (60.0, 70.0), (90.0, 20.0)])

    def group_paths(self, proj, *, tol=0.0):
        yield "42", 0.5, art.Polyline.of([(10.0, 10.0), (60.0, 70.0)])


@pytest.mark.parametrize("style", sorted(art.STYLES))
def test_a_style_draws_from_the_frame_and_nothing_else(style):
    """A render is a style and a query spec, and they know nothing about each other.
    The one crossing is `Style.needs_groups`, which names the *shape* of the data a
    style consumes. A style reading `window.spec` -- for the sample rate, say -- puts
    a query concern inside the paint, and it raises here rather than compiling."""
    import cairo

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 100, 100)
    ctx = cairo.Context(surface)
    proj = art.Projection.fit(BOUNDS, 100, 100)
    art.STYLES[style].draw(ctx, _Bare(), proj, art.RenderOpts(width_px=100))


def test_the_window_says_how_much_light_a_sampled_render_owes(con):
    """`density` composites additively, so a preview drawing one edge in eight has to
    put the light of the missing seven onto the survivors. Which edges there are is
    the spec's business; the number to multiply an alpha by is the window's, so the
    style asks for that rather than reading the sample rate itself."""
    builders.insert_edge(con, 1, lon_e6=-3200000, span_e6=1000)
    builders.insert_services(con, 1, n_trips=100)
    assert art.Window(BOUNDS, con).alpha_compensation == 1.0
    spec = art.QuerySpec(sample=8)
    assert art.Window(BOUNDS, con, spec=spec).alpha_compensation == 8.0
    assert art.Held(art.load_edges(BOUNDS, con=con)).alpha_compensation == 1.0


def test_a_style_must_declare_its_widest_stroke():
    """Banding sizes a band's collar off `max_line_px`. A style that inherited a
    default would get a collar for a picture it does not draw, and too narrow a collar
    is a seam nothing raises about -- so the declaration is required at construction."""
    with pytest.raises(TypeError):
        art.Style(draw=lambda *a: None)


def test_a_format_comes_from_a_path_or_from_a_bare_suffix():
    """`render` holds a filename and `render_bytes` holds `.png` on its own. A bare
    suffix has no suffix of its own, which is what the fallback is for."""
    assert art._fmt(Path("/tmp/a.PNG")) == ".png"
    assert art._fmt("/tmp/a.png") == ".png"
    assert art._fmt(".svg") == ".svg"
    with pytest.raises(ValueError, match="unsupported"):
        art._fmt("/tmp/a.tiff")


def test_unknown_output_format_fails_before_querying(tmp_path):
    """Rejecting the suffix must not cost a full window query first."""
    with pytest.raises(ValueError, match=re.escape("unsupported output format '.tiff'")):
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


def test_unknown_name_mentions_the_window_form():
    with pytest.raises(KeyError, match="minlon"):
        art.resolve("swansea")


# --- Streaming ---------------------------------------------------------------


def test_an_empty_window_has_a_usable_scale():
    w = art.Weights.over([])
    assert w.of(10) == 0.5


def test_window_streams_the_same_edges_the_list_form_returns(con):
    for i, lon in enumerate([-3200000, -3190000, -3180000]):
        builders.insert_edge(con, i + 1, lon_e6=lon, span_e6=1000)
        builders.insert_services(con, i + 1, n_trips=100 * (i + 1))

    streamed = list(art.Window(BOUNDS, con).edges())
    listed = art.load_edges(BOUNDS, con=con)
    assert [e.edge_id for e in streamed] == [e.edge_id for e in listed]
    assert len(streamed) == 3


def test_window_can_be_walked_more_than_once(con):
    """density makes two additive passes, so the stream has to reopen."""
    builders.insert_edge(con, 1, lon_e6=-3200000, span_e6=1000)
    builders.insert_services(con, 1, n_trips=100)
    w = art.Window(BOUNDS, con)
    assert [e.edge_id for e in w.edges()] == [e.edge_id for e in w.edges()] == [1]


def test_by_weight_orders_quietest_first(con):
    """spectrum draws in this order so busy roads finish on top. The order is the
    database's, so a render never holds the window in memory to sort it."""
    builders.insert_edge(con, 1, lon_e6=-3200000, span_e6=1000)
    builders.insert_services(con, 1, n_trips=900)
    builders.insert_edge(con, 2, lon_e6=-3190000, span_e6=1000)
    builders.insert_services(con, 2, n_trips=10)
    builders.insert_edge(con, 3, lon_e6=-3180000, span_e6=1000)
    builders.insert_services(con, 3, n_trips=300)
    w = art.Window(BOUNDS, con)
    assert [e.edge_id for e in w.edges(by_weight=True)] == [2, 3, 1]


def test_strands_arrive_grouped_by_service_widest_first(con):
    """A ribbon is stroked as one path, so a service's edges must arrive together
    and never be revisited once the next service starts."""
    builders.insert_edge(con, 1, lon_e6=-3200000, span_e6=1000)
    builders.insert_services(con, 1, ("42", "9A"), n_trips=100)
    builders.insert_edge(con, 2, lon_e6=-3190000, span_e6=1000)
    builders.insert_services(con, 2, ("42",), n_trips=100)
    builders.insert_edge(con, 3, lon_e6=-3180000, span_e6=1000)
    builders.insert_services(con, 3, ("42",), n_trips=100)
    w = art.Window(BOUNDS, con, with_groups=True)

    proj = art.Projection.fit(BOUNDS, 800, 600)
    names = [name for name, _weight, _line in w.group_paths(proj)]
    assert names == ["42", "42", "42", "9A"]  # widest service first, then grouped
    # Once a name is left behind it never comes back, which is what lets the caller
    # stroke and forget.
    seen, order = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            order.append(n)
    assert order == ["42", "9A"]


# --- Projection, simplification and sampling ---------------------------------


def test_batch_projection_matches_the_scalar_one(con):
    """The batched path is vectorised for speed, not for a different answer. If the
    two ever disagree the same window draws differently depending on whether it came
    from the database or from a list."""
    proj = art.Projection.fit(BOUNDS, 800, 600)
    lons = [[-3200000, -3190000, -3180000], [-3150000, -3140000]]
    lats = [[51480000, 51490000, 51500000], [51450000, 51460000]]

    batched = proj.batch(lons, lats)
    scalar = [
        [proj(x / 1e6, y / 1e6) for x, y in zip(lo, la, strict=True)]
        for lo, la in zip(lons, lats, strict=True)
    ]
    assert batched == scalar


def test_simplify_keeps_the_ends_and_drops_the_sub_pixel_middle():
    pts = [(0.0, 0.0), (0.1, 0.0), (0.2, 0.0), (10.0, 0.0)]
    assert art.simplify(pts, 0.5) == [(0.0, 0.0), (10.0, 0.0)]
    # A zero tolerance is the opt-out, and two points are already minimal.
    assert art.simplify(pts, 0.0) == pts
    assert art.simplify(pts[:2], 0.5) == pts[:2]


def test_simplify_measures_from_the_last_kept_point():
    """Comparing against the previous point instead would let a gently curving road
    accumulate unlimited drift in sub-tolerance steps and come out straight."""
    drift = [(0.0, 0.0), (0.4, 0.0), (0.8, 0.0), (1.2, 0.0), (9.0, 0.0)]
    # Each step is under the tolerance, but the second and fourth are far enough
    # from the last point actually kept.
    assert art.simplify(drift, 0.5) == [(0.0, 0.0), (0.8, 0.0), (9.0, 0.0)]


def test_sampling_thins_the_window_but_not_the_weight_scale(con):
    """A preview must draw fewer edges without changing what a trip count looks
    like, or its colours and line widths would not be the ones being tuned."""
    for i in range(64):
        builders.insert_edge(con, i + 1, lon_e6=-3200000 + i * 1000, span_e6=1000)
        builders.insert_services(con, i + 1, n_trips=10 * (i + 1))

    full = art.Window(BOUNDS, con)
    thin = art.Window(BOUNDS, con, spec=art.QuerySpec(sample=8))

    ids = [e.edge_id for e in thin.edges()]
    assert 0 < len(ids) < 64
    assert set(ids) <= {e.edge_id for e in full.edges()}
    # The scale is what turns a weight into a colour, so it is taken over every edge.
    assert thin.weights == full.weights


def test_sampling_is_not_a_filter(con):
    """`selective` decides LEFT JOIN against JOIN, so a spec that claimed to be
    selective would silently drop every edge carrying no services -- which sampling
    has no business doing."""
    assert not art.QuerySpec(sample=8).selective
    assert art.QuerySpec(min_trips=1).selective


def test_sampling_reaches_the_spec_key():
    """Two specs sharing a key means one's picture is served for the other, and a
    preview served as the export is exactly that mistake."""
    assert art.QuerySpec(sample=8).key != art.QuerySpec().key


def test_a_sample_below_one_is_rejected():
    with pytest.raises(ValueError, match="sample=0 must be 1 or more"):
        art.QuerySpec(sample=0)


def test_sampling_picks_the_same_edges_every_time(con):
    """A preview that redrew a different eighth on every keystroke would flicker,
    and two runs of the same render would not be comparable."""
    for i in range(64):
        builders.insert_edge(con, i + 1, lon_e6=-3200000 + i * 1000, span_e6=1000)
        builders.insert_services(con, i + 1, n_trips=100)
    spec = art.QuerySpec(sample=8)
    first = [e.edge_id for e in art.Window(BOUNDS, con, spec=spec).edges()]
    second = [e.edge_id for e in art.Window(BOUNDS, con, spec=spec).edges()]
    assert first == second


def test_sampling_leaves_group_widths_alone(con):
    """Ribbon width and draw order come from the group listing, which is taken over
    the whole window. Sampling that too would make a preview weight its ribbons
    differently from the render it stands in for."""
    for i in range(64):
        builders.insert_edge(con, i + 1, lon_e6=-3200000 + i * 1000, span_e6=1000)
        builders.insert_services(con, i + 1, ("42",), n_trips=100)

    full = art.Window(BOUNDS, con, with_groups=True)
    thin = art.Window(BOUNDS, con, with_groups=True, spec=art.QuerySpec(sample=8))
    proj = art.Projection.fit(BOUNDS, 800, 600)

    widths = {(n, round(w, 9)) for n, w, _ in full.group_paths(proj)}
    thin_widths = {(n, round(w, 9)) for n, w, _ in thin.group_paths(proj)}
    assert thin_widths == widths
    # ...while the geometry it hands back really is thinner.
    assert 0 < len(list(thin.group_paths(proj))) < len(list(full.group_paths(proj)))


def test_paths_agree_with_projecting_the_edge_stream(con):
    """`paths` exists to skip building degrees at all, so it has its own decode. It
    must still land on the pixels the unprojected stream would have."""
    for i in range(3):
        builders.insert_edge(con, i + 1, lon_e6=-3200000 + i * 5000, span_e6=1000)
        builders.insert_services(con, i + 1, n_trips=100 * (i + 1))
    proj = art.Projection.fit(BOUNDS, 800, 600)
    w = art.Window(BOUNDS, con)

    via_paths = [(weight, line.points()) for weight, line in w.paths(proj)]
    via_edges = [(e.weight, [proj(lon, lat) for lon, lat in e.coords]) for e in w.edges()]
    assert via_paths == via_edges


def test_held_paths_match_the_streaming_ones(con):
    builders.insert_edge(con, 1, lon_e6=-3200000, span_e6=1000)
    builders.insert_services(con, 1, ("42",), n_trips=900)
    builders.insert_edge(con, 2, lon_e6=-3190000, span_e6=1000)
    builders.insert_services(con, 2, ("42",), n_trips=10)
    proj = art.Projection.fit(BOUNDS, 800, 600)

    streamed = art.Window(BOUNDS, con, with_groups=True)
    held = art.Held(art.load_edges(BOUNDS, with_groups=True, con=con))
    assert list(held.paths(proj)) == list(streamed.paths(proj))
    assert [(n, round(w, 9), p) for n, w, p in held.group_paths(proj)] == [
        (n, round(w, 9), p) for n, w, p in streamed.group_paths(proj)
    ]


def test_held_window_matches_the_streaming_one(con):
    """`render(edges=...)` re-renders a window a caller already has; it must draw
    the same picture as the streaming path."""
    builders.insert_edge(con, 1, lon_e6=-3200000, span_e6=1000)
    builders.insert_services(con, 1, ("42", "9A"), n_trips=900)
    builders.insert_edge(con, 2, lon_e6=-3190000, span_e6=1000)
    builders.insert_services(con, 2, ("42",), n_trips=10)

    streamed = art.Window(BOUNDS, con, with_groups=True)
    held = art.Held(art.load_edges(BOUNDS, with_groups=True, con=con))
    proj = art.Projection.fit(BOUNDS, 800, 600)

    assert held.weights == streamed.weights
    assert [e.edge_id for e in held.edges(by_weight=True)] == [
        e.edge_id for e in streamed.edges(by_weight=True)
    ]
    assert [(n, round(w, 9)) for n, w, _ in held.group_paths(proj)] == [
        (n, round(w, 9)) for n, w, _ in streamed.group_paths(proj)
    ]


@pytest.mark.parametrize("order", sorted(art.ORDERS))
def test_held_lays_its_ribbons_down_in_the_order_the_spec_asked_for(con, order):
    """`Held` cannot honour a filter -- its edges were chosen by whatever produced
    them -- but the draw order is a property of the list rather than of the query, and
    it decides which ribbon ends up underneath. Hard-coded to `widest` it silently
    ignored `order=`, so `render(edges=...)` drew a different picture from the
    streaming path for four of the five orders."""
    builders.insert_edge(con, 1, lon_e6=-3200000, span_e6=1000)
    builders.insert_services(con, 1, (("42", "OP1"), ("9A", "OP2")), n_trips=100)
    builders.insert_edge(con, 2, lon_e6=-3190000, span_e6=1000)
    builders.insert_services(con, 2, (("42", "FIRST"),), n_trips=50)
    builders.insert_edge(con, 3, lon_e6=-3180000, span_e6=1000)
    builders.insert_services(con, 3, (("7", "OP1"),), n_trips=30)

    spec = art.QuerySpec(order=order)
    proj = art.Projection.fit(BOUNDS, 800, 600)
    held = art.Held(art.load_edges(BOUNDS, with_groups=True, con=con), spec=spec)
    streamed = art.Window(BOUNDS, con, with_groups=True, spec=spec)
    assert [n for n, _w, _p in held.group_paths(proj)] == [
        n for n, _w, _p in streamed.group_paths(proj)
    ]


def test_held_window_carries_the_groups_the_query_asked_for(con):
    """`with_groups=` populates Edge.groups, and `Held` draws from that field alone --
    so a rename that left one of the two behind would show up as an empty ribbon."""
    builders.insert_edge(con, 1, lon_e6=-3200000, span_e6=1000)
    builders.insert_services(con, 1, ("42", "9A"), n_trips=900)
    assert [e.groups for e in art.load_edges(BOUNDS, with_groups=True, con=con)] == [
        ("42", "9A")
    ]
    assert art.load_edges(BOUNDS, con=con)[0].groups == ()


# --- Rendering the spec -------------------------------------------------------

RENDER_OPTS = art.RenderOpts(width_px=300)


@pytest.fixture
def drawable(con):
    """Enough overlap that every style has something to composite."""
    builders.insert_edge(con, 1, lon_e6=-3200000, span_e6=1000)
    builders.insert_services(con, 1, ("42", "9A"), n_trips=900)
    builders.insert_edge(con, 2, lon_e6=-3190000, span_e6=1000)
    builders.insert_services(con, 2, ("42",), n_trips=40, agency="FIRST")
    builders.insert_edge(con, 3, lon_e6=-3180000, span_e6=1000)
    builders.insert_services(con, 3, ("9A", "7"), n_trips=300)
    return con


@pytest.mark.parametrize("style", sorted(art.STYLES))
@pytest.mark.parametrize("fmt", [".png", ".svg"])
def test_a_render_is_byte_identical_across_two_calls(drawable, style, fmt):
    """SVG is the format that can tell: it records the strokes in the order they were
    issued, where a PNG of `strands` hides an arbitrary order because SCREEN
    compositing is commutative. Two runs once differed in 180,365 of 293,842 bytes."""
    first = art.render_bytes(BOUNDS, style, fmt=fmt, opts=RENDER_OPTS, con=drawable)
    second = art.render_bytes(BOUNDS, style, fmt=fmt, opts=RENDER_OPTS, con=drawable)
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
    assert art.render_bytes(BOUNDS, "strands", **kwargs) == art.render_bytes(
        BOUNDS, "strands", **kwargs
    )


def test_two_specs_do_not_draw_the_same_picture(drawable):
    """If they did, `QuerySpec.key` would be pointless and the cache could not tell
    them apart in the first place."""
    kwargs = {"fmt": ".svg", "opts": RENDER_OPTS, "con": drawable}
    plain = art.render_bytes(BOUNDS, "strands", **kwargs)
    filtered = art.render_bytes(
        BOUNDS, "strands", query=art.QuerySpec(operator=("FIRST",)), **kwargs
    )
    assert plain != filtered


# --- The flat geometry path -------------------------------------------------


def test_polyline_simplifies_the_same_way_the_list_form_does():
    """`Polyline.simplified` and `simplify` are the same rule over two
    representations. They must agree exactly, or a render would thin its geometry
    differently depending on which of `Window` and `Held` produced it."""
    pts = [(0.0, 0.0), (0.1, 0.0), (0.2, 0.0), (9.0, 0.0), (10.0, 0.0)]
    for tol in (0.0, 0.05, 0.5, 5.0):
        assert art.Polyline.of(pts).simplified(tol).points() == art.simplify(pts, tol)


def test_polyline_keeps_its_indices_when_nothing_is_dropped():
    """A `range` rather than a list is the point: the unsimplified case is the
    common one and it should allocate nothing."""
    line = art.Polyline.of([(0.0, 0.0), (1.0, 1.0), (2.0, 2.0)])
    assert isinstance(line.simplified(0.0).idx, range)


def test_polyline_segments_are_its_consecutive_points():
    pts = [(0.0, 1.0), (2.0, 3.0), (4.0, 5.0)]
    line = art.Polyline.of(pts)
    assert list(line.segments()) == [(0.0, 1.0, 2.0, 3.0), (2.0, 3.0, 4.0, 5.0)]
    assert list(art.Polyline.of(pts[:1]).segments()) == []
    assert list(art.Polyline.of([]).segments()) == []


def test_polyline_equality_ignores_how_the_points_are_held():
    """`Window` hands out slices of one buffer shared by a whole fetch and `Held`
    builds a buffer per edge. Only the polyline is meant to be the same."""
    shared = art.Polyline([0.0, 1.0, 2.0], [0.0, 1.0, 2.0], range(1, 3))
    own = art.Polyline.of([(1.0, 1.0), (2.0, 2.0)])
    assert shared == own
    assert shared != art.Polyline.of([(1.0, 1.0), (3.0, 3.0)])


@pytest.mark.parametrize(
    "spec",
    [
        art.DEFAULT_SPEC,
        art.QuerySpec(weight="services"),
        art.QuerySpec(weight="density"),
        art.QuerySpec(min_trips=10),
    ],
    ids=lambda s: s.key,
)
def test_bounds_in_sql_match_the_python_percentile_pass(con, spec):
    """SQL finds the scale rather than pulling every weight into Python, and the two
    must pick the *same* two order statistics -- an approximation would shift a
    render's contrast invisibly. See `_Sql.bounds_query`."""
    from array import array

    for i in range(50):
        builders.insert_edge(con, i + 1, lon_e6=-3200000 + i * 200, span_e6=1000)
        builders.insert_services(con, i + 1, n_trips=(i * i) % 97)
    w = art.Window(BOUNDS, con, spec=spec)
    query, params = w.sql.weights_query()
    expected = art.Weights.over(
        array("d", (r[0] for r in con.execute(query, params).fetchall()))
    )
    assert w.weights == expected


def test_an_empty_window_still_has_a_usable_scale_from_sql(con):
    w = art.Window(BOUNDS, con)
    assert w.weights.of(10) == 0.5


def test_the_weight_scale_is_not_computed_until_it_is_asked_for(con):
    """`strands` never reads it -- it weights ribbons from the group statistics --
    so computing it eagerly was a whole extra pass over the window per render,
    thrown away. See `Window.weights`."""
    builders.insert_edge(con, 1, lon_e6=-3200000, span_e6=1000)
    builders.insert_services(con, 1, n_trips=900)
    w = art.Window(BOUNDS, con)
    assert w._weights is None
    list(w.group_paths(art.Projection.fit(w.bounds, 800, 600)))
    assert w._weights is None, "group_paths must not force the window scale"
    assert w.weights.hi > 0.0
    assert w._weights is not None


def test_streaming_paths_survives_the_scale_being_resolved(con):
    """A DuckDB connection holds one result at a time, so a query issued while a
    stream is open abandons it -- silently, ending the stream early with no error.
    A lazily-computed scale first touched inside the draw loop did exactly that and
    truncated a render to its first fetch. `paths` resolves it up front."""
    n = art.FETCH_ROWS + 500  # more than one fetch, so a truncation is visible
    con.execute(
        "INSERT INTO edges SELECT i, 1, 'R', 'secondary', 100.0, [-3200000, -3199000], "
        "[51480000, 51480000], -3200000, 51480000, -3199000, 51480000 "
        "FROM range(?) t(i)",
        [n],
    )
    con.execute(
        "INSERT INTO edge_services SELECT i, '42', 'OP1', 1, 1 + i % 7 FROM range(?) t(i)",
        [n],
    )
    w = art.Window(BOUNDS, con)
    proj = art.Projection.fit(w.bounds, 800, 600)
    assert w._weights is None
    drawn = [w.weights.of(weight) for weight, _ in w.paths(proj)]
    assert len(drawn) == art.FETCH_ROWS + 500


def test_the_edge_stream_has_a_defined_order(con):
    """A query with no ORDER BY has no row order, and DuckDB's parallel hash join
    returns one that varies run to run -- `density` to SVG gave four distinct
    outputs in four runs over the real databases. The test above cannot catch it:
    three edges never reach a second thread, so an undefined order is a stable one.
    What is checkable at any size is that the order is *defined*, so this asserts
    the edges arrive by edge_id rather than in whatever order they were inserted."""
    for edge_id, lon in ((7, -3200000), (3, -3190000), (9, -3180000), (1, -3170000)):
        builders.insert_edge(con, edge_id, lon_e6=lon, span_e6=1000)
        builders.insert_services(con, edge_id, n_trips=100)
    w = art.Window(BOUNDS, con)
    query, params = w.sql.edges_query(with_groups=False, by_weight=False)
    assert [r[0] for r in con.execute(query, params).fetchall()] == [1, 3, 7, 9]
    assert [e.edge_id for e in w.edges()] == [1, 3, 7, 9]


# --- Coalescing ---------------------------------------------------------------
#
# Joining runs of edges that meet end to end into one stroke, so a shared node is
# capped once instead of twice. The tests below are about the chaining rule rather
# than the picture: which edges end up in the same run, and whether the vertices
# survive. What the picture then looks like is a judgement, not an assertion.


def _link(con, edge_id, pts, trips=100, services=("42",), agency="OP1"):
    """One edge with explicit geometry, so a test can decide what meets what.

    `pts` is (lon_e6, lat_e6) in micro-degrees and in the edge's own direction --
    which matters here, because chaining follows direction.
    """
    builders.insert_edge(con, edge_id, points=pts)
    builders.insert_services(con, edge_id, services, agency=agency, n_trips=trips)


def _runs(con, spec=art.DEFAULT_SPEC):
    """The window's geometry, coalesced, as lists of canvas points."""
    proj = art.Projection.fit(BOUNDS, 400, 400)
    w = art.Window(BOUNDS, con, spec=spec)
    return [line.points() for _weight, line in w.paths(proj, coalesce=True)]


def _plain(con, spec=art.DEFAULT_SPEC):
    proj = art.Projection.fit(BOUNDS, 400, 400)
    w = art.Window(BOUNDS, con, spec=spec)
    return [line.points() for _weight, line in w.paths(proj)]


def test_edges_that_meet_end_to_end_become_one_run(con):
    _link(con, 1, [(-3_200_000, 51_480_000), (-3_190_000, 51_480_000)])
    _link(con, 2, [(-3_190_000, 51_480_000), (-3_180_000, 51_480_000)])
    _link(con, 3, [(-3_180_000, 51_480_000), (-3_170_000, 51_480_000)])
    runs = _runs(con)
    assert len(runs) == 1
    # Four vertices, not six: the two shared nodes appear once each. Every vertex
    # the flat path would draw is still here, in order.
    assert len(runs[0]) == 4
    assert runs[0] == sorted(runs[0])


def test_a_run_stops_where_three_edges_meet(con):
    """The trap `publish._chain` already records. A fork has no single continuation,
    and picking one would draw a line down a road the run does not take."""
    _link(con, 1, [(-3_200_000, 51_480_000), (-3_190_000, 51_480_000)])
    _link(con, 2, [(-3_190_000, 51_480_000), (-3_180_000, 51_480_000)])
    _link(con, 3, [(-3_190_000, 51_480_000), (-3_190_000, 51_490_000)])
    runs = _runs(con)
    assert sorted(len(r) for r in runs) == [2, 2, 2]


def test_the_two_directions_of_a_street_chain_separately(con):
    """The reason this chains head to tail where `publish` chains undirected. A
    two-way street arrives as two coincident edges pointing opposite ways, so an
    undirected rule sees four edges at every node and joins nothing at all."""
    a = [(-3_200_000, 51_480_000), (-3_190_000, 51_480_000)]
    b = [(-3_190_000, 51_480_000), (-3_180_000, 51_480_000)]
    _link(con, 1, a)
    _link(con, 2, b)
    _link(con, 3, list(reversed(b)))
    _link(con, 4, list(reversed(a)))
    runs = _runs(con)
    assert len(runs) == 2
    assert sorted(len(r) for r in runs) == [3, 3]
    # And they really are opposite ways round, not the same run twice.
    assert {r[0] for r in runs} == {r[-1] for r in runs}


def test_edges_that_paint_differently_are_never_joined(con):
    """Width, alpha and saturation all come from the weight, so two edges meeting
    end to end at different weights are two different shapes. Joining them would
    change the picture rather than remove a duplicated cap from it."""
    _link(con, 1, [(-3_200_000, 51_480_000), (-3_190_000, 51_480_000)], trips=100)
    _link(con, 2, [(-3_190_000, 51_480_000), (-3_180_000, 51_480_000)], trips=900)
    assert sorted(len(r) for r in _runs(con)) == [2, 2]


def test_coalescing_draws_the_same_vertices_as_the_flat_path(con):
    """The only thing it removes is the duplicated node. Every point the flat path
    draws is still drawn, so this is not a simplification in disguise."""
    _link(con, 1, [(-3_200_000, 51_480_000), (-3_195_000, 51_481_000)])
    _link(con, 2, [(-3_195_000, 51_481_000), (-3_190_000, 51_480_000)])
    _link(con, 3, [(-3_190_000, 51_480_000), (-3_185_000, 51_482_000)])
    flat = sorted({p for line in _plain(con) for p in line})
    runs = _runs(con)
    assert len(runs) == 1
    assert sorted(set(runs[0])) == flat
    assert len(runs[0]) == len(set(runs[0]))  # no vertex repeated at a join


def test_a_closed_loop_is_entered_at_its_lowest_edge_id(con):
    """A ring has no end to start from, so something has to choose. `publish._chain`
    started wherever the scan happened to be, which made every rebuild differ."""
    ring = [
        (-3_200_000, 51_480_000),
        (-3_190_000, 51_480_000),
        (-3_190_000, 51_490_000),
        (-3_200_000, 51_490_000),
    ]
    for i in range(4):
        _link(con, 9 - i, [ring[i], ring[(i + 1) % 4]])
    runs = _runs(con)
    assert len(runs) == 1
    proj = art.Projection.fit(BOUNDS, 400, 400)
    # Edge 6 is the lowest id, and it runs from the fourth corner back to the first.
    assert runs[0][0] == proj(ring[3][0] / 1e6, ring[3][1] / 1e6)


@pytest.mark.parametrize("fmt", [".png", ".svg"])
def test_a_coalesced_render_is_byte_identical_across_two_calls(drawable, fmt):
    opts = art.RenderOpts(width_px=300, coalesce=True)
    first = art.render_bytes(BOUNDS, fmt=fmt, opts=opts, con=drawable)
    second = art.render_bytes(BOUNDS, fmt=fmt, opts=opts, con=drawable)
    assert first == second


def test_coalescing_changes_the_picture(con):
    """It is a flag rather than a fix applied silently, so it had better do
    something -- and only where there is a shared node to stop capping twice."""
    _link(con, 1, [(-3_200_000, 51_480_000), (-3_190_000, 51_480_000)])
    _link(con, 2, [(-3_190_000, 51_480_000), (-3_180_000, 51_480_000)])
    kw = {"con": con, "bounds_or_name": BOUNDS}
    plain = art.render_bytes(opts=art.RenderOpts(width_px=300), **kw)
    joined = art.render_bytes(opts=art.RenderOpts(width_px=300, coalesce=True), **kw)
    assert plain != joined


@pytest.mark.parametrize("style", ["spectrum", "strands"])
def test_a_style_that_ignores_coalescing_says_so(drawable, style, caplog):
    """`spectrum` strokes each segment separately to colour it, so it has a cap at
    every vertex and no chaining would remove them; `strands` already puts a whole
    service into one cairo path, where overlapping caps do not accumulate. Neither
    should quietly accept a flag it does nothing with."""
    assert not art.STYLES[style].coalesces
    with caplog.at_level("WARNING"):
        art.render_bytes(
            BOUNDS,
            style,
            opts=art.RenderOpts(width_px=300, coalesce=True),
            con=drawable,
        )
    assert "coalesce" in caplog.text


def test_a_supplied_chain_assignment_is_what_gets_drawn(con):
    """The seam banding uses. Which edges share a stroke has to be a property of the
    window rather than of where a band was cut, so the parent works the assignment
    out once and every band is handed it -- exactly as `Source.groups` already does
    for a ribbon's width and draw order."""
    import pyarrow

    _link(con, 1, [(-3_200_000, 51_480_000), (-3_190_000, 51_480_000)])
    _link(con, 2, [(-3_190_000, 51_480_000), (-3_180_000, 51_480_000)])
    assert len(_runs(con)) == 1  # left to itself it joins them

    con.register(
        "wf_fixed",
        pyarrow.table(
            {
                "edge_id": pyarrow.array([1, 2], pyarrow.int64()),
                "chain_id": pyarrow.array([1, 2], pyarrow.int64()),
                "seq": pyarrow.array([0, 0], pyarrow.int32()),
            }
        ),
    )
    proj = art.Projection.fit(BOUNDS, 400, 400)
    w = art.Window(BOUNDS, con, source=art.Source(chains="wf_fixed"))
    assert len(list(w.paths(proj, coalesce=True))) == 2


def test_held_edges_ignore_coalescing(con):
    """There is no query to reorder, so `Held` takes the flag and draws flat."""
    _link(con, 1, [(-3_200_000, 51_480_000), (-3_190_000, 51_480_000)])
    _link(con, 2, [(-3_190_000, 51_480_000), (-3_180_000, 51_480_000)])
    edges = art.load_edges(BOUNDS, con=con)
    proj = art.Projection.fit(BOUNDS, 400, 400)
    held = art.Held(edges)
    assert len(list(held.paths(proj, coalesce=True))) == 2


# --- Banding ------------------------------------------------------------------
#
# The claim banding rests on is that it changes nothing: `workers=8` and `workers=1`
# must produce the same bytes, or every measurement taken against one of them is
# about a different picture. The tests below check that claim from both ends -- the
# pieces that make it true, and the finished render.


def _band_edge(
    con,
    edge_id,
    lat_e6,
    trips=100,
    services=("42",),
    agency="OP1",
    lon_span=(-3_200_000, -3_199_000),
):
    """One edge at a given latitude. Banding cuts north to south, so the geometry has
    to vary in latitude rather than in longitude as most of the file's does."""
    lon0, lon1 = lon_span
    builders.insert_edge(con, edge_id, lon_e6=lon0, span_e6=lon1 - lon0, lat_e6=lat_e6)
    builders.insert_services(con, edge_id, services, agency=agency, n_trips=trips)


def _reopened_read_only(con, tmp_path, monkeypatch):
    """The written database handed back through a *read-only* handle -- which is what
    both `_render` and the server open, and what a band process needs the file to
    still be openable as. `MIN_BAND_EDGES` is dropped to nothing because the floor is
    about start-up cost, not about correctness."""
    con.close()
    monkeypatch.setattr(art.band, "MIN_BAND_EDGES", 0)
    ro = db.connect(tmp_path / "test.duckdb", read_only=True)
    yield ro
    ro.close()


@pytest.fixture
def banded(con, tmp_path, monkeypatch):
    """A window with edges spread over its whole height."""
    for i in range(60):
        _band_edge(
            con,
            i + 1,
            51_420_000 + i * 3_000,
            trips=1 + i * 40,
            services=("42",) if i % 3 else ("9A", "7"),
        )
    yield from _reopened_read_only(con, tmp_path, monkeypatch)


@pytest.mark.parametrize("style", sorted(art.STYLES))
def test_a_banded_render_is_byte_identical_to_a_serial_one(banded, style):
    """The whole justification. Verified on the real 2.7M-edge database over the
    `uk` window as well, for all three styles."""
    kw = {"opts": art.RenderOpts(width_px=200), "con": banded}
    assert art.render_bytes(BOUNDS, style, workers=1, **kw) == art.render_bytes(
        BOUNDS, style, workers=4, **kw
    )


@pytest.mark.parametrize(
    "opts",
    [
        art.RenderOpts(width_px=200, height_px=320),  # letterboxed
        art.RenderOpts(width_px=150, scale=2.0),  # bands cut on device rows
        art.RenderOpts(width_px=200, line_scale=6.0),  # a wider collar is needed
    ],
    ids=["letterbox", "scaled", "wide-lines"],
)
def test_banding_survives_the_awkward_canvases(banded, opts):
    """A letterbox makes the band extent and the window clip disagree; `scale` puts
    the cuts on device rows under a scaled context; `line_scale` widens the strokes
    past the collar a band queries, which is the seam this could most easily grow."""
    kw = {"opts": opts, "con": banded}
    assert art.render_bytes(BOUNDS, "density", workers=1, **kw) == art.render_bytes(
        BOUNDS, "density", workers=4, **kw
    )


@pytest.fixture
def wide_banded(con, tmp_path, monkeypatch):
    """Edges dense in latitude and alternating between busy and quiet, in a window
    short enough that a 6,000px canvas is only 3.5 megapixels.

    Both properties are load-bearing. `density`'s stroke width comes from the weight,
    so only the busiest edges draw at the full width the collar is sized against, and
    the fixture above puts those at one end of the window -- whether one lands in the
    couple of pixels either side of a band cut where a collar that is too narrow
    shows is then luck. Alternating the weight puts a full-width stroke every two
    rows, so a seam is certain rather than likely."""
    for i in range(600):
        _band_edge(
            con,
            i + 1,
            51_500_100 + i * 19,
            trips=5000 if i % 2 == 0 else 10,
            lon_span=(-3_290_000, -3_110_000),
        )
    yield from _reopened_read_only(con, tmp_path, monkeypatch)


WIDE_BOUNDS = art.Bounds(-3.30, 51.500, -3.10, 51.512)


def test_banding_holds_at_a_canvas_wider_than_the_style_reference(wide_banded):
    """`density` quotes its widths against `DENSITY_REF_PX`, so its widest stroke
    grows with the canvas, and a collar quoted in absolute pixels stops covering half
    of it once the canvas passes about 4,842px at `line_scale=1`. Below that the collar
    is merely over-generous, which costs a little work and hides the fault, so the seam
    can only show on a canvas wider than the reference -- hence the wide fixture.

    Bytes rather than a pixel tolerance, for the same reason the serial comparison
    above is: a seam is a handful of pixels one part in 255 out, and any tolerance
    loose enough to be robust is loose enough to miss it."""
    kw = {"opts": art.RenderOpts(width_px=6000), "con": wide_banded}
    assert art.render_bytes(WIDE_BOUNDS, "density", workers=1, **kw) == art.render_bytes(
        WIDE_BOUNDS, "density", workers=4, **kw
    )


@pytest.fixture
def chained_banded(con, tmp_path, monkeypatch):
    """Two unbroken chains running the whole height of the window, one busy and one
    quiet, so a run crosses every band cut there is.

    A chain is the thing that can go wrong here: joining edges is a decision about
    the shape of the graph, and a band sees only part of the graph. Two weights
    rather than one because `density` takes its stroke width from the weight, so an
    unvarying window would draw everything at mid width and never test the collar at
    the size it is sized for.
    """
    for lane, (lon, trips) in enumerate(((-3_250_000, 5000), (-3_150_000, 10))):
        lat, x = 51_500_100, lon
        for i in range(300):
            edge_id = lane * 300 + i + 1
            # A zigzag, so the chain is a road rather than a straight line and its
            # vertices do not all fall on the same column of pixels. Each edge starts
            # where the last one ended, which is what makes it a chain at all.
            nx, nlat = lon + (400 if i % 2 else -400), lat + 38
            builders.insert_edge(con, edge_id, points=[(x, lat), (nx, nlat)])
            builders.insert_services(con, edge_id, n_trips=trips)
            x, lat = nx, nlat
    yield from _reopened_read_only(con, tmp_path, monkeypatch)


@pytest.mark.parametrize("width_px", [200, 6000])
def test_a_coalesced_banded_render_is_byte_identical_to_a_serial_one(
    chained_banded, width_px
):
    """The question coalescing raises about banding, and it does not answer itself.

    Whether two edges share a stroke is decided by the graph around the node between
    them, and a band that worked that out from its own collar would see a fork as a
    through node wherever the third edge fell outside. Under ADD that is not a local
    error: two strokes double-count wherever they overlap, which can be a long way
    from the node that decided it. Measured, before the parent started shipping the
    assignment: London at 6,000px differed in 356 pixels by up to 89/255, all of them
    within 40 rows of a cut -- a seam, not a wash, and invisible at 2,000px.
    """
    kw = {"opts": art.RenderOpts(width_px=width_px, coalesce=True), "con": chained_banded}
    assert art.render_bytes(WIDE_BOUNDS, "density", workers=1, **kw) == art.render_bytes(
        WIDE_BOUNDS, "density", workers=4, **kw
    )


def test_a_band_draws_the_chains_it_was_given(chained_banded, tmp_path):
    """The worker half of the mechanism, driven directly rather than through a pool.

    A job carrying an assignment must draw *that* assignment. Handing it one that
    puts every edge in a chain of its own is the same thing as not coalescing, so if
    the band worked its own out instead the two would come back the same.
    """
    import pyarrow

    n = 600
    alone = pyarrow.table(
        {
            "edge_id": pyarrow.array(range(1, n + 1), pyarrow.int64()),
            "chain_id": pyarrow.array(range(1, n + 1), pyarrow.int64()),
            "seq": pyarrow.array([0] * n, pyarrow.int32()),
        }
    )
    width = 4000  # wide enough that a stroke is more than a pixel across
    proj = art.Projection.fit(
        WIDE_BOUNDS, width, art.Projection.canvas_height(WIDE_BOUNDS, width)
    )
    window = art.Window(WIDE_BOUNDS, chained_banded)

    def band(chains):
        return art._draw_band(
            art._BandJob(
                db_path=str(tmp_path / "test.duckdb"),
                bounds=(
                    WIDE_BOUNDS.min_lon,
                    WIDE_BOUNDS.min_lat,
                    WIDE_BOUNDS.max_lon,
                    WIDE_BOUNDS.max_lat,
                ),
                width=width,
                height=proj.height,
                dev_y0=0,
                dev_y1=proj.height,
                draw_scale=1.0,
                style="density",
                opts=art.RenderOpts(width_px=width, coalesce=True),
                query=art.DEFAULT_SPEC,
                source=art.DEFAULT_SOURCE,
                weights=window.weights,
                group_stats=None,
                chains=chains,
            )
        )

    real = window.chain_table()
    assert real.num_rows == n
    # Two lanes, each an unbroken run: exactly what `alone` is not.
    assert len(set(real.column("chain_id").to_pylist())) == 2
    assert band(real)[3] != band(alone)[3]


def test_the_collar_is_sized_for_the_stroke_density_actually_draws():
    """What the collar arithmetic rests on, and the only part of it a test can carry.

    A band draws past its own rows by `_band_pad`, which is half `max_line_px` plus a
    constant, so a stroke wider than `max_line_px` reaches paint the band never makes
    -- a seam. `max_line_px` is a number declared on the style, and the width is a
    ramp inside `draw_density`: nothing but this equality ties the two together, and
    widening the halo without touching the declaration would leave every banded
    render of `density` seamed while `_band_pad` still looked correct."""
    assert art.STYLES["density"].max_line_px == art.density_halo_width(1.0)


def test_a_style_scaling_with_the_canvas_says_so():
    """The two regimes, stated as a test so a new style has to choose one. `ref_px`
    left None means `max_line_px` is absolute pixels; set, it means pixels at a
    canvas that wide, and the collar scales with `width_px`."""
    absolute = art.Style(draw=lambda *a: None, max_line_px=4.0)
    assert absolute.max_stroke_px(1000) == absolute.max_stroke_px(8000) == 4.0
    scaled = art.Style(draw=lambda *a: None, max_line_px=4.0, ref_px=2000.0)
    assert scaled.max_stroke_px(1000) == 2.0
    assert scaled.max_stroke_px(8000) == 16.0
    assert scaled.max_stroke_px(8000, line_scale=2.0) == 32.0
    # The one style that is in the scaled regime, and the constant it shares with
    # `draw_density` -- the two must not drift, or the collar is sized for a picture
    # the style does not draw.
    assert art.STYLES["density"].ref_px == art.DENSITY_REF_PX


def test_bands_hold_roughly_equal_numbers_of_edges(banded):
    """Cutting the canvas into equal heights put 48% of Great Britain's edges into
    one of eight bands, so the render waited on one core. The cuts follow the edges."""
    proj = art.Projection.fit(BOUNDS, 200, 400)
    w = art.Window(BOUNDS, banded)
    n_edges, cuts = art.band_cuts(banded, w.sql, BOUNDS, proj, 400, 4)
    assert n_edges == 60
    counts = [
        sum(
            1
            for lat in range(51_420_000, 51_420_000 + 60 * 3_000, 3_000)
            if lo <= proj(BOUNDS.min_lon, lat / 1e6)[1] < hi
        )
        for lo, hi in itertools.pairwise(cuts)
    ]
    assert len(counts) == 4
    assert max(counts) - min(counts) <= 2


def test_the_edge_count_respects_the_spec_filter(banded):
    """The count decides whether to band at all, so it has to be the count of what
    will actually be drawn. Reading it straight off `edges` would start eight
    processes for a spec that filters the window down to nothing."""
    proj = art.Projection.fit(BOUNDS, 200, 400)
    w = art.Window(BOUNDS, banded, spec=art.QuerySpec(road_class=("motorway",)))
    n_edges, _ = art.band_cuts(banded, w.sql, BOUNDS, proj, 400, 4)
    assert n_edges == 0


def test_a_band_never_queries_outside_the_window(banded):
    """An unclamped collar would select edges the serial render never drew, and for a
    grouped style those arrive with groups the window's statistics have never seen."""
    proj = art.Projection.fit(BOUNDS, 200, 400)
    top = art._band_window(BOUNDS, proj, 0.0, 100.0, pad=500.0)
    assert top.max_lat <= BOUNDS.max_lat
    assert top.min_lat >= BOUNDS.min_lat


def test_injected_group_statistics_are_what_the_grouped_queries_read(banded):
    """`Source.groups` is how every band gets the whole window's ribbon widths and
    draw order rather than its own. If the substitution silently fell back to the
    derived CTE, `strands` would still render -- just differently per band."""
    stats = art.Window(BOUNDS, banded, with_groups=True).group_stats()
    banded.register("wf_gstat", art._stats_table([(g, n, t * 3) for g, n, t in stats]))
    injected = art.Window(
        BOUNDS, banded, with_groups=True, source=art.Source(groups="wf_gstat")
    )
    assert [(g, n, t * 3) for g, n, t in stats] == injected.group_stats()


def test_svg_falls_back_rather_than_failing(banded):
    """Banding pastes rasters, so there is nothing to paste for a vector format. A
    request for speed that cannot be honoured is not an error.

    Asserted on the bytes rather than by comparing against a serial render, because
    on libcairo 1.16 -- which the shipped image has, though the dev shell has 1.18 --
    `density` and `strands` fall back to one embedded `<image>` whose id comes from a
    process-wide counter. Two SVGs from one process differ there however identical
    their pixels are, so the comparison would fail for a reason that has nothing to
    do with banding."""
    kw = {"fmt": ".svg", "opts": art.RenderOpts(width_px=200), "con": banded}
    out = art.render_bytes(BOUNDS, "strands", workers=4, **kw)
    assert out.startswith(b"<?xml") and out == art.render_bytes(
        BOUNDS, "strands", workers=1, **kw
    )


def test_held_edges_fall_back_too(banded):
    """`render(edges=...)` never touches the database, and a band process has no way
    to be handed a list that lives in the parent."""
    edges = art.load_edges(BOUNDS, con=banded)
    kw = {"opts": art.RenderOpts(width_px=200), "edges": edges}
    assert art.render_bytes(BOUNDS, "density", workers=4, **kw) == art.render_bytes(
        BOUNDS, "density", workers=1, **kw
    )


def test_a_writable_connection_is_not_banded(con):
    """DuckDB gives a writer an exclusive lock, so a band process could not open the
    file at all. `band_source` probes rather than assuming, and the render falls back
    instead of dying in a worker with an IOException nobody asked about."""
    assert art.band_source(con) is None


def test_a_band_reopens_the_database_it_was_given(banded):
    """Not `config.DB_PATH`. A caller may hand `render` a connection to any database,
    and a band that opened the configured one instead would quietly draw a different
    picture -- in the parallel path only."""
    path = art.band_source(banded)
    assert path is not None and path.name == "test.duckdb"


def test_worker_count_comes_from_the_environment_when_it_is_set(monkeypatch):
    monkeypatch.setenv("WAYFARE_RENDER_WORKERS", "3")
    assert art.default_workers() == 3
    assert art.default_workers(7) == 7, "an explicit request still wins"
    monkeypatch.setenv("WAYFARE_RENDER_WORKERS", "not a number")
    assert art.default_workers() >= 1, "a bad value warns and falls back"


def test_worker_count_counts_cores_rather_than_threads(monkeypatch):
    """A hyperthreaded core does not draw a band any faster. Measured on the box that
    serves this: `uk` `density` at 2,000px is 26.9s on four workers and 28.1s on
    eight, on four cores of eight threads."""
    monkeypatch.delenv("WAYFARE_RENDER_WORKERS", raising=False)
    monkeypatch.setattr(art.band.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(art.band, "_cgroup_cpus", lambda: None)
    monkeypatch.setattr(art.band, "_physical_cpus", lambda: 4)
    assert art.default_workers() == 4


def test_worker_count_falls_back_to_threads_where_cores_are_unknown(monkeypatch):
    """Off Linux there is no /proc/cpuinfo to read. Over-counting costs a few percent;
    guessing low would leave half the box idle, so the logical count stands."""
    monkeypatch.delenv("WAYFARE_RENDER_WORKERS", raising=False)
    monkeypatch.setattr(art.band.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(art.band, "_cgroup_cpus", lambda: None)
    monkeypatch.setattr(art.band, "_physical_cpus", lambda: None)
    assert art.default_workers() == 8


def test_a_cpu_quota_still_wins_over_the_core_count(monkeypatch):
    """The render container runs at `cpus: 4` on a bigger box. Whichever is smaller."""
    monkeypatch.delenv("WAYFARE_RENDER_WORKERS", raising=False)
    monkeypatch.setattr(art.band.os, "cpu_count", lambda: 64)
    monkeypatch.setattr(art.band, "_physical_cpus", lambda: 32)
    monkeypatch.setattr(art.band, "_cgroup_cpus", lambda: 4)
    assert art.default_workers() == 4


def test_physical_cpus_reads_core_ids(tmp_path, monkeypatch):
    """Two hardware threads sharing a (physical id, core id) pair are one core."""
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text(
        "processor\t: 0\nphysical id\t: 0\ncore id\t: 0\n\n"
        "processor\t: 1\nphysical id\t: 0\ncore id\t: 1\n\n"
        "processor\t: 2\nphysical id\t: 0\ncore id\t: 0\n\n"
        "processor\t: 3\nphysical id\t: 0\ncore id\t: 1\n"
    )
    real_path = art.band.Path
    monkeypatch.setattr(
        art.band, "Path", lambda p: cpuinfo if p == "/proc/cpuinfo" else real_path(p)
    )
    assert art.band._physical_cpus() == 2


# --- Provenance ---------------------------------------------------------------


def _png_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """Every chunk in a PNG, with its CRC checked. Fails on a malformed one."""
    assert data.startswith(art.PNG_SIGNATURE)
    out, i = [], 8
    while i < len(data):
        (length,) = struct.unpack(">I", data[i : i + 4])
        kind, body = data[i + 4 : i + 8], data[i + 8 : i + 8 + length]
        (crc,) = struct.unpack(">I", data[i + 8 + length : i + 12 + length])
        assert crc == zlib.crc32(kind + body), kind
        out.append((kind, body))
        i += 12 + length
    return out


def _png_text(data: bytes) -> dict[str, str]:
    fields = {}
    for kind, body in _png_chunks(data):
        if kind == b"tEXt":
            keyword, _, value = body.partition(b"\0")
            fields[keyword.decode("latin-1")] = value.decode("latin-1")
    return fields


def _rows(data: bytes) -> tuple[int, int, list[bytes]]:
    """A PNG decoded back to pixel rows by cairo -- a real decoder, and one that
    refuses a file whose chunks do not check out."""
    import cairo

    surface = cairo.ImageSurface.create_from_png(io.BytesIO(data))
    surface.flush()
    stride, height = surface.get_stride(), surface.get_height()
    raw = bytes(surface.get_data())
    rows = [raw[y * stride : (y + 1) * stride] for y in range(height)]
    return surface.get_width(), height, rows


CREDIT_OPTS = replace(RENDER_OPTS, credit=True)


def test_a_render_carries_the_credit_with_no_flag(drawable):
    """Metadata is unconditional: an image served over HTTP leaves this machine
    whether or not whoever asked for it thought about the licence."""
    png = art.render_bytes(BOUNDS, "density", opts=RENDER_OPTS, con=drawable)
    assert _png_text(png)["Copyright"] == licences.text(config.credit_parts())


def test_a_png_with_metadata_still_decodes(drawable):
    """The chunk is written by hand, so what is worth testing is that a decoder
    which validates CRCs still reads the file."""
    png = art.render_bytes(BOUNDS, "density", opts=RENDER_OPTS, con=drawable)
    width, height, _ = _rows(png)
    assert (width, height) == (300, art.Projection.canvas_height(BOUNDS, 300))


def test_the_text_chunks_come_before_the_image_data(drawable):
    """Where a reader looking for a copyright expects one, rather than after
    however many megabytes of IDAT."""
    png = art.render_bytes(BOUNDS, "density", opts=RENDER_OPTS, con=drawable)
    kinds = [kind for kind, _ in _png_chunks(png)]
    assert kinds[0] == b"IHDR"
    assert kinds.index(b"tEXt") < kinds.index(b"IDAT")


def test_a_value_outside_latin_1_widens_the_chunk():
    """`tEXt` is Latin-1 only, and a publisher whose name is not is one
    `config.FEEDS` entry away. It must widen rather than raise mid-render. Latin-1
    covers the accents these islands need, so the case is a name from further off."""
    wide = art._png_text("Copyright", "\N{COPYRIGHT SIGN} Zarząd Transportu")
    assert wide[4:8] == b"iTXt"
    assert "Zarząd" in wide[8:-4].decode("utf-8")
    assert art._png_text("Copyright", "\N{COPYRIGHT SIGN} plain")[4:8] == b"tEXt"


def test_svg_metadata_parses_as_xml_and_holds_the_credit(drawable):
    svg = art.render_bytes(BOUNDS, "density", fmt=".svg", opts=RENDER_OPTS, con=drawable)
    root = ElementTree.fromstring(svg.decode("utf-8"))
    dc = "{http://purl.org/dc/elements/1.1/}"
    assert [e.text for e in root.findall(f".//{dc}rights")] == [
        licences.text(config.credit_parts())
    ]
    assert root.findall(f".//{dc}title")[0].text == "wayfare density: a window"


def test_the_metadata_says_where_the_picture_is(drawable):
    """A render that has been through a chat client and back is otherwise a picture
    of somewhere nobody can name."""
    png = art.render_bytes(BOUNDS, "density", opts=RENDER_OPTS, con=drawable)
    assert "-3.3,51.4,-3.1,51.6" in _png_text(png)["Description"]


def test_the_metadata_holds_nothing_that_moves(tmp_path, drawable):
    """No timestamp, no version, no path. A render is compared byte for byte, so a
    field that moved would break that for every window rather than for one."""
    out = tmp_path / "a-nameable-path.png"
    art.render(BOUNDS, "density", out, opts=RENDER_OPTS, con=drawable, workers=1)
    fields = _png_text(out.read_bytes())
    assert fields["Software"] == "wayfare"  # bare: a version string would move
    assert not any(
        re.search(r"\d{4}-\d\d-\d\d|20\d\d|\d+\.\d+\.\d+", v) for v in fields.values()
    )
    assert not any(str(tmp_path) in v for v in fields.values())


@pytest.mark.parametrize("fmt", [".png", ".svg"])
def test_a_credited_render_is_byte_identical_across_two_calls(drawable, fmt):
    """Both halves of this change sit in the hot path of the determinism claim, and
    neither may make a render a function of anything but its request."""
    kw = {"fmt": fmt, "opts": CREDIT_OPTS, "con": drawable}
    assert art.render_bytes(BOUNDS, "density", **kw) == art.render_bytes(
        BOUNDS, "density", **kw
    )


def test_the_credit_caption_is_absent_until_it_is_asked_for(drawable):
    """Off by default because it changes the artwork; on, it changes only the strip
    it is drawn in and never the map."""
    plain = art.render_bytes(BOUNDS, "density", opts=RENDER_OPTS, con=drawable)
    credited = art.render_bytes(BOUNDS, "density", opts=CREDIT_OPTS, con=drawable)
    assert plain != credited
    _, height, before = _rows(plain)
    _, _, after = _rows(credited)
    differing = [y for y in range(height) if before[y] != after[y]]
    assert differing, "the caption drew nothing"
    assert min(differing) > height * 0.9, "the caption reached above the bottom strip"


def test_a_credited_banded_render_draws_one_caption(banded):
    """Bands are pasted in before the caption is drawn, so the parent lays it down
    once. Drawn inside `_draw_band` there would be one per band -- which is what the
    equality catches, and the strip is where it would show."""
    opts = art.RenderOpts(width_px=200, credit=True)
    serial = art.render_bytes(BOUNDS, "density", opts=opts, con=banded, workers=1)
    parallel = art.render_bytes(BOUNDS, "density", opts=opts, con=banded, workers=4)
    assert serial == parallel
    plain = art.RenderOpts(width_px=200)
    _, height, before = _rows(
        art.render_bytes(BOUNDS, "density", opts=plain, con=banded, workers=4)
    )
    _, _, after = _rows(parallel)
    differing = [y for y in range(height) if before[y] != after[y]]
    # A looser strip than the test above, because this canvas is 200px tall enough
    # for two lines of shrunken text; four bands would still put ink at a quarter,
    # a half and three quarters of the way down, which this rules out.
    assert differing and min(differing) > height * 0.8


def test_the_credit_shrinks_to_fit_a_small_canvas(drawable):
    """No floor, for the reason `density`'s line widths have none: a thumbnail
    should look like the render reduced. What it must not do is run off the edge."""
    kw = {"con": drawable}
    plain = _rows(
        art.render_bytes(BOUNDS, "density", opts=replace(RENDER_OPTS, width_px=120), **kw)
    )
    credited = _rows(
        art.render_bytes(BOUNDS, "density", opts=replace(CREDIT_OPTS, width_px=120), **kw)
    )
    width, height, before = plain
    _, _, after = credited
    inked = [
        x
        for y in range(height)
        for x in range(width)
        if before[y][x * 4 : x * 4 + 4] != after[y][x * 4 : x * 4 + 4]
    ]
    assert inked, "the caption drew nothing at all"
    assert max(inked) < width - 10, "the caption ran past the right margin"


# --- the knobs the tests turn ------------------------------------------------


def test_the_band_floor_is_read_where_it_is_patched(monkeypatch):
    """The banded fixtures drop this to nothing to force banding on a small window.

    Splitting `art` into a package put the reader in `art.band` while the fixtures
    still set it on the package, so the floor stayed at its real value, no render
    banded, and every "banded matches serial" test compared the serial path with
    itself. It is not re-exported now, so patching the wrong object raises.
    """
    assert not hasattr(art, "MIN_BAND_EDGES")
    monkeypatch.setattr(art.band, "MIN_BAND_EDGES", 0)
    assert art.band.MIN_BAND_EDGES == 0


def test_the_worker_knobs_are_read_where_they_are_patched(monkeypatch):
    """Same trap, and the one that hides best: a worker count patched on the package
    leaves `default_workers` reporting the host's real cores, so a test asserting 4
    passes on a 64-core box for the wrong reason."""
    monkeypatch.setattr(art.band.os, "cpu_count", lambda: 64)
    monkeypatch.setattr(art.band, "_physical_cpus", lambda: 32)
    monkeypatch.setattr(art.band, "_cgroup_cpus", lambda: 4)
    assert art.default_workers() == 4


def test_the_group_cap_is_the_one_on_the_package(monkeypatch):
    """`MAX_GROUPS` is the cap an API caller is told about, so `art.MAX_GROUPS` has
    to be the knob. Its reader takes it off the package at call time rather than
    binding it at import, which would make the assignment a silent no-op."""
    monkeypatch.setattr(art, "MAX_GROUPS", 1)
    assert art.stream._max_groups() == 1
