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
import itertools
import math
import struct
import zlib
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from . import config, logs, palette

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
_LAYER_KEY = 3
_LAYER_VALUE = 4
_LAYER_EXTENT = 5
_FEATURE_TAGS = 2
_FEATURE_GEOMETRY = 4

# A `Value` is a one-of, so exactly one of these is present. The numeric kinds are
# all read as a Python number and the caller is left to decide what it wanted: a
# trip count is written as an int64 by one tippecanoe and a uint64 by another, and
# nothing downstream cares which.
_VALUE_STRING = 1
_VALUE_FLOAT = 2
_VALUE_DOUBLE = 3
_VALUE_INT = 4
_VALUE_UINT = 5
_VALUE_SINT = 6
_VALUE_BOOL = 7

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


def _value(buf: bytes, start: int, end: int) -> str | float | bool | None:
    """One `Value` message, whichever of its seven kinds it holds."""
    i = start
    while i < end:
        key, i = _varint(buf, i)
        field, wire = key >> 3, key & 7
        if field == _VALUE_STRING and wire == 2:
            length, i = _varint(buf, i)
            return buf[i : i + length].decode("utf-8", "replace")
        if field == _VALUE_DOUBLE and wire == 1:
            return float(struct.unpack_from("<d", buf, i)[0])
        if field == _VALUE_FLOAT and wire == 5:
            return float(struct.unpack_from("<f", buf, i)[0])
        if field in (_VALUE_INT, _VALUE_UINT) and wire == 0:
            return float(_varint(buf, i)[0])
        if field == _VALUE_SINT and wire == 0:
            return float(_unzigzag(_varint(buf, i)[0]))
        if field == _VALUE_BOOL and wire == 0:
            return bool(_varint(buf, i)[0])
        i = _skip(buf, i, wire)
    return None


@dataclass(frozen=True)
class TileLayer:
    """One layer of one tile, with its features' tags left undecoded.

    Tags are held as the index pairs the wire format writes rather than as a dict
    per feature. A national archive is 6.3M features and the drawing below wants
    one attribute out of each, so building the other five into a dict costs a walk
    of the whole archive to throw most of it away. `attribute` resolves a name to
    its key index once per layer, and each feature is then a scan of a short list.
    """

    name: str
    extent: int
    keys: list[str]
    values: list[str | float | bool | None]
    features: list[tuple[bytes, list[int]]]

    def attribute(self, name: str) -> Callable[[list[int]], str | float | bool | None]:
        """A reader for one attribute, or one that always answers None.

        A layer that does not carry the attribute at all is the ordinary case, not
        an error: `trips` is absent from an archive published before it was taken
        out of `_DETAIL_ONLY`, and `mode` from a track layer written before the
        traced modes joined it.
        """
        if name not in self.keys:
            return lambda _tags: None
        wanted = self.keys.index(name)

        def read(tags: list[int]) -> str | float | bool | None:
            for k in range(0, len(tags) - 1, 2):
                if tags[k] == wanted:
                    return self.values[tags[k + 1]]
            return None

        return read


