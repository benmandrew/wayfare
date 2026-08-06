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


# --- WKT --------------------------------------------------------------------


def test_parse_linestring():
    pts = art.parse_linestring("LINESTRING(-2.245000 53.480000, -2.240000 53.481000)")
    assert pts == [(-2.245, 53.48), (-2.24, 53.481)]


@pytest.mark.parametrize(
    "wkt",
    [
        "LINESTRING (-2.245 53.48, -2.24 53.481)",
        "linestring(-2.245 53.48, -2.24 53.481)",
        "  LINESTRING(-2.245 53.48, -2.24 53.481)  ",
    ],
)
def test_parse_linestring_tolerates_formatting(wkt):
    assert art.parse_linestring(wkt)[0] == (-2.245, 53.48)


def test_wrong_geometry_type_raises():
    """Silently rendering nothing would look like a data gap rather than a bug."""
    with pytest.raises(ValueError, match="not a WKT LINESTRING"):
        art.parse_linestring("POINT(-2.245 53.48)")


# --- Windowing --------------------------------------------------------------


def test_hits_is_a_bbox_overlap_not_containment():
    b = art.Bounds(-2.30, 53.45, -2.20, 53.50)
    # A road crossing the window from outside on both sides still belongs in it.
    assert b.hits([-2.50, -2.10], [53.47, 53.47])
    assert b.hits([-2.25], [53.47])
    assert not b.hits([-2.40, -2.35], [53.47, 53.47])
    assert not b.hits([-2.25], [53.60])


def test_padded_expands_symmetrically():
    b = art.Bounds(-2.0, 53.0, -1.0, 54.0).padded(0.5, 0.25)
    assert (b.min_lon, b.max_lon) == (-2.5, -0.5)
    assert (b.min_lat, b.max_lat) == (52.75, 54.25)


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
