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

## Next — first real run

Nothing has been downloaded yet. The order that de-risks fastest:

1. **Run Wales end to end first.** 41 MB against 1.28 GB, so the whole pipeline
   completes in an evening and every assumption gets tested cheaply. Build the
   Valhalla graph from the `wales-latest.osm.pbf` extract (146 MB) rather than GB
   for this pass.
2. **Measure Valhalla throughput.** This is the one number in the whole design
   that is a guess. The GB run is planned around "a day or two" with no evidence.
   Time 500 patterns of each strategy and extrapolate before committing.
3. **Check `break_through` behaviour at termini and on one-way pairs.** The choice
   over plain `through` is reasoned but untested against real stop coordinates.
4. **Then GB.** Pin the OSM extract, set `force_rebuild: "False"`, and leave the
   graph alone for the whole run — every `edge_id` in the database depends on it.

## Next — correctness

**Validate the `stops` strategy against the `shape` strategy.** The 48% of
patterns with operator geometry are ground truth for the other 52%. Match a
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

**Tune the rejection bounds against real output.** `MIN_MATCH_CONFIDENCE`,
`MAX_DETOUR_RATIO`, `DETOUR_SLACK_M` and `MAX_STOP_GAP_M` are all reasoned
defaults with no data behind them. After the Wales run, look at what each one
actually rejected and whether it deserved it.

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
