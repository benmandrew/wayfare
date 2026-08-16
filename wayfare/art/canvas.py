"""The surface a render is drawn on, and where the finished bytes go.

The format, the cairo surface for it, the context that surface is painted through,
and the default output path. Separate from `render` because a band process needs
the same context on a surface of its own, and separate from `styles` because none
of it depends on how an edge is painted.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from .. import config
from .geometry import Bounds
from .styles import RenderOpts, Style

if TYPE_CHECKING:  # pragma: no cover - typing only
    import cairo


FORMATS = (".png", ".svg")


def _fmt(path_or_suffix: Path | str) -> str:
    """The output format, from a path to write or from a bare suffix.

    Both, because the two entry points hold different things: `render` has a filename
    and `render_bytes` has `.png` or `.svg` on its own. A bare suffix has no suffix of
    its own -- `Path(".png").suffix` is empty, since a leading dot names a hidden file
    -- so an empty one means the text was already the answer.
    """
    text = str(path_or_suffix)
    suffix = (Path(text).suffix or text).lower()
    if suffix not in FORMATS:
        raise ValueError(f"unsupported output format {suffix!r}; use .png or .svg")
    return suffix


# Where a finished render goes: a filesystem path, or a buffer for a caller that
# wants the bytes and never the file. cairo takes either interchangeably, which is
# what lets the HTTP endpoint reuse the whole drawing path unchanged.
Sink = Path | BinaryIO


def _surface(
    fmt: str, buf: BinaryIO, w: int, h: int, scale: float
) -> tuple[cairo.Surface, float]:
    """Surface plus the factor drawing should be scaled by.

    SVG is resolution independent, so `scale` is ignored there and the surface is
    sized in points; PNG gets a bigger pixel buffer and a matching context scale,
    which keeps every line width in the styles meaning the same physical thickness.
    """
    import cairo

    if fmt == ".svg":
        # SVG writes as it draws, so the surface owns the buffer from the start.
        return cairo.SVGSurface(buf, w, h), 1.0
    return (
        cairo.ImageSurface(
            cairo.FORMAT_ARGB32, max(1, round(w * scale)), max(1, round(h * scale))
        ),
        scale,
    )


def _canvas(
    surface: Any, draw_scale: float, sty: Style, opts: RenderOpts, dev_origin: int = 0
) -> Any:
    """A context on `surface`, scaled for print and filled with the ground colour.

    `dev_origin` is the device row the surface's first row stands for, which is how a
    band draws its slice of a taller picture in the whole picture's coordinates; zero
    for a whole canvas. The shift is in device space and the print scale is applied
    after it, so `y_device = y_user * scale - origin` and a style still draws in the
    logical units it was written in.

    Typed loosely because a style takes a `Context[Surface]` and cairo's stubs make
    that invariant in the surface type.
    """
    import cairo

    ctx: Any = cairo.Context(surface)
    ctx.translate(0, -dev_origin)
    ctx.scale(draw_scale, draw_scale)
    ctx.set_antialias(cairo.Antialias.BEST)
    ctx.set_line_cap(cairo.LineCap.ROUND)
    ctx.set_line_join(cairo.LineJoin.ROUND)
    r, g, b = opts.background or sty.background
    ctx.set_source_rgb(r, g, b)
    ctx.paint()
    return ctx


def _default_path(bounds_or_name: Bounds | str, style: str) -> Path:
    stem = bounds_or_name if isinstance(bounds_or_name, str) else "custom"
    return config.OUT / f"{stem}-{style}.png"
