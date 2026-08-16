"""What a finished archive actually draws, and where.

Reads the archive rather than the export or the database: it walks the PMTiles
directory, pulls each tile's Mapbox Vector Tile blob and decodes the geometry back to
longitude and latitude. A published file can therefore be checked wherever it ended up.

Two things to do with that, and the second one is the one to trust.

`drawn` and `bands` count features per cell per zoom. **That measurement has a blind
spot.** A cap on what a low zoom holds keeps many short features spread over many cells;
no cap keeps fewer, longer ones. Counting features rewards the first and only the second
reaches the screen, so a feature count rises while the map gets worse. Populated cells,
features per cell and bins holding anything all fail the same way.

`draw` rasterises the geometry into a window and writes a PNG, which is what a reader
sees. A zoom hollowed into a radial skeleton shows up there and in no count in here, so
reach for it before believing any number this module reports.
"""

from __future__ import annotations

import gzip
import math
import struct
import zlib
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from . import config, logs

log = logs.get("coverage")

Cell = tuple[int, int]
# A directory entry with the z/x/y its tile id decodes to.
Tile = tuple["_Entry", int, int, int]

# PMTiles v3: a 127-byte header, then a root directory, then optional leaf directories,
# then the tile data. The eight offsets and lengths this needs start at byte 8.
_HEADER = 127
_OFFSETS = "<QQQQQQQQ"

# MVT protobuf field numbers. Only the ones on the path to a feature's geometry are
# named; everything else is skipped by wire type.
_TILE_LAYER = 3
_LAYER_NAME = 1
_LAYER_FEATURE = 2
_LAYER_EXTENT = 5
_FEATURE_GEOMETRY = 4

# Geometry command ids, which share the low three bits of a command word with its count.
_MOVE_TO, _CLOSE = 1, 7

_DEFAULT_EXTENT = 4096


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7


def _skip(buf: bytes, i: int, wire: int) -> int:
    """Step over a protobuf field this does not care about."""
    if wire == 0:
        _, i = _varint(buf, i)
    elif wire == 1:
        i += 8
    elif wire == 2:
        length, i = _varint(buf, i)
        i += length
    elif wire == 5:
        i += 4
    else:
        raise ValueError(f"unknown protobuf wire type {wire}")
    return i


