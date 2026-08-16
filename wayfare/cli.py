"""Command line entry point.

The pipeline is a sequence of stages, each of which reads what the last one wrote
and can be re-run on its own. `wayfare all` chains them; the individual
subcommands exist because on a run this long you frequently want to redo exactly
one of them.
"""

# argparse prints the docstring above as the program description, so what a
# maintainer needs is here instead. Three parts, kept apart on purpose: one
# `_add_*_parser` per subcommand declares what it takes, one `_cmd_*` runs it, and
# `_SUBCOMMANDS` is the only thing that knows both names. A stage's failure is a
# RuntimeError it raises and `main` reports -- no subcommand logs and returns 1 for
# itself, because seven copies of that block is how they drifted apart the first
# time.

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from . import (
    acquire,
    aggregate,
    config,
    coverage,
    db,
    gtfs,
    logs,
    maintenance,
    match,
    osmroutes,
    publish,
    railtrips,
    snap,
    trace,
)

log = logs.get("cli")

# What `add_subparsers` hands back. Private in argparse and typed nowhere public,
# so the name is spelled once here rather than in fifteen signatures.
type _Sub = argparse._SubParsersAction[argparse.ArgumentParser]


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logs.setup(args.log)

    if args.data:
        config.retarget(args.data)
    config.ensure_dirs()

    try:
        return _SUBCOMMANDS[args.cmd][1](args)
    except KeyboardInterrupt:
        # Every stage checkpoints, so an interrupt is a normal way to stop.
        log.info("interrupted; progress is saved -- re-run the same command to resume")
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        # The vocabulary every stage refuses in: a message written for the person
        # reading a log at 03:00, and the traceback kept for `--log debug`.
        log.error("%s", exc)
        log.debug("%s failed", args.cmd, exc_info=True)
        return 1


# --- the parser ----------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wayfare", description=__doc__)
    parser.add_argument("--data", type=Path, help="override WAYFARE_DATA")
    parser.add_argument("--log", default=None, help="log level")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for add, _ in _SUBCOMMANDS.values():
        add(sub)
    return parser


def _region_arg(p: argparse.ArgumentParser, help: str) -> None:
    """Which feed. Ambient by default, out of WAYFARE_REGION, because a data root
    holds one region and the flag is only ever an override of that."""
    p.add_argument("--region", default=None, help=help)


def _limit_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--limit", type=int, default=None, help="stop after N patterns")


def _workers_arg(p: argparse.ArgumentParser, help: str) -> None:
    p.add_argument("--workers", type=int, default=None, help=help)


def _retry_arg(p: argparse.ArgumentParser, literal: str) -> None:
    """`--retry`, whose one safe-unattended value is the same for every stage.

    `transient` is an alias rather than a status so that a scheduled run never has
    to name one: everything else means impossible, and a stage that retries the
    impossible never finishes.
    """
    p.add_argument(
        "--retry",
        default=None,
        metavar="STATUS,...",
        help="comma-separated statuses to forget and redo. `transient` is the alias "
        "for the ones that are safe unattended (transport_error: the request never "
        f"arrived, so nothing was learned). {literal}",
    )


def _overpass_args(
    p: argparse.ArgumentParser, cache: str, note: str = "", flag: str = "relations"
) -> None:
    """Where an Overpass response is cached, and whether to ask again.

    A national window is a minutes-long metered query, so the body is cached and
    reused, and only `--refresh` overrides that.

    `flag` because the three stages that ask Overpass do not ask it the same
    question: `snap` wants bare railway ways where the other two want route
    relations, so it needs its own flag over its own file. Sharing one would let
    whichever stage ran first decide the other's coverage, silently.
    """
    p.add_argument(
        f"--{flag}",
        type=Path,
        default=None,
        help=f"where the Overpass response is cached (default: {cache} in the data "
        f"root){note}",
    )
    p.add_argument(
        "--refresh",
        action="store_true",
        help="re-query Overpass even though a cached response is present. A national "
        "window is a minutes-long metered query, so it is cached and reused; this is "
        "for after the relations themselves have moved",
    )


