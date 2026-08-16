"""What a render says about itself: the metadata, and the optional caption.

Two mechanisms on purpose. The credit in the file's metadata is unconditional,
costs nothing and cannot alter the picture; the credit drawn into the corner is a
change to the artwork and so is opt-in. Both are post-processes on finished bytes,
because pycairo writes neither format's metadata.
"""

from __future__ import annotations

import struct
import zlib
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape

from .. import config, licences
from .geometry import Bounds, Projection
from .styles import RenderOpts

if TYPE_CHECKING:  # pragma: no cover - typing only
    import cairo


def _font(ctx: cairo.Context[cairo.Surface], size: float) -> None:
    import cairo

    ctx.select_font_face("sans-serif", cairo.FontSlant.NORMAL, cairo.FontWeight.NORMAL)
    ctx.set_font_size(size)


def _text_width(
    ctx: cairo.Context[cairo.Surface], text: str, size: float, tracking: float
) -> float:
    """How wide `text` will draw, laid out the way :func:`_line` lays it out."""
    _font(ctx, size)
    return sum(ctx.text_extents(ch).x_advance + tracking * size for ch in text)


def _line(
    ctx: cairo.Context[cairo.Surface],
    text: str,
    *,
    x: float,
    y: float,
    size: float,
    alpha: float,
    tracking: float,
) -> None:
    """One line of small, low-contrast text, drawn a glyph at a time.

    The toy text API has no letter-spacing, so the advance is done by hand -- which
    is also what lets :func:`_text_width` predict the result exactly.
    """
    import cairo

    ctx.save()
    ctx.set_operator(cairo.Operator.OVER)
    _font(ctx, size)
    ctx.set_source_rgba(1.0, 1.0, 1.0, alpha)
    for ch in text:
        ctx.move_to(x, y)
        ctx.show_text(ch)
        x += ctx.text_extents(ch).x_advance + tracking * size
    ctx.restore()


# The credit caption's size as a fraction of the canvas width. Smaller than the
# user's own caption because it is a footnote to the picture rather than a title
# for it, and because it is a sentence rather than a phrase.
CREDIT_REF_PX = 220.0
# The size it starts at on a canvas too narrow for the fraction above to give a
# readable one. A starting point and not a floor: fitting the line between the
# margins wins over it, because text running off the edge is a broken picture where
# text too small to read is only a small one.
CREDIT_MIN_PX = 6.5

# The same two for the user's own caption, which is a title rather than a footnote
# and so is drawn larger.
CAPTION_REF_PX = 130.0
CAPTION_MIN_PX = 10.0


def _captions(
    ctx: cairo.Context[cairo.Surface], proj: Projection, opts: RenderOpts
) -> None:
    """Whatever text goes in the bottom-left corner: credit lowest, caption above.

    Drawn once, in the serial parent, after every band has been pasted in -- a
    caption laid down inside :func:`_draw_band` would appear once per band, and each
    band would clip it to its own rows. It is also why the captions are the last
    thing to touch the surface: they composite with OVER, and the additive and
    screening styles would otherwise take the text as light to accumulate.

    Nothing here can perturb the picture beyond the pixels it paints. It reads the
    projection for a canvas size and nothing else -- no weight scale, no window, no
    band collar -- so a credited render draws the same map as an uncredited one.
    """
    size = max(CAPTION_MIN_PX, proj.width / CAPTION_REF_PX)
    x = size * 2.2
    y = proj.height - size * 2.2
    if opts.credit:
        # One line per thing being credited, rather than one long sentence: the
        # break falls where the meaning does, and two short lines fit a canvas that
        # one long one does not.
        lines = licences.lines(config.credit_parts(), links=False)
        c_size = max(CREDIT_MIN_PX, proj.width / CREDIT_REF_PX)
        room = proj.width - 2 * x
        widest = max(_text_width(ctx, line, c_size, 0.0) for line in lines)
        # Shrunk to fit rather than clipped, and with no floor once it comes to
        # that -- the same rule as `density`'s line widths. A thumbnail should look
        # like the render reduced, and text that keeps its point size while the
        # canvas halves is the same mistake as a stroke width that does. Below a few
        # hundred pixels the credit is a grey mark rather than a readable line; the
        # metadata is what carries the obligation at that size.
        if widest > room > 0:
            c_size *= room / widest
        for line in reversed(lines):
            _line(ctx, line, x=x, y=y, size=c_size, alpha=0.45, tracking=0.0)
            y -= c_size * 1.5
        # A gap before the user's own caption, so the two read as separate things.
        y -= c_size * 0.8
    if opts.caption:
        _line(ctx, opts.caption.upper(), x=x, y=y, size=size, alpha=0.40, tracking=0.22)


