# PLAN

## Done

Scaffold, end to end. 39 tests pass, ruff and mypy clean.

- `acquire` — resumable downloads with `.part` staging and a size floor, because
  BODS sends no `Content-Length`.
- `patterns` — GTFS to distinct ordered stop sequences, in DuckDB.
- `match` — Valhalla, two strategies, interruption-safe.
- `aggregate` — pattern-to-edges inverted to edge-to-services.
- `publish` — GeoJSONL, tippecanoe, PMTiles, with an overflow sidecar.
- `art` — three styles (`density`, `spectrum`, `strands`), PNG and SVG.
- `web/index.html` — MapLibre viewer, hover, service filter, light/dark.
- Docker Compose: `valhalla`, `wayfare`, `matcher`.

## Done — Wales, end to end

Ran 2026-08-06. Numbers in CLAUDE.md. 95.6% of timetabled trips represented;
throughput 3.6/s. Four defects found that the mini fixture could not reach: the
GTFS size floor, the missing `confidence_score`, the redundant OSM download, and
retrying files that were complete but invalid.

## Next — GB

1. **Re-measure on the first national batch.** Wales extrapolates to roughly 12
   hours, but Wales is 85% `shape` and the nation is 48%, and `stops` costs two
   Valhalla calls to `shape`'s one. Treat the Wales rate as a lower bound.
2. **Watch memory on `patterns`.** The group-by over Wales's 0.12 GB
   `stop_times.txt` was trivial; nationally it is 5.09 GB. `WAYFARE_MEM` defaults
   to 8 GB and DuckDB will spill to `temp_directory` — make sure that path has
   room.
3. **Pin the OSM extract.** Set `force_rebuild: "False"` and leave the graph alone
   for the whole run; every `edge_id` in the database depends on it.
4. **Budget the disk.** ~40 GB including the graph.

## Next — correctness

**Look at the 148 skipped and 23 errored patterns.** Wales skipped 4.2% on the
`MAX_STOP_GAP_M` bound. Some are certainly TrawsCymru long-distance coaches, which
genuinely have huge stop gaps and should probably be matched rather than dropped.
Others may be bad stop coordinates. This is now a concrete list to inspect rather
than a hypothetical.

**Check `break_through` at termini and on one-way pairs.** Still untested; the
choice over plain `through` is reasoned but unverified against real geometry.

## Next — correctness

**Validate the `stops` strategy against the `shape` strategy.** Wales is 85%
`shape`, which makes it an unusually good validation set: those patterns are
ground truth for the synthesised ones. Match a
sample of them *both* ways and measure how often the synthesised route recovers
the same edge set. This is the single best available check on the primary code
path, and it costs almost nothing because the data is already there. Report it as
a coverage/agreement figure alongside `status`.

**Direction.** Valhalla edges are directed, and `edge_id` distinguishes the two
directions of a one-way pair. Two things are currently unverified: whether
opposite directions of the same service render as two parallel lines (they
should, on dual carriageways and one-way systems, but should not on ordinary
two-way streets), and whether that reads well on the map. Decide after seeing
Wales rendered.

**Tune the rejection bounds against real output.** Wales rejected 13 patterns as
low confidence and skipped 148 on stop gap. `MIN_MATCH_CONFIDENCE` in particular
has still never rejected anything on merit -- the one shape-path rejection was on
detour, not score -- so 0.30 remains an untested guess.

## Next — scale

**Subsequence reduction.** Many patterns are short workings: a contiguous
subsequence of a longer pattern on the same service. Matching the longest and
deriving the rest could cut Valhalla calls substantially. Worth measuring the
share of patterns this covers before building it — do this after the throughput
measurement, since it only matters if matching turns out to be the bottleneck.

**Wire the overflow sidecar into the viewer.** `publish` writes
`out/overflow.json`, and the viewer currently shows the truncated list with a
note. Central London edges will exceed the cap. Have the viewer fetch the full
list on hover for those.

## Known gaps

**Northern Ireland.** BODS and NaPTAN are both GB-only. Translink publishes
ATCO.CIF via OpenDataNI (Metro & Glider, Ulsterbus & Goldline datasets, refreshed
monthly), which carries no geometry at all — so NI would be 100% `stops`-matched.
It needs a separate parser and the `ireland-and-northern-ireland` OSM extract
(409 MB; there is no standalone NI extract). Sequenced after GB works, because it
shares nothing with the GTFS path except the matcher.

**`calendar_dates` exceptions are ignored** when weighting patterns by trips per
week. They shift individual days rather than the shape of the week, and the number
is only ever a rendering weight — but it does mean a service that runs only on
bank holidays is weighted as if it ran a normal week.

**No incremental update path.** BODS refreshes and Geofabrik rebuilds daily; a
re-run currently redoes everything. Fine for now, since the dataset is a snapshot,
but worth revisiting if this becomes a standing service rather than a one-off
build.

**`agency_id` is carried but unused.** Colouring or filtering by operator is
plausible for both the map and the art, and the data is already there.