def _archive_args(p: argparse.ArgumentParser) -> None:
    """Where the archive goes. Two ways to say it, and they are exclusive.

    Naming by region is opt-in rather than the default because the filename is what
    a deployment mounts, fetches and labels: moving it silently would leave whatever
    is being served stale while the publish still reported success.
    """
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--out",
        type=Path,
        default=None,
        metavar="PATH",
        help=f"write the archive here (default: {publish.DEFAULT_ARCHIVE} in the "
        "data root's out/)",
    )
    g.add_argument(
        "--name-by-region",
        action="store_true",
        help="name the archive after --region instead: `--region ireland` writes "
        "ireland.pmtiles, which is also the label the viewer gives it. Serving "
        "several regions from one directory needs this, or they overwrite one "
        "another",
    )


def _add_acquire_parser(sub: _Sub) -> None:
    p = sub.add_parser("acquire", help="download the GTFS feed, OSM extract and NaPTAN")
    _region_arg(
        p,
        "region slug (default: all). A BODS slug, `ireland` for the National "
        "Transport Authority's Republic of Ireland feed, or `northern_ireland` for "
        "Translink's four OpenDataNI datasets",
    )
    p.add_argument("--force", action="store_true", help="re-download even if present")
    p.add_argument(
        "--with-osm",
        action="store_true",
        help="also archive the OSM extract; Valhalla fetches its own copy, so "
        "this is only for recording which extract a set of edge ids belongs to",
    )


def _add_patterns_parser(sub: _Sub) -> None:
    p = sub.add_parser("patterns", help="reduce the timetable to distinct route patterns")
    p.add_argument("--memory", default=None, help="DuckDB memory limit, e.g. 8GB")
    p.add_argument(
        "--upgrade-shapes",
        action="store_true",
        help="re-match patterns that were matched from bare stops and have since "
        "gained operator geometry",
    )
    _modes_arg(p)


def _modes_arg(p: argparse.ArgumentParser) -> None:
    """The selection `patterns` rebuilds `meta.modes` from.

    Narrowing it retires the deselected patterns, so it is a decision a person
    makes and never a default an invocation carries: a scheduled run passes nothing
    and inherits whatever the database was last built with.
    """
    p.add_argument(
        "--modes",
        default=None,
        metavar="LIST",
        help="comma-separated modes to keep, from "
        f"{{{','.join(sorted(config.MODES))}}} "
        "(default: whatever this database was last built with, or "
        f"{','.join(sorted(config.DEFAULT_MODES))} for a new one). Only road modes "
        "are ever map-matched; the rest are kept for their operator geometry.",
    )


def _add_match_parser(sub: _Sub) -> None:
    p = sub.add_parser(
        "match", help="map-match patterns onto the road graph (the long one)"
    )
    _workers_arg(p, "requests in flight at once; default one per core")
    _limit_arg(p)
    p.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        help="stop after this long, at the next batch boundary; what a scheduled "
        "run uses to take a slice of the queue rather than all of it",
    )
    p.add_argument("--valhalla", default=None, help="Valhalla base URL")
    p.add_argument(
        "--force-graph",
        action="store_true",
        help="match on even though Valhalla reports a different graph build than "
        "the stored edge ids belong to",
    )
    _retry_arg(
        p,
        "A literal status such as low_confidence or error is for after fixing the "
        "matcher itself",
    )
    p.add_argument(
        "--reclassify-transport",
        action="store_true",
        help="one-off repair for a database matched before transport faults had "
        "their own status: move the `error` rows that were connection failures to "
        "transport_error. Combine with --retry transient to redo them",
    )


