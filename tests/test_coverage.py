"""The archive reader that tells a thinner map from a map with holes in it.

Everything here builds its own PMTiles by hand rather than running tippecanoe, so the
tests say what the format is as much as they check the reader against it.
"""

from __future__ import annotations

import gzip
import struct

import pytest

from wayfare import config, coverage


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def zigzag(n: int) -> int:
    return (n << 1) ^ (n >> 31)


def key(field: int, wire: int) -> bytes:
    return varint((field << 3) | wire)


def blob(field: int, payload: bytes) -> bytes:
    return key(field, 2) + varint(len(payload)) + payload


def mvt(points: list[tuple[int, int]], extent: int = 4096) -> bytes:
    """One layer holding one LineString per point, each starting there."""
    features = b""
    for px, py in points:
        # MoveTo one point, then LineTo one more, which is the shortest real line.
        geometry = b"".join(
            varint(v)
            for v in (
                (1 << 3) | 1,
                zigzag(px),
                zigzag(py),
                (1 << 3) | 2,
                zigzag(4),
                zigzag(4),
            )
        )
        features += blob(2, key(3, 0) + varint(2) + blob(4, geometry))
    layer = blob(1, b"bus") + features + key(5, 0) + varint(extent) + key(15, 0) + varint(2)
    return blob(3, layer)


def directory(entries: list[tuple[int, int, int, int]]) -> bytes:
    """(tile_id, run_length, length, offset), in the four-array form PMTiles uses."""
    out = varint(len(entries))
    last = 0
    for tile_id, _, _, _ in entries:
        out += varint(tile_id - last)
        last = tile_id
    for _, run, _, _ in entries:
        out += varint(run)
    for _, _, length, _ in entries:
        out += varint(length)
    for _, _, _, offset in entries:
        out += varint(offset + 1)
    return out


def archive(path, tiles: dict[int, bytes], leaves: bool = False):
    """A PMTiles v3 file: header, root directory, leaf directories, tile data."""
    data = b""
    entries = []
    for tile_id, tile in sorted(tiles.items()):
        entries.append((tile_id, 1, len(tile), len(data)))
        data += tile

    root = gzip.compress(directory(entries))
    leaf_blob = b""
    if leaves:
        # One leaf holding every entry, and a root that only points at it. run_length
        # zero is what marks the pointer.
        leaf_blob = gzip.compress(directory(entries))
        root = gzip.compress(directory([(entries[0][0], 0, len(leaf_blob), 0)]))

    root_offset = coverage._HEADER
    leaf_offset = root_offset + len(root)
    tile_offset = leaf_offset + len(leaf_blob)

    header = bytearray(b"PMTiles" + bytes([3]) + bytes(coverage._HEADER - 8))
    struct.pack_into(
        coverage._OFFSETS,
        header,
        8,
        root_offset,
        len(root),
        0,
        0,
        leaf_offset,
        len(leaf_blob),
        tile_offset,
        len(data),
    )
    path.write_bytes(bytes(header) + root + leaf_blob + data)
    return path


def at(lon: float, lat: float, extent: int = 4096) -> tuple[int, int]:
    """Where a point falls inside the single z0 tile, in tile units."""
    import math

    x = (lon + 180.0) / 360.0 * extent
    sin = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + sin) / (1 - sin)) / (4 * math.pi)) * extent
    return round(x), round(y)


def test_a_feature_comes_back_in_the_cell_it_was_drawn_in(tmp_path):
    """The whole reader in one pass: header, directory, tile, geometry, projection."""
    path = archive(tmp_path / "a.pmtiles", {0: gzip.compress(mvt([at(-3.0, 51.5)]))})
    drawn = coverage.drawn(path, 0, 0.25)
    assert drawn == {(round(-3.0 / 0.25), round(51.5 / 0.25)): 1}


def test_every_feature_in_a_tile_is_counted(tmp_path):
    path = archive(
        tmp_path / "a.pmtiles",
        {0: gzip.compress(mvt([at(-3.0, 51.5), at(-3.01, 51.5), at(9.0, 53.0)]))},
    )
    drawn = coverage.drawn(path, 0, 0.25)
    assert drawn[(round(-3.0 / 0.25), round(51.5 / 0.25))] == 2
    assert drawn[(round(9.0 / 0.25), round(53.0 / 0.25))] == 1


