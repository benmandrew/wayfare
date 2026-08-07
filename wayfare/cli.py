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
from pathlib import Path

from . import acquire, aggregate, config, db, gtfs, logs, match, publish

log = logs.get("cli")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wayfare", description=__doc__)
    parser.add_argument("--data", type=Path, help="override WAYFARE_DATA")
    parser.add_argument("--log", default=None, help="log level")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("acquire", help="download BODS GTFS, OSM extract and NaPTAN")
    p.add_argument("--region", default=None, help="BODS region slug (default: all)")
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
        help="comma-separated statuses to forget and redo, e.g. "
        "low_confidence,error -- use after fixing the matcher itself",
    )

    sub.add_parser("aggregate", help="invert to edge -> services")
    sub.add_parser("publish", help="export GeoJSON and build PMTiles")
    sub.add_parser("status", help="show progress and coverage")
    sub.add_parser(
        "prune", help="drop operator geometry once matching is complete (reclaims space)"
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

    p = sub.add_parser("all", help="acquire, patterns, match, aggregate, publish")
    p.add_argument("--region", default=None)
    p.add_argument("--workers", type=int, default=None)

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
        gtfs.build_patterns(
            gtfs_dir,
            con,
            memory_limit=args.memory,
            upgrade_shapes=args.upgrade_shapes,
        )
        con.close()
        return 0

    if args.cmd == "match":
        from . import valhalla

        con = db.connect()
        client = valhalla.Client(args.valhalla)
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
        _require_db()
        con = db.connect(read_only=True)
        out = publish.build(con)
        con.close()
        log.info("done: %s", out)
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
                width_px=args.width, scale=args.scale, caption=args.caption
            ),
        )
        log.info("done: %s", out)
        return 0

    if args.cmd == "all":
        acquire.acquire_all(region=args.region)
        con = db.connect()
        gtfs.build_patterns(config.WORK / "gtfs", con)
        match.run(con, workers=args.workers)
        aggregate.build(con)
        publish.build(con)
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