def _add_trace_parser(sub: _Sub) -> None:
    p = sub.add_parser(
        "trace",
        help="draw the non-road patterns with no operator geometry, from OSM "
        "route relations (the Underground, the DLR, London Trams)",
    )
    _limit_arg(p)
    _overpass_args(p, "raw/osm_relations.json")
    _retry_arg(
        p,
        "A literal status such as no_relation or chain_break is for after fixing "
        "the tracer itself, or after re-querying Overpass. `ok` redraws what "
        "already worked, which is what recuts a trace stored before the tracer kept "
        "the ways under its own slice -- until then those patterns draw per "
        "pattern, not per way",
    )


def _add_snap_parser(sub: _Sub) -> None:
    p = sub.add_parser(
        "snap",
        help="give an operator's own rail shape the OSM way ids it does not carry, "
        "so overlapping services share the track they run over",
    )
    _limit_arg(p)
    _overpass_args(
        p,
        "raw/osm_track.json",
        ". Its own file, not the ones `trace` and `routes` use: this stage asks for "
        "bare railway ways rather than route relations",
        flag="track",
    )
    _retry_arg(
        p,
        "A literal status such as partial_cover is for after re-querying Overpass "
        "against better-mapped track. `ok` re-snaps what already worked, which is "
        "what the snapper changing under a stored result calls for",
    )


def _add_routes_parser(sub: _Sub) -> None:
    p = sub.add_parser(
        "routes",
        help="build services from OSM route relations, for the modes with no "
        "timetable at all (Great Britain's National Rail)",
    )
    _overpass_args(
        p,
        "raw/osm_routes.json",
        ". Deliberately not the file `trace` uses: the two stages ask for different "
        "windows, and sharing one body lets whichever ran first decide the other's "
        "coverage",
    )
    p.add_argument(
        "--cif",
        type=Path,
        default=None,
        help="a Network Rail CIF schedule to attribute trips from. Optional: the "
        "track draws without one and `trips` stays null, which is the whole point "
        "of building the geometry from OpenStreetMap first",
    )
    p.add_argument(
        "--stops",
        type=Path,
        default=None,
        help="the NaPTAN CSV that turns a CIF TIPLOC into a place "
        "(default: raw/naptan.csv in the data root)",
    )
    p.add_argument(
        "--on",
        default=None,
        help="the date whose service to count, YYYY-MM-DD (default: today). One "
        "day rather than a range, because a service cancelled for one week of a "
        "six-month schedule is running and is not, and either answer for the whole "
        "range is one that cannot be defended",
    )


def _add_aggregate_parser(sub: _Sub) -> None:
    sub.add_parser("aggregate", help="invert to edge -> services")


def _add_publish_parser(sub: _Sub) -> None:
    p = sub.add_parser("publish", help="export GeoJSON and build PMTiles")
    _region_arg(
        p,
        "which feed's credit to stamp into the archive (default: the WAYFARE_REGION "
        "this data root was acquired with). The licence is a condition, not a label, "
        "so this has to match the data. With --name-by-region it also decides the "
        "filename, and therefore what the viewer calls the region",
    )
    p.add_argument(
        "--from-export",
        nargs="?",
        const=True,
        default=None,
        help="build the tiles from a GeoJSONL a previous publish wrote instead of "
        "exporting one, for a data root whose database is gone. Defaults to "
        "work/edges.geojsonl. This rebuilds the same tiles; it does not refresh "
        "the region",
    )
    _archive_args(p)


def _add_coverage_parser(sub: _Sub) -> None:
    p = sub.add_parser(
        "coverage", help="measure what a built archive actually draws, per cell per zoom"
    )
    p.add_argument("archive", type=Path, help="a .pmtiles file")
    p.add_argument(
        "--zooms",
        default=None,
        metavar="Z,Z,...",
        help=f"which zooms to measure (default: {config.MIN_ZOOM} up to "
        f"{config.DETAIL_ZOOM - 1}, the banded overview). Each is measured against "
        f"z{config.MAX_ZOOM}, which carries the complete network",
    )
    p.add_argument(
        "--cell",
        type=float,
        default=None,
        metavar="DEGREES",
        help=f"the cell to count over, in degrees (default: {config.COVERAGE_CELL})",
    )


