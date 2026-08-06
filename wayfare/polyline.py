"""Encoded polyline codec.

Valhalla speaks Google's encoded polyline at precision 6 (the routing APIs default
to 6, not the 5 used by the original Google Maps format). Getting the precision
wrong shifts everything by a factor of ten, which reads as "the matcher put the bus
in the North Sea" rather than as a decode error, so precision is always explicit.
"""

from __future__ import annotations

Point = tuple[float, float]  # (lat, lon)


def decode(encoded: str, precision: int = 6) -> list[Point]:
    factor = float(10**precision)
    points: list[Point] = []
    index = 0
    lat = 0
    lon = 0
    length = len(encoded)

    while index < length:
        for axis in range(2):
            shift = 0
            result = 0
            while True:
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if axis == 0:
                lat += delta
            else:
                lon += delta
        points.append((lat / factor, lon / factor))

    return points


def encode(points: list[Point], precision: int = 6) -> str:
    factor = 10**precision
    out: list[str] = []
    prev_lat = 0
    prev_lon = 0

    for lat, lon in points:
        ilat = round(lat * factor)
        ilon = round(lon * factor)
        _chunk(ilat - prev_lat, out)
        _chunk(ilon - prev_lon, out)
        prev_lat, prev_lon = ilat, ilon

    return "".join(out)


def _chunk(delta: int, out: list[str]) -> None:
    value = ~(delta << 1) if delta < 0 else delta << 1
    while value >= 0x20:
        out.append(chr((0x20 | (value & 0x1F)) + 63))
        value >>= 5
    out.append(chr(value + 63))
