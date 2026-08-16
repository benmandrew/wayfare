"""The archive reader that tells a thinner map from a map with holes in it.

Everything here builds its own PMTiles by hand rather than running tippecanoe, so the
tests say what the format is as much as they check the reader against it.
"""

from __future__ import annotations

import gzip
import struct

import pytest

from wayfare import config, coverage, palette


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


def test_every_zoom_the_archive_holds_is_sized(tmp_path):
    """One pass over the index has to reach the leaves and every zoom in them, because
    a zoom that is missed reads as a zoom holding no tiles at all."""
    tiles = {
        0: gzip.compress(mvt([at(-3.0, 51.5)])),
        1: gzip.compress(mvt([(1, 1)])),
        5: gzip.compress(mvt([(2, 2), (3, 3)])),
    }
    path = archive(tmp_path / "a.pmtiles", tiles, leaves=True)
    per = coverage.sizes(path)
    assert sorted(per) == [0, 1, 2]
    assert [per[0], per[1], per[2]] == [[len(tiles[0])], [len(tiles[1])], [len(tiles[5])]]


def test_an_archive_that_draws_nothing_at_max_zoom_is_an_error(tmp_path):
    """A detail band that came out empty is the one failure a per-zoom comparison
    cannot report, because there is nothing left to compare against."""
    path = archive(tmp_path / "a.pmtiles", {0: gzip.compress(mvt([at(-3.0, 51.5)]))})
    with pytest.raises(RuntimeError, match="draws nothing"):
        coverage.bands(path, [0])


def test_the_tilt_is_what_a_hole_shows_up_in(monkeypatch, tmp_path):
    """A filter can draw every populated cell and carry more features than one that
    thins nothing, and still leave a hole. What separates them is how much the
    emptiest quarter of the country draws against the busiest."""
    reference = {(i, 0): 100 + i for i in range(8)}

    def fake(_archive, zooms, _cell):
        # Thin the sparse half hard and the dense half barely, which is what a quota
        # shared out in proportion to cell size does.
        thinned = {c: (2 if c[0] < 4 else 90) for c in reference}
        return {z: (reference if z == config.MAX_ZOOM else thinned) for z in zooms}

    monkeypatch.setattr(coverage, "_drawn_by_zoom", fake)
    _, bands = coverage.bands(tmp_path / "a.pmtiles", [5])
    assert bands[0].empty == []
    assert bands[0].quartiles[0][0] == 2
    assert bands[0].quartiles[-1][0] == 90
    assert bands[0].tilt == 45.0


def _rgb(path):
    """A PNG decoded back to (width, height, one (r, g, b) tuple per pixel).

    Every claim about a drawn map is made on these bytes, because a shade nothing is
    painted in and a road nothing is drawn along both leave the constants correct.
    The colour type is asserted rather than assumed: an eight-bit greyscale file
    read three channels at a time is a picture that still decodes, at a third of
    the width, with every claim below quietly measuring the wrong pixels.
    """
    import struct as _struct
    import zlib as _zlib

    raw = path.read_bytes()
    i, chunks, size, colour_type = 8, [], None, None
    while i < len(raw):
        length = _struct.unpack_from(">I", raw, i)[0]
        tag = raw[i + 4 : i + 8]
        body = raw[i + 8 : i + 8 + length]
        if tag == b"IHDR":
            size = _struct.unpack_from(">II", body, 0)
            colour_type = body[9]
        elif tag == b"IDAT":
            chunks.append(body)
        i += 12 + length
    assert colour_type == 2, f"expected truecolour RGB, got colour type {colour_type}"
    width, height = size
    data = _zlib.decompress(b"".join(chunks))
    stride = width * 3 + 1  # each row is prefixed by its filter byte
    pixels = []
    for y in range(height):
        row = data[y * stride + 1 : (y + 1) * stride]
        pixels.extend(tuple(row[x * 3 : x * 3 + 3]) for x in range(width))
    return width, height, pixels


def _lit(path, background=(0, 0, 0)):
    """The pixels a PNG has anything drawn in."""
    width, height, pixels = _rgb(path)
    return sum(1 for p in pixels if p != background), (width, height)


