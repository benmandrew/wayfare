"""HTTP server: the viewer, the tile archives, and renders on demand.

Two jobs that belong on one port.

The first is static. PMTiles works by reading byte ranges out of one large file, so
the server has to answer with 206 Partial Content. Python's own ``http.server`` does
not implement Range at all -- it replies 200 with the whole file, which makes the
viewer fetch all 24 MB for every tile it wants. That looks like "slow" rather than
"broken", which is the annoying way to discover it.

The second is ``/art``. The expensive half of this project -- acquire, match,
aggregate -- happens on a server, and the design work does not, so iterating on a
style otherwise means copying tens of gigabytes to a laptop. Rendering where the
data already is makes that a query string: the endpoint takes a window, a style,
the style's knobs and the query spec -- what drives the ramps, what a group is,
which services count -- and answers with a PNG.

Renders are serialised and bounded. One at a time, because a render is CPU-bound
cairo over a full scan of ``edges`` and the same box is usually also matching --
running two would not finish either sooner. Bounded, because pixel count is the
one parameter a caller can raise without limit.

    wayfare serve [--port 8099] [--dir web] [--out /data/out] [--no-art]
"""

from __future__ import annotations

import contextlib
import functools
import gzip
import hashlib
import http.server
import io
import json
import os
import re
import socketserver
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import ParseResult, parse_qs, urlparse

import duckdb

from . import art, config, db, licences, logs

log = logs.get("server")

RANGE = re.compile(r"bytes=(\d*)-(\d*)")

# Emitted by `wayfare publish` into the output directory, not into web/. Any
# archive there is servable, not a fixed pair of names, so a machine holding
# several regions can offer all of them -- `wales.pmtiles` beside
# `london.pmtiles` -- and the viewer picks between them with ?tiles=.
ARTEFACT_SUFFIXES = (".pmtiles",)

# Worth compressing, and small enough that doing it per request costs nothing.
# Both spellings of JavaScript, because which one `guess_type` returns for .js
# depends on the interpreter -- text/javascript from 3.12, application/javascript
# under 3.11 -- and the vendored maplibre build is the largest thing the page
# loads, so a list holding one spelling serves it uncompressed on half of them. An
# archive is excluded by both tests below: it is three orders of magnitude past the
# cap, and gzipping a body the client means to read in ranges would defeat PMTiles
# entirely.
COMPRESSIBLE = frozenset(
    {
        "text/html",
        "text/css",
        "text/plain",
        "text/javascript",
        "application/javascript",
        "application/json",
    }
)
COMPRESS_MAX = 1 << 20

# How many compressed bodies to keep. Compressing per request is not cheap: the
# vendored maplibre build is 784 KB and takes 15.8 ms of CPU to gzip, against
# 0.04 ms to read off disk and hand to the socket. The page loads the same five or
# six files every time, so nearly all of that goes on producing bytes gzip has
# already produced moments earlier.
#
# A count rather than a byte budget, because `_gzip_wanted` already refuses
# anything over COMPRESS_MAX: sixteen entries is a 16 MB ceiling by construction
# and a few hundred KB in practice. The web directory holds six compressible files
# today, so the cap exists to bound a directory nobody has served yet rather than
# to evict anything real.
GZIP_CACHE_ENTRIES = 16

# How long a browser may reuse an archive without asking. A day by default.
#
# The archive is the one static thing here worth caching outright: it is ~130 MB
# read as hundreds of separate ranges, and republishing it is a monthly event
# with little change in between. Revalidating instead costs a round trip per
# range, and a round trip is most of what a range request costs -- measured
# against the deployed instance, a 16 KB range takes ~22 ms of which ~21 ms is the
# round trip itself. Relaying accounts for about 1.6 ms of that, so a direct path
# would not change the shape of this.
#
# A day rather than something nearer the publish interval, because the whole
# benefit is already collected well inside one: a session lasts minutes and
# revisits are usually same-day, so a longer window buys almost nothing further
# while widening how far behind a returning visitor can be. Past the day it is
# one 304 against the ETag, not 130 MB.
#
# The trade is still real: for up to a day after a publish, a returning visitor
# can be looking at the previous map. A reload revalidates, so there is always a
# way out. --max-age moves it; 0 goes back to revalidating every time.
ARCHIVE_MAX_AGE = 24 * 60 * 60

# The page keeps revalidating. It changes whenever the image is rebuilt rather
# than on the pipeline's schedule, and a stale index.html is a bug that outlives
# its own fix.
REVALIDATE = "no-cache"

# The vendored libraries are the exception, and the reason is their URLs. Each
# carries the version out of web/vendor/README.md as a query, so the URL a page
# asks for changes when the bytes behind it do -- which is what `immutable`
# promises and what the page alone could not.
#
# They were on REVALIDATE with the page, on the argument that the checks multiplex
# into roughly one round trip. That holds behind an HTTP/2 front end; a direct
# `wayfare serve` is not one. `protocol_version` is HTTP/1.1 over a threading TCP
# server, so a repeat visit spends two round trips across six connections asking
# seven conditional questions and transferring nothing. 803 KB of MapLibre, 65 KB
# of its CSS and 20 KB of pmtiles.js are the bulk of that, and none of them has
# ever changed without its version changing.
VENDOR_MAX_AGE = 365 * 24 * 60 * 60
VENDOR_PREFIX = "vendor"

# Render limits. Width alone is not the thing to cap: the window's aspect ratio
# decides the height, and `scale` multiplies both, so a modest-looking
# `width=4000&scale=4` over a tall window is 200 megapixels. The pixel budget is
# what actually bounds the work; the dimension cap is there to give a clearer error
# for the common typo of an extra zero. It bounds `height` as well as `width`, which
# is why it is not named for either.
MAX_DIMENSION = 12_000
MAX_PIXELS = 64_000_000
MAX_SCALE = 4.0
MAX_CAPTION = 120

# Query-spec limits. A filter becomes an `IN (...)` list of bound parameters, so a
# URL repeating `operator=` a few thousand times would have the server build the
# query rather than draw the picture. Sixty-four is well past any real choice: the
# whole country has a few hundred operators and five road classes.
MAX_FILTER_VALUES = 64
# A floor on weekly trips. The busiest edge in the country carries tens of
# thousands, so anything above this filters everything out and is a typo.
MAX_MIN_TRIPS = 1_000_000
# How many distinct operators and road classes /art/meta will list for the page's
# dropdowns. Enough for every real value, bounded so that a database with dirty
# agency ids cannot turn the metadata into the largest response the server sends.
MAX_FACET_VALUES = 500

