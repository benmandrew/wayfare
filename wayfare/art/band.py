"""Drawing one picture in several processes.

The canvas splits into horizontal bands, one process each, and the bands are pasted
back together byte for byte. Everything here exists to keep that identity: where the
cuts fall, how far past its own rows a band draws, and which of the two scales a
band must be handed rather than derive.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import db, logs
from .canvas import _canvas
from .geometry import Bounds, Projection
from .query import CHAIN_VIEW, DEFAULT_SOURCE, EDGES, QuerySpec, Source, _Sql
from .stream import Weights, Window
from .styles import STYLES, RenderOpts, Style

if TYPE_CHECKING:  # pragma: no cover - typing only
    import duckdb

log = logs.get("art")


# Below this many edges in the window, a render is drawn on one core. Banding starts
# eight interpreters -- `spawn`, not `fork`, because the parent holds a DuckDB handle
# -- and that costs about a second whatever the picture. Cardiff at 1,200px is 56,000
# edges and 0.75s serial, so banding it made it twice as slow; London at 200,000 goes
# 8.2s to 4.2s. The floor sits between them, nearer the small end because the loss
# below it is bounded by the start-up cost and the win above it is not.
MIN_BAND_EDGES = 150_000


#
# A render is CPU-bound cairo on one core, and the box it runs on has eight. The
# canvas splits into horizontal bands, one process each, and the bands are pasted
# back together. Nothing about the picture changes: the bands are disjoint, each is
# clipped to its own rows, so no pixel is painted twice and no compositing operator
# has to be commutative across a cut. Measured byte-identical to the serial render
# on all three styles over the `uk` window.
#
# Two things have to be global rather than per band, and both are scales rather than
# geometry: `Weights` and, for a grouped style, the ribbon weights. A band that took
# its contrast from its own edges would be brighter over the Highlands than over the
# Midlands, and the join would be visible as a step.


def default_workers(workers: int | None = None) -> int:
    """How many bands to draw at once, when a caller has not said.

    Every *physical* core, because the thing being parallelised is the only thing
    running: the render server takes one render at a time by design, and a
    command-line render is what the operator is sitting waiting for.
    `WAYFARE_RENDER_WORKERS` overrides, for a box where that is not true.

    Physical rather than logical, which is measured rather than assumed. On the
    four-core, eight-thread Xeon that serves this, `uk` `density` at 2,000px takes
    26.9s on four workers, 27.2s on six and 28.1s on eight: the second thread of a
    core buys nothing, because tessellating round caps is ALU- and branch-bound and
    there are no memory stalls for it to fill. Eight processes also carry eight
    interpreters and eight DuckDB connections against the render container's memory
    limit, which is the part that actually bites.
    """
    if workers is not None:
        return max(1, workers)
    env = os.environ.get("WAYFARE_RENDER_WORKERS")
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            log.warning("ignoring WAYFARE_RENDER_WORKERS=%r, which is not a number", env)
    logical = os.cpu_count() or 1
    return max(1, min(_physical_cpus() or logical, _cgroup_cpus() or 1_000))


def _physical_cpus() -> int | None:
    """Cores rather than hardware threads, or None if that cannot be established.

    Linux only, and deliberately: it reads the distinct `core_id`/`physical_id`
    pairs out of `/proc/cpuinfo`, which is where the render actually runs. Anywhere
    else there is no reliable physical count, so this returns None and the logical
    count stands -- over-counting costs a few percent, and guessing wrong in the
    other direction would leave half the box idle.
    """
    try:
        text = Path("/proc/cpuinfo").read_text()
    except OSError:
        return None
    cores: set[tuple[str, str]] = set()
    physical = core = None
    for line in text.splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key == "physical id":
            physical = value
        elif key == "core id":
            core = value
        elif not line.strip() and physical is not None and core is not None:
            cores.add((physical, core))
            physical = core = None
    if physical is not None and core is not None:
        cores.add((physical, core))
    return len(cores) or None


def _cgroup_cpus() -> int | None:
    """The container's CPU quota, whole cores, or None outside a limited cgroup.

    `os.cpu_count()` reports the host's cores from inside a container, so the render
    service -- which runs at `cpus: 4` on an eight-core box -- would otherwise start
    eight band processes to share four cores' worth of quota and four gigabytes of
    memory limit. Overcommitting CPU only wastes context switches; overcommitting the
    memory limit gets the container killed.
    """
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
    except (OSError, ValueError):
        return None
    if quota == "max":
        return None
    try:
        return max(1, int(float(quota) / float(period)))
    except (ValueError, ZeroDivisionError):
        return None


def _lat_at(proj: Projection, y_px: float) -> float:
    """Inverse of :meth:`Projection.__call__`'s y, in degrees. Bands cut in pixels."""
    my = proj.y1 - (y_px - proj.oy) / proj.k
    return math.degrees(2.0 * math.atan(math.exp(my)) - math.pi / 2.0)