#
# Every render carries its credit whether or not anyone asked for one: an image
# served over HTTP leaves this machine, and a picture drawn from timetables under
# attribution and ODbL road geometry that says so nowhere is an uncredited
# derivative work. The timetable's licence varies by region -- OGL v3.0 for BODS
# and Translink, CC BY 4.0 for the Republic's NTA feed -- so the text comes from
# `config.credit_text()` rather than being named here.
# Metadata costs nothing, cannot alter the picture, and needs no flag. The visible
# caption above does alter the picture, so that one is opt-in.
#
# Nothing here may vary between two renders of the same window: no timestamp, no
# hostname, no output path, no version. A render is tested byte for byte, and a
# field that moves would break that for every window rather than for the one it
# was added for. This is why there is no `Creation Time` and why `Software` is the
# bare name -- a version string would be correct, and it would also make every
# stored render's bytes a function of the release that drew it.

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Dublin Core is what an SVG `<metadata>` block conventionally carries, so each PNG
# keyword is mapped to its element rather than invented twice. PNG's registered
# keywords are the keys because a decoder that shows any text at all shows those.
_DC_ELEMENTS = {
    "Title": "dc:title",
    "Description": "dc:description",
    "Software": "dc:creator",
    "Copyright": "dc:rights",
}


def _provenance(bounds: Bounds, bounds_or_name: Bounds | str, style: str) -> dict[str, str]:
    """What a finished render says about itself.

    Four fields, and the argument for each is that it is fixed by the request. The
    credit is the obligation. The style and the window are what the picture *is*,
    and a render that has been through a chat client and back is otherwise a
    picture of somewhere nobody can name -- both are arguments the caller supplied,
    so neither can move under a re-render.

    The feed version is deliberately absent, tempting though it is. It would have to
    be queried, which `render(edges=...)` has no connection to do, and it would make
    the bytes of a render a function of when the timetable was downloaded rather than
    of what was asked for. The database's provenance belongs to the database.
    """
    where = bounds_or_name if isinstance(bounds_or_name, str) else "a window"
    box = ",".join(
        f"{v:g}" for v in (bounds.min_lon, bounds.min_lat, bounds.max_lon, bounds.max_lat)
    )
    return {
        "Title": f"wayfare {style}: {where}",
        "Description": f"Bus routes on the road network, window {box}.",
        "Software": "wayfare",
        "Copyright": licences.text(config.credit_parts()),
    }


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    """Length, type, data, CRC32 of type and data. That is the whole format."""
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data))
    )


def _png_text(keyword: str, value: str) -> bytes:
    """One text chunk: `tEXt` where the value is Latin-1, `iTXt` where it is not.

    `tEXt` is the chunk every decoder reads and it is Latin-1 only, which covers the
    copyright sign and so covers the credit as it stands. An attribution with an
    accent in it is one `config.FEEDS` entry away, though, and the failure would be
    a `UnicodeEncodeError` in the middle of a render -- so the wider chunk is the
    fallback rather than the default.
    """
    try:
        return _png_chunk(
            b"tEXt", keyword.encode("latin-1") + b"\0" + value.encode("latin-1")
        )
    except UnicodeEncodeError:
        # keyword, NUL, uncompressed, method 0, empty language and translated
        # keyword, then UTF-8 text.
        head = keyword.encode("latin-1") + b"\0\0\0" + b"\0" + b"\0"
        return _png_chunk(b"iTXt", head + value.encode("utf-8"))


def _png_with(data: bytes, fields: dict[str, str]) -> bytes:
    """Splice text chunks in after IHDR, which is where a reader expects them.

    pycairo writes no metadata of its own and this is not worth a dependency for:
    a PNG is a signature and a run of chunks, and the only thing to get right is the
    CRC.
    """
    if not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        raise ValueError("cairo did not write a PNG this can annotate")
    end = 8 + 12 + struct.unpack(">I", data[8:12])[0]
    return data[:end] + b"".join(_png_text(k, v) for k, v in fields.items()) + data[end:]


def _svg_with(data: bytes, fields: dict[str, str]) -> bytes:
    """Insert an RDF `<metadata>` block directly after the opening `<svg>` tag.

    Also a post-process, and for the same reason: cairo has no way to write one.
    """
    open_tag = data.find(b"<svg")
    end = data.find(b">", open_tag) + 1
    if open_tag < 0 or end <= 0:
        raise ValueError("cairo did not write an SVG this can annotate")
    body = "".join(
        f"\n   <{_DC_ELEMENTS[k]}>{escape(v)}</{_DC_ELEMENTS[k]}>"
        for k, v in fields.items()
    )
    block = (
        '\n<metadata>\n <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"'
        '\n          xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'\n  <rdf:Description rdf:about="">{body}\n  </rdf:Description>'
        "\n </rdf:RDF>\n</metadata>"
    )
    return data[:end] + block.encode("utf-8") + data[end:]


def _stamped(data: bytes, fmt: str, fields: dict[str, str]) -> bytes:
    return _png_with(data, fields) if fmt == ".png" else _svg_with(data, fields)
