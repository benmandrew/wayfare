"""How an edge is painted.

A style is handed a :class:`~wayfare.art.stream.Frame`, a projection and the
drawing options, and nothing else -- it may not ask what the query was. Adding one
is a `draw_` function and an entry in :data:`STYLES`, and the fields on
:class:`Style` are what the rest of the package needs to know about it.
"""

from __future__ import annotations

import colorsys
import hashlib
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .. import logs
from .geometry import Polyline, Projection
from .stream import Frame

if TYPE_CHECKING:  # pragma: no cover - typing only
    import cairo

log = logs.get("art")


RGB = tuple[float, float, float]
# Maps a normalised traffic weight in [0, 1] to a line width, alpha or saturation.
Ramp = Callable[[float], float]

# The canvas width `density`'s stroke widths are quoted against. `RenderOpts.scale`
# handles print resolution; this handles the other axis, how much map a pixel
# covers. Chosen as 2,000 because that is `RenderOpts.width_px`'s own default.
DENSITY_REF_PX = 2000.0


@dataclass(frozen=True, slots=True)
class RenderOpts:
    width_px: int = 2000
    height_px: int | None = None  # None fits the window exactly, no letterbox
    scale: float = 1.0  # surface multiplier for print; 2.0 is ~192 dpi
    caption: str | None = None  # off by default
    # Burn the data credit into the corner. Off by default, unlike the metadata
    # every render carries: this one changes the artwork, and whether a picture is
    # going somewhere that keeps a file's metadata is the caller's knowledge, not
    # this module's. See the provenance section.
    credit: bool = False
    background: RGB | None = None  # overrides the style's own ground
    hue: float = 0.56  # base hue for density, palette rotation elsewhere
    line_scale: float = 1.0
    alpha_scale: float = 1.0
    # Vertices closer than this to the last one kept are dropped. In canvas pixels,
    # so the detail retained follows the output size: the same window keeps four
    # times the vertices at 4,000px that it does at 1,000. Half a pixel is below
    # what antialiasing can show. Set to 0 to keep every vertex as stored.
    #
    # A drawing concern rather than a query one, which is why it lives here and
    # `QuerySpec.sample` does not: this changes how a line is stroked, not which
    # lines there are.
    simplify_px: float = 0.5
    # Join runs of edges that meet end to end and paint the same into one stroke, so
    # a shared node is capped once instead of twice. Off by default: it is a change
    # to the picture, and which picture is right is a judgement about what the render
    # is for. See the coalescing section. Only `density` reads it.
    coalesce: bool = False


StyleFn = Callable[["cairo.Context[cairo.Surface]", Frame, Projection, RenderOpts], None]


@dataclass(frozen=True, slots=True)
class Style:
    draw: StyleFn
    # The widest stroke this style lays down at `line_scale=1`. Required rather than
    # defaulted: only banding reads it, and only to work out how far outside a band
    # an edge can still be and paint into it -- so a style that inherited someone
    # else's number would either grow a seam or query a collar it never draws in,
    # and both are invisible until a picture is looked at closely. It is a property
    # of the ramps in `draw`, so declaring it is part of writing a style.
    #
    # There are two regimes, and `ref_px` is which one this style is in. Left None,
    # `max_line_px` is absolute pixels: the style strokes the same width whatever the
    # canvas. Set, it is pixels *at a canvas `ref_px` wide*, and the real width scales
    # with `width_px` -- which is what `density` does, so that the map and the lines
    # shrink together. A canvas-scaling style that inherited the absolute reading
    # would get a collar too wide below `ref_px` and, worse, too narrow above it.
    max_line_px: float
    background: RGB = (0.02, 0.02, 0.035)
    needs_groups: bool = False
    blurb: str = ""
    ref_px: float | None = None
    # Whether this style reads `RenderOpts.coalesce`. Declared rather than inferred so
    # a request for it against a style that ignores it says so instead of quietly
    # doing nothing -- and so the reasons the other two decline are written down in
    # one place. See the coalescing section.
    coalesces: bool = False

    def max_stroke_px(self, width_px: float, line_scale: float = 1.0) -> float:
        """The widest stroke this style can lay down on a canvas `width_px` wide."""
        w = (
            self.max_line_px
            if self.ref_px is None
            else self.max_line_px * width_px / self.ref_px
        )
        return w * line_scale