# `sample=n` draws one edge in n. The cost of a render is per edge and hardly moves
# with the canvas, so this -- not `width` -- is what makes a preview cheap: a whole-
# of-GB window costs about the same at 900 pixels as at 4,000. 8 is what the studio
# page asks for while a slider is moving. Past about 16 there is too little left of
# the network to judge a change by, so the cap is low on purpose.
MAX_SAMPLE = 16
PREVIEW_SAMPLE = 8

# How long a request will wait for the render slot before giving up, and how many
# are allowed to be waiting. A studio page that re-renders on every slider move
# would otherwise build an unbounded backlog of renders nobody is looking at any
# more.
RENDER_WAIT_S = 90.0
QUEUE_LIMIT = 4

# Recent renders, kept because iterating on a design revisits the same parameters
# constantly -- nudge a slider and nudge it back, or reload the page. Sized to hold
# a handful of large PNGs and nothing more.
CACHE_BYTES = 96 * 1024 * 1024

CONTENT_TYPES = {".png": "image/png", ".svg": "image/svg+xml"}

# An instance, not the class: RenderOpts has slots, so its defaults live in the
# dataclass fields rather than as class attributes to read off.
DEFAULTS = art.RenderOpts(width_px=1600)
DEFAULT_STYLE = "density"


class BadRequest(ValueError):
    """A parameter the caller can fix, reported as 400 with the reason."""


def archives(out_dir: Path) -> list[str]:
    if not out_dir.is_dir():
        return []
    return sorted(p.name for p in out_dir.iterdir() if p.name.endswith(ARTEFACT_SUFFIXES))


# --- The render request -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArtRequest:
    """A parsed, validated and bounded ``/art`` query."""

    area: str  # a preset name or a raw window, kept as given for the cache key
    style: str
    fmt: str
    opts: art.RenderOpts
    # The other half of the render: which edges, weighted how, grouped by what. It
    # changes the picture as much as the style does, which is why its identity has to
    # reach `key` below.
    query: art.QuerySpec = art.DEFAULT_SPEC
    # Something the render will not fail on but the caller probably meant
    # differently. `art.parse_bbox` logs this to a terminal, which is no help at all
    # to an <img> tag: over HTTP the same mistake arrives as a black rectangle.
    warning: str | None = None

    @property
    def key(self) -> str:
        o = self.opts
        return "|".join(
            str(x)
            for x in (
                self.area,
                self.style,
                self.fmt,
                self.query.key,
                o.width_px,
                o.height_px,
                o.scale,
                o.hue,
                o.line_scale,
                o.alpha_scale,
                o.caption,
                o.credit,
                o.background,
                o.simplify_px,
                o.coalesce,
                # `sample` is not listed: it lives in the query spec, so it is
                # already inside `self.query.key`.
            )
        )

    def filename(self) -> str:
        stem = self.area if self.area in art.PRESETS else "window"
        return f"{stem}-{self.style}{self.fmt}"


def _one(q: dict[str, list[str]], name: str) -> str | None:
    values = q.get(name)
    return values[-1].strip() if values and values[-1].strip() else None


def _flag(q: dict[str, list[str]], name: str) -> bool:
    """An on/off switch, absent meaning off.

    The spellings a checkbox, a hand-typed URL and a scripted caller each reach for.
    Anything else is a mistake worth naming rather than reading as false, which is
    what `bool(raw)` would do with `coalesce=no`.
    """
    raw = _one(q, name)
    if raw is None:
        return False
    if raw.lower() in ("1", "true", "yes", "on"):
        return True
    if raw.lower() in ("0", "false", "no", "off"):
        return False
    raise BadRequest(f"{name}={raw!r} is not a yes or a no")


def _in_range(name: str, raw: str, lo: float, hi: float) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise BadRequest(f"{name}={raw!r} is not a number") from None
    if not lo <= value <= hi:
        raise BadRequest(f"{name}={value:g} is out of range; it runs {lo:g} to {hi:g}")
    return value


def _number(
    q: dict[str, list[str]], name: str, lo: float, hi: float, default: float
) -> float:
    """A range-checked number, or `default` when the parameter is absent.

    A default rather than None because several of these are legitimately zero --
    `hue=0` is red -- and falling back on a falsy value would silently ignore it.
    """
    raw = _one(q, name)
    return default if raw is None else _in_range(name, raw, lo, hi)


def _optional_number(
    q: dict[str, list[str]], name: str, lo: float, hi: float
) -> float | None:
    """The same check where absence is not a number at all.

    `height` has no value that stands in for leaving it out: unset, the window's
    aspect ratio decides it, and that is not something a default could say.
    """
    raw = _one(q, name)
    return None if raw is None else _in_range(name, raw, lo, hi)


def _count(q: dict[str, list[str]], name: str, hi: int) -> int:
    """A whole number from zero up, or zero when the parameter is absent.

    Separate from `_number` rather than an int() of it: truncating `min_trips=5.9` to
    5 would answer a question nobody asked, and a threshold is the parameter where
    quietly rounding is least welcome.
    """
    raw = _one(q, name)
    if raw is None:
        return 0
    try:
        value = int(raw)
    except ValueError:
        raise BadRequest(f"{name}={raw!r} is not a whole number") from None
    if not 0 <= value <= hi:
        raise BadRequest(f"{name}={value} is out of range; it runs 0 to {hi}")
    return value


def _many(q: dict[str, list[str]], name: str) -> tuple[str, ...]:
    """A filter's values, from repeats, commas, or both.

    Repeatable *and* comma-separated because the two callers want different things:
    a multi-select serialises as one comma-joined value and stays short, while a
    hand-written URL or a shell loop appends `&operator=` per value. Accepting both
    costs one split.

    Sorted and deduplicated, which is what makes `operator=A,B` and `operator=B,A`
    one cache entry rather than two. Order cannot matter to an `IN` list, so two
    spellings of the same filter must not draw twice.
    """
    values = {
        part.strip() for raw in q.get(name, []) for part in raw.split(",") if part.strip()
    }
    if len(values) > MAX_FILTER_VALUES:
        raise BadRequest(
            f"{name}= lists {len(values)} values, over the {MAX_FILTER_VALUES} limit. "
            "Filter to a handful and let the picture show the rest."
        )
    return tuple(sorted(values))


