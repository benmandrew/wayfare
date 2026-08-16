#!/usr/bin/env python3
"""Clip the Natural Earth coastline to these islands, for `readme_map.py` to draw.

    python scripts/coastline.py            # docs/coastline.json
    python scripts/coastline.py --force    # re-fetch the source as well

Two levels of not asking twice. The 10 MB global source is downloaded once into
`RAW` and reused from there like every other download this project makes, and the
clipped result is committed, so a redraw of the README map makes no request at
all and works in an offline clone. Only moving the window or the source needs
either of them again -- and the window can move freely, because the clip is to
`map.toml`'s roam box rather than to the picture's own frame.

Natural Earth is public domain, so nothing here owes an attribution. That is most
of why it is the coastline drawn rather than a basemap's: a raster backdrop from
a tile service would put a licence condition on a PNG that travels without the
page it was made for.

1:10m rather than 1:50m because the output is about 480 m to the pixel and 1:50m
is coarser than that -- the Firth of Clyde closes up and Strangford Lough goes.
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

from wayfare import config, palette

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "coastline.json"

SOURCE = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
    "master/geojson/ne_10m_coastline.geojson"
)
CACHED = "ne_10m_coastline.geojson"

# Three decimal places is about 70 m of longitude at this latitude, against a
# pixel that is about 480 m at the width the README map is drawn. Rounding is
# what takes the file from 162 KB to 144 KB, and consecutive points that collapse
# onto each other afterwards are dropped rather than drawn as a zero-length line.
PLACES = 3


def fetch(force: bool = False) -> Path:
    """The global source, downloaded once into RAW and reused after that."""
    config.RAW.mkdir(parents=True, exist_ok=True)
    dest = config.RAW / CACHED
    if dest.exists() and not force:
        print(f"already have {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
        return dest
    print(f"fetching {SOURCE}")
    with urllib.request.urlopen(SOURCE, timeout=120) as response:  # noqa: S310
        dest.write_bytes(response.read())
    print(f"wrote {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
    return dest


def clip(source: Path, box: tuple[float, float, float, float]) -> list[list[list[float]]]:
    """Every run of coastline that touches the box, as rounded lon/lat pairs.

    A segment is kept when either end is inside, so a coast entering the box is
    drawn up to the edge rather than starting at it. Whole features are not kept:
    the lines here are continents, and the one carrying Kerry also carries
    Portugal.
    """
    west, south, east, north = box
    data = json.loads(source.read_text())

    def inside(point: list[float]) -> bool:
        return west <= point[0] <= east and south <= point[1] <= north

    runs: list[list[list[float]]] = []
    for feature in data["features"]:
        geometry = feature["geometry"]
        lines = (
            [geometry["coordinates"]]
            if geometry["type"] == "LineString"
            else geometry["coordinates"]
        )
        for line in lines:
            run: list[list[float]] = []
            for a, b in zip(line, line[1:], strict=False):
                if inside(a) or inside(b):
                    if not run:
                        run.append(a)
                    run.append(b)
                elif run:
                    runs.append(run)
                    run = []
            if len(run) > 1:
                runs.append(run)

    out: list[list[list[float]]] = []
    for run in runs:
        rounded: list[list[float]] = []
        for x, y in run:
            point = [round(x, PLACES), round(y, PLACES)]
            if not rounded or rounded[-1] != point:
                rounded.append(point)
        if len(rounded) > 1:
            out.append(rounded)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--force", action="store_true", help="re-fetch the source before clipping"
    )
    ap.add_argument("--out", type=Path, default=OUT, help="the .json to write")
    args = ap.parse_args()

    box = palette.load().roam
    runs = clip(fetch(args.force), box)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(runs, separators=(",", ":")) + "\n")
    points = sum(len(r) for r in runs)
    size = args.out.stat().st_size / 1024
    shown = args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out
    print(f"{shown}: {len(runs)} runs, {points} points, {size:.0f} KB over {box}")


if __name__ == "__main__":
    main()
