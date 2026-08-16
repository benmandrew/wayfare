#!/usr/bin/env python3
"""Does physically ordering `edges` by a space-filling curve buy row-group pruning?

`art` filters a window with four integer comparisons on the precomputed bbox
columns and there is no spatial index, so the received wisdom is that every render
reads the whole table. That is only half true. DuckDB keeps min/max zonemaps per
row group (122,880 rows) and skips a group whose zonemap cannot satisfy the filter,
so the bbox filter is already a spatial index of sorts -- it just cannot work,
because `match` inserts edges in batch order, which is spatially random. Every row
group's zonemap then spans most of Great Britain and none can be skipped.

Wales cannot show this either way: 169,857 edges is two row groups, so the coarsest
pruning available is "read half the table". A national 4.2M edges is 35 groups, which
is where the question becomes real. Hence synthetic data at national scale.

What is measured, per layout per window: rows the scan physically read, taken from
DuckDB's own `operator_rows_scanned` metric rather than inferred, plus wall time as
corroboration.

Measured on 4.2M edges and 10.25M edge-service rows. The bbox scan on `cardiff`
falls from 100% of the table to 11.7% under Morton and 5.9% under Hilbert, 22 ms to
4 ms; `london` falls to 26.3% / 23.4%. `uk` reads everything under every layout, as
it must. `edge_services` is never pruned at all -- it carries no bbox column, and
DuckDB pushes no min/max filter through the join -- so the weights query keeps a
floor of one full 10M-row scan whatever `edges` is ordered by.

    .venv/bin/python scripts/bench_window.py

Add `--edges N` to scale down, `--fetch` to also time materialising every row into
Python the way `Window.edges()` does. Writes ~1.9 GB of databases under `--work`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from wayfare import art, db, maintenance

# --- Synthetic geography ----------------------------------------------------

# A bus network is not a uniform scatter: it is a few dozen dense knots joined by
# thin rural threads. A uniform scatter would flatter any clustered layout, because
# every window would then hold the same fraction of a nationally-sorted table --
# clustering is exactly what makes some row groups relevant and others not.
#
# Real centres, so the `cardinality` and `london` presets land on real density.
# `weight` is a share of the clustered edges, roughly by network size rather than
# population; `radius` is the degrees of latitude the network sprawls over.
CITIES: list[tuple[str, float, float, float, float]] = [
    # name, lon, lat, weight, radius
    ("london", -0.118, 51.509, 22.0, 0.34),
    ("birmingham", -1.898, 52.481, 6.0, 0.20),
    ("manchester", -2.244, 53.481, 6.0, 0.22),
    ("glasgow", -4.252, 55.861, 4.5, 0.18),
    ("leeds", -1.549, 53.801, 4.0, 0.20),
    ("liverpool", -2.978, 53.408, 3.5, 0.16),
    ("newcastle", -1.614, 54.978, 3.0, 0.16),
    ("sheffield", -1.470, 53.383, 2.8, 0.14),
    ("bristol", -2.588, 51.454, 2.6, 0.14),
    ("edinburgh", -3.189, 55.953, 2.5, 0.14),
    ("nottingham", -1.148, 52.954, 2.0, 0.13),
    ("cardiff", -3.179, 51.482, 1.9, 0.13),
    ("leicester", -1.133, 52.637, 1.7, 0.11),
    ("coventry", -1.510, 52.407, 1.4, 0.10),
    ("stoke", -2.178, 53.003, 1.3, 0.11),
    ("hull", -0.336, 53.745, 1.2, 0.10),
    ("plymouth", -4.143, 50.376, 1.1, 0.10),
    ("derby", -1.476, 52.922, 1.1, 0.09),
    ("southampton", -1.404, 50.909, 1.1, 0.10),
    ("portsmouth", -1.088, 50.799, 1.0, 0.09),
    ("brighton", -0.137, 50.822, 1.0, 0.09),
    ("aberdeen", -2.095, 57.149, 1.0, 0.11),
    ("swansea", -3.944, 51.622, 0.9, 0.11),
    ("reading", -0.974, 51.454, 0.9, 0.09),
    ("preston", -2.703, 53.759, 0.9, 0.11),
    ("milton_keynes", -0.759, 52.041, 0.8, 0.09),
    ("norwich", 1.298, 52.630, 0.8, 0.09),
    ("luton", -0.420, 51.879, 0.8, 0.08),
    ("york", -1.081, 53.959, 0.8, 0.09),
    ("bournemouth", -1.878, 50.720, 0.8, 0.09),
    ("middlesbrough", -1.234, 54.575, 0.8, 0.09),
    ("swindon", -1.780, 51.559, 0.7, 0.08),
    ("ipswich", 1.155, 52.059, 0.7, 0.08),
    ("oxford", -1.258, 51.752, 0.7, 0.08),
    ("cambridge", 0.122, 52.205, 0.7, 0.08),
    ("dundee", -2.970, 56.462, 0.7, 0.09),
    ("blackpool", -3.036, 53.817, 0.7, 0.09),
    ("exeter", -3.533, 50.719, 0.7, 0.09),
    ("gloucester", -2.244, 51.864, 0.6, 0.08),
    ("inverness", -4.225, 57.478, 0.4, 0.12),
]

# Great Britain, and the box the Hilbert and Morton codes are quantised over. Wider
# than the data so the curve's cells line up with a fixed grid rather than with
# whatever this run happened to generate.
GB = art.PRESETS["uk"]

# Share of edges that fall outside any city. Rural mileage is long but sparse.
RURAL_SHARE = 0.20

# docs/pipeline.md: a Valhalla directed edge averages 4.14 coordinates. These thresholds
# on a uniform draw give a mean of 4.17, which is close enough that the geometry
# column compresses and scans like the real one.
POINT_BUCKETS = ((0.34, 3), (0.63, 4), (0.86, 5), (1.01, 6))
MAX_POINTS = 6

# Wales: 413,915 edge-service pairs over 169,857 edges, so 2.44 services an edge.
# A geometric draw with that mean, capped at the 64 the tile `refs` cap allows.
SERVICES_PER_EDGE = 2.44
MAX_SERVICES = 64

ROAD_CLASSES = (
    (0.55, "residential"),
    (0.67, "service"),
    (0.79, "tertiary"),
    (0.90, "secondary"),
    (0.97, "primary"),
    (1.01, "trunk"),
)

STREETS = [
    "High Street",
    "Station Road",
    "Church Lane",
    "Victoria Road",
    "Mill Lane",
    "London Road",
    "Park Avenue",
    "Queens Road",
    "Kings Way",
    "Bridge Street",
    "New Road",
    "Manor Way",
    "Albert Road",
    "Grange Road",
    "School Lane",
    "The Green",
    "Windsor Drive",
    "Alexandra Road",
    "Chapel Street",
    "Springfield Road",
]

WINDOWS = ("cardiff", "london", "uk")


# --- Deterministic pseudo-randomness ----------------------------------------


def salted(salt: str, key: str = "edge_id") -> str:
    """SQL hashing a row key into an independent stream named by `salt`.

    Do not write this as `hash(key, salt)`. DuckDB combines its hash arguments by
    XOR, so hash(i, 'a') and hash(i, 'b') differ only in flipped bits and come out
    0.88 rank-correlated -- every supposedly independent draw was then a near copy of
    every other. That is not a cosmetic flaw here: it made the shuffled layout's row
    groups spatially tight, so the baseline appeared to prune 24 of 35 row groups and
    the whole benchmark read as already solved. Shifting the key by a per-salt
    constant and hashing that has no such structure (measured correlation -0.003).
    """
    offset = int.from_bytes(hashlib.blake2b(salt.encode(), digest_size=6).digest(), "big")
    return f"hash(({key}) + {offset})"


def unit(salt: str, key: str = "edge_id") -> str:
    """A deterministic uniform draw in [0, 1) from a row key and a salt.

    `random()` would do, but DuckDB seeds it per thread, so a parallel scan gives a
    different table on a machine with a different core count. Hashing the row index
    is reproducible by construction: the value depends on the row, not on how the
    row was reached. 53 bits is exactly a double's mantissa.
    """
    return f"(({salted(salt, key)} >> 11)::DOUBLE / 9007199254740992.0)"


def buckets(u: str, table: tuple[tuple[float, Any], ...]) -> str:
    """A CASE mapping a uniform draw onto weighted discrete values."""
    arms = " ".join(f"WHEN {u} < {p} THEN {v!r}" for p, v in table[:-1])
    return f"CASE {arms} ELSE {table[-1][1]!r} END"


# --- Generation -------------------------------------------------------------


def _cities_sql() -> str:
    """The cluster table, cities plus one rural disc, as a VALUES list.

    `power` shapes the radial draw: 0.5 spreads points evenly over a disc, which is
    what the rural background wants, and 1.6 piles them toward the middle, which is
    what a city looks like. One column instead of two code paths.
    """
    total = sum(c[3] for c in CITIES)
    rows, lo = [], 0.0
    for name, lon, lat, weight, radius in CITIES:
        hi = lo + (weight / total) * (1.0 - RURAL_SHARE)
        rows.append(f"('{name}', {lon}, {lat}, {radius}, 1.6, {lo}, {hi})")
        lo = hi
    mid_lon = (GB.min_lon + GB.max_lon) / 2
    rows.append(f"('rural', {mid_lon}, {GB.mid_lat}, 5.4, 0.5, {lo}, 1.0)")
    return (
        "SELECT * FROM (VALUES " + ", ".join(rows) + ") "
        "AS t(name, lon, lat, radius, power, cum_lo, cum_hi)"
    )


def _points_sql() -> str:
    """The `MAX_POINTS` candidate vertices, later sliced to the edge's real length.

    Built as one expression per vertex rather than by unnesting and regrouping,
    because `list(x ORDER BY seq)` pins its per-group sort state in memory and this
    would be 17M groups of it -- the same trap `patterns` hit on the London feed.
    """
    lons, lats = [], []
    for k in range(MAX_POINTS):
        jitter = f"(({unit(f'j{k}')} - 0.5) * 6.0)"  # metres, perpendicular
        along = f"({k} * step_m)"
        lons.append(
            f"(lon0_e6 + round(({along} * sin(theta) + {jitter} * cos(theta))"
            f" * e6_per_m_lon))::INTEGER"
        )
        lats.append(
            f"(lat0_e6 + round(({along} * cos(theta) - {jitter} * sin(theta))"
            f" * e6_per_m_lat))::INTEGER"
        )
    return (
        f"list_slice([{', '.join(lons)}], 1, npts) AS lon_e6,\n"
        f"       list_slice([{', '.join(lats)}], 1, npts) AS lat_e6"
    )


def build_source(con: duckdb.DuckDBPyConnection, n_edges: int) -> None:
    """Generate `edges` and `edge_services` once, in no particular physical order."""
    con.execute(f"CREATE OR REPLACE TABLE cluster AS {_cities_sql()}")
    pick = unit("city", "r.i")

    # Place each edge in a cluster, then at a radius and bearing within it. The
    # longitude offset is divided by cos(lat) so a city is round on the ground
    # rather than stretched east-west by the projection of degrees onto metres.
    con.execute(f"""
        CREATE OR REPLACE TABLE seed AS
        SELECT
            r.i AS edge_id,
            c.name AS city,
            c.lon + (c.radius * pow({unit("rad", "r.i")}, c.power))
                  * cos({unit("ang", "r.i")} * 2 * pi()) / cos(radians(c.lat)) AS lon,
            c.lat + (c.radius * pow({unit("rad", "r.i")}, c.power))
                  * sin({unit("ang", "r.i")} * 2 * pi()) AS lat
        FROM range({n_edges}) r(i)
        JOIN cluster c ON {pick} >= c.cum_lo AND {pick} < c.cum_hi
    """)

    # Clamped rather than resampled: a resample would need a loop, and the handful of
    # rural points that fall in the sea only ever bias density at the edge of the box,
    # which no preset window looks at. Inset by a hair because the start point is an
    # endpoint and not the centre, so an edge clamped exactly onto the boundary and
    # pointing outward lands wholly outside the `uk` window and stops the country
    # total from being the whole table.
    inset = 0.02
    con.execute(f"""
        CREATE OR REPLACE TABLE geom AS
        SELECT edge_id, city,
               least(greatest(lon, {GB.min_lon + inset}), {GB.max_lon - inset}) AS lon,
               least(greatest(lat, {GB.min_lat + inset}), {GB.max_lat - inset}) AS lat
        FROM seed
    """)

    npts = buckets(unit("np"), POINT_BUCKETS)
    # Road names are prefixed with the city, so their cardinality and their spatial
    # correlation both resemble the real column rather than a global pool of twenty.
    street = buckets(
        unit("st"), tuple(((k + 1) / len(STREETS), s) for k, s in enumerate(STREETS))
    )
    con.execute(f"""
        CREATE OR REPLACE TABLE edges_src AS
        WITH e AS (
            SELECT edge_id, city,
                   round(lon * 1e6)::INTEGER AS lon0_e6,
                   round(lat * 1e6)::INTEGER AS lat0_e6,
                   {npts} AS npts,
                   {unit("dir")} * 2 * pi() AS theta,
                   -- "tens of metres", squared so the long tail is thin.
                   15.0 + 120.0 * pow({unit("len")}, 2) AS length_m,
                   9.045 AS e6_per_m_lat,
                   1e6 / (111320.0 * cos(radians(lat))) AS e6_per_m_lon
            FROM geom
        ), pts AS (
            SELECT edge_id, city, length_m,
                   {_points_sql()}
            FROM (SELECT *, length_m / (npts - 1) AS step_m FROM e)
        )
        SELECT edge_id,
               -- Several directed edges share an OSM way, as in the real graph.
               (edge_id / 3)::BIGINT + 100000 AS way_id,
               city || ' ' || {street} AS road_name,
               {buckets(unit("rc"), ROAD_CLASSES)} AS road_class,
               length_m, lon_e6, lat_e6,
               list_min(lon_e6) AS min_lon_e6, list_min(lat_e6) AS min_lat_e6,
               list_max(lon_e6) AS max_lon_e6, list_max(lat_e6) AS max_lat_e6
        FROM pts
    """)
    # `geom` is kept only so edge_services can draw its names from the same
    # city an edge sits in; the real schema has no such column.
    con.execute("DROP TABLE seed")

    # Service names come from the edge's own city, so a window holds a few hundred
    # services rather than a national sample of them. Offsetting a per-city base by
    # k keeps an edge's own names distinct, which is what the DISTINCT count in the
    # weight expressions assumes.
    #
    # `edge_id * MAX_SERVICES + k` is the row key for the per-pair draws: adding k to
    # the edge id instead would make edge n's second service share a key with edge
    # n+1's first.
    pair_key = f"(e.edge_id * {MAX_SERVICES} + s.k)"
    p_stop = 1.0 - 1.0 / SERVICES_PER_EDGE
    con.execute(f"""
        CREATE OR REPLACE TABLE edge_services_src AS
        SELECT e.edge_id,
               e.city || '-' || ((base + s.k) % 120)::VARCHAR AS short_name,
               'OP' || ({salted("op", "(hash(e.city) >> 1)::BIGINT")} % 30)::VARCHAR
                   AS agency_id,
               1 + ({salted("np", pair_key)} % 8)::INTEGER AS n_patterns,
               (1 + pow({unit("tr", pair_key)}, 3) * 3000)::INTEGER AS n_trips
        FROM (
            SELECT edge_id, city,
                   ({salted("base")} % 120)::BIGINT AS base,
                   least(
                       {MAX_SERVICES},
                       1 + floor(ln(1.0 - {unit("ns")}) / ln({p_stop}))
                   )::INTEGER AS n_svc
            FROM geom
        ) e, range(e.n_svc) s(k)
    """)


# --- Layouts ----------------------------------------------------------------


# Centre of the stored bbox, in degrees. Both curves order on this: it is the one
# point an edge has that a window query is asking about, and it is already columnar.
_CX = "(min_lon_e6 + max_lon_e6) / 2e6"
_CY = "(min_lat_e6 + max_lat_e6) / 2e6"

_BOX = (
    f"{{'min_x': {GB.min_lon}, 'min_y': {GB.min_lat}, "
    f"'max_x': {GB.max_lon}, 'max_y': {GB.max_lat}}}::BOX_2D"
)


def order_sql(layout: str) -> str:
    """The ORDER BY that gives a layout its physical row order."""
    if layout == "random":
        # What `match` does today. Edges arrive as their patterns are matched, and a
        # batch of patterns is a national sample, so insertion order carries no
        # geography at all -- a hash of the id models it exactly.
        return salted("batch")
    if layout == "morton":
        # The package's own, so what this measures and what `wayfare cluster` does
        # cannot drift apart. `maintenance.CLUSTER_BOX` is this file's `GB` by construction.
        return maintenance.morton_sql(_CX, _CY)
    if layout == "hilbert":
        return f"ST_Hilbert({_CX}, {_CY}, {_BOX})"
    raise ValueError(layout)


def build_layout(
    path: Path, src: Path, layout: str, *, indices: bool, spatial: bool
) -> None:
    con = duckdb.connect(str(path))
    if spatial:
        con.execute("LOAD spatial")
    con.execute(db.SCHEMA)
    con.execute(f"ATTACH '{src}' AS src (READ_ONLY)")
    key = order_sql(layout)
    # preserve_insertion_order is on by default, so an ordered source lands in row
    # groups in that order -- which is the whole intervention being measured.
    con.execute(f"""
        INSERT INTO edges BY NAME
        SELECT * FROM src.edges_src ORDER BY {key}
    """)
    # edge_services follows its edges. Clustering one and not the other would be an
    # odd half-measure: `aggregate` writes this table from a scan of the edges, so
    # whatever order edges are in is the order this inherits for free.
    con.execute(f"""
        INSERT INTO edge_services BY NAME
        SELECT s.* FROM src.edge_services_src s
        JOIN src.edges_src e USING (edge_id)
        ORDER BY {key}
    """)
    if indices:
        db.create_indices(con)
    con.execute("CHECKPOINT")
    con.close()


# --- Measurement ------------------------------------------------------------
#
# The queries come from art's own builder rather than being copied here, so this
# measures whatever `Window` actually issues today and cannot drift from it.

# Every scanned column is touched, but nothing is transferred: pulling four million
# geometry lists into Python costs the same constant under every layout and would
# swamp the thing being measured. list_sum reads the list children rather than just
# their offsets, so no column escapes the scan through projection pushdown.
_TOUCH = (
    "count(*), sum(list_sum(lon_e6)) + sum(list_sum(lat_e6)), sum(length_m), "
    "sum(way_id), sum(length(road_name)), count(road_class)"
)


def bbox(b: art.Bounds) -> list[int]:
    """The four bounds in the order the window predicate binds them, as Window does."""
    return b.as_predicate_params()


def queries(bounds: art.Bounds) -> dict[str, tuple[str, list[Any], str | None]]:
    """(counted form, params, streamable form) for each per-render scan."""
    sql = art._Sql(art.DEFAULT_SPEC, art.DEFAULT_SOURCE, bbox(bounds))
    window, win_params = sql.window()
    weights, weight_params = sql.weights_query()
    return {
        "window": (f"SELECT {_TOUCH} FROM ({window}) w", win_params, window),
        # weights_query already returns one narrow row an edge; count it to keep the
        # timing on the database side.
        "weights": (
            f"SELECT count(*), sum(t.c) FROM ({weights}) t(c)",
            weight_params,
            None,
        ),
    }


@dataclass
class Result:
    layout: str
    window: str
    query: str
    rows: int
    ms: float
    fetch_ms: float | None
    scanned: dict[str, int]


def scanned_rows(profile: dict[str, Any]) -> dict[str, int]:
    """Rows each table scan physically read, per table, from DuckDB's profile.

    `operator_rows_scanned` on a scan is the count before the pushed-down filter,
    which is precisely the pruning figure: a skipped row group contributes nothing.
    The filter's own output shows up as the operator's cardinality instead.
    """
    out: dict[str, int] = {}

    def walk(node: dict[str, Any]) -> None:
        if node.get("operator_type") in ("SEQ_SCAN", "TABLE_SCAN"):
            info = node.get("extra_info", {})
            table = str(info.get("Text") or info.get("Table", "?"))
            read = int(node.get("operator_rows_scanned", 0))
            out[table] = out.get(table, 0) + read
        for child in node.get("children", []):
            walk(child)

    walk(profile)
    return out


def profile_query(
    con: duckdb.DuckDBPyConnection, sql: str, params: list[Any], out: Path
) -> dict[str, Any]:
    con.execute("PRAGMA enable_profiling='json'")
    con.execute(f"PRAGMA profiling_output='{out}'")
    try:
        con.execute(sql, params).fetchall()
    finally:
        con.execute("PRAGMA disable_profiling")
    with out.open() as fh:
        return dict(json.load(fh))


def best_ms(
    con: duckdb.DuckDBPyConnection, sql: str, params: list[Any], runs: int
) -> float:
    con.execute(sql, params).fetchall()  # warm-up: page cache, not the measurement
    best = float("inf")
    for _ in range(runs):
        t0 = time.perf_counter()
        con.execute(sql, params).fetchall()
        best = min(best, (time.perf_counter() - t0) * 1000)
    return best


def fetch_ms(
    con: duckdb.DuckDBPyConnection, sql: str, params: list[Any], runs: int
) -> float:
    """The same query drained in chunks, as `Window.edges()` drains it."""
    best = float("inf")
    for _ in range(runs):
        t0 = time.perf_counter()
        cur = con.execute(sql, params)
        while cur.fetchmany(art.FETCH_ROWS):
            pass
        best = min(best, (time.perf_counter() - t0) * 1000)
    return best


def measure(
    path: Path,
    layout: str,
    *,
    runs: int,
    with_fetch: bool,
    spatial: bool,
    scratch: Path,
) -> list[Result]:
    con = duckdb.connect(str(path), read_only=True)
    if spatial:
        con.execute("LOAD spatial")
    results = []
    for name in WINDOWS:
        for label, (sql, params, raw) in queries(art.PRESETS[name]).items():
            prof = profile_query(con, sql, params, scratch / "profile.json")
            rows = int(con.execute(sql, params).fetchone()[0])  # type: ignore[index]
            results.append(
                Result(
                    layout=layout,
                    window=name,
                    query=label,
                    rows=rows,
                    ms=best_ms(con, sql, params, runs),
                    fetch_ms=(
                        fetch_ms(con, raw, params, min(runs, 3))
                        if with_fetch and raw is not None
                        else None
                    ),
                    scanned=scanned_rows(prof),
                )
            )
    con.close()
    return results


# --- Reporting --------------------------------------------------------------


ROW_GROUP = 122_880  # DuckDB's fixed row group, and so the unit of pruning


def _split(scanned: dict[str, int]) -> tuple[int, int]:
    """Rows read from `edges` and from `edge_services`, kept apart.

    Only `edges` carries the bbox filter, so only `edges` can be pruned by it.
    Summing the two would let a full `edge_services` scan hide the effect entirely.
    """
    edges = sum(v for k, v in scanned.items() if "edge_services" not in k)
    services = sum(v for k, v in scanned.items() if "edge_services" in k)
    return edges, services


def report(
    results: list[Result], n_edges: int, n_services: int, sizes: dict[str, int]
) -> None:
    print(
        f"\n{n_edges:,} edges in {-(-n_edges // ROW_GROUP)} row groups of "
        f"{ROW_GROUP:,}; {n_services:,} edge-service rows"
    )
    print("file sizes: " + ", ".join(f"{k} {v / 1e6:.0f} MB" for k, v in sizes.items()))

    cols = (
        f"{'query':7} {'window':8} {'layout':8} {'rows':>9} "
        f"{'edges read':>12} {'':>6} {'svc read':>12} {'':>6} {'ms':>7}"
    )
    if any(r.fetch_ms is not None for r in results):
        cols += f" {'fetch ms':>9}"
    print("\n" + cols)
    print("-" * len(cols))

    for label in ("window", "weights"):
        for window in WINDOWS:
            for r in [x for x in results if x.query == label and x.window == window]:
                edges, services = _split(r.scanned)
                line = (
                    f"{label:7} {window:8} {r.layout:8} {r.rows:9,} "
                    f"{edges:12,} {100 * edges / n_edges:5.1f}% "
                    f"{services:12,} {100 * services / n_services:5.1f}% {r.ms:7.1f}"
                )
                if r.fetch_ms is not None:
                    line += f" {r.fetch_ms:9.1f}"
                print(line)
            print()


# --- Entry point ------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--edges", type=int, default=4_200_000, help="synthetic edge count")
    ap.add_argument("--runs", type=int, default=5, help="timed runs per query, best wins")
    ap.add_argument("--work", type=Path, default=Path("data/work/bench"))
    ap.add_argument("--fetch", action="store_true", help="also time the full row transfer")
    ap.add_argument("--reuse", action="store_true", help="keep databases from a past run")
    ap.add_argument("--no-indices", action="store_true", help="skip the pipeline's indices")
    args = ap.parse_args()

    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)

    spatial = _have_spatial()
    layouts = ["random", "morton"] + (["hilbert"] if spatial else [])
    if not spatial:
        print("spatial extension unavailable; skipping the hilbert layout")

    src = work / "source.duckdb"
    if not (args.reuse and src.exists()):
        src.unlink(missing_ok=True)
        t0 = time.perf_counter()
        con = duckdb.connect(str(src))
        build_source(con, args.edges)
        con.execute("CHECKPOINT")
        con.close()
        print(f"generated source in {time.perf_counter() - t0:.1f}s")

    con = duckdb.connect(str(src), read_only=True)
    n_edges = int(con.execute("SELECT count(*) FROM edges_src").fetchone()[0])  # type: ignore[index]
    n_services = int(
        con.execute("SELECT count(*) FROM edge_services_src").fetchone()[0]  # type: ignore[index]
    )
    con.close()

    results, sizes = [], {}
    for layout in layouts:
        path = work / f"{layout}.duckdb"
        if not (args.reuse and path.exists()):
            path.unlink(missing_ok=True)
            t0 = time.perf_counter()
            build_layout(path, src, layout, indices=not args.no_indices, spatial=spatial)
            print(f"built {layout} in {time.perf_counter() - t0:.1f}s")
        sizes[layout] = path.stat().st_size
        results += measure(
            path,
            layout,
            runs=args.runs,
            with_fetch=args.fetch,
            spatial=spatial,
            scratch=work,
        )

    report(results, n_edges, n_services, sizes)


def _have_spatial() -> bool:
    """The hilbert layout is optional: it is the only part that needs an extension."""
    try:
        con = duckdb.connect()
        con.execute("INSTALL spatial")
        con.execute("LOAD spatial")
        con.close()
    except duckdb.Error:
        return False
    return True


if __name__ == "__main__":
    main()
