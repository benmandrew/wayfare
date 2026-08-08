"""HTTP server: the viewer, the tile archives, and renders on demand.

Two jobs that belong on one port.

The first is static. PMTiles works by reading byte ranges out of one large file, so
the server has to answer with 206 Partial Content. Python's own ``http.server`` does
not implement Range at all -- it replies 200 with the whole file, which makes the
viewer fetch all 24 MB for every tile it wants. That looks like "slow" rather than
"broken", which is the annoying way to discover it.

The second is ``/art``. The expensive half of this project -- acquire, match,
aggregate -- happens on a server, and until now ``wayfare art`` could only draw
against a database on the same machine. Iterating on a design therefore meant
copying tens of gigabytes to a laptop, or editing a style, rebuilding an image and
watching a log. Rendering where the data already is turns that into a query string:
the endpoint takes a window, a style and the style's knobs, and answers with a PNG.

Renders are serialised and bounded. One at a time, because a render is CPU-bound
cairo over a full scan of ``edges`` and the same box is usually also matching --
running two would not finish either sooner. Bounded, because pixel count is the
one parameter a caller can raise without limit.

    wayfare serve [--port 8099] [--dir web] [--out /data/out] [--no-art]
"""

from __future__ import annotations

import functools
import hashlib
import http.server
import json
import os
import re
import socketserver
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import art, config, db, logs

log = logs.get("server")

RANGE = re.compile(r"bytes=(\d*)-(\d*)")

# Emitted by `wayfare publish` into the output directory, not into web/. Any
# archive there is servable, not a fixed pair of names, so a machine holding
# several regions can offer all of them -- `wales.pmtiles` beside
# `london.pmtiles` -- and the viewer picks between them with ?tiles=.
ARTEFACT_SUFFIXES = (".pmtiles",)

# Render limits. Width alone is not the thing to cap: the window's aspect ratio
# decides the height, and `scale` multiplies both, so a modest-looking
# `width=4000&scale=4` over a tall window is 200 megapixels. The pixel budget is
# what actually bounds the work; the width cap is there to give a clearer error for
# the common typo of an extra zero.
MAX_WIDTH = 12_000
MAX_PIXELS = 64_000_000
MAX_SCALE = 4.0
MAX_CAPTION = 120

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


class BadRequest(ValueError):
    """A parameter the caller can fix, reported as 400 with the reason."""


def archives(out_dir: Path) -> list[str]:
    return sorted(p.name for p in out_dir.glob("*.pmtiles")) if out_dir.is_dir() else []


# --- The render request -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArtRequest:
    """A parsed, validated and bounded ``/art`` query."""

    area: str  # a preset name or a raw window, kept as given for the cache key
    style: str
    fmt: str
    opts: art.RenderOpts
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
                o.width_px,
                o.height_px,
                o.scale,
                o.hue,
                o.line_scale,
                o.alpha_scale,
                o.caption,
                o.background,
            )
        )

    def filename(self) -> str:
        stem = self.area if self.area in art.PRESETS else "window"
        return f"{stem}-{self.style}{self.fmt}"


def _one(q: dict[str, list[str]], name: str) -> str | None:
    values = q.get(name)
    return values[-1].strip() if values and values[-1].strip() else None


