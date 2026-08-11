"""What a finished archive actually draws, and where.

Every version of the low-zoom filter that reached the map passed the checks that were
being run on it. Feature counts per zoom went up, no populated cell was emptied, tile
sizes were under the limit -- and the map still showed cities in a black field, because
a cell holding one road counts the same as a cell holding eighty.

So this reads the archive rather than the export: it walks the PMTiles directory, pulls
each tile's Mapbox Vector Tile blob, decodes the first point of every feature back to
longitude and latitude, and counts what is drawn per cell per zoom. Comparing a low zoom
against the maximum zoom, split by how much each cell holds there, is what distinguishes
a map that is evenly thinner from one with holes in it.

Nothing here writes, and nothing here needs the database or the export -- an archive on
its own is the whole input, so a published file can be checked wherever it ended up.

Only the first point of each feature is decoded. A feature is a coalesced run along one
OSM way, tens to hundreds of metres long, and the cell is 0.25 degrees; putting a segment
in the cell its first vertex falls in is the same rule `publish` uses to allocate the
quota, so the two measurements are counting the same thing.
"""

from __future__ import annotations

import gzip
import math
import struct
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from . import config, logs

log = logs.get("coverage")

Cell = tuple[int, int]

# PMTiles v3: a 127-byte header, then a root directory, then optional leaf directories,
# then the tile data. The eight offsets and lengths this needs start at byte 8.
_HEADER = 127
_OFFSETS = "<QQQQQQQQ"

# MVT protobuf field numbers. Only the ones on the path to a feature's first point are
# named; everything else is skipped by wire type.
_TILE_LAYER = 3
_LAYER_FEATURE = 2
_LAYER_EXTENT = 5
_FEATURE_GEOMETRY = 4
_MOVE_TO = 1
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


def _first_points(tile: bytes) -> Iterator[tuple[int, int, int]]:
    """The first point of every feature in a tile, in tile units, with the extent.

    Geometry is a command stream of zigzag-encoded deltas, and every LineString opens
    with a MoveTo whose delta is from the tile origin. So the first pair after the first
    command is the feature's position, and the rest of the stream can be stepped over.
    """
    i = 0
    while i < len(tile):
        key, i = _varint(tile, i)
        field, wire = key >> 3, key & 7
        if field != _TILE_LAYER or wire != 2:
            i = _skip(tile, i, wire)
            continue

        length, i = _varint(tile, i)
        layer_end = i + length
        # The extent can appear after the features it applies to, so the layer's points
        # are held until the layer is finished.
        points: list[tuple[int, int]] = []
        extent = _DEFAULT_EXTENT
        j = i
        while j < layer_end:
            key, j = _varint(tile, j)
            field, wire = key >> 3, key & 7
            if field == _LAYER_EXTENT and wire == 0:
                extent, j = _varint(tile, j)
            elif field == _LAYER_FEATURE and wire == 2:
                length, j = _varint(tile, j)
                point, j = _feature_point(tile, j, j + length)
                if point is not None:
                    points.append(point)
            else:
                j = _skip(tile, j, wire)
        yield from ((px, py, extent) for px, py in points)
        i = layer_end


def _feature_point(tile: bytes, i: int, end: int) -> tuple[tuple[int, int] | None, int]:
    point: tuple[int, int] | None = None
    while i < end:
        key, i = _varint(tile, i)
        field, wire = key >> 3, key & 7
        if field != _FEATURE_GEOMETRY or wire != 2:
            i = _skip(tile, i, wire)
            continue
        length, i = _varint(tile, i)
        geometry_end = i + length
        if point is None and i < geometry_end:
            command, i = _varint(tile, i)
            if (command & 7) == _MOVE_TO and i < geometry_end:
                dx, i = _varint(tile, i)
                dy, i = _varint(tile, i)
                point = (_unzigzag(dx), _unzigzag(dy))
        i = geometry_end
    return point, end


def _unzigzag(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def _lonlat(z: int, x: int, y: int, px: int, py: int, extent: int) -> tuple[float, float]:
    """A point in tile units back to longitude and latitude, undoing Web Mercator."""
    side = 1 << z
    lon = (x + px / extent) / side * 360.0 - 180.0
    ty = 1 - 2 * (y + py / extent) / side
    return lon, math.degrees(math.atan(math.sinh(math.pi * ty)))


def drawn(archive: Path, zoom: int, cell_size: float | None = None) -> dict[Cell, int]:
    """Count the features actually drawn at one zoom, per cell.

    Reads the whole archive's directory but only the tiles at this zoom, so checking
    four zooms of a 127 MB national archive is four passes over its tile index rather
    than four decompressions of the whole file.
    """
    cell_size = cell_size or config.OVERVIEW_CELL
    counts: dict[Cell, int] = defaultdict(int)
    with archive.open("rb") as fh:
        header = fh.read(_HEADER)
        if len(header) < _HEADER:
            raise RuntimeError(f"{archive} is too short to be a PMTiles archive")
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
        for leaf in (e for e in entries if not e.run_length):
            fh.seek(leaf_offset + leaf.offset)
            leaves = _read_directory(_decompress(fh.read(leaf.length)))
            tiles += [e for e in leaves if e.run_length]

        for entry in tiles:
            z, x, y = _tile_zxy(entry.tile_id)
            if z != zoom:
                continue
            fh.seek(tile_offset + entry.offset)
            for px, py, extent in _first_points(_decompress(fh.read(entry.length))):
                lon, lat = _lonlat(z, x, y, px, py, extent)
                counts[(round(lon / cell_size), round(lat / cell_size))] += 1
    return dict(counts)


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
    cell_size = cell_size or config.OVERVIEW_CELL
    reference = drawn(archive, config.MAX_ZOOM, cell_size)
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
        counts = drawn(archive, zoom, cell_size)
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
