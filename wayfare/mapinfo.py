"""Read a MapInfo Interchange Format (MIF/MID) pair.

Translink publishes Northern Ireland's road geometry this way and nothing else in
this project does, so this is a reader for exactly the dialect they emit rather
than a general MapInfo implementation.

The format is two files that must be read together. The ``.MIF`` holds a header
declaring the columns, then one *object* per feature -- a keyword and, for a
polyline, the coordinates that follow it. The ``.MID`` holds one delimited row of
attributes per object, in the same order and with no key of its own. **Position is
the only join between them**, which is why every keyword this file does not
recognise is an error rather than something to skip: one unconsumed object shifts
every attribute row after it onto the wrong geometry, and the result is a map that
draws.

``None`` is a real object type and the trap in this format. It is an attribute row
with no geometry, it occupies a line of its own, and it does not look like a
keyword. Translink's PtLinks files carry 152 of them across 37,913 polylines; a
reader that treats ``None`` as noise loses count and mis-joins the remainder.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# MapInfo names its charsets; only the one Translink writes is mapped, because a
# guess at an unknown one is a silent mojibake in a road name.
CHARSETS = {
    "WindowsLatin1": "cp1252",
    "Neutral": "ascii",
    "UTF-8": "utf-8",
}

# Objects that carry no geometry but still consume a MID row.
EMPTY = {"NONE", "NULL"}
# Per-object drawing attributes. They follow the coordinates and belong to the
# object already being read, so they are consumed rather than counted.
DECORATION = {"PEN", "BRUSH", "SYMBOL", "SMOOTH", "CENTER", "FONT"}

_HEADER = re.compile(r"^\s*(\w+)\s*(.*)$")


@dataclass(frozen=True)
class Header:
    columns: tuple[str, ...]
    encoding: str
    delimiter: str
    coordsys: str


@dataclass(frozen=True)
class Feature:
    """One MID row and the MIF object it sits against.

    ``points`` is (longitude, latitude) and is empty for a ``None`` object, which
    is a row Translink writes for a link whose geometry it does not hold.
    """

    values: dict[str, str]
    points: tuple[tuple[float, float], ...]


class Malformed(Exception):
    """The pair cannot be read without guessing where the objects line up."""


def header(lines: Iterator[str]) -> Header:
    """Consume the MIF header, leaving the iterator on the first object.

    ``Columns N`` is followed by exactly N name/type lines and then ``Data``. The
    types are not kept: MID values reach GTFS and DuckDB as text either way, and
    the same rule that keeps GTFS ids as strings applies here.
    """
    columns: list[str] = []
    encoding = "cp1252"
    delimiter = "\t"  # the MIF default when the header does not say
    coordsys = ""
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        m = _HEADER.match(line)
        if not m:
            raise Malformed(f"unreadable header line {line!r}")
        key, rest = m.group(1).lower(), m.group(2).strip()
        if key == "charset":
            name = rest.strip('"')
            if name not in CHARSETS:
                raise Malformed(f"unknown charset {name!r}")
            encoding = CHARSETS[name]
        elif key == "delimiter":
            delimiter = rest.strip('"')
        elif key == "coordsys":
            coordsys = rest
        elif key == "columns":
            for _ in range(int(rest)):
                columns.append(next(lines).split()[0])
        elif key == "data":
            break
    else:
        raise Malformed("no Data section")
    # `Earth Projection 1` is longitude/latitude. Anything else is a projected
    # grid, and reading its eastings as degrees puts Belfast in the Atlantic
    # without raising -- the numbers are still numbers.
    if coordsys and not re.match(r"Earth\s+Projection\s+1\b", coordsys):
        raise Malformed(f"expected a longitude/latitude CoordSys, got {coordsys!r}")
    return Header(tuple(columns), encoding, delimiter, coordsys)


def _points(lines: Iterator[str], n: int) -> tuple[tuple[float, float], ...]:
    out = []
    for _ in range(n):
        parts = next(lines).split()
        out.append((float(parts[0]), float(parts[1])))
    return tuple(out)


def objects(lines: Iterator[str]) -> Iterator[tuple[tuple[float, float], ...]]:
    """Yield one geometry per MIF object, in file order.

    Only the object types Translink writes are accepted. A keyword this does not
    know raises, because the alternative -- skipping it -- silently re-pairs every
    later MID row with the wrong geometry.
    """
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        word, _, rest = line.partition(" ")
        kind = word.upper()
        if kind in EMPTY:
            yield ()
        elif kind == "POINT":
            x, y = rest.split()[:2]
            yield ((float(x), float(y)),)
        elif kind == "LINE":
            x1, y1, x2, y2 = rest.split()[:4]
            yield ((float(x1), float(y1)), (float(x2), float(y2)))
        elif kind == "PLINE":
            fields = rest.split()
            # `PLINE MULTIPLE n` is n sections, each a count and its coordinates.
            if fields and fields[0].upper() == "MULTIPLE":
                pts: list[tuple[float, float]] = []
                for _ in range(int(fields[1])):
                    pts.extend(_points(lines, int(next(lines).strip())))
                yield tuple(pts)
            else:
                yield _points(lines, int(fields[0]))
        elif kind in DECORATION:
            continue
        else:
            raise Malformed(f"unsupported MIF object {word!r}")


def read(mif: Path, mid: Path) -> Iterator[Feature]:
    """Stream the pair as features. The PtLinks MIF is 36 MB, so this does not
    hold either file in memory.

    The MIF is read as cp1252 regardless of what its own header declares, because
    the header has to be read before the charset is known and everything in the
    file -- keywords, column names, coordinates -- is ASCII. The MID is the half
    that carries stop names, and that one is opened with the declared charset.
    """
    with mif.open("r", encoding="cp1252", errors="replace") as fh:
        lines = iter(fh)
        head = header(lines)
        with mid.open("r", encoding=head.encoding, errors="replace", newline="") as mfh:
            rows = csv.reader(mfh, delimiter=head.delimiter, skipinitialspace=True)
            for n, points in enumerate(objects(lines), start=1):
                try:
                    row = next(rows)
                except StopIteration:
                    raise Malformed(f"{mid.name}: ran out of rows at object {n}") from None
                yield Feature(dict(zip(head.columns, row, strict=False)), points)
            # A MID longer than the MIF means an object type went unread above,
            # so every feature already yielded may be joined to the wrong row.
            if next(rows, None) is not None:
                raise Malformed(f"{mid.name}: more rows than {mif.name} has objects")