def _stable_unit(text: str) -> float:
    """A deterministic float in [0, 1) from a string.

    Python's `hash` is salted per process, so using it here would give a different
    palette on every run. Rendering the same area twice must give the same picture.
    """
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2**64


_GOLDEN = 0.6180339887498949


def _stroke_path(ctx: cairo.Context[cairo.Surface], line: Polyline) -> None:
    """Append one polyline to the current path, vertex by vertex.

    Indexing the shared coordinate buffers rather than unpacking a tuple per vertex,
    which is the whole reason :class:`Polyline` holds indices: cairo wants two floats
    and everything between the database and here now hands it two floats.
    """
    xs, ys = line.xs, line.ys
    it = iter(line.idx)
    first = next(it, None)
    if first is None:
        return
    ctx.move_to(xs[first], ys[first])
    for i in it:
        ctx.line_to(xs[i], ys[i])


def density_halo_width(t: float) -> float:
    """The halo pass's width, in units of DENSITY_REF_PX of canvas.

    It is the widest thing `density` draws, so `STYLES["density"].max_line_px` is its
    value at t=1 and the band collar is sized off that. Named rather than inlined
    among the ramps below so the two cannot drift apart unnoticed.
    """
    return 1.5 + 8.0 * t


def draw_density(
    ctx: cairo.Context[cairo.Surface],
    window: Frame,
    proj: Projection,
    opts: RenderOpts,
) -> None:
    """One hue on a dark ground; busy corridors bloom.

    Two additive strokes -- a wide, almost invisible halo under a narrow bright
    core. ADD is commutative, so overlapping routes accumulate light exactly the
    way a long exposure does and draw order does not matter.

    Both strokes are laid down in *one* walk of the window rather than two. The
    commutativity above is exactly what licenses that: cairo's ADD saturates at
    full brightness, and saturating addition is commutative and associative, so
    halo-then-core per edge and every-halo-then-every-core give the same buffer to
    the byte. It halves the scanning, decoding and projecting a render does.

    The same commutativity is what makes this the one style with the junction
    artefact, and the one that `opts.coalesce` addresses. Every stroke gets a round
    cap at both ends, so where two edges meet the shared node is painted twice and
    ADD makes that a bright dot -- see the coalescing section.
    """
    import cairo

    ctx.set_operator(cairo.Operator.ADD)

    # (width, alpha, saturation) as functions of normalised traffic: first the
    # broad dim halo, then the narrow bright core over it. Widths are in units of
    # DENSITY_REF_PX of canvas, not in pixels -- see below.
    passes: tuple[tuple[Ramp, Ramp, Ramp], ...] = (
        (density_halo_width, lambda t: 0.012 + 0.075 * t, lambda t: 0.95),
        (
            lambda t: 0.25 + 1.8 * t**0.8,
            lambda t: 0.10 + 0.80 * t,
            lambda t: 0.90 - 0.75 * t,
        ),
    )
    # A stroke width fixed in pixels is a different picture at every canvas size:
    # the map shrinks with the canvas and the lines do not, so the same window at
    # 1,600px lays the 4,000px weight over 40% of the road length and the additive
    # passes clip to white in every town centre. That is what made the /art default
    # (1,600px) look nothing like the CLI one (4,000px), and it made a preview a
    # poor guide to the render it stands in for. Scaling with the canvas makes the
    # two the same picture at two resolutions.
    #
    # No floor: a genuinely small canvas *should* draw hairlines, the same way
    # downsampling the big render would. `line_scale` is the knob for overriding
    # any of this.
    weight_scale = opts.line_scale * opts.width_px / DENSITY_REF_PX
    # A sampled preview draws a fraction of the edges, so each survivor carries the
    # light of the ones that were dropped. Linear in the sample rate because ADD is
    # linear: n times the alpha over 1/n of the edges sums to the same brightness.
    #
    # Only until it clips. The core pass already runs at alpha 0.10 to 0.90, so
    # multiplying by 8 pins most of it at 1.0 and the light that would have gone
    # above cannot be recovered -- measured at 62% of the full render's brightness
    # rather than 100%. Widening the lines instead would close the gap and ruin the
    # thing a preview is for, since line weight is one of the knobs being judged. So
    # the preview stays a little dark, says so on the page, and is followed by the
    # real render.
    alpha_scale = opts.alpha_scale * window.alpha_compensation

    # Bound once: this is a property that may run a query on first touch, and it
    # is read once per edge.
    weights = window.weights
    for weight, pts in window.paths(proj, tol=opts.simplify_px, coalesce=opts.coalesce):
        t = weights.of(weight)
        for width_of, alpha_of, sat_of in passes:
            r, g, b = colorsys.hsv_to_rgb(opts.hue, sat_of(t), 1.0)
            ctx.set_source_rgba(r, g, b, min(1.0, alpha_of(t) * alpha_scale))
            ctx.set_line_width(width_of(t) * weight_scale)
            ctx.new_path()
            _stroke_path(ctx, pts)
            ctx.stroke()