def test_a_drawn_line_reaches_the_pixels_between_its_ends(tmp_path):
    """The whole point of drawing rather than counting: a feature is a line, and what
    reaches the screen is every pixel along it, not the one its first point lands in."""
    path = archive(
        tmp_path / "a.pmtiles",
        {0: gzip.compress(_line_tile(at(-20.0, 20.0), at(20.0, -20.0)))},
    )
    out = tmp_path / "a.png"
    fraction = coverage.draw(path, 0, (-40.0, -40.0, 40.0, 40.0), out, width=100)
    lit, (w, h) = _lit(out)
    # Taller than it is wide: Mercator stretches latitude, so a square box in degrees
    # is not a square in pixels, and forcing it to be would skew every render.
    assert w == 100
    assert h > 100
    # A diagonal across the window lights roughly its diagonal's worth of pixels, far
    # more than the single pixel a first-point count would see.
    assert lit > 50
    assert fraction == lit / (w * h)


def _line_tile(start, end, extent=4096, name=b"bus"):
    """One layer, one two-point LineString from start to end, in tile units."""
    sx, sy = start
    ex, ey = end
    geometry = b"".join(
        varint(v)
        for v in (
            (1 << 3) | 1,
            zigzag(sx),
            zigzag(sy),
            (1 << 3) | 2,
            zigzag(ex - sx),
            zigzag(ey - sy),
        )
    )
    feature = blob(2, key(3, 0) + varint(1) + blob(4, geometry))
    layer = blob(1, name) + feature + key(5, 0) + varint(extent) + key(15, 0) + varint(2)
    return blob(3, layer)


def _tagged_tile(start, end, props, extent=4096, name=b"bus"):
    """The same one-line layer, with attributes on the feature.

    Keys and values are written *after* the feature that refers to them, which the
    wire format allows and tippecanoe does: a walker that resolves a tag as it
    reads it sees an empty table and answers None for every attribute in the
    archive.
    """
    sx, sy = start
    ex, ey = end
    geometry = b"".join(
        varint(v)
        for v in (
            (1 << 3) | 1,
            zigzag(sx),
            zigzag(sy),
            (1 << 3) | 2,
            zigzag(ex - sx),
            zigzag(ey - sy),
        )
    )
    tags = b"".join(varint(v) for i in range(len(props)) for v in (i, i))
    feature = blob(2, key(3, 0) + varint(1) + blob(2, tags) + blob(4, geometry))
    keys = b"".join(blob(3, k.encode()) for k in props)
    values = b"".join(
        blob(4, blob(1, v.encode()) if isinstance(v, str) else key(4, 0) + varint(v))
        for v in props.values()
    )
    layer = (
        blob(1, name)
        + feature
        + keys
        + values
        + key(5, 0)
        + varint(extent)
        + key(15, 0)
        + varint(2)
    )
    return blob(3, layer)


def test_a_features_attributes_are_read_back_off_the_wire(tmp_path):
    """Colouring by journeys a day needs the tags, and the tables they index into
    can be written after the features that use them."""
    tile = _tagged_tile(at(-20.0, 20.0), at(20.0, -20.0), {"trips": 700, "mode": "tram"})
    layers = list(coverage._tile_layers(tile))
    assert len(layers) == 1
    read_trips = layers[0].attribute("trips")
    read_mode = layers[0].attribute("mode")
    absent = layers[0].attribute("nothing")
    _geometry, tags = layers[0].features[0]
    assert read_trips(tags) == 700
    assert read_mode(tags) == "tram"
    # A layer without the attribute is the ordinary case, not an error: `trips` is
    # absent from every overview band of an archive built before it reached them.
    assert absent(tags) is None