def _band_window(
    bounds: Bounds, proj: Projection, y0: float, y1: float, pad: float
) -> Bounds:
    """The window a band has to query: its own rows, plus a collar, clamped.

    The collar is what stops a seam. An edge whose geometry sits just outside the
    band still strokes into it, by up to half a line width, so the query has to
    reach that far past the cut.

    Clamped back to `bounds` because the collar must not *add* edges the serial
    render would not have drawn. Serial selects every edge whose bbox overlaps the
    window; an unclamped collar on the outermost band would select edges beyond it,
    and for a grouped style those bring groups the global weight map has never seen.
    Clamping costs nothing, because anything outside the window is clipped away.
    """
    north = min(_lat_at(proj, y0 - pad), bounds.max_lat)
    south = max(_lat_at(proj, y1 + pad), bounds.min_lat)
    # A band can be thinner than the clamp when the window is tiny; keep it legal.
    if north <= south:
        north = min(bounds.max_lat, south + 1e-9)
    return Bounds(bounds.min_lon, south, bounds.max_lon, north)


def band_cuts(
    con: duckdb.DuckDBPyConnection,
    sql: _Sql,
    bounds: Bounds,
    proj: Projection,
    height: float,
    n: int,
) -> tuple[int, list[float]]:
    """How many edges the window holds, and band boundaries splitting them evenly.

    The count comes back with the cuts because it is the same scan and it is what
    decides whether to band at all -- see MIN_BAND_EDGES.

    Equal-height bands do not work here and it is not a close call. Great Britain's
    buses are not spread evenly in latitude: cutting the `uk` window into eight equal
    strips put 1,307,069 of 2,746,261 edges into one of them, so seven cores finished
    in seconds and the render waited on the eighth for 35. Balancing on the edge
    distribution instead took the same render from 37s to 27s.

    Latitude quantiles rather than a count per band, because the cost is per edge and
    the quantiles are one cheap aggregate over a column the zonemaps already prune.
    The cuts land on whole device rows so the paste is a memcpy of exact rows.
    """
    where, params = sql.where(sampled=True)
    row = con.execute(
        "SELECT count(*), quantile_cont((min_lat_e6 + max_lat_e6) / 2.0, ?) "
        f"FROM {EDGES} WHERE {where}",
        [[i / n for i in range(1, n)], *params],
    ).fetchone()
    n_edges = int(row[0]) if row else 0
    cuts = [0.0]
    # North to south, because y grows downward and the bands are listed top first.
    for lat_e6 in sorted(row[1] if row and row[1] else [], reverse=True):
        y = float(round(proj(bounds.min_lon, lat_e6 / 1e6)[1]))
        if cuts[-1] < y < height:
            cuts.append(y)
    cuts.append(float(height))
    return n_edges, cuts


@dataclass(frozen=True, slots=True)
class _BandJob:
    """Everything a worker needs, and nothing it cannot pickle.

    Notably not a connection: DuckDB handles do not cross a process boundary, so a
    band opens the file read-only itself and closes it when it is done. That keeps
    the rule the render server depends on -- no handle outlives a render -- rather
    than working around it.
    """

    db_path: str
    bounds: tuple[float, float, float, float]
    width: int
    height: int
    dev_y0: int
    dev_y1: int
    draw_scale: float
    style: str
    opts: RenderOpts
    query: QuerySpec
    source: Source
    weights: Weights
    group_stats: list[tuple[str, int, float]] | None
    # The parent's chain assignment, as an Arrow table, or None when not coalescing.
    # About 20 bytes an edge and picklable, which is what lets it cross to a worker;
    # see `Source.chains` for why a band must not work one out for itself.
    chains: Any | None


