from __future__ import annotations

import math

import pytest

from wayfare import art, db


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


# --- Projection, simplification and sampling ---------------------------------


def test_batch_projection_matches_the_scalar_one(con):
    """The batched path is vectorised for speed, not for a different answer. If the
    two ever disagree the same window draws differently depending on whether it came
    from the database or from a list."""
    proj = art.Projection.fit(art.Bounds(-3.3, 51.4, -3.1, 51.6), 800, 600)
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
        _art_edge(con, i + 1, -3200000 + i * 1000, trips=10 * (i + 1))
    bounds = art.Bounds(-3.30, 51.40, -3.10, 51.60)

    full = art.Window(bounds, con)
    thin = art.Window(bounds, con, spec=art.QuerySpec(sample=8))

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
        _art_edge(con, i + 1, -3200000 + i * 1000, trips=100)
    bounds = art.Bounds(-3.30, 51.40, -3.10, 51.60)
    spec = art.QuerySpec(sample=8)
    first = [e.edge_id for e in art.Window(bounds, con, spec=spec).edges()]
    second = [e.edge_id for e in art.Window(bounds, con, spec=spec).edges()]
    assert first == second


def test_sampling_leaves_group_widths_alone(con):
    """Ribbon width and draw order come from the group listing, which is taken over
    the whole window. Sampling that too would make a preview weight its ribbons
    differently from the render it stands in for."""
    for i in range(64):
        _art_edge(con, i + 1, -3200000 + i * 1000, trips=100, services=("42",))
    bounds = art.Bounds(-3.30, 51.40, -3.10, 51.60)

    full = art.Window(bounds, con, with_groups=True)
    thin = art.Window(bounds, con, with_groups=True, spec=art.QuerySpec(sample=8))

    widths = {(n, round(w, 9)) for n, w, _ in full.groups()}
    thin_widths = {(n, round(w, 9)) for n, w, _ in thin.groups()}
    assert thin_widths == widths
    # ...while the geometry it hands back really is thinner.
    assert 0 < len(list(thin.groups())) < len(list(full.groups()))


def test_paths_agree_with_projecting_the_edge_stream(con):
    """`paths` exists to skip building degrees at all, so it has its own decode. It
    must still land on the pixels the unprojected stream would have."""
    for i in range(3):
        _art_edge(con, i + 1, -3200000 + i * 5000, trips=100 * (i + 1))
    bounds = art.Bounds(-3.30, 51.40, -3.10, 51.60)
    proj = art.Projection.fit(bounds, 800, 600)
    w = art.Window(bounds, con)

    viaPaths = [(weight, line.points()) for weight, line in w.paths(proj)]
    viaEdges = [(e.weight, [proj(lon, lat) for lon, lat in e.coords]) for e in w.edges()]
    assert viaPaths == viaEdges


def test_held_paths_match_the_streaming_ones(con):
    _art_edge(con, 1, -3200000, trips=900, services=("42",))
    _art_edge(con, 2, -3190000, trips=10, services=("42",))
    bounds = art.Bounds(-3.30, 51.40, -3.10, 51.60)
    proj = art.Projection.fit(bounds, 800, 600)

    streamed = art.Window(bounds, con, with_groups=True)
    held = art.Held(art.load_edges(bounds, with_groups=True, con=con))
    assert list(held.paths(proj)) == list(streamed.paths(proj))
    assert [(n, round(w, 9), p) for n, w, p in held.group_paths(proj)] == [
        (n, round(w, 9), p) for n, w, p in streamed.group_paths(proj)
    ]


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


def test_bounds_in_sql_match_the_python_percentile_pass(con):
    """The scale is found by SQL now instead of by pulling every weight into
    Python. The two must pick the *same* two order statistics -- an approximation
    would shift a render's contrast invisibly. See `_Sql.bounds_query`."""
    from array import array

    for i in range(50):
        _art_edge(con, i + 1, -3200000 + i * 200, trips=(i * i) % 97)
    bounds = art.Bounds(-3.30, 51.40, -3.10, 51.60)
    for spec in (
        art.DEFAULT_SPEC,
        art.QuerySpec(weight="services"),
        art.QuerySpec(weight="density"),
        art.QuerySpec(min_trips=10),
    ):
        w = art.Window(bounds, con, spec=spec)
        query, params = w.sql.weights_query()
        expected = art.Weights.over(
            array("d", (r[0] for r in con.execute(query, params).fetchall()))
        )
        assert w.weights == expected, spec.key


def test_an_empty_window_still_has_a_usable_scale_from_sql(con):
    w = art.Window(art.Bounds(-3.30, 51.40, -3.10, 51.60), con)
    assert w.weights.of(10) == 0.5


def test_the_weight_scale_is_not_computed_until_it_is_asked_for(con):
    """`strands` never reads it -- it weights ribbons from the group statistics --
    so computing it eagerly was a whole extra pass over the window per render,
    thrown away. See `Window.weights`."""
    _art_edge(con, 1, -3200000, trips=900)
    w = art.Window(art.Bounds(-3.30, 51.40, -3.10, 51.60), con)
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
    w = art.Window(art.Bounds(-3.30, 51.40, -3.10, 51.60), con)
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
        _art_edge(con, edge_id, lon, trips=100)
    w = art.Window(art.Bounds(-3.30, 51.40, -3.10, 51.60), con)
    query, params = w.sql.edges_query(with_groups=False, by_weight=False)
    assert [r[0] for r in con.execute(query, params).fetchall()] == [1, 3, 7, 9]
    assert [e.edge_id for e in w.edges()] == [1, 3, 7, 9]