def _number(
    q: dict[str, list[str]], name: str, lo: float, hi: float, default: float
) -> float:
    """A range-checked number, or `default` when the parameter is absent.

    A default rather than None because several of these are legitimately zero --
    `hue=0` is red -- and falling back on a falsy value would silently ignore it.
    """
    raw = _one(q, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise BadRequest(f"{name}={raw!r} is not a number") from None
    if not lo <= value <= hi:
        raise BadRequest(f"{name}={value:g} is out of range; it runs {lo:g} to {hi:g}")
    return value


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

    style = _one(q, "style") or "density"
    if style not in art.STYLES:
        raise BadRequest(
            f"unknown style {style!r}; known styles: {', '.join(sorted(art.STYLES))}"
        )

    fmt = "." + (_one(q, "format") or "png").lstrip(".").lower()
    if fmt not in art.FORMATS:
        raise BadRequest(f"unknown format {fmt!r}; use png or svg")

    width = int(_number(q, "width", 64, MAX_WIDTH, DEFAULTS.width_px))
    height = int(_number(q, "height", 64, MAX_WIDTH, 0)) if _one(q, "height") else None
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
        warning=warning,
        opts=art.RenderOpts(
            width_px=width,
            height_px=height,
            scale=scale,
            caption=caption,
            background=_colour(background) if background else None,
            hue=_number(q, "hue", 0.0, 1.0, DEFAULTS.hue),
            line_scale=_number(q, "line_scale", 0.05, 8.0, DEFAULTS.line_scale),
            alpha_scale=_number(q, "alpha_scale", 0.05, 8.0, DEFAULTS.alpha_scale),
        ),
    )


# --- Rendering --------------------------------------------------------------


class Unavailable(RuntimeError):
    """Nothing the caller did wrong; try again. Reported as 503."""


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
        """Identity of the current database file, for cache keys and ETags.

        Size and mtime rather than a content hash: the file is up to tens of
        gigabytes, and every stage that changes it rewrites it in place.
        """
        try:
            st = config.DB_PATH.stat()
        except OSError:
            return "absent"
        return f"{st.st_size}-{st.st_mtime_ns}"

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
        if not config.DB_PATH.exists():
            raise Unavailable(
                f"no database at {config.DB_PATH}; the pipeline has not run here"
            )
        # Opened per request and closed again, deliberately. DuckDB gives a writer an
        # exclusive lock on the file, so a read-only handle held open by this server
        # would stop the next `match` or `aggregate` run from starting -- a viewer
        # nobody is using would quietly block the pipeline.
        try:
            con = db.connect(read_only=True)
        except Exception as exc:  # duckdb raises several types for a held lock
            raise Unavailable(
                f"cannot read {config.DB_PATH}: {exc}. A pipeline stage is probably "
                "writing to it; renders work again once it finishes."
            ) from None
        try:
            return art.render_bytes(
                request.area, request.style, fmt=request.fmt, opts=request.opts, con=con
            )
        finally:
            con.close()

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
                "needs_services": spec.needs_services,
            }
            for name, spec in art.STYLES.items()
        ],
        "presets": {
            name: [b.min_lon, b.min_lat, b.max_lon, b.max_lat]
            for name, b in art.PRESETS.items()
        },
        "defaults": {
            "style": "density",
            "width": DEFAULTS.width_px,
            "scale": DEFAULTS.scale,
            "hue": DEFAULTS.hue,
            "line_scale": DEFAULTS.line_scale,
            "alpha_scale": DEFAULTS.alpha_scale,
        },
        "limits": {
            "max_width": MAX_WIDTH,
            "max_pixels": MAX_PIXELS,
            "max_scale": MAX_SCALE,
            "formats": list(art.FORMATS),
        },
        "database": {"present": config.DB_PATH.exists()},
    }
    if meta["database"]["present"]:
        # Best effort: a pipeline stage may hold the write lock, and a viewer that
        # cannot report the feed version is still a working viewer.
        try:
            con = db.connect(read_only=True)
            try:
                # max() rather than a bare select, so a database with no feed_version
                # row still returns one row and reports null instead of raising.
                meta["database"]["feed_version"] = db.scalar(
                    con, "SELECT max(value) FROM meta WHERE key = 'feed_version'"
                )
                meta["database"]["edges"] = db.scalar(con, "SELECT count(*) FROM edges")
            finally:
                con.close()
        except Exception as exc:
            meta["database"]["error"] = str(exc)
    return meta


# --- The server -------------------------------------------------------------