def band_source(con: duckdb.DuckDBPyConnection) -> Path | None:
    """The file a band process should reopen, or None if it cannot.

    Asked of the connection rather than assumed from the config, because they are not
    always the same file: a caller can hand `render` a connection to any database, and
    a band that opened `config.DB_PATH` instead would draw a different picture from the
    one it was asked for -- quietly, and only in the parallel path.

    None means do not band. That covers an in-memory database, which a worker has no
    way to reach, and a file this process cannot open a second time read-only, which
    is what a *writable* handle looks like: DuckDB gives a writer an exclusive lock.
    The probe is an open and a close, so it tests the thing that has to work rather
    than reasoning about it.
    """
    try:
        row = con.execute(
            "SELECT path FROM duckdb_databases() WHERE NOT internal ORDER BY database_oid"
        ).fetchone()
    except Exception:  # an older DuckDB without the view; not worth a version check
        return None
    if not row or not row[0]:
        return None
    path = Path(row[0])
    try:
        db.connect(path, read_only=True).close()
    except Exception as exc:
        log.debug("not banding: %s cannot be reopened read-only (%s)", path, exc)
        return None
    return path


def _stats_table(rows: list[tuple[str, int, float]]) -> Any:
    """The parent's group statistics as an Arrow table, ready to `register`."""
    import pyarrow

    return pyarrow.table(
        {
            "grp": pyarrow.array([r[0] for r in rows], pyarrow.string()),
            "n_edges": pyarrow.array([r[1] for r in rows], pyarrow.int64()),
            "trips": pyarrow.array([r[2] for r in rows], pyarrow.float64()),
        }
    )


def _band_pad(sty: Style, opts: RenderOpts, width_px: float) -> float:
    """How far outside its own rows a band must draw and query, in logical pixels.

    Half the widest stroke, because a stroke is centred on its path, plus two pixels
    of slack for the round caps and joins cairo adds past a vertex. The slack is
    absolute, so it is proportionally thinner the wider the strokes get; it is a
    margin on the arithmetic, not the arithmetic.
    """
    return sty.max_stroke_px(width_px, opts.line_scale) / 2.0 + 2.0


def _draw_band(job: _BandJob) -> tuple[int, int, int, bytes]:
    """One band, drawn into its own surface and handed back as raw ARGB rows."""
    import cairo

    bounds = Bounds(*job.bounds)
    proj = Projection.fit(bounds, job.width, job.height)
    sty = STYLES[job.style]
    s = job.draw_scale

    # The surface is the band plus a margin, and the margin is thrown away. That is
    # what makes a band byte-identical rather than merely indistinguishable: clipping
    # to the band would cut a stroke in half at the boundary, and cairo tessellates in
    # 24.8 fixed point, so the two halves' coverage does not always re-add to what the
    # whole shape rasterised to. It showed up as one row of one Cardiff render off by
    # 1/255. Drawing past the cut and pasting only the middle means no shape is ever
    # split, and the clip that remains is exactly the serial path's.
    #
    # `max_stroke_px` takes the canvas width because a style may quote its widths
    # against a reference canvas rather than in absolute pixels; a collar read as
    # absolute pixels under a style that scales with `width_px` is too narrow above
    # that style's reference canvas, which is a seam, and merely wasteful below it.
    pad = _band_pad(sty, job.opts, job.width)
    dev_pad = math.ceil(pad * s)
    surface = cairo.ImageSurface(
        cairo.FORMAT_ARGB32,
        max(1, round(job.width * s)),
        (job.dev_y1 - job.dev_y0) + 2 * dev_pad,
    )
    ctx = _canvas(surface, s, sty, job.opts, dev_origin=job.dev_y0 - dev_pad)

    ctx.save()
    # Clip to the window and nothing else -- the same rectangle `_render` uses, so an
    # edge in the collar is kept out of the letterbox here too. The band's own extent
    # is not a clip; it is which rows get returned.
    ctx.rectangle(*proj.content_rect(bounds))
    ctx.clip()
    top, bottom = job.dev_y0 / s, job.dev_y1 / s
    con = db.connect(Path(job.db_path), read_only=True)
    try:
        # One thread each. DuckDB defaults to a thread per core *per process*, so
        # eight bands would put sixty-four of them on eight cores; the scan was
        # never the bottleneck here and the contention is real.
        con.execute("SET threads=1")
        source = job.source
        if job.group_stats is not None:
            # Registered rather than inserted: at most MAX_GROUPS rows, and
            # DuckDB takes about 2,700 a second through bound parameters, so
            # 20,000 services would cost seven seconds a band. `register` hands
            # it an Arrow table and costs nothing. The name is ours, so
            # `Source.groups` still only ever holds an identifier this code chose.
            con.register("wf_gstat", _stats_table(job.group_stats))
            source = replace(source, groups="wf_gstat")
        if job.chains is not None:
            con.register(CHAIN_VIEW, job.chains)
            source = replace(source, chains=CHAIN_VIEW)
        window = Window(
            _band_window(bounds, proj, top, bottom, pad),
            con,
            with_groups=sty.needs_groups,
            spec=job.query,
            source=source,
        )
        # The window's scale, injected rather than recomputed. See the section
        # header: a band that scales itself draws a different picture.
        window._weights = job.weights
        sty.draw(ctx, window, proj, job.opts)
    finally:
        con.close()
    ctx.restore()

    surface.flush()
    stride = surface.get_stride()
    rows = job.dev_y1 - job.dev_y0
    data = bytes(surface.get_data())
    return job.dev_y0, rows, stride, data[dev_pad * stride : (dev_pad + rows) * stride]