def _add_draw_parser(sub: _Sub) -> None:
    p = sub.add_parser(
        "draw",
        help="rasterise a built archive to PNG -- what a zoom looks like, not how "
        "much it holds",
    )
    p.add_argument("archive", type=Path, help="a .pmtiles file")
    p.add_argument("out", type=Path, help="the .png to write")
    p.add_argument("--zoom", type=int, required=True)
    # Four values rather than one comma-separated string. Every window over these
    # islands opens on a negative longitude, and argparse reads `-1.4,51.0,1.0,52.2`
    # as an option because only a bare number matches its negative-number rule --
    # so the comma form fails on essentially every real window.
    p.add_argument(
        "--window",
        required=True,
        nargs=4,
        type=float,
        metavar=("W", "S", "E", "N"),
        help="the longitude/latitude box to draw, e.g. -1.4 51.0 1.0 52.2 for London "
        "and the country around it",
    )
    p.add_argument("--width", type=int, default=1400, help="output width in pixels")


def _add_status_parser(sub: _Sub) -> None:
    sub.add_parser("status", help="show progress and coverage")


def _add_prune_parser(sub: _Sub) -> None:
    sub.add_parser(
        "prune", help="drop operator geometry once matching is complete (reclaims space)"
    )


def _add_cluster_parser(sub: _Sub) -> None:
    sub.add_parser(
        "cluster", help="reorder edges spatially so window queries can skip row groups"
    )


def _add_art_parser(sub: _Sub) -> None:
    p = sub.add_parser("art", help="render an area")
    p.add_argument(
        "area",
        nargs="?",
        help="preset name, or a window as minlon,minlat,maxlon,maxlat",
    )
    # A window that starts west of Greenwich begins with a minus, which argparse
    # reads as an option flag. `--bbox=...` sidesteps that, because the value is
    # attached rather than a separate token.
    p.add_argument(
        "--bbox",
        default=None,
        metavar="minlon,minlat,maxlon,maxlat",
        help="explicit window; use --bbox=-3.3,51.4,-3.0,51.6 for negative longitudes",
    )
    p.add_argument("--style", default="density", help="density | spectrum | strands")
    p.add_argument("--out", type=Path, default=None, help=".png or .svg")
    p.add_argument("--width", type=int, default=4000)
    p.add_argument("--scale", type=float, default=1.0, help="2.0 is roughly 192 dpi")
    p.add_argument("--caption", default=None)
    p.add_argument(
        "--credit",
        action="store_true",
        help="draw the data credit in the corner. Every render carries it in its "
        "PNG or SVG metadata already; this is for anywhere that metadata will not "
        "survive the trip",
    )
    p.add_argument(
        "--coalesce",
        action="store_true",
        help="join edges that meet end to end into one stroke, so a shared node is "
        "capped once rather than twice; density only",
    )
    _workers_arg(p, "bands drawn in parallel; default one per core, 1 to draw serially")


def _add_serve_parser(sub: _Sub) -> None:
    p = sub.add_parser("serve", help="serve the viewer, the tiles and /art")
    p.add_argument("--port", type=int, default=8099)
    p.add_argument("--host", default="", help="bind address; default all interfaces")
    p.add_argument("--dir", type=Path, default=Path("web"), help="the viewer bundle")
    p.add_argument(
        "--out", type=Path, default=None, help="where the .pmtiles are (default: OUT)"
    )
    p.add_argument(
        "--no-art",
        action="store_true",
        help="switch off /art; a render is the one request here that costs real CPU. "
        "WAYFARE_ART=off does the same for a deployment that cannot change the command",
    )
    # Defaulted in server.serve rather than here, because `server` is imported
    # lazily further down -- it pulls in cairo and duckdb, which is a lot to load
    # to print a usage message for some other subcommand.
    p.add_argument(
        "--max-age",
        type=int,
        default=None,
        metavar="SECONDS",
        help="how long a browser may reuse a cached .pmtiles archive without "
        "revalidating (default one day; 0 revalidates every time). "
        "The page itself always revalidates",
    )