# --- Banding ------------------------------------------------------------------
#
# The claim banding rests on is that it changes nothing: `workers=8` and `workers=1`
# must produce the same bytes, or every measurement taken against one of them is
# about a different picture. The tests below check that claim from both ends -- the
# pieces that make it true, and the finished render.


def _band_edge(con, edge_id, lat_e6, trips=100, services=("42",), agency="OP1"):
    """One edge at a given latitude. Banding cuts north to south, so unlike
    `_art_edge` the geometry has to vary in latitude rather than longitude."""
    con.execute(
        "INSERT INTO edges VALUES (?, 1, 'R', 'secondary', 100.0, "
        "[-3200000, -3199000], [?, ?], -3200000, ?, -3199000, ?)",
        [edge_id, lat_e6, lat_e6, lat_e6, lat_e6],
    )
    for s in services:
        con.execute(
            "INSERT INTO edge_services VALUES (?, ?, ?, 1, ?)", [edge_id, s, agency, trips]
        )


@pytest.fixture
def banded(con, tmp_path, monkeypatch):
    """A window with edges spread over its whole height, read through a *read-only*
    handle -- which is what both `_render` and the server open, and what a band
    process needs the file to still be openable as. `MIN_BAND_EDGES` is dropped to
    nothing because the floor is about start-up cost, not about correctness."""
    for i in range(60):
        _band_edge(
            con,
            i + 1,
            51_420_000 + i * 3_000,
            trips=1 + i * 40,
            services=("42",) if i % 3 else ("9A", "7"),
        )
    con.close()
    monkeypatch.setattr(art, "MIN_BAND_EDGES", 0)
    ro = db.connect(tmp_path / "test.duckdb", read_only=True)
    yield ro
    ro.close()


BAND_BOUNDS = art.Bounds(-3.30, 51.40, -3.10, 51.60)


@pytest.mark.parametrize("style", sorted(art.STYLES))
def test_a_banded_render_is_byte_identical_to_a_serial_one(banded, style):
    """The whole justification. Verified on the real 2.7M-edge database over the
    `uk` window as well, for all three styles."""
    kw = {"opts": art.RenderOpts(width_px=200), "con": banded}
    assert art.render_bytes(BAND_BOUNDS, style, workers=1, **kw) == art.render_bytes(
        BAND_BOUNDS, style, workers=4, **kw
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
    assert art.render_bytes(BAND_BOUNDS, "density", workers=1, **kw) == art.render_bytes(
        BAND_BOUNDS, "density", workers=4, **kw
    )


def test_bands_hold_roughly_equal_numbers_of_edges(banded):
    """Cutting the canvas into equal heights put 48% of Great Britain's edges into
    one of eight bands, so the render waited on one core. The cuts follow the edges."""
    proj = art.Projection.fit(BAND_BOUNDS, 200, 400)
    w = art.Window(BAND_BOUNDS, banded)
    n_edges, cuts = art.band_cuts(banded, w.sql, BAND_BOUNDS, proj, 400, 4)
    assert n_edges == 60
    counts = [
        sum(
            1
            for lat in range(51_420_000, 51_420_000 + 60 * 3_000, 3_000)
            if lo <= proj(BAND_BOUNDS.min_lon, lat / 1e6)[1] < hi
        )
        for lo, hi in zip(cuts, cuts[1:], strict=False)
    ]
    assert len(counts) == 4
    assert max(counts) - min(counts) <= 2


def test_the_edge_count_respects_the_spec_filter(banded):
    """The count decides whether to band at all, so it has to be the count of what
    will actually be drawn. Reading it straight off `edges` would start eight
    processes for a spec that filters the window down to nothing."""
    proj = art.Projection.fit(BAND_BOUNDS, 200, 400)
    w = art.Window(BAND_BOUNDS, banded, spec=art.QuerySpec(road_class=("motorway",)))
    n_edges, _ = art.band_cuts(banded, w.sql, BAND_BOUNDS, proj, 400, 4)
    assert n_edges == 0


def test_a_band_never_queries_outside_the_window(banded):
    """An unclamped collar would select edges the serial render never drew, and for a
    grouped style those arrive with groups the window's statistics have never seen."""
    proj = art.Projection.fit(BAND_BOUNDS, 200, 400)
    top = art._band_window(BAND_BOUNDS, proj, 0.0, 100.0, pad=500.0)
    assert top.max_lat <= BAND_BOUNDS.max_lat
    assert top.min_lat >= BAND_BOUNDS.min_lat


def test_injected_group_statistics_are_what_the_grouped_queries_read(banded):
    """`Source.groups` is how every band gets the whole window's ribbon widths and
    draw order rather than its own. If the substitution silently fell back to the
    derived CTE, `strands` would still render -- just differently per band."""
    stats = art.Window(BAND_BOUNDS, banded, with_groups=True).group_stats()
    banded.register("wf_gstat", art._stats_table([(g, n, t * 3) for g, n, t in stats]))
    injected = art.Window(
        BAND_BOUNDS, banded, with_groups=True, source=art.Source(groups="wf_gstat")
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
    out = art.render_bytes(BAND_BOUNDS, "strands", workers=4, **kw)
    assert out.startswith(b"<?xml") and out == art.render_bytes(
        BAND_BOUNDS, "strands", workers=1, **kw
    )


def test_held_edges_fall_back_too(banded):
    """`render(edges=...)` never touches the database, and a band process has no way
    to be handed a list that lives in the parent."""
    edges = art.load_edges(BAND_BOUNDS, con=banded)
    kw = {"opts": art.RenderOpts(width_px=200), "edges": edges}
    assert art.render_bytes(BAND_BOUNDS, "density", workers=4, **kw) == art.render_bytes(
        BAND_BOUNDS, "density", workers=1, **kw
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