def test_a_leaf_directory_is_followed(tmp_path):
    """A national archive's root directory does not hold every tile. Missing the leaves
    would read as an archive that draws almost nothing, at every zoom equally."""
    path = archive(
        tmp_path / "a.pmtiles", {0: gzip.compress(mvt([at(-3.0, 51.5)]))}, leaves=True
    )
    assert sum(coverage.drawn(path, 0, 0.25).values()) == 1


def test_a_layer_that_declares_its_own_extent_is_read_at_that_extent(tmp_path):
    """The extent can be written after the features it applies to, so reading it as it
    arrives puts every feature in that layer at the wrong place."""
    path = archive(
        tmp_path / "a.pmtiles", {0: gzip.compress(mvt([at(-3.0, 51.5, 8192)], 8192))}
    )
    assert coverage.drawn(path, 0, 0.25) == {(round(-3.0 / 0.25), round(51.5 / 0.25)): 1}


def test_an_uncompressed_tile_reads_too(tmp_path):
    path = archive(tmp_path / "a.pmtiles", {0: mvt([at(-3.0, 51.5)])})
    assert sum(coverage.drawn(path, 0, 0.25).values()) == 1


def test_only_the_zoom_asked_for_is_counted(tmp_path):
    """z0 is tile 0 and the four z1 tiles are 1 to 4."""
    path = archive(
        tmp_path / "a.pmtiles",
        {
            0: gzip.compress(mvt([at(-3.0, 51.5)])),
            1: gzip.compress(mvt([(1, 1), (2, 2)])),
        },
    )
    assert sum(coverage.drawn(path, 0, 0.25).values()) == 1
    assert sum(coverage.drawn(path, 1, 0.25).values()) == 2
    assert coverage.drawn(path, 5, 0.25) == {}


def test_the_zoom_blocks_are_laid_end_to_end():
    """Tile ids run along a Hilbert curve within a zoom, and the zooms follow one
    another, so the id alone says which zoom a tile belongs to."""
    assert coverage._tile_zxy(0) == (0, 0, 0)
    assert {coverage._tile_zxy(i) for i in (1, 2, 3, 4)} == {
        (1, 0, 0),
        (1, 0, 1),
        (1, 1, 0),
        (1, 1, 1),
    }
    assert coverage._tile_zxy(5)[0] == 2
    assert coverage._tile_zxy(21)[0] == 3


def test_an_offset_of_zero_means_straight_after_the_one_before():
    """How a run of adjacent tiles costs one byte each. Reading it as a real offset
    stacks every tile in the run on top of the first."""
    entries = coverage._read_directory(directory([(0, 1, 10, 0), (1, 1, 20, -1)]))
    assert [e.offset for e in entries] == [0, 10]
    assert [e.length for e in entries] == [10, 20]


def test_an_archive_that_draws_nothing_at_max_zoom_is_an_error(tmp_path):
    """A detail band that came out empty is the one failure a per-zoom comparison
    cannot report, because there is nothing left to compare against."""
    path = archive(tmp_path / "a.pmtiles", {0: gzip.compress(mvt([at(-3.0, 51.5)]))})
    with pytest.raises(RuntimeError, match="draws nothing"):
        coverage.bands(path, [0])


def test_the_tilt_is_what_a_hole_shows_up_in(monkeypatch, tmp_path):
    """Both filters that reached the map drew every populated cell and carried more
    features than the build before them. What separates them is how much the emptiest
    quarter of the country draws against the busiest."""
    reference = {(i, 0): 100 + i for i in range(8)}

    def fake(_archive, zoom, _cell=None):
        if zoom == config.MAX_ZOOM:
            return reference
        # Thin the sparse half hard and the dense half barely, which is what a quota
        # shared out in proportion to cell size does.
        return {c: (2 if c[0] < 4 else 90) for c in reference}

    monkeypatch.setattr(coverage, "drawn", fake)
    _, bands = coverage.bands(tmp_path / "a.pmtiles", [5])
    assert bands[0].empty == []
    assert bands[0].quartiles[0][0] == 2
    assert bands[0].quartiles[-1][0] == 90
    assert bands[0].tilt == 45.0
