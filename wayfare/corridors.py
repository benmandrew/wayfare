"""Group the road export into corridors, so a thinner can drop whole lines.

Every low-zoom cap tried so far picked features one at a time, and that is what made
them look like speckle. A feature is a coalesced run along one way with one service
set, so a road a reader sees as a single line is a dozen features end to end, and any
per-feature rank keeps some of them and drops the others. What is left is a dashed
road, which reads as broken rather than as absent -- and a broken road is worse than
no road, because the eye follows it and finds a hole.

A corridor is the unit that fixes that. It is a maximal run of features linked end to
end through *good continuation*: at a junction two features are joined only when each
is the other's straightest onward choice. The idea is the cartographic literature's
"stroke" (Thomson and Richardson, 1999), renamed here because the viewer already calls
a drawn line's width its stroke. Drop a corridor and a whole road disappears; nothing
that stays drawn ends in mid-air.

Nothing in here decides *what* to drop. `build` reports the corridors, their length,
and where their features fall, and leaves the quota to `thin`, which spends it per cell
the way `publish` does -- the cell machinery and its recorded failures are written up in
`config.OVERVIEW_CAP_FAR`.

**This is measurement, not a published behaviour, and it is the half of this work that
did not pay.** `publish` does not call it. What it *did* turn up is
`publish.merge_overview`, which lives there because it is now what every publish does:
the same reading of the export as one connected network, used to join lines rather than
to rank them.

`wayfare corridors` thins an export so the result can be built with
`publish --overview-export` and put next to an unthinned archive under `coverage draw`.
Judge it there and on nothing else: every previous attempt was defensible on counts, and
so is this one -- a cap of 120,000 leaves *more* features standing at z5 over Great
Britain than no cap does, 112,357 against 105,117, while lighting fewer pixels in every
window measured.
"""

from __future__ import annotations

import math
from array import array
from collections import defaultdict
from collections.abc import Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import NamedTuple

from . import logs

# Two pieces of `publish`, imported rather than reimplemented: `_quotas` so a corridor
# cap and a `trips` cap cannot come to differ about what a cell's share is, and
# `_read_full` so there is one reader of the export's line format. `publish` does not
# import this module -- the merge moved there when it became a publish behaviour -- so
# nothing here is a cycle.
from .publish import _quotas, _read_full

log = logs.get("corridors")

Cell = tuple[int, int]

# How far from a junction the onward direction is measured, in metres.
#
# Not the adjacent vertex: a way's first segment is often a couple of metres of kerb
# geometry pointing wherever the junction happened to be drawn, and two roads that
# carry straight on through each other can come out tens of degrees apart on it. 50 m
# is past that and still short enough that a bend a hundred metres along does not
# swing it.
REACH_M = 50.0
# The most a corridor may turn at a junction and still be one corridor, in degrees.
#
# 60 is the usual figure in the stroke literature and it is about what a reader will
# follow through a junction. Tighter and a corridor stops at every roundabout
# approach; looser and a corridor turns off down a side street, which puts a long
# trunk road's survival at the mercy of whatever it happened to link to.
MAX_TURN_DEG = 60.0

# Metres per degree of latitude, and of longitude at the equator. A corridor is ranked
# by length against others in a cell about 1.4 km across, so a spherical
# approximation about the feature's own latitude is more precision than the answer
# needs -- and it costs no projection.
_M_PER_DEG = 111_320.0

# Cells are packed into one integer to keep several million of them out of the Python
# heap as tuples. The offset is what makes the halves separable: a negative latitude
# index under plain floor division borrows from the longitude and moves the cell a
# degree west, silently. 100,000 clears the ~9,000 cells a 0.02-degree grid puts
# across the whole globe by more than an order of magnitude.
_CELL_OFFSET = 100_000
_CELL_STRIDE = 1_000_000