class Handler(http.server.SimpleHTTPRequestHandler):
    out_dir: Path = Path("data/out")
    renderer: Renderer | None = None  # None disables /art

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
        url = urlparse(self.path)
        if url.path == "/archives.json":
            self._json(archives(self.out_dir))
            return
        if url.path == "/art/meta":
            self._json(art_meta(self.renderer is not None))
            return
        if url.path == "/art":
            self._art(url.query)
            return
        super().do_GET()

    def _art(self, query: str) -> None:
        if self.renderer is None:
            self._problem(501, "rendering is switched off on this server (--no-art)")
            return
        try:
            request = parse_art(query)
        except BadRequest as exc:
            self._problem(400, str(exc))
            return

        # A design under iteration is requested over and over with one value moved,
        # so the unchanged ones should cost a 304 rather than a redraw.
        etag = self.renderer.etag(request)
        if _matches(self.headers.get("If-None-Match"), etag):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return

        t0 = time.monotonic()
        try:
            body, etag = self.renderer.render(request)
        except Unavailable as exc:
            self._problem(503, str(exc), retry_after=10)
            return
        except ImportError as exc:
            # pycairo missing. Reported as 501 rather than 500 because the fix is to
            # the installation, not to the request, and the message says which.
            self._problem(501, str(exc))
            return
        except (KeyError, ValueError) as exc:
            # A window that resolve() accepted but a style rejected, and anything
            # else the renderer considers the caller's fault.
            self._problem(400, str(exc).strip("'\""))
            return
        except Exception:
            log.exception("render failed: %s", query)
            self._problem(500, "the render failed; see the server log")
            return

        # keep_blank_values, because `&download` on its own is how a link says it.
        download = "download" in parse_qs(query, keep_blank_values=True)
        disposition = "attachment" if download else "inline"
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES[request.fmt])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        # no-cache, not no-store: the browser may keep it, but must revalidate, so a
        # rebuilt database is never served from a stale copy.
        self.send_header("Cache-Control", "no-cache")
        self.send_header(
            "Content-Disposition", f'{disposition}; filename="{request.filename()}"'
        )
        if request.warning:
            self.send_header("X-Wayfare-Warning", request.warning)
        self.send_header("Server-Timing", f"render;dur={(time.monotonic() - t0) * 1e3:.0f}")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _problem(self, status: int, detail: str, retry_after: int | None = None) -> None:
        """An error as JSON, because every caller of /art is a program.

        SimpleHTTPRequestHandler's send_error writes an HTML page, which an <img> or
        a fetch() shows as a broken image with the reason nowhere the user can see
        it. The studio page displays this `detail` verbatim.
        """
        body = json.dumps({"error": detail, "status": status}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        self.wfile.write(body)

    def send_head(self):  # type: ignore[no-untyped-def]
        header = self.headers.get("Range")
        if not header:
            return super().send_head()

        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        try:
            f = open(path, "rb")  # noqa: SIM115 - closed by the caller, as in the base class
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
            self.end_headers()
            return None

        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self._cors()
        self.end_headers()
        f.seek(start)
        return _Slice(f, end - start + 1)

    def end_headers(self) -> None:
        if self.command == "OPTIONS" or "Content-Range" not in self._headers_buffer_str():
            self._cors()
        super().end_headers()

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Expose-Headers",
            "Content-Range, Accept-Ranges, Server-Timing, X-Wayfare-Warning",
        )

    def _headers_buffer_str(self) -> str:
        return b"".join(getattr(self, "_headers_buffer", [])).decode("latin-1")

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
) -> None:
    """Serve `web_dir`, the archives in `out_dir`, and /art, until interrupted."""
    Handler.out_dir = out_dir.resolve()
    Handler.renderer = Renderer() if art_enabled else None

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
        log.info("serving %s at http://localhost:%d/  (ctrl-c to stop)", web_dir, port)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            # Caught here rather than in cli.main, whose message is about a pipeline
            # stage checkpointing -- there is nothing to resume about a server.
            log.info("stopped")