def _unzigzag(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def _decompress(blob: bytes) -> bytes:
    """PMTiles compresses its directories and tiles, and tippecanoe writes gzip.

    Sniffed rather than read out of the header's compression byte, so an archive
    written with compression off still reads.
    """
    try:
        return gzip.decompress(blob)
    except OSError:
        return blob


@dataclass(frozen=True)
class _Entry:
    tile_id: int
    run_length: int
    length: int
    offset: int


def _read_directory(buf: bytes) -> list[_Entry]:
    """Decode one PMTiles directory.

    Four delta-or-plain varint arrays, one after another, rather than four fields per
    entry. A run_length of 0 marks a pointer to a leaf directory instead of a tile, and
    an offset of 0 on any entry but the first means "immediately after the previous
    one", which is how a run of adjacent tiles costs one byte each.
    """
    count, i = _varint(buf, 0)
    tile_ids = [0] * count
    runs = [0] * count
    lengths = [0] * count
    offsets = [0] * count

    last = 0
    for k in range(count):
        delta, i = _varint(buf, i)
        last += delta
        tile_ids[k] = last
    for k in range(count):
        runs[k], i = _varint(buf, i)
    for k in range(count):
        lengths[k], i = _varint(buf, i)
    for k in range(count):
        value, i = _varint(buf, i)
        offsets[k] = offsets[k - 1] + lengths[k - 1] if value == 0 and k > 0 else value - 1

    return [_Entry(*row) for row in zip(tile_ids, runs, lengths, offsets, strict=True)]


def _tile_zxy(tile_id: int) -> tuple[int, int, int]:
    """PMTiles tile id to z/x/y.

    Ids run along a Hilbert curve within each zoom, and the zooms are laid end to end,
    so the zoom is whichever level's block the id falls in and the rest is a Hilbert
    distance to invert. The curve is the reason tiles that are near each other on the
    map are near each other in the file, which is what makes a range request useful.
    """
    zoom = 0
    base = 0
    while True:
        span = 1 << (2 * zoom)
        if tile_id < base + span:
            break
        base += span
        zoom += 1

    distance = tile_id - base
    side = 1 << zoom
    x = y = 0
    step = 1
    while step < side:
        rx = 1 & (distance // 2)
        ry = 1 & (distance ^ rx)
        if ry == 0:
            if rx == 1:
                x, y = step - 1 - x, step - 1 - y
            x, y = y, x
        x += step * rx
        y += step * ry
        distance //= 4
        step *= 2
    return zoom, x, y


def _tile_layers(tile: bytes) -> Iterator[tuple[str, int, list[bytes]]]:
    """Each layer as its name, its extent, and one geometry blob per feature.

    The one walker over a tile: counting and drawing differ in what they do with a
    command stream, not in how they reach one, and two walkers over the same wire
    format drift apart silently because each is only exercised by its own caller.
    """
    i = 0
    while i < len(tile):
        key, i = _varint(tile, i)
        field, wire = key >> 3, key & 7
        if field != _TILE_LAYER or wire != 2:
            i = _skip(tile, i, wire)
            continue

        length, i = _varint(tile, i)
        layer_end, j = i + length, i
        # The extent can appear after the features it applies to, so the layer is held
        # until it is finished rather than yielded feature by feature.
        name, extent, geometries = "", _DEFAULT_EXTENT, []
        while j < layer_end:
            key, j = _varint(tile, j)
            field, wire = key >> 3, key & 7
            if field == _LAYER_NAME and wire == 2:
                length, j = _varint(tile, j)
                name = tile[j : j + length].decode("utf-8", "replace")
                j += length
            elif field == _LAYER_EXTENT and wire == 0:
                extent, j = _varint(tile, j)
            elif field == _LAYER_FEATURE and wire == 2:
                length, j = _varint(tile, j)
                feature_end, m = j + length, j
                # Geometry is a packed repeated field, so a writer may split it across
                # chunks that mean nothing apart: the deltas in the second continue
                # from where the first left off.
                parts: list[bytes] = []
                while m < feature_end:
                    key, m = _varint(tile, m)
                    if (key >> 3) == _FEATURE_GEOMETRY and (key & 7) == 2:
                        length, m = _varint(tile, m)
                        parts.append(tile[m : m + length])
                        m += length
                    else:
                        m = _skip(tile, m, key & 7)
                if parts:
                    geometries.append(b"".join(parts))
                j = feature_end
            else:
                j = _skip(tile, j, wire)
        yield name, extent, geometries
        i = layer_end


def _first_point(geometry: bytes) -> tuple[int, int] | None:
    """Where a feature starts, in tile units.

    Every path opens with a MoveTo, and the cursor starts at the tile origin, so the
    pair after the first command is the position and the rest can be ignored.
    """
    if not geometry:
        return None
    command, i = _varint(geometry, 0)
    if (command & 7) != _MOVE_TO or i >= len(geometry):
        return None
    dx, i = _varint(geometry, i)
    dy, i = _varint(geometry, i)
    return _unzigzag(dx), _unzigzag(dy)


def _first_points(tile: bytes) -> Iterator[tuple[int, int, int]]:
    """The first point of every feature in a tile, in tile units, with the extent."""
    for _name, extent, geometries in _tile_layers(tile):
        for geometry in geometries:
            point = _first_point(geometry)
            if point is not None:
                yield point[0], point[1], extent


def _lonlat(z: int, x: int, y: int, px: int, py: int, extent: int) -> tuple[float, float]:
    """A point in tile units back to longitude and latitude, undoing Web Mercator."""
    side = 1 << z
    lon = (x + px / extent) / side * 360.0 - 180.0
    ty = 1 - 2 * (y + py / extent) / side
    return lon, math.degrees(math.atan(math.sinh(math.pi * ty)))


def _all_tiles(fh: BinaryIO) -> tuple[dict[int, list[Tile]], int]:
    """Every tile grouped by zoom, with its z/x/y, and where the tile data starts.

    One pass over the archive's index whatever a caller then asks for, and no tile
    decompressed here. Reading the index per zoom instead costs a national archive a
    walk of its whole directory for every zoom a tile could be at, to answer a question
    about four of them.
    """
    fh.seek(0)
    header = fh.read(_HEADER)
    if len(header) < _HEADER:
        raise RuntimeError("too short to be a PMTiles archive")
    (
        root_offset,
        root_length,
        _metadata_offset,
        _metadata_length,
        leaf_offset,
        _leaf_length,
        tile_offset,
        _tile_length,
    ) = struct.unpack_from(_OFFSETS, header, 8)

    fh.seek(root_offset)
    entries = _read_directory(_decompress(fh.read(root_length)))
    tiles = [e for e in entries if e.run_length]
    # A national archive's root directory does not hold every tile. Missing the leaves
    # reads as an archive that draws almost nothing, at every zoom equally.
    for leaf in (e for e in entries if not e.run_length):
        fh.seek(leaf_offset + leaf.offset)
        tiles += [
            e for e in _read_directory(_decompress(fh.read(leaf.length))) if e.run_length
        ]

    by_zoom: dict[int, list[Tile]] = defaultdict(list)
    for entry in tiles:
        z, x, y = _tile_zxy(entry.tile_id)
        by_zoom[z].append((entry, z, x, y))
    return dict(by_zoom), tile_offset


def _drawn_by_zoom(
    archive: Path, zooms: list[int], cell_size: float
) -> dict[int, dict[Cell, int]]:
    """Count the features drawn at each zoom, per cell, in one pass over the index."""
    out: dict[int, dict[Cell, int]] = {}
    with archive.open("rb") as fh:
        by_zoom, tile_offset = _all_tiles(fh)
        for zoom in zooms:
            counts: dict[Cell, int] = defaultdict(int)
            for entry, z, x, y in by_zoom.get(zoom, []):
                fh.seek(tile_offset + entry.offset)
                for px, py, extent in _first_points(_decompress(fh.read(entry.length))):
                    lon, lat = _lonlat(z, x, y, px, py, extent)
                    counts[(round(lon / cell_size), round(lat / cell_size))] += 1
            out[zoom] = dict(counts)
    return out


def drawn(archive: Path, zoom: int, cell_size: float | None = None) -> dict[Cell, int]:
    """Count the features actually drawn at one zoom, per cell.

    Counting is the measurement with the blind spot -- see the module docstring. Use
    `draw` to judge how a zoom looks.
    """
    return _drawn_by_zoom(archive, [zoom], cell_size or config.COVERAGE_CELL)[zoom]


@dataclass(frozen=True)
class Band:
    """What one zoom draws, against what the maximum zoom draws in the same cells."""

    zoom: int
    features: int
    cells: int
    empty: list[Cell]
    quartiles: list[tuple[int, int]]  # (median features per cell, cells under five)

    @property
    def tilt(self) -> float:
        """The busiest quarter of cells' median over the emptiest quarter's.

        Ireland, which is under every cap and so is not filtered at all, is the shape to
        aim at. What matters is that this figure does not move much between a region
        that is filtered and one that is not.
        """
        sparse = self.quartiles[0][0]
        return self.quartiles[-1][0] / sparse if sparse else float("inf")


def bands(
    archive: Path, zooms: list[int], cell_size: float | None = None
) -> tuple[dict[Cell, int], list[Band]]:
    """Measure each zoom against `config.MAX_ZOOM`, which is the complete network."""
    cell_size = cell_size or config.COVERAGE_CELL
    counted = _drawn_by_zoom(
        archive, list(dict.fromkeys([config.MAX_ZOOM, *zooms])), cell_size
    )
    reference = counted[config.MAX_ZOOM]
    if not reference:
        raise RuntimeError(
            f"{archive} draws nothing at z{config.MAX_ZOOM}. Either it is not a wayfare "
            "archive or its detail band is empty, which no other check would notice."
        )

    ranked = sorted(reference, key=lambda c: (reference[c], c))
    quarter = max(1, len(ranked) // 4)
    groups = [ranked[i * quarter : (i + 1) * quarter] for i in range(3)]
    groups.append(ranked[3 * quarter :])

    out = []
    for zoom in zooms:
        counts = counted[zoom]
        quartiles = []
        for group in groups:
            per_cell = sorted(counts.get(c, 0) for c in group)
            quartiles.append(
                (
                    per_cell[len(per_cell) // 2],
                    sum(1 for n in per_cell if n < 5),
                )
            )
        out.append(
            Band(
                zoom=zoom,
                features=sum(counts.values()),
                cells=len(counts),
                empty=[c for c in reference if c not in counts],
                quartiles=quartiles,
            )
        )
    return reference, out


def report(archive: Path, zooms: list[int], cell_size: float | None = None) -> list[Band]:
    """Log what each zoom draws. Returns the bands so a caller can assert on them."""
    reference, measured = bands(archive, zooms, cell_size)
    log.info(
        "%s draws %d features across %d cells at z%d",
        archive.name,
        sum(reference.values()),
        len(reference),
        config.MAX_ZOOM,
    )
    for band in measured:
        log.info(
            "z%-2d %9d features in %4d cells, %d never drawn; "
            "features per cell, emptiest quarter to busiest: %s; tilt %.1fx",
            band.zoom,
            band.features,
            band.cells,
            len(band.empty),
            " ".join(f"{median}({thin} under 5)" for median, thin in band.quartiles),
            band.tilt,
        )
    return measured


# --- Drawing ---------------------------------------------------------------
#
# Counting says how much an archive holds. Drawing says what it looks like, and the
# two disagree in the direction that matters: a set of roads can be numerous and
# disconnected, which is what a thinned overview looks like on the screen and what no
# count in this module can see.

# Which shade each layer is drawn in. The road layer is the subject; operator geometry
# is dimmer so a tram line is not mistaken for a road that survived a filter.
_SHADES = {"bus": 255, "segments": 90}
_OTHER_SHADE = 160


def _mercator(lon: float, lat: float) -> tuple[float, float]:
    """Longitude and latitude to the 0..1 square tile coordinates live in."""
    s = math.sin(math.radians(lat))
    return (lon + 180.0) / 360.0, 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)


def _paths(geometry: bytes) -> Iterator[list[tuple[int, int]]]:
    """The command stream as paths in tile units.

    A MoveTo opens a path and a LineTo continues it, and the cursor carries across
    both -- every delta is from the previous point rather than from the origin, so
    resetting it per command would scatter every path back to the tile corner.
    """
    i = cx = cy = 0
    path: list[tuple[int, int]] = []
    while i < len(geometry):
        command, i = _varint(geometry, i)
        op, count = command & 7, command >> 3
        if op == _CLOSE:
            if len(path) > 1:
                path.append(path[0])
            continue
        for _ in range(count):
            dx, i = _varint(geometry, i)
            dy, i = _varint(geometry, i)
            cx += _unzigzag(dx)
            cy += _unzigzag(dy)
            if op == _MOVE_TO:
                if len(path) > 1:
                    yield path
                path = [(cx, cy)]
            else:
                path.append((cx, cy))
    if len(path) > 1:
        yield path


def _stroke(
    pixels: bytearray, w: int, h: int, x0: int, y0: int, x1: int, y1: int, shade: int
) -> None:
    """Bresenham, clipped per pixel rather than per line.

    Per pixel because a road that leaves the window still has to be drawn up to the
    edge, and clipping the line first would need the whole Cohen-Sutherland dance for
    a picture that is thrown away after being looked at.
    """
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        if 0 <= x0 < w and 0 <= y0 < h and pixels[y0 * w + x0] < shade:
            pixels[y0 * w + x0] = shade
        if x0 == x1 and y0 == y1:
            return
        step = 2 * err
        if step >= dy:
            err += dy
            x0 += sx
        if step <= dx:
            err += dx
            y0 += sy


def _png(path: Path, width: int, height: int, pixels: bytearray) -> None:
    """Eight-bit greyscale, written by hand.

    No pillow and no numpy, because `art` owns the drawing dependencies and this has
    to run anywhere an archive does -- including inside the pipeline image, which does
    not carry the art extra.
    """
    raw = b"".join(
        b"\x00" + bytes(pixels[y * width : (y + 1) * width]) for y in range(height)
    )

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    with path.open("wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)))
        fh.write(chunk(b"IDAT", zlib.compress(raw, 9)))
        fh.write(chunk(b"IEND", b""))


def draw(
    archive: Path,
    zoom: int,
    bbox: tuple[float, float, float, float],
    out: Path,
    width: int = 1400,
) -> float:
    """Rasterise one zoom of an archive into a window. Returns the fraction lit.

    The fraction of pixels carrying anything is the summary statistic that answers
    what every count in this module cannot: two archives can hold the same number of
    features and light very different amounts of the screen. Comparing that figure
    between two builds of one region, or between a region and one that is not
    filtered, is what a change to the overview has to be judged on.
    """
    west, south, east, north = bbox
    if west >= east or south >= north:
        raise ValueError(f"window {bbox} is empty; wanted west<east and south<north")
    x0, y0 = _mercator(west, north)
    x1, y1 = _mercator(east, south)
    height = max(1, round(width * (y1 - y0) / (x1 - x0)))
    pixels = bytearray(width * height)

    with archive.open("rb") as fh:
        by_zoom, tile_offset = _all_tiles(fh)
        for entry, tz, tx, ty in by_zoom.get(zoom, []):
            scale = 1 << tz
            # A tile's own geometry can run past its edges, so the window is widened
            # by a tile before rejecting one. Rejecting on the exact bounds clips
            # roads that cross into the picture from outside it.
            if (tx + 2) / scale < x0 or (tx - 1) / scale > x1:
                continue
            if (ty + 2) / scale < y0 or (ty - 1) / scale > y1:
                continue
            fh.seek(tile_offset + entry.offset)
            tile = _decompress(fh.read(entry.length))
            for name, extent, geometries in _tile_layers(tile):
                shade = _SHADES.get(name, _OTHER_SHADE)
                for geometry in geometries:
                    for path in _paths(geometry):
                        points = [
                            (
                                round(
                                    ((tx + px / extent) / scale - x0) / (x1 - x0) * width
                                ),
                                round(
                                    ((ty + py / extent) / scale - y0) / (y1 - y0) * height
                                ),
                            )
                            for px, py in path
                        ]
                        # Deliberately not strict: this is a sliding pair over one
                        # list, so the shorter tail is the point.
                        for (ax, ay), (bx, by) in zip(points, points[1:], strict=False):
                            _stroke(pixels, width, height, ax, ay, bx, by, shade)

    _png(out, width, height, pixels)
    lit = sum(1 for p in pixels if p) / len(pixels)
    log.info(
        "%s z%d over %s -> %s, %dx%d, %.1f%% lit",
        archive.name,
        zoom,
        bbox,
        out,
        width,
        height,
        100 * lit,
    )
    return lit


def sizes(archive: Path) -> dict[int, list[int]]:
    """Every tile's stored size, per zoom, read from the directory alone.

    No tile is decompressed, so this is a pass over the index rather than the file.
    The sizes are what PMTiles stores, which is what a client fetches over a range
    request and what tippecanoe's limit is applied to.
    """
    with archive.open("rb") as fh:
        by_zoom, _ = _all_tiles(fh)
    return {
        zoom: [entry.length for entry, _, _, _ in by_zoom[zoom]] for zoom in sorted(by_zoom)
    }


def report_sizes(archive: Path, limit: int | None = None) -> dict[int, int]:
    """Log how much of the per-tile budget each zoom is using. Returns the maxima.

    The gap between the median and the maximum is the thing to read. Great Britain's
    median z11 tile is 3 KB against a 308 KB worst case, so the budget is set by a
    handful of tiles over central London while the rest of the country has two orders
    of magnitude spare -- detail added everywhere is priced by the worst tile, and
    detail added only where there is room is nearly free.

    A zoom sitting just under the limit is not headroom. It means
    `--drop-densest-as-needed` pushed it there, and the only way to give that zoom
    more is to raise `config.MAX_TILE_BYTES`.
    """
    limit = limit or config.MAX_TILE_BYTES
    per = sizes(archive)
    log.info("%s: tile sizes against the %d KB limit", archive.name, round(limit / 1024))
    peaks = {}
    for zoom in sorted(per):
        v = sorted(per[zoom])
        peaks[zoom] = v[-1]
        log.info(
            "z%-2d %6d tiles  total %6.1f MB  median %5.0f KB  max %5.0f KB  %5.2fx spare",
            zoom,
            len(v),
            sum(v) / 1e6,
            v[len(v) // 2] / 1024,
            v[-1] / 1024,
            limit / max(v[-1], 1),
        )
    return peaks
