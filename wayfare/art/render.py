"""A window and a style in, a PNG or an SVG out.

The path every render takes, whichever entry point asked for it: resolve the
window, open the surface, draw the map serially or in bands, lay the captions over
it, stamp the provenance in, and hand the bytes to a file or to a caller.
"""

from __future__ import annotations

import io
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import config, db, logs
from .band import _draw_banded, default_workers
from .canvas import Sink, _canvas, _default_path, _fmt, _surface
from .deps import _require_cairo
from .geometry import Bounds, Projection, resolve
from .provenance import _captions, _provenance, _stamped
from .query import DEFAULT_SPEC, QuerySpec
from .stream import Edge, Frame, Held, Window
from .styles import STYLES, RenderOpts, Style

if TYPE_CHECKING:  # pragma: no cover - typing only
    import duckdb

log = logs.get("art")


def _render(
    bounds_or_name: Bounds | str,
    style: str,
    fmt: str,
    sink: Sink,
    label: str,
    *,
    opts: RenderOpts | None = None,
    query: QuerySpec = DEFAULT_SPEC,
    con: duckdb.DuckDBPyConnection | None = None,
    edges: Sequence[Edge] | None = None,
    workers: int = 1,
) -> None:
    """Draw into `sink`, which is a path to write or a buffer to fill.

    `label` names the destination in the log line only. Everything else about the
    drawing is identical either way -- there is no separate in-memory code path to
    diverge from the one that writes files.

    `style` and `query` are the two halves: the style decides how an edge is painted,
    the query decides which edges there are, what their weight means, and what a
    group is. Neither knows about the other, which is what lets three styles cover
    the whole product of the two.

    `workers` splits the canvas into that many horizontal bands and draws them in
    separate processes -- see the banding section. It is a speed knob and nothing
    else: the output is byte-identical either way, which is what makes it safe to
    turn on by default rather than something a caller has to reason about.
    """
    # Argument checks come first, and before requiring cairo: a mistyped style
    # should say which styles exist, not tell the caller to install a dependency
    # they would then discover was not the problem.
    try:
        sty = STYLES[style]
    except KeyError:
        known = ", ".join(sorted(STYLES))
        raise KeyError(f"unknown style {style!r}; known styles: {known}") from None

    bounds = resolve(bounds_or_name)
    opts = opts or RenderOpts()
    if opts.coalesce and not sty.coalesces:
        # Not an error -- it is a request for a different picture that this style does
        # not have -- but silence would look like it had been honoured.
        log.warning("%s ignores coalesce; see the coalescing section", style)

    _require_cairo()
    proj = Projection.fit(bounds, opts.width_px, _canvas_height(bounds, opts))

    # cairo draws into a buffer rather than into the sink, because both formats are
    # post-processed before they land: neither PNG nor SVG metadata is something
    # pycairo can write, so the finished bytes have to pass through this process
    # once. It costs one copy of the *encoded* image, against a raster that is
    # already resident and several times larger.
    buf = io.BytesIO()
    surface, draw_scale = _surface(fmt, buf, proj.width, proj.height, opts.scale)
    # Styles draw in logical units and the context is scaled up for print, so a
    # tolerance of half a logical pixel is half a *device* pixel only at 1x. Divide
    # it here, where `draw_scale` is known -- and where SVG's fixed 1.0 keeps a
    # vector output at full detail whatever `scale` was asked for.
    opts = replace(opts, simplify_px=opts.simplify_px / draw_scale)
    ctx = _canvas(surface, draw_scale, sty, opts)

    t0 = time.monotonic()
    _paint(
        surface,
        ctx,
        bounds,
        proj,
        style,
        opts,
        draw_scale,
        fmt,
        con=con,
        edges=edges,
        query=query,
        workers=workers,
    )
    # Last, and in this process whether or not the map was drawn in others: text
    # composites with OVER, and the additive and screening styles would take it as
    # light to accumulate.
    _captions(ctx, proj, opts)

    _emit(surface, buf, fmt, sink, _provenance(bounds, bounds_or_name, style))
    log.info(
        "%s %dx%d %s in %.1fs -> %s",
        style,
        proj.width,
        proj.height,
        f"@{opts.scale:g}x" if opts.scale != 1.0 else "",
        time.monotonic() - t0,
        label,
    )


def _canvas_height(bounds: Bounds, opts: RenderOpts) -> int:
    """The canvas height: the caller's, or the one that fits the window exactly."""
    return opts.height_px or Projection.canvas_height(bounds, opts.width_px)