class Corridors(NamedTuple):
    """Which corridor each feature belongs to, and what each corridor is worth.

    `of_feature` is in file order, so its index is the line number `thin` reads back.
    The other four are indexed by corridor id.

    `cell_features` is per corridor rather than per feature because that is what a
    quota is spent in: a corridor crossing four cells takes some of each one's
    allowance, and a cell ranks the corridors that touch it.
    """

    of_feature: Sequence[int]
    length_m: Sequence[float]
    features: Sequence[int]
    trips: Sequence[int]
    cell_features: Sequence[Mapping[Cell, int]]
    cell_sizes: Mapping[Cell, int]

    @property
    def n_corridors(self) -> int:
        return len(self.length_m)

    @property
    def total_features(self) -> int:
        return len(self.of_feature)


class _Scan(NamedTuple):
    """One pass over the export, reduced to what corridor building needs."""

    node: array[int]  # 2 per feature: the node its head and its tail sit on
    direction: array[float]  # 4 per feature: outward unit vector at head, then tail
    length_m: array[float]
    trips: array[int]
    cell: array[int]  # packed, one per feature
    cell_sizes: dict[Cell, int]


def _pack_point(lon: float, lat: float) -> int:
    """A micro-degree point as one integer, for interning junctions.

    The export writes `lon_e6 / 1e6`, so multiplying back and rounding recovers the
    integer exactly -- two features meeting at a junction share its coordinates to the
    micro-degree, and this is what makes them share a node.
    """
    return (round(lon * 1e6) + 200_000_000) * 400_000_000 + round(lat * 1e6) + 200_000_000


def _pack_cell(lon: float, lat: float, size: float) -> int:
    return (
        (round(lon / size) + _CELL_OFFSET) * _CELL_STRIDE + round(lat / size) + _CELL_OFFSET
    )


def _unpack_cell(packed: int) -> Cell:
    return (
        packed // _CELL_STRIDE - _CELL_OFFSET,
        packed % _CELL_STRIDE - _CELL_OFFSET,
    )


def _metres(coords: list[list[float]]) -> list[tuple[float, float]]:
    """The polyline in metres about its own first latitude."""
    scale = _M_PER_DEG * math.cos(math.radians(coords[0][1]))
    return [(lon * scale, lat * _M_PER_DEG) for lon, lat in coords]


def _outward(pts: Sequence[tuple[float, float]], head: bool) -> tuple[float, float]:
    """The unit vector pointing away from one end of a line, along the line.

    Measured over `REACH_M` rather than over the first segment, and falling back on
    the far end for a line shorter than that. A line whose points all coincide comes
    back as a zero vector, which no continuation test can match -- so a degenerate
    feature ends its corridor instead of joining an arbitrary one.
    """
    order = range(len(pts)) if head else range(len(pts) - 1, -1, -1)
    it = iter(order)
    x0, y0 = pts[next(it)]
    dx = dy = 0.0
    for i in it:
        dx, dy = pts[i][0] - x0, pts[i][1] - y0
        if math.hypot(dx, dy) >= REACH_M:
            break
    norm = math.hypot(dx, dy)
    if norm == 0.0:
        return (0.0, 0.0)
    return (dx / norm, dy / norm)


def _scan(path: Path, cell_size: float) -> _Scan:
    """Reduce the export to endpoints, directions, length and cell, in file order.

    The whole file's topology has to be resident to build corridors out of it -- there
    is no streaming answer to "what does this road join onto" -- so what is kept is as
    small as it can be: five packed arrays and one dict of junctions. The 1.6 GB
    export itself is never held.
    """
    node = array("q")
    direction = array("d")
    length_m = array("d")
    trips_of = array("q")
    cell = array("q")
    cell_sizes: defaultdict[Cell, int] = defaultdict(int)
    nodes: dict[int, int] = {}

    # Nothing is skipped, including a line the reader cannot make sense of -- it raises
    # instead. `thin` writes back by line number, so a scan that stepped over anything
    # would hand the kept features somebody else's geometry.
    for coords, _fid, _n, trips in _read_full(path):
        pts = _metres(coords)
        for lon, lat in (coords[0], coords[-1]):
            node.append(nodes.setdefault(_pack_point(lon, lat), len(nodes)))
        direction.extend(_outward(pts, head=True))
        direction.extend(_outward(pts, head=False))
        length_m.append(sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in pairwise(pts)))
        trips_of.append(trips)
        # The cell a feature's *first* point falls in, which is what
        # `publish._features` counts by. A feature is short against a cell, and a
        # corridor's cells are the union of its features', so nothing here needs the
        # finer answer.
        cell.append(_pack_cell(coords[0][0], coords[0][1], cell_size))
        cell_sizes[_unpack_cell(cell[-1])] += 1

    return _Scan(node, direction, length_m, trips_of, cell, dict(cell_sizes))


