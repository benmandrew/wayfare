# Measured runs

## Wales, end to end, 2026-08-06

The first real run. Feed version `20260806_022608`, Valhalla 3.8.3, graph built
from `wales-latest.osm.pbf`.

| Stage | Result |
|---|---|
| acquire | 41 MB zip, 0.26 GB unpacked |
| patterns | 37,028 trips -> **3,584 patterns** (10.3x), 2s |
| | 85.2% carry operator geometry (Wales runs far above the 48.3% national figure) |
| Valhalla graph | ~6 min for Wales |
| match | 3,552 patterns in **16m23s at 3.6/s**, 6 workers |
| | ok 3,400 (94.9%) · skipped 148 · error 23 · low_confidence 13 |
| | **95.6% of timetabled trips** represented |
| aggregate | 169,857 edges, 413,915 edge-service pairs, 478 distinct services |
| publish | 169,857 edges -> 53,013 features -> **9.5 MB PMTiles**, no features dropped |
| art | 0.5s per 2400px render |

3.6/s is the honest throughput. An earlier 15.3/s was measured while the confidence
bug was rejecting most patterns instantly, and meant nothing.

**Extrapolating to GB is not a straight multiply.** Wales is 2.4% of national trips,
which suggests roughly 12 hours — but Wales is 85% `shape` and the nation is 48%, and
the `stops` path costs two Valhalla calls instead of one.

## Greater London, 2026-08-07

Run in a separate data root (`data-london`) against its own Valhalla instance on port
8003, because rebuilding the shared graph invalidates every `edge_id` in the Wales
database.

- 304 MB zip, 1.5 GB unpacked, `stop_times.txt` 1.50 GB, 17,611,239 stop times.
- 480,412 trips -> **4,709 patterns**, a 102x collapse against Wales's 10.3x. London
  runs the same route all day at high frequency.
- Only **0.9% carry operator geometry** (44 shapes), against Wales's 85.2% and the
  national 48.3%. London is almost entirely the `stops` path, at two Valhalla calls
  per pattern.
- Matching at **1.0/s** against Wales's 3.6/s. This is the honest cost of `stops` at
  London road density, and a far better basis than Wales for extrapolating to GB.

## Great Britain, 2026-08-07

Complete end to end, on the server, feed `20260807_022616`.

| Stage | Result |
|---|---|
| patterns | **52,554** |
| match | ok 50,395 (95.9%) · skipped 1,555 (3.0%) · error 462 (0.9%) · low_confidence 142 (0.3%) |
| aggregate | 2,746,261 edges, 8,301,705 edge-service pairs |
| cluster | current — `meta.edges_clustered` = 2,746,261, the whole table |
| publish | 130 MB PMTiles |
| graph | pinned at `3.8.3/1786113507` |

These figures predate the stop-gap and chunking fixes recorded in
docs/pipeline.md, so the `skipped` and `error` counts are what those bounds cost
rather than what the pipeline now achieves. Re-matching the 1,555 skipped and 462
error patterns is a `wayfare match --retry skipped,error` away and has not been run.

## Feed churn, Wales, 2026-08-08

The first measurement of churn against two real consecutive feeds. Wales data root
(`data`), previous stored feed `20260806_023912`, new feed `20260808_024504` — a
two-day gap, not a month.

| Measure | Result |
|---|---|
| patterns before -> after | 3,584 -> **3,541** |
| `patterns` log | **30 new** · 0 carried over still unmatched · **73 departed** |
| accounting | 3,584 − 73 + 30 = 3,541, exact |
| `wayfare status` | `patterns_pending` 30, `patterns_departed` 73, 3,327 of 3,541 matched (94.0%) |
| share of the live set | 30 patterns = 0.85%, carrying 2,741 of 146,433 trips = **1.87% of service** |
| catch-up cost | 30 patterns at Wales's measured 3.6/s, **~8s** against 16m23s for the full run |

New patterns run busier than average: 0.85% of the live set carries 1.87% of
timetabled trips. The catch-up is roughly a 120x reduction in match time, which is
what makes an incremental run a routine operation rather than a rebuild.

All 73 departed patterns keep their `match_status` rows — 3,614 pattern rows against
3,584 status rows, 73 of 73 departed still cached — so a seasonal service that
returns is free.

Three caveats travel with the number. Two days is not a month: scaling linearly to
~12.8% over 30 days is an **upper bound**, not an estimate, because the same volatile
patterns flipping in and out repeatedly make the union over 30 days far smaller than
15 two-day windows. It is also Wales, at 85.2% operator geometry against the national
48.3%, and one region rather than the nation. And the run also triggered
`_migrate_pattern_ids`, because this database still held rank ids — that migration
recomputes the identity hash from the stored `pattern_stops` with the same SQL
expression `build_patterns` uses, and aborts on any unmapped pattern or hash
collision, so old and new ids are directly comparable and the churn figure is not a
migration artefact.

## Determinism

All three art styles are byte-identical run to run, which none of them were. Ties
previously fell in whatever order the scan returned, and two runs of the old
`spectrum` differed by 426 bytes. Verified by rendering Cardiff before and after the
streaming rewrite: `density` byte-identical, `strands` differing by 7 bytes out of
5.8M at delta 1, `spectrum` differing more because its ties now resolve differently.
That claim was PNG-only until the `strands`-to-SVG ordering bug was found; see
docs/rendering.md.

## What the render numbers were measured against

The banding, coalescing and cairo figures in docs/rendering.md are the first `art`
measurements taken against real national data — 2,746,261 edges and 8,301,705
edge-service rows, on the four-core, eight-thread box that serves it.

The earlier render speed-ups (simplification, Arrow, one-walk `density`) were
measured against a synthetic 1M-edge database, because the timings that prompted them
came from a window far larger than Wales. The ratios are structural and should hold,
but two numbers are worth re-taking on the real thing: the 62% preview brightness,
which depends on how much the network actually overlaps, and `strands`, whose cost is
dominated by the (service, edge) fan-out rather than by vertices.
