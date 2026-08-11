"""Command line entry point.

The pipeline is a sequence of stages, each of which reads what the last one wrote
and can be re-run on its own. `wayfare all` chains them; the individual
subcommands exist because on a run this long you frequently want to redo exactly
one of them.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import duckdb

from . import acquire, aggregate, config, coverage, db, gtfs, logs, match, publish

log = logs.get("cli")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wayfare", description=__doc__)
    parser.add_argument("--data", type=Path, help="override WAYFARE_DATA")
    parser.add_argument("--log", default=None, help="log level")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("acquire", help="download the GTFS feed, OSM extract and NaPTAN")
    p.add_argument(
        "--region",
        default=None,
        help="region slug (default: all). A BODS slug, `ireland` for the "
        "National Transport Authority's Republic of Ireland feed, or "
        "`northern_ireland` for Translink's four OpenDataNI datasets",
    )
    p.add_argument("--force", action="store_true", help="re-download even if present")
    p.add_argument(
        "--with-osm",
        action="store_true",
        help="also archive the OSM extract; Valhalla fetches its own copy, so "
        "this is only for recording which extract a set of edge ids belongs to",
    )

    p = sub.add_parser("patterns", help="reduce the timetable to distinct route patterns")
    p.add_argument("--memory", default=None, help="DuckDB memory limit, e.g. 8GB")
    p.add_argument(
        "--upgrade-shapes",
        action="store_true",
        help="re-match patterns that were matched from bare stops and have since "
        "gained operator geometry",
    )
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

    p = sub.add_parser(
        "match", help="map-match patterns onto the road graph (the long one)"
    )
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--limit", type=int, default=None, help="stop after N patterns")
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
    p.add_argument(
        "--retry",
        default=None,
        help="comma-separated statuses to forget and redo. `transient` is the "
        "alias for the ones that are safe unattended (transport_error: the "
        "request never reached Valhalla). A literal status such as "
        "low_confidence or error is for after fixing the matcher itself",
    )
    p.add_argument(
        "--reclassify-transport",
        action="store_true",
        help="one-off repair for a database matched before transport faults had "
        "their own status: move the `error` rows that were connection failures to "
        "transport_error. Combine with --retry transient to redo them",
    )

    sub.add_parser("aggregate", help="invert to edge -> services")
    p = sub.add_parser("publish", help="export GeoJSON and build PMTiles")
    p.add_argument(
        "--region",
        default=None,
        help="which feed's credit to stamp into the archive (default: the "
        "WAYFARE_REGION this data root was acquired with). The licence is a "
        "condition, not a label, so this has to match the data. With "
        "--name-by-region it also decides the filename, and therefore what the "
        "viewer calls the region",
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
        help=f"the cell to count over (default: {config.OVERVIEW_CELL}, the same one "
        "publish shares its quota over)",
    )

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

    sub.add_parser("status", help="show progress and coverage")
    sub.add_parser(
        "prune", help="drop operator geometry once matching is complete (reclaims space)"
    )
    sub.add_parser(
        "cluster", help="reorder edges spatially so window queries can skip row groups"
    )

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
    p.add_argument(
        "--workers",
        type=int,
        default=None,
        help="bands drawn in parallel; default one per core, 1 to draw serially",
    )

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

    p = sub.add_parser("all", help="acquire, patterns, match, aggregate, publish")
    p.add_argument("--region", default=None)
    p.add_argument("--workers", type=int, default=None)
    _archive_args(p)

    args = parser.parse_args(argv)
    logs.setup(args.log)

    if args.data:
        _retarget(args.data)
    config.ensure_dirs()

    try:
        return _dispatch(args)
    except KeyboardInterrupt:
        # Every stage checkpoints, so an interrupt is a normal way to stop.
        log.info("interrupted; progress is saved -- re-run the same command to resume")
        return 130


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


def _dispatch(args: argparse.Namespace) -> int:
    if args.cmd == "acquire":
        acquire.acquire_all(region=args.region, force=args.force, with_osm=args.with_osm)
        return 0

    if args.cmd == "patterns":
        con = db.connect()
        gtfs_dir = config.WORK / "gtfs"
        if not (gtfs_dir / "stop_times.txt").exists():
            log.error("no unpacked feed at %s -- run `wayfare acquire` first", gtfs_dir)
            return 1
        try:
            modes = _parse_modes(args.modes)
        except ValueError as e:
            log.error("%s", e)
            return 1
        gtfs.build_patterns(
            gtfs_dir,
            con,
            memory_limit=args.memory,
            upgrade_shapes=args.upgrade_shapes,
            modes=modes,
        )
        con.close()
        return 0

    if args.cmd == "match":
        from . import valhalla

        con = db.connect()
        client = valhalla.Client(args.valhalla)
        # Both of these rewrite match_status, and both must land before the first
        # batch is loaded: work is selected by the absence of a row, so a row
        # deleted while its pattern is in flight is handed out twice.
        if args.reclassify_transport:
            match.reclassify_transport_faults(con)
        if args.retry:
            match.retry(con, [s.strip() for s in args.retry.split(",") if s.strip()])
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
        con.close()
        return 0

    if args.cmd == "aggregate":
        con = db.connect()
        aggregate.build(con)
        con.close()
        return 0

    if args.cmd == "publish":
        # --from-export is the one path here that needs no database, and a root whose
        # database has been taken away is exactly when it is reached for -- so the
        # `_require_db` that guards every other publish must not run for it.
        export = args.from_export
        if export is True:
            export = config.WORK / "edges.geojsonl"
        elif export is not None:
            export = Path(export)

        pub_con: duckdb.DuckDBPyConnection | None = None
        if export is None:
            _require_db()
            pub_con = db.connect(read_only=True)
        try:
            out = publish.build(
                pub_con, region=args.region, out=_archive_out(args), from_export=export
            )
        except (RuntimeError, ValueError) as exc:
            log.error("%s", exc)
            return 1
        finally:
            if pub_con is not None:
                pub_con.close()
        log.info("done: %s", out)
        return 0

    if args.cmd == "draw":
        try:
            west, south, east, north = args.window
            coverage.draw(
                args.archive, args.zoom, (west, south, east, north), args.out, args.width
            )
        except (OSError, RuntimeError, ValueError) as exc:
            log.error("%s", exc)
            return 1
        return 0

    if args.cmd == "coverage":
        zooms = (
            [int(z) for z in args.zooms.split(",")]
            if args.zooms
            else list(range(config.MIN_ZOOM, config.DETAIL_ZOOM))
        )
        try:
            coverage.report_sizes(args.archive)
            coverage.report(args.archive, zooms, args.cell)
        except (OSError, RuntimeError, ValueError) as exc:
            log.error("%s", exc)
            return 1
        return 0

    if args.cmd == "prune":
        _require_db()
        con = db.connect()
        try:
            n = db.prune_shapes(con)
        except RuntimeError as exc:
            log.error("%s", exc)
            return 1
        finally:
            con.close()
        log.info("dropped %d shapes", n)
        return 0

    if args.cmd == "cluster":
        _require_db()
        t0 = time.monotonic()
        try:
            n, before, after = db.cluster()
        except RuntimeError as exc:
            log.error("%s", exc)
            return 1
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

    if args.cmd == "status":
        _require_db()
        con = db.connect(read_only=True)
        print(json.dumps(aggregate.coverage(con), indent=2))
        con.close()
        return 0

    if args.cmd == "art":
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

    if args.cmd == "serve":
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

    if args.cmd == "all":
        # Settle where the archive goes before anything expensive runs. A name that
        # will be refused is refused just as well now as after a day of matching.
        try:
            out = _archive_out(args) or publish.default_out(args.region)
        except (RuntimeError, ValueError) as exc:
            log.error("%s", exc)
            return 1
        acquire.acquire_all(region=args.region)
        con = db.connect()
        gtfs.build_patterns(config.WORK / "gtfs", con)
        match.run(con, workers=args.workers)
        aggregate.build(con)
        publish.build(con, region=args.region, out=out)
        print(json.dumps(aggregate.coverage(con), indent=2))
        con.close()
        return 0

    return 1


def _retarget(data: Path) -> None:
    """Point every path at a different data root.

    config computes its paths at import time, which keeps them cheap to read
    everywhere else; --data is the one place that needs to override them.
    """
    config.DATA = data.resolve()
    config.RAW = config.DATA / "raw"
    config.WORK = config.DATA / "work"
    config.OUT = config.DATA / "out"
    config.DB_PATH = config.WORK / "wayfare.duckdb"


if __name__ == "__main__":
    sys.exit(main())