def _link(scan: _Scan) -> array[int]:
    """Pair up feature ends that carry straight on through a junction.

    One entry per end -- `2 * feature` for its head and `2 * feature + 1` for its tail
    -- holding the end it continues into, or -1. Mutual best fit: an end joins another
    only when each is the other's straightest choice, so a side street cannot claim a
    trunk road that carries on past it. That is what stops a corridor turning off at a
    junction and leaving the road it was on to be ranked as two halves.

    Ties are broken on the lower end index, because two roads leaving a junction at
    the same angle is a real thing -- a symmetric fork -- and a corridor's identity may
    not depend on the order DuckDB's parallel scan returned rows in.
    """
    n_ends = len(scan.node)
    at: defaultdict[int, list[int]] = defaultdict(list)
    for end in range(n_ends):
        at[scan.node[end]].append(end)

    limit = math.cos(math.radians(MAX_TURN_DEG))
    best = array("q", [-1]) * n_ends
    for ends in at.values():
        if len(ends) < 2:
            continue
        for a in ends:
            ax, ay = scan.direction[2 * a], scan.direction[2 * a + 1]
            pick, pick_cos = -1, -2.0
            for b in ends:
                # A line's two ends can sit on one node -- a loop -- and are not a
                # continuation of each other: joining them closes a ring onto itself.
                if b == a or b // 2 == a // 2:
                    continue
                bx, by = scan.direction[2 * b], scan.direction[2 * b + 1]
                # Straight on means the two outward directions point opposite ways,
                # so the *negated* dot product is the cosine of the turn: 1 for a road
                # that does not turn at all, and 0 for a right angle.
                straight = -(ax * bx + ay * by)
                if straight > pick_cos:
                    pick, pick_cos = b, straight
            if pick_cos >= limit:
                best[a] = pick
    return array("q", [b if b >= 0 and best[b] == a else -1 for a, b in enumerate(best)])