def _tile_layers(tile: bytes) -> Iterator[TileLayer]:
    """Each layer of a tile, with its features' geometry and tags.

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
        # The extent, and the key and value tables, can all appear after the
        # features that refer to them, so the layer is held until it is finished
        # rather than yielded feature by feature.
        name, extent = "", _DEFAULT_EXTENT
        keys: list[str] = []
        values: list[str | float | bool | None] = []
        features: list[tuple[bytes, list[int]]] = []
        while j < layer_end:
            key, j = _varint(tile, j)
            field, wire = key >> 3, key & 7
            if field == _LAYER_NAME and wire == 2:
                length, j = _varint(tile, j)
                name = tile[j : j + length].decode("utf-8", "replace")
                j += length
            elif field == _LAYER_KEY and wire == 2:
                length, j = _varint(tile, j)
                keys.append(tile[j : j + length].decode("utf-8", "replace"))
                j += length
            elif field == _LAYER_VALUE and wire == 2:
                length, j = _varint(tile, j)
                values.append(_value(tile, j, j + length))
                j += length
            elif field == _LAYER_EXTENT and wire == 0:
                extent, j = _varint(tile, j)
            elif field == _LAYER_FEATURE and wire == 2:
                length, j = _varint(tile, j)
                feature_end, m = j + length, j
                # Geometry and tags are both packed repeated fields, so a writer may
                # split either across chunks that mean nothing apart: the deltas in
                # the second continue from where the first left off.
                parts: list[bytes] = []
                tags: list[int] = []
                while m < feature_end:
                    key, m = _varint(tile, m)
                    field, wire = key >> 3, key & 7
                    if field == _FEATURE_GEOMETRY and wire == 2:
                        length, m = _varint(tile, m)
                        parts.append(tile[m : m + length])
                        m += length
                    elif field == _FEATURE_TAGS and wire == 2:
                        length, m = _varint(tile, m)
                        chunk_end = m + length
                        while m < chunk_end:
                            tag, m = _varint(tile, m)
                            tags.append(tag)
                    else:
                        m = _skip(tile, m, wire)
                if parts:
                    features.append((b"".join(parts), tags))
                j = feature_end
            else:
                j = _skip(tile, j, wire)
        yield TileLayer(name, extent, keys, values, features)
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
    for layer in _tile_layers(tile):
        for geometry, _tags in layer.features:
            point = _first_point(geometry)
            if point is not None:
                yield point[0], point[1], layer.extent


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

# Which grey each layer is drawn in when no palette is asked for. The road layer is
# the subject; operator geometry and relation track are dimmer, so a tram line is not
# mistaken for a road that survived a filter.
#
# Keyed on `palette.load().layers` rather than on three strings written here: the
# names are tippecanoe's, and a fourth layer added to `publish` used to arrive as
# `_OTHER_GREY` with nothing to say it had. `track` did exactly that.
_GREYS = {"road": 255, "segments": 90, "track": 160}
_OTHER_GREY = 160

# The pixel buffer is red, green, blue and a compositing weight. The fourth channel
# is not drawn: it holds what the pixel was last claimed by, so a crossing decides
# which feature keeps it, and it doubles as the "is this pixel lit at all" flag that
# a colour of (0, 0, 0) could not answer for on its own.
_CHANNELS = 4
_WEIGHT = 3

# What an unlit pixel is. The themes are the viewer's grounds rather than its panel
# colours: a render is the map without the page around it.
_BACKGROUND: dict[palette.Theme, tuple[int, int, int]] = {
    "light": (255, 255, 255),
    "dark": (13, 16, 20),
}


def _layer_greys() -> dict[str, int]:
    """The grey per source-layer name, resolved through the shared layer names."""
    layers = palette.load().layers
    return {layers[role]: grey for role, grey in _GREYS.items()}


def _layer_ranks() -> dict[str, int]:
    """Which layer wins a pixel, in the order the viewer stacks them.

    Road underneath, then relation track, then operator geometry: a tram line and
    the road it runs beside are two features over the same ground, and the one the
    viewer puts on top is the one this has to put on top.
    """
    layers = palette.load().layers
    return {layers["road"]: 0, layers["track"]: 1, layers["segments"]: 2}


# How much of the weight byte a layer claims. Three ranks and a fourth for a layer
# this does not know, each with 63 levels of trip count under it, which is finer
# than a six-step ramp can show.
#
# Every drawn weight is offset, because zero is what an untouched pixel holds and
# the same channel answers "is anything here at all". Without the offset the
# quietest road of the bottom layer weighs nothing, and a national render came out
# 0.2% lit against the greyscale's 5.1% -- the roads were drawn and then counted as
# background.
_RANK_STEP = 63
_MIN_WEIGHT = 2

# Weight 1 is the underlay's, below every feature and above nothing. A coastline
# is context and must never take a pixel from a road: the quietest road on the
# bottom layer weighs `_MIN_WEIGHT`, which is why features start at two.
_UNDERLAY_WEIGHT = 1

# What the underlay is drawn in. Dim enough to read as ground rather than as
# something running on it -- a coastline in a road's colour is a coastal service
# nobody operates. Not in `map.toml`: the viewer has no coastline of its own,
# because its basemap draws one, so this is a colour no other reader shares.
_COASTLINE: dict[palette.Theme, tuple[int, int, int]] = {
    "light": (198, 204, 212),
    "dark": (38, 46, 58),
}
_COASTLINE_GREY = (60, 60, 60)


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
    pixels: bytearray,
    w: int,
    h: int,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    rgb: tuple[int, int, int],
    weight: int,
    pen: int = 1,
) -> None:
    """Bresenham, clipped per pixel rather than per line.

    Per pixel because a road that leaves the window still has to be drawn up to the
    edge, and clipping the line first would need the whole Cohen-Sutherland dance for
    a picture that is thrown away after being looked at.

    Where two lines cross, the heavier one keeps the pixel whole rather than the two
    being mixed per channel. A blend of a rail colour and a road colour is a third
    hue that names neither mode, which is the one thing the mode palette exists to
    prevent -- so the pixel states one of them, and `weight` decides which. The
    greyscale path passes its shade as the weight, which is the brighter-wins rule
    this had before there was any colour in it.

    `pen` is the square nib in buffer pixels, and is the supersampling factor when
    the buffer is a supersampled one: a line has to stay one *output* pixel wide, or
    drawing at three times the size and averaging back down leaves every road at a
    third of its ink. It is centred on the run, so a pen of one is the single-pixel
    line this drew before there was a nib at all.
    """
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    red, green, blue = rgb
    nib = range(-(pen // 2), -(pen // 2) + pen)
    while True:
        for oy in nib:
            py = y0 + oy
            if not 0 <= py < h:
                continue
            row = py * w
            for ox in nib:
                px = x0 + ox
                if not 0 <= px < w:
                    continue
                at = (row + px) * _CHANNELS
                if pixels[at + _WEIGHT] <= weight:
                    pixels[at] = red
                    pixels[at + 1] = green
                    pixels[at + 2] = blue
                    pixels[at + _WEIGHT] = weight
        if x0 == x1 and y0 == y1:
            return
        step = 2 * err
        if step >= dy:
            err += dy
            x0 += sx
        if step <= dx:
            err += dx
            y0 += sy


def _resolve(
    pixels: bytearray,
    width: int,
    height: int,
    background: tuple[int, int, int],
    supersample: int = 1,
) -> tuple[bytes, int]:
    """The buffer as PNG scanlines, and how many output pixels carry ink.

    The fourth channel of the buffer is the compositing weight and is not written:
    PNG has no room for it and nothing downstream reads it. Unlit pixels take
    `background`, which is where a dark render gets its ground.

    Above one, `supersample` is where the antialiasing happens: an output pixel is
    the mean of the s * s buffer pixels under it, with the unlit ones counted as
    background, so a diagonal road comes out as a graded edge rather than a stair.
    Averaging here rather than while drawing is what keeps the compositing rule
    intact -- every buffer pixel still states one feature whole, and a mixed hue can
    only appear where two of them fall inside one output pixel, which is the width
    of the blend antialiasing is.

    The count is of output pixels rather than buffer pixels, so the lit fraction
    means the same thing at any supersampling: a road covers the same share of the
    picture whether or not it was drawn large and shrunk.
    """
    stride = width * _CHANNELS
    rows = []
    lit = 0
    if supersample == 1:
        for y in range(height):
            row = bytearray(b"\x00")
            base = y * stride
            for x in range(width):
                at = base + x * _CHANNELS
                if pixels[at + _WEIGHT]:
                    row += pixels[at : at + 3]
                    lit += 1
                else:
                    row += bytes(background)
            rows.append(bytes(row))
        return b"".join(rows), lit

    s = supersample
    area = s * s
    ground = bytes(background)
    back_r, back_g, back_b = background
    for y in range(height // s):
        row = bytearray(b"\x00")
        bases = [(y * s + dy) * stride for dy in range(s)]
        for x in range(width // s):
            left = x * s * _CHANNELS
            red = green = blue = covered = 0
            for base in bases:
                at = base + left
                for _ in range(s):
                    if pixels[at + _WEIGHT]:
                        red += pixels[at]
                        green += pixels[at + 1]
                        blue += pixels[at + 2]
                        covered += 1
                    at += _CHANNELS
            if not covered:
                row += ground
                continue
            lit += 1
            bare = area - covered
            row += bytes(
                (
                    (red + back_r * bare) // area,
                    (green + back_g * bare) // area,
                    (blue + back_b * bare) // area,
                )
            )
        rows.append(bytes(row))
    return b"".join(rows), lit


def _png(path: Path, width: int, height: int, raw: bytes) -> None:
    """Eight-bit truecolour, written by hand.

    No pillow and no numpy, because `art` owns the drawing dependencies and this has
    to run anywhere an archive does -- including inside the pipeline image, which does
    not carry the art extra.
    """

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    with path.open("wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)))
        fh.write(chunk(b"IDAT", zlib.compress(raw, 9)))
        fh.write(chunk(b"IEND", b""))


def _feature_colour(
    layer: TileLayer, theme: palette.Theme
) -> Callable[[list[int]], tuple[tuple[int, int, int], int]]:
    """How one layer's features are coloured, and which wins where two cross.

    The road layer is read off the road ramp by journeys a day, and the other two
    off their own mode's ramp -- the same two rules the viewer paints by, out of the
    same file. A feature with no `trips` takes the flat middle of its mode's ramp on
    the non-road layers and the "no answer" grey on the road one, again as the
    viewer does.

    The weight decides a crossing, and it is the layer first and the trip count
    only within a layer -- which is the order the viewer stacks its layers in, road
    underneath, then track, then operator geometry on top. Ranking by trips alone
    would put a trunk road over the tram line that crosses it, and the viewer draws
    that the other way round.
    """
    ink = palette.load()
    names = ink.layers
    read_trips = layer.attribute(ink.ramp_needs)
    read_mode = layer.attribute("mode")
    road = layer.name == names["road"]
    ranks = _layer_ranks()
    rank = ranks.get(layer.name, len(ranks))

    def colour(tags: list[int]) -> tuple[tuple[int, int, int], int]:
        raw = read_trips(tags)
        trips = float(raw) if isinstance(raw, int | float) else None
        # The low bits of the weight, so a busier feature of one layer wins a
        # pixel from a quieter one, and no feature of a lower layer wins at all.
        within = round(ink.position(trips) * (_RANK_STEP - 1))
        weight = _MIN_WEIGHT + rank * _RANK_STEP + within
        if road:
            return ink.road_rgb(theme, trips), weight
        mode = read_mode(tags)
        name = mode if isinstance(mode, str) else ink.track_default_mode
        return ink.mode_rgb(theme, name, trips), weight

    return colour


def layer_attributes(archive: Path, zoom: int, sample: int = 64) -> dict[str, set[str]]:
    """Which attributes each layer carries at one zoom, off a sample of its tiles.

    What this answers is whether a render can colour by an attribute at all. A
    paint that reads one the band does not carry is not an error anything reports:
    the viewer falls back to a flat grey and so does `draw`, and a whole country in
    one colour reads as a region with no buses rather than as a stale archive.

    A sample because the answer is a property of the band rather than of a tile,
    and walking 53,633 tiles of z14 to learn what the first few already said is a
    minute spent to reach the same set.
    """
    found: dict[str, set[str]] = {}
    with archive.open("rb") as fh:
        by_zoom, tile_offset = _all_tiles(fh)
        for entry, _tz, _tx, _ty in by_zoom.get(zoom, [])[:sample]:
            fh.seek(tile_offset + entry.offset)
            for layer in _tile_layers(_decompress(fh.read(entry.length))):
                found.setdefault(layer.name, set()).update(layer.keys)
    return found


def draw(
    archives: Path | Sequence[Path],
    zoom: int,
    bbox: tuple[float, float, float, float],
    out: Path,
    width: int = 1400,
    theme: palette.Theme | None = None,
    underlay: Sequence[Sequence[tuple[float, float]]] | None = None,
    supersample: int = 1,
) -> float:
    """Rasterise one zoom of some archives into a window. Returns the fraction lit.

    The fraction of pixels carrying anything is the summary statistic that answers
    what every count in this module cannot: two archives can hold the same number of
    features and light very different amounts of the screen. Comparing that figure
    between two builds of one region, or between a region and one that is not
    filtered, is what a change to the overview has to be judged on.

    Several archives because a region is one archive and these islands are three,
    and the viewer draws every archive it is offered onto one map -- a picture of
    what it draws has to do the same or it is a picture of one region. They are
    composited into one buffer in the order given, so a later archive wins a pixel
    only by carrying more trips over it.

    `theme` paints the viewer's own colours instead of the diagnostic greys: the
    road ramp by journeys a day, and each non-road mode off its own ramp. Left
    None, this is the flat greyscale that judging a low zoom wants, where a hue
    would say something about a feature that the question is not about.

    `underlay` is longitude/latitude polylines drawn under everything, for a
    coastline. Without one the only thing saying where the land is, is where the
    buses are, so Kerry and Cornwall read as ink rather than as places. It is
    passed in rather than loaded here because this module reads archives and
    nothing else -- no network, no data files -- which is what lets it run
    wherever an archive does.

    `supersample` draws into a buffer that many times wider and taller and averages
    it back down, which is the whole of the antialiasing: every line here is one
    pixel of a hand-written Bresenham, so at 1 a diagonal road is a staircase, which
    is unreadable in a picture anybody looks at rather than measures. It costs the
    square of itself in memory, and much less than that in time -- these islands at
    z11 go from 15s to 42s at 6, since about 8s of either is reading the band and
    parsing its features, which the drawing happens after. 1 stays the default that
    every diagnostic call takes, and a picture asks for more.
    """
    # One archive is the ordinary diagnostic call and reads better without a list
    # around it, so both spellings are taken rather than one being the only one.
    archives = [archives] if isinstance(archives, Path) else list(archives)
    if not archives:
        raise ValueError("no archives to draw")
    if supersample < 1:
        raise ValueError(f"supersample {supersample} is under one")
    west, south, east, north = bbox
    if west >= east or south >= north:
        raise ValueError(f"window {bbox} is empty; wanted west<east and south<north")
    x0, y0 = _mercator(west, north)
    x1, y1 = _mercator(east, south)
    height = max(1, round(width * (y1 - y0) / (x1 - x0)))
    # The picture is `width` by `height`; everything below this draws into the
    # buffer, which is that times the supersampling in each direction.
    buf_w, buf_h = width * supersample, height * supersample
    pixels = bytearray(buf_w * buf_h * _CHANNELS)
    greys = _layer_greys()

    # First, so every feature composites over it: the weight ordering would hold
    # either way, but drawing ground after the things standing on it is the kind
    # of ordering that survives until someone changes a weight.
    if underlay:
        ink = _COASTLINE[theme] if theme else _COASTLINE_GREY
        for line in underlay:
            points = [
                (
                    round((mx - x0) / (x1 - x0) * buf_w),
                    round((my - y0) / (y1 - y0) * buf_h),
                )
                for mx, my in (_mercator(lon, lat) for lon, lat in line)
            ]
            for (ax, ay), (bx, by) in itertools.pairwise(points):
                _stroke(
                    pixels,
                    buf_w,
                    buf_h,
                    ax,
                    ay,
                    bx,
                    by,
                    ink,
                    _UNDERLAY_WEIGHT,
                    supersample,
                )

    for archive in archives:
        with archive.open("rb") as fh:
            by_zoom, tile_offset = _all_tiles(fh)
            for entry, tz, tx, ty in by_zoom.get(zoom, []):
                scale = 1 << tz
                # A tile's own geometry can run past its edges, so the window is
                # widened by a tile before rejecting one. Rejecting on the exact
                # bounds clips roads that cross into the picture from outside it.
                if (tx + 2) / scale < x0 or (tx - 1) / scale > x1:
                    continue
                if (ty + 2) / scale < y0 or (ty - 1) / scale > y1:
                    continue
                fh.seek(tile_offset + entry.offset)
                tile = _decompress(fh.read(entry.length))
                for layer in _tile_layers(tile):
                    extent = layer.extent
                    grey = greys.get(layer.name, _OTHER_GREY)
                    colour = _feature_colour(layer, theme) if theme else None
                    for geometry, tags in layer.features:
                        rgb, weight = colour(tags) if colour else ((grey, grey, grey), grey)
                        for path in _paths(geometry):
                            points = [
                                (
                                    round(
                                        ((tx + px / extent) / scale - x0)
                                        / (x1 - x0)
                                        * buf_w
                                    ),
                                    round(
                                        ((ty + py / extent) / scale - y0)
                                        / (y1 - y0)
                                        * buf_h
                                    ),
                                )
                                for px, py in path
                            ]
                            # Deliberately not strict: this is a sliding pair over
                            # one list, so the shorter tail is the point.
                            for (ax, ay), (bx, by) in itertools.pairwise(points):
                                _stroke(
                                    pixels,
                                    buf_w,
                                    buf_h,
                                    ax,
                                    ay,
                                    bx,
                                    by,
                                    rgb,
                                    weight,
                                    supersample,
                                )

    background = _BACKGROUND[theme] if theme else (0, 0, 0)
    raw, drawn = _resolve(pixels, buf_w, buf_h, background, supersample)
    _png(out, width, height, raw)
    lit = drawn / (width * height)
    log.info(
        "%s z%d over %s -> %s, %dx%d, %.1f%% lit",
        ", ".join(a.name for a in archives),
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