def draw_spectrum(
    ctx: cairo.Context[cairo.Surface],
    window: Frame,
    proj: Projection,
    opts: RenderOpts,
) -> None:
    """Hue by compass bearing, so the grid's orientation becomes visible colour.

    Bearing is taken modulo 180 degrees and stretched over the full colour wheel.
    A road is an axis, not an arrow -- driving it the other way must not change its
    colour -- and folding at 180 has the happy side effect that perpendicular
    streets come out in complementary hues, which is what makes a gridded city read
    as a plaid and an organic one read as a smear.

    The angle is measured in screen space, which is legitimate here only because
    Mercator is conformal: the projected angle equals the true bearing.

    This style alone never simplifies its geometry, and the reason is that here a
    vertex is not only shape. Every other style would draw the same line through
    fewer points; this one derives the *colour* from the angle between them, so
    dropping a vertex merges two bearings into their average and repaints that
    stretch of road a different hue. Measured over a million edges, half a pixel of
    tolerance moved 74% of the output bytes -- against 0.05% for `density`. Any
    future style taking colour, width or order from geometry inherits this.
    """
    import cairo

    ctx.set_operator(cairo.Operator.OVER)

    # Quietest first so the busy roads finish on top and stay legible. The ordering
    # is done in SQL rather than by sorting the window in memory -- the weight is
    # monotonic in the trip count, so ordering by one orders by the other.
    weights = window.weights
    for weight, pts in window.paths(proj, tol=0.0, by_weight=True):
        t = weights.of(weight)
        sat = 0.30 + 0.62 * t
        val = 0.52 + 0.48 * t
        alpha = min(1.0, (0.30 + 0.62 * t) * opts.alpha_scale)
        ctx.set_line_width((0.6 + 3.4 * t**0.8) * opts.line_scale)
        for x0, y0, x1, y1 in pts.segments():
            dx, dy = x1 - x0, y1 - y0
            if dx == 0.0 and dy == 0.0:
                continue
            # Screen y grows downward, so negate it to get a north-up bearing.
            bearing = math.atan2(dx, -dy) % math.pi
            r, g, b = colorsys.hsv_to_rgb((bearing / math.pi + opts.hue) % 1.0, sat, val)
            ctx.set_source_rgba(r, g, b, alpha)
            ctx.new_path()
            ctx.move_to(x0, y0)
            ctx.line_to(x1, y1)
            ctx.stroke()