def build(path: Path, cell_size: float) -> Corridors:
    """Group the export's features into corridors."""
    scan = _scan(path, cell_size)
    joined = _link(scan)
    n_features = len(scan.length_m)

    of_feature = array("q", [-1]) * n_features
    length_m: list[float] = []
    features: list[int] = []
    trips: list[int] = []
    cell_features: list[Mapping[Cell, int]] = []

    for seed in range(n_features):
        if of_feature[seed] >= 0:
            continue
        # Walk to one end of the corridor first, so which of its features seeded it
        # does not change where it starts. A ring has no end and comes back to the
        # seed, which is what the second test catches.
        here, leaving = seed, 0
        while True:
            nxt = joined[2 * here + leaving]
            if nxt < 0 or nxt // 2 == seed:
                break
            here, leaving = nxt // 2, 1 - nxt % 2

        cid = len(length_m)
        total_m, total_trips, count = 0.0, 0, 0
        cells: defaultdict[Cell, int] = defaultdict(int)
        leaving = 1 - leaving
        while True:
            of_feature[here] = cid
            total_m += scan.length_m[here]
            total_trips += scan.trips[here]
            cells[_unpack_cell(scan.cell[here])] += 1
            count += 1
            nxt = joined[2 * here + leaving]
            if nxt < 0 or of_feature[nxt // 2] >= 0:
                break
            here, leaving = nxt // 2, 1 - nxt % 2

        length_m.append(total_m)
        features.append(count)
        trips.append(total_trips)
        cell_features.append(dict(cells))

    log.info(
        "%d features form %d corridors; longest %.1f km, median %d features",
        n_features,
        len(length_m),
        max(length_m, default=0.0) / 1000,
        sorted(features)[len(features) // 2] if features else 0,
    )
    return Corridors(of_feature, length_m, features, trips, cell_features, scan.cell_sizes)


def rank(corridors: Corridors) -> list[int]:
    """Corridor ids, best first.

    Length, because length is what a low zoom shows. A hundred metres of busy city
    street and a hundred metres of country lane are the same two pixels, and the four
    recorded failures all ranked on `trips`, which is why every one of them kept the
    city centres and lost the roads between towns.

    `trips` breaks the tie and the corridor id breaks that one. A *defined* order is
    the requirement rather than a plausible one: two corridors of the same length is
    an ordinary thing on a road network, and a rebuild has to produce the same bytes.
    """
    return sorted(
        range(corridors.n_corridors),
        key=lambda c: (-corridors.length_m[c], -corridors.trips[c], c),
    )


def select(corridors: Corridors, cap: int, weight: float) -> set[int] | None:
    """Which corridors a cap of `cap` features can afford, or None for all of them.

    The quota is shared out over the cells exactly as `publish._cell_floors` shares
    it, and for the same reason: `trips` and length are both absolute, so one national
    ranking sorts the map by how urban it is rather than by what is worth drawing.
    What is different is only the unit it is spent on.

    A corridor is kept if any cell it touches can afford it, so a trunk road crossing
    the countryside survives on being the best thing in a quiet cell even where it
    would be nothing special in the city it ends at. That is deliberate, and it is
    what makes the cap soft: keeping a corridor for one cell spends the others'
    allowance too. The overshoot is bounded by the corridors that reach a cell from
    outside it, and it falls on the dense cells, which are the ones with something to
    spare.

    None rather than an empty selection when the file is already under the cap, so a
    region that never troubles the tile size limit is handed back its own file
    untouched -- which is both parts of Ireland, and the reference for what an
    unthinned map looks like.
    """
    if corridors.total_features <= cap:
        return None
    quotas = _quotas(corridors.cell_sizes, cap, weight)
    place = {cid: i for i, cid in enumerate(rank(corridors))}
    by_cell: defaultdict[Cell, list[int]] = defaultdict(list)
    for cid, cells in enumerate(corridors.cell_features):
        for cell in cells:
            by_cell[cell].append(cid)

    kept: set[int] = set()
    for cell, cids in by_cell.items():
        quota = quotas.get(cell, corridors.cell_sizes[cell])
        spent = 0
        for cid in sorted(cids, key=place.__getitem__):
            # Added before the test, so every populated cell draws its best corridor
            # however small its share is. Rounding alone empties the five sparsest
            # cells in Great Britain, and an empty cell is a hole rather than a
            # thinner patch.
            kept.add(cid)
            spent += corridors.cell_features[cid][cell]
            if spent >= quota:
                break
    return kept


def thin(path: Path, out: Path, cap: int, weight: float, cell_size: float) -> Path:
    """Write out the export with whole corridors removed, and say what it cost.

    Returns the input unchanged where the cap does not bite, so a region under it
    pays for no copy -- the same contract as `publish._hold_back`.

    The kilometres are the number to read, not the features. Dropping half the
    features off a network while keeping 92% of its drawn length is the claim this
    whole approach rests on, and the two figures come apart precisely because a
    corridor is many short features: what goes is a great many small pieces of a few
    roads, rather than a few pieces of a great many roads.
    """
    corridors = build(path, cell_size)
    kept = select(corridors, cap, weight)
    if kept is None:
        log.info(
            "%d features is under the %d cap; nothing thinned",
            corridors.total_features,
            cap,
        )
        return path

    total_m = sum(corridors.length_m)
    kept_m = sum(corridors.length_m[c] for c in kept)
    written = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    with path.open("rb") as src, out.open("wb") as dst:
        for i, line in enumerate(src):
            if corridors.of_feature[i] in kept:
                dst.write(line)
                written += 1

    log.info(
        "kept %d of %d features (%.1f%%) in %d of %d corridors (%.1f%%), "
        "%.0f of %.0f km (%.1f%%)",
        written,
        corridors.total_features,
        100 * written / corridors.total_features,
        len(kept),
        corridors.n_corridors,
        100 * len(kept) / corridors.n_corridors,
        kept_m / 1000,
        total_m / 1000,
        100 * kept_m / total_m if total_m else 100.0,
    )
    return out