def _draw_banded(
    surface: Any,
    bounds: Bounds,
    proj: Projection,
    style: str,
    opts: RenderOpts,
    draw_scale: float,
    con: duckdb.DuckDBPyConnection,
    query: QuerySpec,
    workers: int,
) -> bool:
    """Fill `surface` from `workers` processes, one band each; False if it declined.

    Declining rather than raising, because `workers` is a request for speed and a
    small window is simply faster without it. The caller then draws serially, which
    is the only other thing it could sensibly do.

    One band per worker rather than several, because the per-band cost has a floor
    that does not shrink as bands do: `edge_services` carries no bbox column and
    DuckDB pushes no min/max filter through the join, so every band scans all of it
    whatever its height. Twenty-four balanced bands measured *slower* than eight
    (36.7s against 27.0s) for exactly that reason. Balancing the cuts is what buys
    the parallelism; multiplying them only buys more scans.
    """
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    db_path = band_source(con)
    if db_path is None:
        return False
    sty = STYLES[style]
    window = Window(bounds, con, with_groups=sty.needs_groups, spec=query)
    n_edges, cuts = band_cuts(
        con, window.sql, bounds, proj, surface.get_height() / draw_scale, workers
    )
    if n_edges < MIN_BAND_EDGES or len(cuts) < 3:
        return False
    # Worked out once over the whole window, exactly like the group statistics below,
    # and for the reason `Source.chains` records. Doing it per band is both four
    # times the work and the wrong answer.
    chains = window.chain_table() if opts.coalesce and sty.coalesces else None
    jobs = [
        _BandJob(
            db_path=str(db_path),
            bounds=(bounds.min_lon, bounds.min_lat, bounds.max_lon, bounds.max_lat),
            width=opts.width_px,
            height=proj.height,
            dev_y0=round(cuts[i] * draw_scale),
            dev_y1=round(cuts[i + 1] * draw_scale),
            draw_scale=draw_scale,
            style=style,
            opts=opts,
            query=query,
            source=DEFAULT_SOURCE,
            # Resolved here, on the parent's connection, precisely once.
            weights=window.weights,
            group_stats=window.group_stats() if sty.needs_groups else None,
            chains=chains,
        )
        for i in range(len(cuts) - 1)
    ]

    surface.flush()
    dst = surface.get_data()
    dst_stride = surface.get_stride()
    # Spawn, not the Linux default of fork. The parent is holding an open DuckDB
    # connection at this point -- it just read the weights off it -- and DuckDB runs
    # background threads, which a fork does not carry across. The child inherits the
    # connection's state without the threads that maintain it and dies on first use;
    # it presents as BrokenProcessPool with no traceback, because the child is killed
    # rather than raising. Spawn costs an interpreter start and a re-import per band,
    # which against a render measured in tens of seconds is not worth avoiding.
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
        for dev_y0, rows, stride, data in pool.map(_draw_band, jobs):
            for row in range(rows):
                off = (dev_y0 + row) * dst_stride
                dst[off : off + dst_stride] = data[row * stride : row * stride + dst_stride]
    surface.mark_dirty()
    return True