def test_a_theme_paints_the_road_ramp_rather_than_a_grey(tmp_path):
    """The picture is of the map, so a busy road and a quiet one differ in it.

    Both are drawn, both carry a trip count three orders of magnitude apart, and
    the colours they come out are the two ends of the road ramp the viewer reads
    from the same file.
    """
    path = archive(
        tmp_path / "a.pmtiles",
        {
            0: gzip.compress(
                _tagged_tile(at(-20.0, 20.0), at(20.0, 20.0), {"trips": 7})
                + _tagged_tile(at(-20.0, -20.0), at(20.0, -20.0), {"trips": 70_000})
            )
        },
    )
    out = tmp_path / "a.png"
    coverage.draw(path, 0, (-40.0, -40.0, 40.0, 40.0), out, width=100, theme="dark")
    width, height, pixels = _rgb(out)

    ink = palette.load()
    ramp = ink.road_ramp["dark"]
    rows = [
        {p for p in pixels[y * width : (y + 1) * width] if p != (13, 16, 20)}
        for y in range(height)
    ]
    quiet = {c for row in rows[: height // 2] for c in row}
    busy = {c for row in rows[height // 2 :] for c in row}
    assert quiet == {palette.hex_to_rgb(ramp[0])}
    assert busy == {palette.hex_to_rgb(ramp[-1])}


def test_an_underlay_is_drawn_beneath_every_feature(tmp_path):
    """A coastline is context, and must never take a pixel from a road.

    The two are drawn over each other here on purpose: the road runs along the
    same latitude as the underlay, so every pixel of one is a pixel of the other,
    and the question is which the picture ends up carrying. Weight is what
    decides, and the underlay's is below the quietest road's -- which is why
    features start at two rather than at one.
    """
    path = archive(
        tmp_path / "a.pmtiles",
        {0: gzip.compress(_tagged_tile(at(-20.0, 0.0), at(20.0, 0.0), {"trips": 7}))},
    )
    window = (-40.0, -40.0, 40.0, 40.0)
    # Wider than the road, so the overlap answers which wins and the overhang
    # answers whether the underlay was drawn at all. One or the other alone
    # passes for a `draw` that ignored the underlay entirely.
    line = [[(-30.0, 0.0), (30.0, 0.0)]]

    out = tmp_path / "both.png"
    coverage.draw(path, 0, window, out, width=100, theme="dark", underlay=line)
    _w, _h, both = _rgb(out)

    ink = palette.load()
    road = palette.hex_to_rgb(ink.road_ramp["dark"][0])
    coast = coverage._COASTLINE["dark"]
    drawn = {p for p in both if p != (13, 16, 20)}
    # The road won every pixel the two share, and the coastline is still drawn
    # where the road is not -- it runs the width of the window and the road does
    # not reach the edges.
    assert road in drawn
    assert coast in drawn


def test_an_underlay_needs_no_archive_to_be_drawn(tmp_path):
    """The coastline is passed in as longitude and latitude, so it is projected
    here rather than read out of a tile. Nothing about it comes from an archive."""
    empty = archive(tmp_path / "empty.pmtiles", {})
    out = tmp_path / "coast.png"
    coverage.draw(
        empty,
        0,
        (-40.0, -40.0, 40.0, 40.0),
        out,
        width=100,
        theme="dark",
        underlay=[[(-30.0, 10.0), (30.0, 10.0)]],
    )
    lit, _size = _lit(out, background=(13, 16, 20))
    assert lit > 50


def test_several_archives_composite_into_one_picture(tmp_path):
    """These islands are three archives and the viewer draws them onto one map."""
    west = archive(
        tmp_path / "west.pmtiles",
        {0: gzip.compress(_line_tile(at(-30.0, 20.0), at(-10.0, 20.0)))},
    )
    east = archive(
        tmp_path / "east.pmtiles",
        {0: gzip.compress(_line_tile(at(10.0, -20.0), at(30.0, -20.0)))},
    )
    window = (-40.0, -40.0, 40.0, 40.0)
    alone, _ = _lit_after(coverage.draw, west, window, tmp_path / "one.png")
    both, _ = _lit_after(coverage.draw, [west, east], window, tmp_path / "two.png")
    assert both > alone


def _lit_after(draw, archives, window, out):
    """Draw and report the lit pixels, so two runs can be compared by ink."""
    draw(archives, 0, window, out, width=100)
    return _lit(out)


def test_the_layers_are_drawn_in_different_shades(tmp_path):
    """A tram line must not be mistaken for a road that survived a filter.

    Read off the picture rather than off `_GREYS`, because the constants can differ
    while the map does not: a layer whose name never reaches the grey lookup falls
    back to `_OTHER_GREY`, and every road and every tram then come out the one
    colour with nothing to show for it. `track` was in exactly that state -- named
    by `publish`, absent from the lookup -- until the names came out of one file.
    """
    path = archive(
        tmp_path / "a.pmtiles",
        {
            0: gzip.compress(
                _line_tile(at(-20.0, 20.0), at(20.0, 20.0))
                + _line_tile(at(-20.0, -20.0), at(20.0, -20.0), name=b"segments")
            )
        },
    )
    out = tmp_path / "a.png"
    coverage.draw(path, 0, (-40.0, -40.0, 40.0, 40.0), out, width=100)
    width, height, pixels = _rgb(out)

    rows = [
        {p for p in pixels[y * width : (y + 1) * width] if p != (0, 0, 0)}
        for y in range(height)
    ]
    road = {shade for row in rows[: height // 2] for shade in row}
    track = {shade for row in rows[height // 2 :] for shade in row}
    # One shade each, and the road brighter: two values that a viewer can tell apart.
    assert len(road) == len(track) == 1
    assert max(road) > max(track)


def _geometry(points, split: bool):
    """A three-point LineString, as one command stream or as two."""
    (sx, sy), (mx, my), (ex, ey) = points
    head = ((1 << 3) | 1, zigzag(sx), zigzag(sy))
    if not split:
        vals = (*head, (2 << 3) | 2, zigzag(mx - sx), zigzag(my - sy), zigzag(ex - mx))
        return [b"".join(varint(v) for v in (*vals, zigzag(ey - my)))]
    first = (*head, (1 << 3) | 2, zigzag(mx - sx), zigzag(my - sy))
    second = ((1 << 3) | 2, zigzag(ex - mx), zigzag(ey - my))
    return [b"".join(varint(v) for v in part) for part in (first, second)]


def _one_feature_tile(points, split: bool, extent=4096):
    feature = blob(
        2, key(3, 0) + varint(1) + b"".join(blob(4, g) for g in _geometry(points, split))
    )
    layer = blob(1, b"bus") + feature + key(5, 0) + varint(extent) + key(15, 0) + varint(2)
    return blob(3, layer)


def test_a_geometry_split_across_chunks_is_one_road(tmp_path):
    """Geometry is a packed repeated field, so a writer may split it across chunks
    that mean nothing apart: the second continues from where the first stopped.

    Counting the chunks makes one road two features. Reading each chunk from the tile
    origin snaps the tail of every split road back to the corner. The archive has to
    read the same either way, which is what having one walker over a tile buys.
    """
    points = [at(-20.0, 20.0), at(0.0, 0.0), at(20.0, -20.0)]
    window, out = (-40.0, -40.0, 40.0, 40.0), {}
    for name, split in (("split", True), ("whole", False)):
        path = archive(
            tmp_path / f"{name}.pmtiles",
            {0: gzip.compress(_one_feature_tile(points, split))},
        )
        out[name] = tmp_path / f"{name}.png"
        coverage.draw(path, 0, window, out[name], width=100)
        assert coverage.drawn(path, 0, 0.25) == {
            (round(-20.0 / 0.25), round(20.0 / 0.25)): 1
        }
    assert out["split"].read_bytes() == out["whole"].read_bytes()


def test_an_empty_window_is_refused_rather_than_drawn(tmp_path):
    """A flipped box silently produces a negative height and a blank image, which
    reads as an archive holding nothing."""
    path = archive(tmp_path / "a.pmtiles", {0: gzip.compress(mvt([at(0.0, 0.0)]))})
    with pytest.raises(ValueError, match="empty"):
        coverage.draw(path, 0, (10.0, 0.0, -10.0, 5.0), tmp_path / "a.png")
