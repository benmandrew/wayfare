"""Art renderings of the bus network.

Purpose (2) of the project: take a window onto the UK and draw every road that
carries a bus, weighted by how much bus it carries. This package owns the whole
path from a bounding box to a finished PNG or SVG. It is deliberately separate
from the tile publishing code -- tiles are for reading, these are for looking at.

The three styles are the point. ``density`` is the classic glowing-arteries look,
``spectrum`` colours the grid by the compass bearing of each segment, and
``strands`` gives every service its own translucent ribbon so the overlaps weave.
Add more via :data:`STYLES`.

The split, in the order a render passes through it:

- `deps` -- the lazy imports of the `art` extra.
- `geometry` -- the window, the projection, the polyline an edge becomes.
- `query` -- which edges, weighted how, grouped by what: the spec and its SQL.
- `stream` -- the edges themselves, offered one at a time, and their weight scale.
- `styles` -- how an edge is painted.
- `provenance` -- what a finished render says about itself.
- `canvas` -- the surface it is drawn on and the format it is written in.
- `band` -- drawing one picture in several processes.
- `render` -- the entry points that put those together.

Everything a caller used before the split is re-exported here, so `art.X` still
resolves. Two of those names are knobs rather than values -- `MAX_GROUPS` and
`MIN_BAND_EDGES` -- and their readers deliberately take them off this package at
call time rather than binding them at import; see the comments at each reader.
"""

from __future__ import annotations

from .band import (
    _band_pad as _band_pad,
)
from .band import (
    _band_window as _band_window,
)
from .band import (
    _BandJob as _BandJob,
)
from .band import (
    _draw_band as _draw_band,
)
from .band import (
    _stats_table as _stats_table,
)
from .band import (
    band_cuts,
    band_source,
    default_workers,
)
from .canvas import (
    FORMATS,
    Sink,
)
from .canvas import (
    _canvas as _canvas,
)
from .canvas import (
    _default_path as _default_path,
)
from .canvas import (
    _fmt as _fmt,
)
from .canvas import (
    _surface as _surface,
)
from .geometry import (
    ISLES,
    PRESETS,
    Bounds,
    Polyline,
    Projection,
    parse_bbox,
    resolve,
    simplify,
)
from .provenance import (
    CAPTION_MIN_PX,
    CAPTION_REF_PX,
    CREDIT_MIN_PX,
    CREDIT_REF_PX,
    PNG_SIGNATURE,
)
from .provenance import (
    _png_text as _png_text,
)
from .provenance import (
    _provenance as _provenance,
)
from .provenance import (
    _stamped as _stamped,
)
from .query import (
    CHAIN_VIEW,
    DEFAULT_SOURCE,
    DEFAULT_SPEC,
    EDGES,
    GROUPS,
    MAX_GROUPS,
    ORDERS,
    SERVICES,
    WEIGHTS,
    QuerySpec,
    Source,
)
from .query import (
    _Sql as _Sql,
)
from .render import render, render_bytes
from .stream import FETCH_ROWS, Edge, Frame, Held, Weights, Window, load_edges
from .styles import (
    DENSITY_REF_PX,
    RGB,
    STYLES,
    Ramp,
    RenderOpts,
    Style,
    StyleFn,
    density_halo_width,
    draw_density,
    draw_spectrum,
    draw_strands,
)

__all__ = [
    "CAPTION_MIN_PX",
    "CAPTION_REF_PX",
    "CHAIN_VIEW",
    "CREDIT_MIN_PX",
    "CREDIT_REF_PX",
    "DEFAULT_SOURCE",
    "DEFAULT_SPEC",
    "DENSITY_REF_PX",
    "EDGES",
    "FETCH_ROWS",
    "FORMATS",
    "GROUPS",
    "ISLES",
    "MAX_GROUPS",
    "ORDERS",
    "PNG_SIGNATURE",
    "PRESETS",
    "RGB",
    "SERVICES",
    "STYLES",
    "WEIGHTS",
    "Bounds",
    "Edge",
    "Frame",
    "Held",
    "Polyline",
    "Projection",
    "QuerySpec",
    "Ramp",
    "RenderOpts",
    "Sink",
    "Source",
    "Style",
    "StyleFn",
    "Weights",
    "Window",
    "band_cuts",
    "band_source",
    "default_workers",
    "density_halo_width",
    "draw_density",
    "draw_spectrum",
    "draw_strands",
    "load_edges",
    "parse_bbox",
    "render",
    "render_bytes",
    "resolve",
    "simplify",
]