def _open_window(
    bounds: Bounds,
    sty: Style,
    query: QuerySpec,
    con: duckdb.DuckDBPyConnection | None,
    edges: Sequence[Edge] | None,
) -> Frame:
    """What the style will draw from: the database, or edges the caller holds."""
    if edges is not None:
        return Held(edges, spec=query)
    assert con is not None  # _paint opens one before it asks for a window
    return Window(bounds, con, with_groups=sty.needs_groups, spec=query)


def _paint(
    surface: Any,
    ctx: Any,
    bounds: Bounds,
    proj: Projection,
    style: str,
    opts: RenderOpts,
    draw_scale: float,
    fmt: str,
    *,
    con: duckdb.DuckDBPyConnection | None,
    edges: Sequence[Edge] | None,
    query: QuerySpec,
    workers: int,
) -> None:
    """Draw the map onto `surface`, in bands or serially, and own the connection.

    Whatever this opens, it closes -- which is the render server's whole rule: DuckDB
    gives a writer an exclusive lock, so a handle left alive by a finished render
    stops the next pipeline stage from starting. A connection the caller supplied is
    the caller's to close.

    Banding needs a file to reopen per process and a raster to paste into, so it is
    off for an SVG, for edges the caller already holds, and for a database a worker
    cannot open. Each of those falls back to drawing serially rather than failing:
    `workers` asks for speed, and speed that cannot be had is not an error.
    """
    sty = STYLES[style]
    own_con = con is None and edges is None
    if own_con:
        con = db.connect(read_only=True)
    try:
        if (
            workers > 1
            and fmt == ".png"
            and edges is None
            and con is not None
            and _draw_banded(
                surface, bounds, proj, style, opts, draw_scale, con, query, workers
            )
        ):
            return
        window = _open_window(bounds, sty, query, con, edges)
        ctx.save()
        # Clip to the window rather than the frame: the query returns a collar of
        # edges just outside the bounds, and without this they bleed into the
        # letterbox.
        ctx.rectangle(*proj.content_rect(bounds))
        ctx.clip()
        try:
            sty.draw(ctx, window, proj, opts)
        finally:
            ctx.restore()
    finally:
        if own_con and con is not None:
            con.close()


def _emit(
    surface: Any, buf: io.BytesIO, fmt: str, sink: Sink, fields: dict[str, str]
) -> None:
    """Finish the surface, stamp the provenance in, and hand the bytes to the sink."""
    if fmt == ".png":
        surface.write_to_png(buf)
    # Flushes the SVG writer as well, so the buffer holds a complete document by the
    # time this returns.
    surface.finish()
    data = _stamped(buf.getvalue(), fmt, fields)
    if isinstance(sink, Path):
        sink.write_bytes(data)
    else:
        sink.write(data)


def render(
    bounds_or_name: Bounds | str,
    style: str = "density",
    out_path: str | Path | None = None,
    *,
    opts: RenderOpts | None = None,
    query: QuerySpec = DEFAULT_SPEC,
    con: duckdb.DuckDBPyConnection | None = None,
    edges: Sequence[Edge] | None = None,
    workers: int | None = None,
) -> Path:
    """Draw `bounds_or_name` in `style` and return the path written.

    `out_path` decides the format by suffix (.png or .svg) and defaults to
    ``OUT/<area>-<style>.png``. Pass `edges` to re-render a window you already
    loaded without touching the database again.

    `workers` defaults to every core -- see :func:`default_workers`.
    """
    path = Path(out_path) if out_path else _default_path(bounds_or_name, style)
    fmt = _fmt(path)  # before the query, so a typo'd suffix fails in milliseconds
    config.ensure_dirs()
    path.parent.mkdir(parents=True, exist_ok=True)
    _render(
        bounds_or_name,
        style,
        fmt,
        path,
        str(path),
        opts=opts,
        query=query,
        con=con,
        edges=edges,
        workers=default_workers(workers),
    )
    return path


def render_bytes(
    bounds_or_name: Bounds | str,
    style: str = "density",
    *,
    fmt: str = ".png",
    opts: RenderOpts | None = None,
    query: QuerySpec = DEFAULT_SPEC,
    con: duckdb.DuckDBPyConnection | None = None,
    edges: Sequence[Edge] | None = None,
    workers: int | None = None,
) -> bytes:
    """The same render, returned rather than written.

    What the HTTP endpoint serves. Nothing on a server that answers requests should
    have to invent a filename, and an image the size of a print render has no
    business landing in the output directory on the way to a socket.
    """
    fmt = _fmt(fmt)
    buf = io.BytesIO()
    _render(
        bounds_or_name,
        style,
        fmt,
        buf,
        "memory",
        opts=opts,
        query=query,
        con=con,
        edges=edges,
        workers=default_workers(workers),
    )
    return buf.getvalue()