def _add_all_parser(sub: _Sub) -> None:
    p = sub.add_parser("all", help="run every stage of the pipeline in order")
    _region_arg(p, "the region to acquire and to credit (default: WAYFARE_REGION)")
    _workers_arg(p, "match requests in flight at once; default one per core")
    _modes_arg(p)
    _archive_args(p)


# --- the shared pieces ---------------------------------------------------------


@contextlib.contextmanager
def _open(read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
    """The connection a stage runs on, closed however the stage ends.

    A context manager rather than a `finally` per stage, because DuckDB takes a
    single writer: a stage that raises with one still open is a stage nothing can
    follow, and the failure then lands on whichever command is run next.
    """
    con = db.connect(read_only=read_only)
    try:
        yield con
    finally:
        con.close()


def _archive_out(args: argparse.Namespace) -> Path | None:
    """The path the flags ask for, or None to let `publish.build` decide."""
    if args.name_by_region:
        return config.OUT / config.archive_name(args.region)
    out: Path | None = args.out
    return out


def _require_db() -> Path:
    """Fail with the command to run rather than a DuckDB traceback.

    Reading a database that does not exist is the most likely first-run mistake,
    and the underlying IOException says nothing about how to fix it.
    """
    if not config.DB_PATH.exists():
        raise SystemExit(
            f"no database at {config.DB_PATH}\n"
            "Run `wayfare acquire` then `wayfare patterns` to build one."
        )
    return config.DB_PATH


def _statuses(spec: str) -> list[str]:
    """`--retry` as a list of status names, blanks dropped.

    Cleared before the run and never during it: work is selected by the *absence*
    of a status row, so a row deleted while its pattern is in flight is handed out
    twice.
    """
    return [s.strip() for s in spec.split(",") if s.strip()]


def _parse_modes(spec: str | None) -> frozenset[str] | None:
    """`--modes` as a set of names, or None to take the default.

    An empty selection is refused rather than treated as "everything": `--modes ''`
    would otherwise build a database with no patterns in it and report success.
    """
    if spec is None:
        return None
    names = frozenset(m.strip() for m in spec.split(",") if m.strip())
    if not names:
        raise ValueError("--modes was given no mode names")
    config.route_types(names)  # raises on an unknown name
    return names


# --- the subcommands -----------------------------------------------------------


def _cmd_acquire(args: argparse.Namespace) -> int:
    acquire.acquire_all(region=args.region, force=args.force, with_osm=args.with_osm)
    return 0


def _cmd_patterns(args: argparse.Namespace) -> int:
    gtfs_dir = config.WORK / "gtfs"
    if not (gtfs_dir / "stop_times.txt").exists():
        log.error("no unpacked feed at %s -- run `wayfare acquire` first", gtfs_dir)
        return 1
    modes = _parse_modes(args.modes)
    with _open() as con:
        gtfs.build_patterns(
            gtfs_dir,
            con,
            memory_limit=args.memory,
            upgrade_shapes=args.upgrade_shapes,
            modes=modes,
        )
    return 0


def _cmd_match(args: argparse.Namespace) -> int:
    from . import valhalla

    client = valhalla.Client(args.valhalla)
    with _open() as con:
        # Both of these rewrite match_status, and both must land before the first
        # batch is loaded: work is selected by the absence of a row, so a row
        # deleted while its pattern is in flight is handed out twice.
        if args.reclassify_transport:
            match.reclassify_transport_faults(con)
        if args.retry:
            match.retry(con, _statuses(args.retry))
        match.run(
            con,
            client_=client,
            workers=args.workers,
            limit=args.limit,
            max_seconds=args.max_seconds,
            force_graph=args.force_graph,
        )
        for row in match.summary(con):
            log.info("  %-16s %-6s n=%-7d edges=%-6s detour=%s", *row)
    return 0


def _cmd_trace(args: argparse.Namespace) -> int:
    _require_db()
    with _open() as con:
        if args.retry:
            trace.retry(con, _statuses(args.retry))
        trace.run(con, cache=args.relations, refresh=args.refresh, limit=args.limit)
        for status, n, km, worst in trace.summary(con):
            log.info("  %-16s n=%-6d track=%-8.1fkm worst stop=%.0fm", status, n, km, worst)
    return 0


def _cmd_routes(args: argparse.Namespace) -> int:
    _require_db()
    with _open() as con:
        built = osmroutes.run(con, cache=args.relations, refresh=args.refresh)
        log.info(
            "  %d relations considered, %d chained, %d services over %d ways",
            built.considered,
            built.chained,
            built.patterns,
            built.ways,
        )
        log.info(
            "  refused: %d belong to another region, %d did not chain, "
            "%d named fewer than two stops",
            built.skipped_not_ours,
            built.skipped_broken,
            built.skipped_no_stops,
        )
        if args.cif:
            when = (
                datetime.strptime(args.on, "%Y-%m-%d").date()
                if args.on
                else datetime.now(UTC).date()
            )
            got = railtrips.run_cached(
                con,
                args.cif,
                args.stops or (config.RAW / "naptan.csv"),
                on=when,
                cache=args.relations,
            )
            log.info(
                "  %d of %d legs placed, %.1f%% of weekly leg-trips over %d ways",
                got.legs_placed,
                got.legs,
                got.trip_coverage,
                got.ways,
            )
    return 0


def _cmd_snap(args: argparse.Namespace) -> int:
    _require_db()
    with _open() as con:
        # Cleared before the run and never during, for the reason `trace` clears
        # before its own: work is selected by the absence of a status row.
        if args.retry:
            snap.retry(con, _statuses(args.retry))
        snap.run(con, cache=args.track, refresh=args.refresh, limit=args.limit)
        for status, n, km, cover in snap.summary(con):
            log.info("  %-16s n=%-6d shape=%-8.1fkm covered=%.1f%%", status, n, km, cover)
    return 0


def _cmd_aggregate(args: argparse.Namespace) -> int:
    with _open() as con:
        aggregate.build(con)
    return 0


def _cmd_publish(args: argparse.Namespace) -> int:
    # --from-export is the one path here that needs no database, and a root whose
    # database has been taken away is exactly when it is reached for -- so the
    # `_require_db` that guards every other publish must not run for it.
    export = args.from_export
    if export is True:
        export = config.WORK / "edges.geojsonl"
    elif export is not None:
        export = Path(export)

    opened: AbstractContextManager[duckdb.DuckDBPyConnection | None]
    if export is None:
        _require_db()
        opened = _open(read_only=True)
    else:
        opened = contextlib.nullcontext(None)

    with opened as con:
        out = publish.build(
            con, region=args.region, out=_archive_out(args), from_export=export
        )
    log.info("done: %s", out)
    return 0


def _cmd_coverage(args: argparse.Namespace) -> int:
    zooms = (
        [int(z) for z in args.zooms.split(",")]
        if args.zooms
        else list(range(config.MIN_ZOOM, config.DETAIL_ZOOM))
    )
    coverage.report_sizes(args.archive)
    coverage.report(args.archive, zooms, args.cell)
    return 0


def _cmd_draw(args: argparse.Namespace) -> int:
    west, south, east, north = args.window
    coverage.draw(args.archive, args.zoom, (west, south, east, north), args.out, args.width)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    _require_db()
    with _open(read_only=True) as con:
        # stdout, and the logging goes to stderr: `deploy/refresh.sh` reads this
        # with `jq`, so the two must never mix in one capture.
        print(json.dumps(aggregate.funnel(con), indent=2))
    return 0


def _cmd_prune(args: argparse.Namespace) -> int:
    _require_db()
    with _open() as con:
        n = maintenance.prune_shapes(con)
    log.info("dropped %d shapes", n)
    return 0


def _cmd_cluster(args: argparse.Namespace) -> int:
    _require_db()
    t0 = time.monotonic()
    n, before, after = maintenance.cluster()
    if not n:
        log.info("no edges to cluster")
        return 0
    # The size is worth reporting because it is half the reason to run this: the
    # rows compress better sorted, whatever the window queries then do.
    log.info(
        "clustered %d edges in %.1fs; %.0f MB -> %.0f MB",
        n,
        time.monotonic() - t0,
        before / 1e6,
        after / 1e6,
    )
    return 0


def _cmd_art(args: argparse.Namespace) -> int:
    from . import art

    area = args.bbox or args.area
    if not area:
        log.error(
            "give an area: a preset (%s) or --bbox=minlon,minlat,maxlon,maxlat",
            ", ".join(sorted(art.PRESETS)),
        )
        return 1
    if args.bbox and args.area:
        log.error("give either an area or --bbox, not both")
        return 1
    _require_db()

    out = art.render(
        area,
        style=args.style,
        out_path=args.out,
        opts=art.RenderOpts(
            width_px=args.width,
            scale=args.scale,
            caption=args.caption,
            credit=args.credit,
            coalesce=args.coalesce,
        ),
        workers=args.workers,
    )
    log.info("done: %s", out)
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from . import server

    server.serve(
        port=args.port,
        host=args.host,
        web_dir=args.dir,
        out_dir=args.out or config.OUT,
        art_enabled=config.ART_ENABLED and not args.no_art,
        max_age=args.max_age,
    )
    return 0


# --- the whole chain -----------------------------------------------------------


def _run(cmd: str, *flags: str) -> int:
    """One subcommand, exactly as typing it would run it.

    Through the parser rather than through a hand-built Namespace, so a stage's
    defaults are the ones its own `_add_*_parser` declares. A Namespace assembled
    here would be a second copy of every default, and a flag added to a stage
    would reach the stage and not the chain.
    """
    return _SUBCOMMANDS[cmd][1](_parser().parse_args([cmd, *flags]))


def _tolerate(cmd: str, *flags: str) -> None:
    """Run a stage that asks Overpass, and carry on if it could not.

    Overpass is a third party's metered public service, and a refresh that dropped
    a whole region's buses because a public API was busy would be the wrong trade
    entirely -- the more so here, where the run behind it has just cost a day or
    two of matching. What these stages do not draw keeps no status row, so the next
    run selects it again unchanged.
    """
    try:
        if _run(cmd, *flags):
            log.warning("%s did not finish; what it missed stays pending", cmd)
    except (OSError, RuntimeError) as exc:
        log.warning("skipping %s: %s", cmd, exc)


def _publishable(funnel: dict[str, object]) -> bool:
    """The publish gate, as two counts and not one.

    `patterns_pending` counts patterns with no `match_status` row at all, and a
    transport fault *is* a row: a Valhalla outage part-way through a drain leaves
    those patterns unmatched but not pending, so the run reaches the end reporting
    nothing pending. Publishing on that one number ships a tileset missing every
    road the outage interrupted, and missing road does not read as an incomplete
    run -- it reads as a region that lost its buses.

    `deploy/refresh.sh` gates the scheduled run on the same two numbers, read out
    of `wayfare status` with `jq`, because the deployed sequence is shell.
    """
    pending = funnel.get("patterns_pending") or 0
    by_status = funnel.get("by_status")
    faults = by_status.get("transport_error", 0) if isinstance(by_status, dict) else 0
    if pending or faults:
        log.error(
            "drain incomplete -- %s pending, %s transport faults; not publishing",
            pending,
            faults,
        )
        return False
    return True


def _cmd_all(args: argparse.Namespace) -> int:
    """Every stage in the order `deploy/refresh.sh` runs them.

    Calling the subcommands rather than restating the chain, because two
    definitions of the pipeline is exactly how this one came to omit `routes`,
    `prune`, `cluster` and the publish gate while the script that deployments
    actually run had all four.

    The one deliberate difference is `acquire`: the scheduled run forces a
    re-download because the point of a refresh is the feed that replaced the one on
    disk, and an attended `all` should not spend 1.28 GB to fetch what is already
    there. `wayfare acquire --force` is how you say otherwise.
    """
    # Settle where the archive goes before anything expensive runs. A name that
    # will be refused is refused just as well now as after a day of matching.
    out = _archive_out(args) or publish.default_out(args.region)

    region = ["--region", args.region] if args.region else []
    modes = ["--modes", args.modes] if args.modes else []
    workers = ["--workers", str(args.workers)] if args.workers is not None else []

    # `patterns` takes no flags beyond the selection: the mode selection lives in
    # `meta.modes`, and a run that passes none inherits what the database was last
    # built with.
    for cmd, flags in (
        ("acquire", region),
        ("patterns", modes),
        ("match", ["--retry", "transient", *workers]),
    ):
        if code := _run(cmd, *flags):
            return code

    with _open(read_only=True) as con:
        funnel = aggregate.funnel(con)
    if not _publishable(funnel):
        print(json.dumps(funnel, indent=2))
        return 1

    _tolerate("trace", "--retry", "transient")
    # Its own `_tolerate` and not folded into `trace`'s, because the two ask Overpass
    # different questions: one being refused says nothing about the other, and a
    # shared handler would skip the stage that would have answered.
    _tolerate("snap")
    _tolerate("routes")

    # `prune` sits between `aggregate`, which builds `segments` out of `patterns`
    # and `shapes`, and `cluster`, which is the only thing that gives the space
    # back: pruning after the copy would reclaim nothing until the next run.
    for cmd, flags in (
        ("aggregate", []),
        ("prune", []),
        ("cluster", []),
        ("publish", ["--out", str(out), *region]),
    ):
        if code := _run(cmd, *flags):
            return code

    with _open(read_only=True) as con:
        print(json.dumps(aggregate.funnel(con), indent=2))
    return 0


# Every subcommand, in the order `--help` lists them: what it takes, and what it
# does. One table rather than two, so a stage cannot arrive with a parser and no
# command -- which is a KeyError at the end of a parse and not at import.
_SUBCOMMANDS: dict[str, tuple[Callable[[_Sub], None], Callable[[argparse.Namespace], int]]]
_SUBCOMMANDS = {
    "acquire": (_add_acquire_parser, _cmd_acquire),
    "patterns": (_add_patterns_parser, _cmd_patterns),
    "match": (_add_match_parser, _cmd_match),
    "trace": (_add_trace_parser, _cmd_trace),
    "snap": (_add_snap_parser, _cmd_snap),
    "routes": (_add_routes_parser, _cmd_routes),
    "aggregate": (_add_aggregate_parser, _cmd_aggregate),
    "publish": (_add_publish_parser, _cmd_publish),
    "coverage": (_add_coverage_parser, _cmd_coverage),
    "draw": (_add_draw_parser, _cmd_draw),
    "status": (_add_status_parser, _cmd_status),
    "prune": (_add_prune_parser, _cmd_prune),
    "cluster": (_add_cluster_parser, _cmd_cluster),
    "art": (_add_art_parser, _cmd_art),
    "serve": (_add_serve_parser, _cmd_serve),
    "all": (_add_all_parser, _cmd_all),
}


if __name__ == "__main__":
    sys.exit(main())