def _spec(q: dict[str, list[str]]) -> art.QuerySpec:
    """The data half of the request, validated by the spec itself.

    `weight`, `group` and `order` are checked by `QuerySpec.__post_init__`, whose
    message already names the alternatives -- and names them from the right table, so
    the reply to a mistyped `weight=` lists weights and not orders. Converting that
    one ValueError is better than restating three vocabularies here, where they would
    drift from `art.py` the first time one gained an entry.
    """
    # The filters are read outside the try so that their own BadRequest -- which is a
    # ValueError -- reaches the caller with its own message rather than this one.
    operator = _many(q, "operator")
    service = _many(q, "service")
    # `class` in a URL, `road_class` in the spec: the query string is written by hand
    # often enough that the shorter name is worth the mapping.
    road_class = _many(q, "class")
    min_trips = _count(q, "min_trips", MAX_MIN_TRIPS)
    # Range-checked here rather than left to the spec, because the spec's own floor
    # of 1 is a correctness bound and this is a usability one: past about 16 there
    # is too little of the network left to judge a change by.
    sample = int(_number(q, "sample", 1, MAX_SAMPLE, art.DEFAULT_SPEC.sample))
    try:
        return art.QuerySpec(
            weight=_one(q, "weight") or art.DEFAULT_SPEC.weight,
            group=_one(q, "group") or art.DEFAULT_SPEC.group,
            order=_one(q, "order") or art.DEFAULT_SPEC.order,
            operator=operator,
            service=service,
            road_class=road_class,
            min_trips=min_trips,
            sample=sample,
        )
    except ValueError as exc:
        raise BadRequest(str(exc)) from None


def _hex(colour: art.RGB) -> str:
    """A style's background as a colour input can take it."""
    return "#" + "".join(f"{round(c * 255):02x}" for c in colour)