def draw_strands(
    ctx: cairo.Context[cairo.Surface],
    window: Frame,
    proj: Projection,
    opts: RenderOpts,
) -> None:
    """Every service its own translucent ribbon, woven together.

    All of a service's edges go into a single path and are stroked once. That is
    the whole trick: cairo composites a stroke as one operation, so a service that
    doubles back on itself stays evenly translucent, and only *different* services
    build up on top of each other. Stroking edge by edge would blotch every
    terminus and shared corridor.

    That requirement is why this one streams grouped rather than flat: the window
    hands back (service, edge) pairs already ordered by service, so a ribbon is
    accumulated into cairo's current path and stroked when the name changes. Only
    one service's geometry is ever live.
    """
    import cairo

    ctx.set_operator(cairo.Operator.SCREEN)
    current: str | None = None
    drew = False

    def finish() -> None:
        if current is not None:
            ctx.stroke()

    # Widest first, so the long trunk routes lie underneath the local fiddly ones.
    for name, weight, pts in window.group_paths(proj, tol=opts.simplify_px):
        if name != current:
            finish()
            current = name
            drew = True
            u = _stable_unit(name)
            # Golden-ratio hue stepping off a stable hash: adjacent services in the
            # list land far apart on the wheel without a hand-built palette.
            hue = (u + _GOLDEN * len(name)) % 1.0
            # Held deliberately saturated and a little dark: SCREEN washes everything
            # toward white where services pile up, so pale ribbons turn the busy
            # middle of a city into a grey blur instead of a weave.
            r, g, b = colorsys.hsv_to_rgb(
                (hue + opts.hue) % 1.0, 0.68 + 0.27 * u, 0.68 + 0.24 * (1.0 - u)
            )
            ctx.set_source_rgba(
                r, g, b, min(1.0, (0.22 + 0.26 * weight) * opts.alpha_scale)
            )
            ctx.set_line_width((0.9 + 3.0 * weight) * opts.line_scale)
            ctx.new_path()
        _stroke_path(ctx, pts)
    finish()

    if not drew:
        log.warning("no service names on these edges; strands has nothing to draw")


# --- Coalescing ---------------------------------------------------------------
#
# `art` strokes one cairo path per directed edge, and a Valhalla directed edge is
# 4.14 coordinates over tens of metres -- so a road is dozens of short strokes laid
# end to end. Every stroke gets a round cap at both ends, and `density` composites
# with ADD, so a node two edges share is painted twice: a bright dot at every
# junction, and at every point Valhalla happened to split a road. That is an
# artefact of how the geometry is stored, not something in the timetable.
#
# `RenderOpts.coalesce` joins runs of edges that meet head to tail and paint the same
# into a single stroke, which caps the run's two ends and joins everything between.
# `publish` already does this for tiles, and this is deliberately not the same code:
# there the grouping key is the tile attributes and the chaining is undirected, here
# it is the drawn weight and the chaining follows direction. See `_Sql.chain_query`
# for why direction matters and `Window._chained_paths` for why simplification stays
# per edge.
#
# Three things are worth stating outright.
#
# **Banding still holds.** A band computes its chains over its own collar window, so
# it can chain differently from the serial render -- but only at a node outside that
# window, because any edge incident on a node *inside* it has a bounding box that
# overlaps it and is therefore selected too. `_band_pad` is half the widest stroke
# plus slack, so ink from a node outside the collar cannot reach the band's own rows.
# The two renders agree on every pixel that is kept. This is the same argument the
# existing collar rests on, applied to chaining decisions rather than to strokes, and
# it needs no wider collar than the one already there.
#
# **Directed pairs are not collapsed.** An ordinary two-way street is two coincident
# edges and `publish` drops one of them. Doing that here would halve the light on
# every two-way road, which is a different picture rather than a repaired one.
#
# **The two other styles decline, for opposite reasons.** `spectrum` strokes each
# *segment* separately to colour it by its own bearing, so it has a cap at every
# vertex rather than only at shared nodes, and nothing short of changing what it
# means by colour would remove them. `strands` already puts a whole service into one
# cairo path; cairo fills a stroke's outline once with nonzero winding, so caps that
# overlap inside a single stroke do not accumulate, and there is nothing to remove.

STYLES: dict[str, Style] = {
    "density": Style(
        draw=draw_density,
        background=(0.015, 0.018, 0.03),
        blurb="weekly trip volume as light",
        # `density_halo_width` at full traffic, quoted at DENSITY_REF_PX as it is.
        max_line_px=9.5,
        ref_px=DENSITY_REF_PX,
        coalesces=True,
    ),
    "spectrum": Style(
        draw=draw_spectrum,
        background=(0.03, 0.03, 0.04),
        blurb="hue by compass bearing",
        max_line_px=4.0,  # 0.6 + 3.4
    ),
    "strands": Style(
        draw=draw_strands,
        background=(0.04, 0.035, 0.045),
        needs_groups=True,
        blurb="one ribbon per service",
        max_line_px=3.9,  # 0.9 + 3.0
    ),
}