def _colour(raw: str) -> art.RGB:
    """``#rrggbb``, ``rrggbb`` or three floats.

    Hex because that is what a colour input emits and what a stylesheet uses;
    floats because that is what :data:`art.STYLES` holds, so a background copied
    out of the source works unchanged.
    """
    text = raw.lstrip("#")
    if len(text) == 6 and "," not in text:
        try:
            r, g, b = (int(text[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
        except ValueError:
            raise BadRequest(f"background={raw!r} is not a hex colour") from None
        return (r, g, b)
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 3:
        raise BadRequest(
            f"background={raw!r} should be #rrggbb or three 0-1 floats as r,g,b"
        )
    try:
        r, g, b = (float(p) for p in parts)
    except ValueError:
        raise BadRequest(
            f"background={raw!r} has a component that is not a number"
        ) from None
    if not all(0.0 <= c <= 1.0 for c in (r, g, b)):
        raise BadRequest(f"background={raw!r} has a component outside 0 to 1")
    return (r, g, b)


def parse_art(query: str) -> ArtRequest:
    """Turn a query string into a bounded render request, or say what is wrong.

    Every value is range-checked here rather than at the point of use, so a bad
    request costs a parse and not a window query. The one check that cannot be done
    per-parameter is the pixel budget, because it is a product of three of them.
    """
    q = parse_qs(query, keep_blank_values=False)

    area = _one(q, "bbox") or _one(q, "area")
    if not area:
        raise BadRequest(
            "give an area: bbox=minlon,minlat,maxlon,maxlat, or area=<preset>. "
            f"Presets: {', '.join(sorted(art.PRESETS))}"
        )
    try:
        bounds = art.resolve(area)
    except (KeyError, ValueError) as exc:
        raise BadRequest(str(exc).strip("'\"")) from None

    style = _one(q, "style") or DEFAULT_STYLE
    if style not in art.STYLES:
        raise BadRequest(
            f"unknown style {style!r}; known styles: {', '.join(sorted(art.STYLES))}"
        )

    fmt = "." + (_one(q, "format") or "png").lstrip(".").lower()
    if fmt not in art.FORMATS:
        raise BadRequest(f"unknown format {fmt!r}; use png or svg")

    width = int(_number(q, "width", 64, MAX_DIMENSION, DEFAULTS.width_px))
    given_height = _optional_number(q, "height", 64, MAX_DIMENSION)
    height = None if given_height is None else int(given_height)
    # SVG is resolution independent, so `scale` does nothing there. Held at 1 rather
    # than accepted and ignored, which would put it in the cache key for no reason.
    scale = 1.0 if fmt == ".svg" else _number(q, "scale", 0.1, MAX_SCALE, 1.0)

    drawn_height = height or art.Projection.canvas_height(bounds, width)
    pixels = width * drawn_height * scale * scale
    if pixels > MAX_PIXELS:
        raise BadRequest(
            f"{width}x{drawn_height} at {scale:g}x is {pixels / 1e6:.0f} megapixels, "
            f"over the {MAX_PIXELS / 1e6:.0f} limit. Narrow the window, or drop the "
            "width or the scale."
        )

    caption = _one(q, "caption")
    if caption and len(caption) > MAX_CAPTION:
        raise BadRequest(f"caption is longer than {MAX_CAPTION} characters")

    # A lat,lon window parses cleanly and silently lands off West Africa -- a UK
    # latitude is a valid longitude and vice versa -- so this cannot be an error, but
    # it is the explanation for almost every empty render.
    warning = None
    if not bounds.hits(
        [art.ISLES.min_lon, art.ISLES.max_lon], [art.ISLES.min_lat, art.ISLES.max_lat]
    ):
        warning = (
            f"{area} lies outside the British Isles, so this render is empty. The "
            "order is minlon,minlat,maxlon,maxlat -- lon first, not lat."
        )

    background = _one(q, "background")
    return ArtRequest(
        area=area,
        style=style,
        fmt=fmt,
        query=_spec(q),
        warning=warning,
        opts=art.RenderOpts(
            width_px=width,
            height_px=height,
            scale=scale,
            caption=caption,
            # Off unless asked for, which is the whole design: the metadata credit
            # is unconditional and invisible, and this one is visible and therefore
            # a decision. See `art`'s provenance section.
            credit=_flag(q, "credit"),
            background=_colour(background) if background else None,
            hue=_number(q, "hue", 0.0, 1.0, DEFAULTS.hue),
            line_scale=_number(q, "line_scale", 0.05, 8.0, DEFAULTS.line_scale),
            alpha_scale=_number(q, "alpha_scale", 0.05, 8.0, DEFAULTS.alpha_scale),
            coalesce=_flag(q, "coalesce"),
        ),
    )


# --- Rendering --------------------------------------------------------------


class Unavailable(RuntimeError):
    """Nothing the caller did wrong; try again. Reported as 503."""


@contextlib.contextmanager
def _read_only() -> Iterator[duckdb.DuckDBPyConnection]:
    """A read-only handle for the length of one job, and closed again after it.

    Never kept between requests. DuckDB gives a writer an exclusive lock on the
    file, so a handle held open by this server would stop the next `match` or
    `aggregate` run from starting -- a viewer nobody is using would quietly block
    the pipeline. The open is metadata; the lock is what matters.

    A file that is missing or already locked is `Unavailable` rather than a
    traceback: neither is the caller's doing, and both come right on their own.
    """
    if not config.DB_PATH.exists():
        raise Unavailable(f"no database at {config.DB_PATH}; the pipeline has not run here")
    try:
        con = db.connect(read_only=True)
    except Exception as exc:  # duckdb raises several types for a held lock
        raise Unavailable(
            f"cannot read {config.DB_PATH}: {exc}. A pipeline stage is probably "
            "writing to it; renders work again once it finishes."
        ) from None
    try:
        yield con
    finally:
        con.close()


def _stamp() -> str:
    """Identity of the current database file, for cache keys and ETags.

    Size and mtime rather than a content hash: the file is up to tens of
    gigabytes, and every stage that changes it rewrites it in place.
    """
    try:
        st = config.DB_PATH.stat()
    except OSError:
        return "absent"
    return f"{st.st_size}-{st.st_mtime_ns}"


def _matches(if_none_match: str | None, etag: str) -> bool:
    """Whether an If-None-Match header covers this ETag.

    A list and a weak validator are both legal, and this sits behind a reverse proxy
    -- `tailscale serve` in the deployment it was written for -- so exact string
    equality would quietly redraw an image the client already has.
    """
    if not if_none_match:
        return False
    candidates = (c.strip() for c in if_none_match.split(","))
    return any(
        c == "*" or c.removeprefix("W/") == etag.removeprefix("W/") for c in candidates
    )


def _etag(key: str) -> str:
    # A digest rather than hash(), which is salted per process: two servers behind
    # the same URL would disagree about an identical render.
    return '"' + hashlib.sha256(key.encode()).hexdigest()[:32] + '"'


class Renderer:
    """Serialises renders, caches recent ones, and keeps the queue short."""

    def __init__(self, *, cache_bytes: int = CACHE_BYTES) -> None:
        self._slot = threading.BoundedSemaphore(1)
        self._waiting = 0
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
        self._cache_bytes = cache_bytes
        self._held = 0

    def stamp(self) -> str:
        return _stamp()

    def key(self, request: ArtRequest) -> str:
        """Everything a render depends on: its parameters and the database."""
        return f"{request.key}|{self.stamp()}"

    def etag(self, request: ArtRequest) -> str:
        return _etag(self.key(request))

    def render(self, request: ArtRequest) -> tuple[bytes, str]:
        """The image and the ETag it was stored under."""
        key = self.key(request)
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit
            if self._waiting >= QUEUE_LIMIT:
                raise Unavailable(
                    f"{self._waiting} renders already queued; this one would wait "
                    "too long to be worth drawing"
                )
            self._waiting += 1

        try:
            if not self._slot.acquire(timeout=RENDER_WAIT_S):
                raise Unavailable(f"a render has held the slot for over {RENDER_WAIT_S:g}s")
        finally:
            with self._lock:
                self._waiting -= 1

        try:
            # Checked again under the slot: while this request queued, the render it
            # was waiting behind may well have been the same one.
            with self._lock:
                hit = self._cache.get(key)
                if hit is not None:
                    self._cache.move_to_end(key)
                    return hit
            # The ETag comes from the key the bytes are stored under, not from a fresh
            # stamp(): a database rewritten mid-render would otherwise label this
            # image with an identity it was not drawn from.
            entry = (self._draw(request), _etag(key))
            self._store(key, entry)
            return entry
        finally:
            self._slot.release()

    def _draw(self, request: ArtRequest) -> bytes:
        with _read_only() as con:
            return art.render_bytes(
                request.area,
                request.style,
                fmt=request.fmt,
                opts=request.opts,
                query=request.query,
                con=con,
            )

    def _store(self, key: str, entry: tuple[bytes, str]) -> None:
        size = len(entry[0])
        if size > self._cache_bytes:
            return  # one render bigger than the whole cache; keeping it evicts everything
        with self._lock:
            # Replacing a key gives back what it held. Nothing reaches this today --
            # `render` re-checks the cache under the slot -- but a running total that
            # only ever grows would shrink the cache to nothing rather than fail.
            previous = self._cache.pop(key, None)
            if previous is not None:
                self._held -= len(previous[0])
            self._cache[key] = entry
            self._held += size
            while self._held > self._cache_bytes and len(self._cache) > 1:
                _, evicted = self._cache.popitem(last=False)
                self._held -= len(evicted[0])


def art_meta(enabled: bool) -> dict[str, Any]:
    """What the studio page needs to build its controls.

    Served rather than compiled into the page so that adding a style or a preset in
    `art.py` shows up in the UI without touching any HTML.
    """
    meta: dict[str, Any] = {
        "enabled": enabled,
        "styles": [
            {
                "name": name,
                "blurb": spec.blurb,
                "background": _hex(spec.background),
                "needs_groups": spec.needs_groups,
            }
            for name, spec in art.STYLES.items()
        ],
        "presets": {
            name: [b.min_lon, b.min_lat, b.max_lon, b.max_lat]
            for name, b in art.PRESETS.items()
        },
        # The closed vocabularies of the query spec, in the order art.py declares them
        # rather than sorted: `trips` first is the useful default to land on, and
        # alphabetical would open the weight menu on `busiest`.
        "query": {
            "weights": list(art.WEIGHTS),
            "groups": list(art.GROUPS),
            "orders": list(art.ORDERS),
        },
        "defaults": {
            "style": DEFAULT_STYLE,
            "width": DEFAULTS.width_px,
            "scale": DEFAULTS.scale,
            "hue": DEFAULTS.hue,
            "line_scale": DEFAULTS.line_scale,
            "alpha_scale": DEFAULTS.alpha_scale,
            "weight": art.DEFAULT_SPEC.weight,
            "group": art.DEFAULT_SPEC.group,
            "order": art.DEFAULT_SPEC.order,
            "min_trips": art.DEFAULT_SPEC.min_trips,
            "sample": art.DEFAULT_SPEC.sample,
            # What the page should ask for while a control is being dragged. Served
            # rather than hard-coded so the trade-off is tuned in one place.
            "preview_sample": PREVIEW_SAMPLE,
        },
        "limits": {
            # One cap under two names: it bounds `height` as much as `width`, and the
            # studio page reads `max_width`, so dropping that name would leave the
            # width control unbounded until the page is rewritten.
            "max_width": MAX_DIMENSION,
            "max_dimension": MAX_DIMENSION,
            "max_pixels": MAX_PIXELS,
            "max_scale": MAX_SCALE,
            "max_sample": MAX_SAMPLE,
            "formats": list(art.FORMATS),
            "max_groups": art.MAX_GROUPS,
            "max_filter_values": MAX_FILTER_VALUES,
            "max_min_trips": MAX_MIN_TRIPS,
        },
        # What a render owes, from the one definition `art` also stamps into every
        # file it writes. Served rather than written into the page because it follows
        # the region this server's database holds, not the page's markup.
        "credit": licences.html(config.credit_parts()),
        "database": {"present": config.DB_PATH.exists()},
    }
    if meta["database"]["present"]:
        # Best effort: a pipeline stage may hold the write lock, and a viewer that
        # cannot report the feed version is still a working viewer.
        try:
            meta["database"].update(_database_meta())
        except Exception as exc:
            meta["database"]["error"] = str(exc)
    return meta


# What the last read of the database said about itself, against the file identity it
# was read from. `/art/meta` is a page load, and the operator facet is a DISTINCT over
# `edge_services` -- 10.25M rows with nothing to prune -- so a studio page reloading
# every few minutes paid for a full scan each time. Invalidated the way the render
# cache is, on size and mtime, so a rebuilt database is never described by the
# previous one's dropdowns. Keyed on the path as well, since a test or a second
# region can move `config.DB_PATH` under a live process.
_meta_lock = threading.Lock()
_meta_cache: tuple[tuple[str, str], dict[str, Any]] | None = None


def _database_meta() -> dict[str, Any]:
    """The feed version, the edge count and the facet lists, in one read.

    A hit opens nothing, which is the point: the connection is the cheap half and
    the lock it competes for is not.
    """
    global _meta_cache  # noqa: PLW0603 - a process-wide cache, guarded by `_meta_lock`
    key = (str(config.DB_PATH), _stamp())
    with _meta_lock:
        if _meta_cache is not None and _meta_cache[0] == key:
            return dict(_meta_cache[1])
    with _read_only() as con:
        details: dict[str, Any] = {
            # max() rather than a bare select, so a database with no feed_version
            # row still returns one row and reports null instead of raising.
            "feed_version": db.scalar(
                con, "SELECT max(value) FROM meta WHERE key = 'feed_version'"
            ),
            "edges": db.scalar(con, "SELECT count(*) FROM edges"),
            # What the filters can usefully be set to. A dropdown of the operators
            # this database actually holds beats a free-text box that answers a typo
            # with an empty picture -- and there is no other way for a caller to
            # learn that this region is `FIRST` and `STAGE` rather than the national
            # list.
            "operators": _facet(con, "SELECT DISTINCT agency_id FROM edge_services"),
            "road_classes": _facet(con, "SELECT DISTINCT road_class FROM edges"),
        }
    with _meta_lock:
        _meta_cache = (key, details)
    return dict(details)


def _facet(con: duckdb.DuckDBPyConnection, sql: str) -> list[str]:
    """Distinct values of one column, bounded and sorted.

    LIMIT inside the query rather than a slice afterwards, so a column with a million
    distinct values costs a bounded result set rather than a list this process mostly
    discards. Sorting happens here for the same reason: an ORDER BY would have the
    database sort every distinct value before the limit could discard any.
    """
    rows = con.execute(f"{sql} LIMIT {MAX_FACET_VALUES}").fetchall()
    return sorted(str(r[0]) for r in rows if r[0] is not None)


# --- The server -------------------------------------------------------------

# Keyed on the same (path, mtime_ns, size) identity `_file_etag` builds its
# validator from, so an edited file recompresses rather than being served stale
# under an ETag that has already moved.
_gzip_cache: OrderedDict[tuple[str, int, int], bytes] = OrderedDict()
_gzip_lock = threading.Lock()


def _gzipped(path: str) -> bytes | None:
    """A small static file's gzip, compressed at most once per revision.

    The lock covers the dictionary and nothing else. Holding it across
    `gzip.compress` would put every compressible request behind whichever one
    happened to miss -- the 15.8 ms above, charged to unrelated files, on a server
    whose whole point is answering hundreds of small requests at once. The cost of
    letting go is that two threads racing a cold key both compress and one result
    is overwritten: one duplicated compress per file per server lifetime, against
    a per-key lock's bookkeeping for a cache with about six live keys.

    `mtime=0` because gzip otherwise stamps the current time into bytes 4-7 of its
    header, so two compressions of one unchanged file a second apart come back
    different -- under an ETag that promises they do not. Nothing downstream
    compared those bytes, but the file now has to be identical across the cache
    boundary anyway, and a validator that is only nearly true is the kind of thing
    that gets discovered from a proxy rather than from a test.
    """
    try:
        st = Path(path).stat()
    except OSError:
        return None
    key = (path, st.st_mtime_ns, st.st_size)
    with _gzip_lock:
        hit = _gzip_cache.get(key)
        if hit is not None:
            _gzip_cache.move_to_end(key)
            return hit
    try:
        body = gzip.compress(Path(path).read_bytes(), 6, mtime=0)
    except OSError:
        return None
    with _gzip_lock:
        _gzip_cache[key] = body
        _gzip_cache.move_to_end(key)
        while len(_gzip_cache) > GZIP_CACHE_ENTRIES:
            _gzip_cache.popitem(last=False)
    return body


class ArtEndpoint:
    """The three routes that answer with JSON or with an image, not with a file.

    A collaborator rather than more handler methods, because the two halves of this
    server have nothing in common past the socket. The static half is validators,
    freshness and byte ranges over whatever is on disk; this half parses a request,
    queues behind the render slot, and reports its faults in a form a program can
    read. `/archives.json` sits here for that last reason alone: it is the same
    three headers and the same encode as the other two.
    """

    PATHS = ("/archives.json", "/art/meta", "/art")

    def __init__(self, handler: Handler) -> None:
        self.handler = handler

    def serve(self, url: ParseResult) -> None:
        if url.path == "/archives.json":
            self.json(archives(self.handler.out_dir), validated=True)
        elif url.path == "/art/meta":
            self.json(art_meta(self.handler.renderer is not None), validated=True)
        else:
            self.render(url.query)

    def render(self, query: str) -> None:
        handler, renderer = self.handler, self.handler.renderer
        if renderer is None:
            self.problem(501, "rendering is switched off on this server (--no-art)")
            return
        try:
            request = parse_art(query)
        except BadRequest as exc:
            self.problem(400, str(exc))
            return

        # A design under iteration is requested over and over with one value moved,
        # so the unchanged ones should cost a 304 rather than a redraw.
        etag = renderer.etag(request)
        if _matches(handler.headers.get("If-None-Match"), etag):
            handler.send_response(304)
            handler.send_header("ETag", etag)
            handler.end_headers()
            return

        t0 = time.monotonic()
        try:
            body, etag = renderer.render(request)
        except Unavailable as exc:
            self.problem(503, str(exc), retry_after=10)
            return
        except ImportError as exc:
            # pycairo missing. Reported as 501 rather than 500 because the fix is to
            # the installation, not to the request, and the message says which.
            self.problem(501, str(exc))
            return
        except (KeyError, ValueError) as exc:
            # A window that resolve() accepted but a style rejected, and anything
            # else the renderer considers the caller's fault.
            self.problem(400, str(exc).strip("'\""))
            return
        except Exception:
            log.exception("render failed: %s", query)
            self.problem(500, "the render failed; see the server log")
            return

        # keep_blank_values, because `&download` on its own is how a link says it.
        download = "download" in parse_qs(query, keep_blank_values=True)
        disposition = "attachment" if download else "inline"
        handler.send_response(200)
        handler.send_header("Content-Type", CONTENT_TYPES[request.fmt])
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("ETag", etag)
        # no-cache, not no-store: the browser may keep it, but must revalidate, so a
        # rebuilt database is never served from a stale copy.
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header(
            "Content-Disposition", f'{disposition}; filename="{request.filename()}"'
        )
        if request.warning:
            handler.send_header("X-Wayfare-Warning", request.warning)
        elapsed = (time.monotonic() - t0) * 1e3
        handler.send_header("Server-Timing", f"render;dur={elapsed:.0f}")
        handler.end_headers()
        handler.wfile.write(body)

    def json(
        self,
        payload: object,
        status: int = 200,
        retry_after: int | None = None,
        *,
        validated: bool = False,
    ) -> None:
        """A JSON body, optionally under an ETag the client may revalidate against.

        `validated` is for the two answers that describe the server rather than
        report on a request. The viewer asks for `/archives.json` in `<head>`, on
        the critical path of every load, and the answer is the same bytes between
        publishes -- under `no-store` that round trip carried the whole body back
        every time. Under `no-cache` with a validator it is a 304 with none.

        A fault keeps `no-store`. A 503 is about the moment it was asked, and there
        is nothing there worth a client keeping.
        """
        handler = self.handler
        body = json.dumps(payload).encode()
        # A digest of the body, which is small and already in hand. The static
        # half's `_file_etag` is an mtime and a size for the opposite reason: an
        # archive is ~130 MB and hashing it would cost more than the transfer.
        etag = _etag(body.decode()) if validated else None
        if etag is not None and _matches(handler.headers.get("If-None-Match"), etag):
            handler.send_response(304)
            handler.send_header("ETag", etag)
            handler.send_header("Cache-Control", REVALIDATE)
            handler.end_headers()
            return
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        if etag is not None:
            handler.send_header("ETag", etag)
            handler.send_header("Cache-Control", REVALIDATE)
        else:
            handler.send_header("Cache-Control", "no-store")
        if retry_after is not None:
            handler.send_header("Retry-After", str(retry_after))
        handler.end_headers()
        handler.wfile.write(body)

    def problem(self, status: int, detail: str, retry_after: int | None = None) -> None:
        """An error as JSON, because every caller of /art is a program.

        SimpleHTTPRequestHandler's send_error writes an HTML page, which an <img> or
        a fetch() shows as a broken image with the reason nowhere the user can see
        it. The studio page displays this `detail` verbatim.
        """
        self.json({"error": detail, "status": status}, status, retry_after)


class Handler(http.server.SimpleHTTPRequestHandler):
    # Keep-alive. The default is HTTP/1.0, so every request cost a fresh TCP
    # connection and a fresh thread -- and MapLibre issues dozens of PMTiles range
    # requests per pan. On loopback that setup is 0.20 ms of a 0.56 ms request and
    # hardly matters; over the deployed tailnet path it is a full round trip each,
    # which is most of what a range request costs -- 21 ms of it, measured from a
    # laptop to the deployed instance. The relay the path takes is worth about 1.6 ms
    # of that (21.7 ms round trip relayed, 20.1 ms direct to the same host), so it is
    # the round trip itself that the connection reuse removes, not the relaying.
    #
    # Three things had to be true first, and all three are quiet under HTTP/1.0
    # because the connection dies after one response either way: an aborted body
    # must close the connection rather than leave a half-written one for the next
    # response to be parsed as the tail of (see `copyfile`), every response must
    # carry accurate framing since there is no read-until-EOF (the 416 below sends
    # `Content-Length: 0` for that reason), and a malformed Range must not raise
    # mid-connection (`bytes=-`, also below).
    protocol_version = "HTTP/1.1"

    # Keep-alive is what makes this necessary, so it belongs with the line above.
    # `BaseHTTPRequestHandler` flushes its headers and then writes the body as a
    # second, smaller write. Nagle holds that second write until the peer
    # acknowledges the first, and Linux delays that acknowledgement by 40 ms. Under
    # HTTP/1.0 the close after each response flushed it immediately, so the stall
    # could not appear; with the connection kept open it lands on every request
    # after the first, which is every range request of a pan but one.
    #
    # Measured in the container on emel, on loopback with no network in the path:
    # 41 ms a request against 0.3 ms with this set. Over the deployed tailnet path a
    # warm 16 KB range was 62 ms against a 21 ms round trip -- the round trip, then
    # the timer. Roughly three requests in four paid it, since a request that happens
    # to arrive after the peer's acknowledgement does not.
    disable_nagle_algorithm = True

    # Keep-alive holds a thread for the whole life of a connection, and
    # ThreadingTCPServer is thread-per-connection and unbounded, so an idle browser
    # tab would otherwise pin a thread indefinitely. The stdlib applies
    # this to the socket in `setup()` and turns the resulting timeout into a closed
    # connection in `handle_one_request`.
    #
    # Fifteen seconds: a pan's worth of range requests arrive milliseconds apart, so
    # anything above a second or two already collects the whole benefit, while the
    # value is also the ceiling on a single blocked write to a stalled client and
    # wants headroom over a slow mobile link. Reconnecting after an idle gap costs
    # the one round trip this change removed from the other dozens.
    timeout = 15.0

    out_dir: Path = Path("data/out")
    renderer: Renderer | None = None  # None disables /art
    max_age: int = ARCHIVE_MAX_AGE
    # Set per request by send_head and read by end_headers, which is the one hook
    # both the 200 and 206 paths pass through. Reset on every request: the
    # connection is kept alive, so a leftover value would tag the next response
    # with the previous file's validator.
    _pending_etag: str | None = None
    _pending_cc: str = REVALIDATE

    def translate_path(self, path: str) -> str:
        """Resolve the pipeline's own outputs out of the artefact directory.

        Anything else is served from the page directory as usual, so the viewer
        stays a plain static bundle.
        """
        name = path.split("?", 1)[0].split("#", 1)[0].lstrip("/")
        # A bare name only: no directory part, so a request cannot climb out of the
        # artefact directory with "../".
        if name.endswith(ARTEFACT_SUFFIXES) and "/" not in name:
            candidate = self.out_dir / name
            if candidate.exists():
                return str(candidate)
        return super().translate_path(path)

    def do_GET(self) -> None:
        self._pending_etag = None
        self._pending_cc = REVALIDATE
        url = urlparse(self.path)
        if url.path in ArtEndpoint.PATHS:
            ArtEndpoint(self).serve(url)
            return
        super().do_GET()

    def send_head(self):  # type: ignore[no-untyped-def]
        path = self._resolve(self.translate_path(self.path))
        gz = path is not None and self._gzip_wanted(path)
        self._pending_etag = self._file_etag(path, gz) if path else None
        self._pending_cc = self._cache_control(path) if path else REVALIDATE

        # A conditional hit costs one round trip and sends no body. This is what
        # makes coming back cheap: an archive is read as hundreds of separate
        # ranges, and a browser will only reuse cached *partial* content when it
        # has a strong validator to check it against. Last-Modified is not one,
        # so without this every visit re-fetched ranges it already held.
        if self._pending_etag and _matches(
            self.headers.get("If-None-Match"), self._pending_etag
        ):
            self.send_response(304)
            self.end_headers()
            return None

        if path is None:
            return super().send_head()
        if gz:
            compressed = self._send_compressed(path)
            if compressed is not None:
                return compressed
        if not self.headers.get("Range"):
            return super().send_head()
        return self._send_range(path)

    def _resolve(self, path: str) -> str | None:
        """The file a request actually maps to, or None to let the base class deal.

        `GET /` lands on a directory and the base class then picks index.html out
        of it. Repeating that here is what lets the page itself carry a validator:
        resolving only as far as the directory left every request for `/` looking
        unfileable, so the page went out with neither an ETag nor compression
        while the archive beside it got both.
        """
        if not Path(path).is_dir():
            return path
        if not urlparse(self.path).path.endswith("/"):
            return None  # base class issues the redirect to the trailing-slash form
        for index in ("index.html", "index.htm"):
            candidate = Path(path) / index
            if candidate.is_file():
                return str(candidate)
        return None  # no index: base class lists the directory

    def _file_etag(self, path: str, gzipped: bool = False) -> str | None:
        """A strong validator for a static file, from its mtime and size.

        Not a content hash, unlike the render ETags above: an archive is ~130 MB
        and hashing it on every range request would cost far more than the
        transfer it saves. mtime_ns and size together change whenever
        `wayfare publish` rewrites it, which is the only way its contents move.
        """
        try:
            st = Path(path).stat()
        except OSError:
            return None
        suffix = "-gzip" if gzipped else ""
        return f'"{st.st_mtime_ns:x}-{st.st_size:x}{suffix}"'

    def _cache_control(self, path: str) -> str:
        """How long this particular file may be reused without asking.

        Split by what republishes the file rather than by content type. An archive
        comes from the pipeline on a monthly cadence and is the expensive thing to
        re-fetch, so it gets a real freshness lifetime. The page comes from an image
        rebuild and is cheap, so it keeps revalidating -- caching it would mean
        shipping a fix that returning visitors could not see.

        A vendored library is the one file that is cheap to re-fetch and still worth
        caching outright, because the pages ask for it under a versioned URL: the
        request changes when the bytes do, so `immutable` is a promise that holds.
        Matched on the directory rather than on the suffix, so nothing outside
        `web/vendor/` can claim it by being named `.js`.
        """
        if path.endswith(ARTEFACT_SUFFIXES) and self.max_age > 0:
            return f"public, max-age={self.max_age}"
        if Path(path).parent.name == VENDOR_PREFIX:
            return f"public, max-age={VENDOR_MAX_AGE}, immutable"
        return REVALIDATE

    def _gzip_wanted(self, path: str) -> bool:
        if "gzip" not in self.headers.get("Accept-Encoding", ""):
            return False
        if self.headers.get("Range"):
            return False
        if self.guess_type(path).split(";")[0] not in COMPRESSIBLE:
            return False
        try:
            return 0 < Path(path).stat().st_size <= COMPRESS_MAX
        except OSError:
            return False

    def _send_compressed(self, path: str):  # type: ignore[no-untyped-def]
        """Serve a small text file gzipped, from memory.

        The viewer is ~24 KB of HTML fetched before anything else can start, and
        it compresses to about a third of that. `Vary` is not decoration here: the
        encoding is baked into the ETag above, so a shared cache must key on it or
        it will hand a gzipped body to a client that asked for identity.

        The compression itself is cached by revision; see `_gzipped`.
        """
        body = _gzipped(path)
        if body is None:
            return None
        self.send_response(200)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")
        self.end_headers()
        return io.BytesIO(body)

    def _send_range(self, path: str):  # type: ignore[no-untyped-def]
        header = self.headers.get("Range", "")
        try:
            # Left open: the caller closes it, as in the base class.
            f = Path(path).open("rb")  # noqa: SIM115
        except OSError:
            self.send_error(404)
            return None

        size = os.fstat(f.fileno()).st_size
        m = RANGE.fullmatch(header.strip())
        if not m:
            f.close()
            self.send_error(400, "malformed Range")
            return None

        first, last = m.group(1), m.group(2)
        # `bytes=-` matches the pattern -- both halves are `\d*` -- and names no
        # range at all, so it belongs with the other malformed spellings and gets a
        # 400. Left to the suffix branch below it raises ValueError out of `int("")`,
        # which costs a traceback and an aborted connection the client means to reuse.
        if not first and not last:
            f.close()
            self.send_error(400, "malformed Range")
            return None
        if first:
            start = int(first)
            end = int(last) if last else size - 1
        else:
            # A suffix range -- "the last N bytes". PMTiles uses this to find the
            # footer without knowing the file length up front.
            start = max(0, size - int(last))
            end = size - 1
        end = min(end, size - 1)

        if start >= size or start > end:
            f.close()
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            # Explicit, because there is no body and HTTP/1.1 has no
            # read-until-EOF framing to fall back on: without it a client on a
            # persistent connection waits for a body that is never coming.
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None

        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        f.seek(start)
        return _Slice(f, end - start + 1)

    def end_headers(self) -> None:
        """The one hook every response passes through, whatever wrote it.

        So the headers that belong on all of them are added here and nowhere else:
        a response that also added its own would send them twice, and the only way
        to notice would be to read back the buffer the stdlib is still building.
        """
        self._cors()
        if self._pending_etag:
            self.send_header("ETag", self._pending_etag)
            self.send_header("Cache-Control", self._pending_cc)
        super().end_headers()

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Expose-Headers",
            "Content-Range, Accept-Ranges, Server-Timing, X-Wayfare-Warning",
        )

    def copyfile(self, source: Any, outputfile: Any) -> None:
        """Panning the map cancels every tile still in flight, so a client hanging
        up mid-body is ordinary traffic here rather than a fault. The base class
        lets the write raise, and socketserver prints a full traceback per abort --
        which buries anything that actually matters.

        Swallowing it is only safe once the connection goes with it. A body that
        stopped short of its Content-Length has desynchronised the stream, so
        under keep-alive the next response would be read as the tail of this one
        -- a truncated tile becoming a corrupt one, on a connection that looks
        healthy. Under HTTP/1.0 the connection closed anyway and this did not
        arise.

        TimeoutError is here for the same reason and arrives from the same place:
        `Handler.timeout` puts a deadline on socket writes as well as reads, so a
        client that stops reading mid-body now surfaces as a timeout rather than
        as a block forever.
        """
        try:
            super().copyfile(source, outputfile)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # one line per tile is unreadable


class _Slice:
    """A file object that stops after n bytes, so copyfile sends only the range."""

    def __init__(self, f: Any, n: int) -> None:
        self.f = f
        self.left = n

    def read(self, size: int = -1) -> bytes:
        if self.left <= 0:
            return b""
        if size < 0 or size > self.left:
            size = self.left
        data: bytes = self.f.read(size)
        self.left -= len(data)
        return data

    def close(self) -> None:
        self.f.close()


def serve(
    *,
    port: int = 8099,
    web_dir: Path = Path("web"),
    out_dir: Path = Path("data/out"),
    art_enabled: bool = True,
    host: str = "",
    max_age: int | None = None,
) -> None:
    """Serve `web_dir`, the archives in `out_dir`, and /art, until interrupted."""
    Handler.out_dir = out_dir.resolve()
    Handler.renderer = Renderer() if art_enabled else None
    Handler.max_age = ARCHIVE_MAX_AGE if max_age is None else max_age

    found = archives(Handler.out_dir)
    for name in found:
        size = (Handler.out_dir / name).stat().st_size / 1e6
        log.info("tiles: %s (%.1f MB)", name, size)
    if not found:
        log.warning("no .pmtiles in %s -- run `wayfare publish` first", out_dir)
    if art_enabled:
        log.info("renders: /art, one at a time, up to %.0f megapixels", MAX_PIXELS / 1e6)

    handler = functools.partial(Handler, directory=str(web_dir.resolve()))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer((host, port), handler) as httpd:
        # ThreadingMixIn joins every live thread on close by default, which was
        # instant while each thread served one request. With keep-alive a thread
        # lives as long as its connection, so ctrl-c would sit for up to
        # `Handler.timeout` per idle browser tab before the process exited. There
        # is nothing to checkpoint in a server, so cutting them is the right end.
        httpd.daemon_threads = True
        log.info("serving %s at http://localhost:%d/  (ctrl-c to stop)", web_dir, port)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            # Caught here rather than in cli.main, whose message is about a pipeline
            # stage checkpointing -- there is nothing to resume about a server.
            log.info("stopped")
