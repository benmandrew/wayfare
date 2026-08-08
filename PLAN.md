# PLAN

## Done

Scaffold, end to end. 123 tests pass, ruff and mypy clean.

- `acquire` — `.part` staging, archive validation rather than a size floor, and
  Range resumption for the one host that supports it.
- `patterns` — GTFS to distinct ordered stop sequences, in DuckDB.
- `match` — Valhalla, two strategies, interruption-safe.
- `aggregate` — pattern-to-edges inverted to edge-to-services.
- `publish` — GeoJSONL, tippecanoe, PMTiles.
- `art` — three styles (`density`, `spectrum`, `strands`), PNG and SVG.
- `web/index.html` — MapLibre viewer, hover, service filter, light/dark.
- Docker Compose: `valhalla`, `wayfare`, `matcher`.

## Done — Wales, end to end

Ran 2026-08-06. Numbers in CLAUDE.md. 95.6% of timetabled trips represented;
throughput 3.6/s. Four defects found that the mini fixture could not reach: the
GTFS size floor, the missing `confidence_score`, the redundant OSM download, and
retrying files that were complete but invalid.

## Done — data representation

Ran 2026-08-06. Numbers in CLAUDE.md.

- **Tile coalescing.** Runs of edges with identical tile attributes that meet end
  to end merge into one feature: 169,857 directed edges -> 102,925 after
  collapsing directed pairs -> 53,013 after chaining along the way. Lossless.
- **Archive 23.8 MB -> 9.5 MB (60%)**, and tippecanoe now drops no features at
  any zoom. The first build was thinning the densest tiles to 27%.
- **`refs` cap 12 -> 64, overflow sidecar deleted.** Wales's longest list is 53.
- **Geometry as micro-degree integer lists** plus bbox columns, replacing WKT.
  `shapes` is one row per shape and `wayfare prune` drops it after matching.
  Wales database 160 MB -> 114 MB compacted, migrated in place on connect.
- **`patterns` partitioned on `hash(trip_id)`** to get round DuckDB's inability to
  spill an ordered list aggregate.

## Done — streaming and determinism

Ran 2026-08-06. Numbers in CLAUDE.md.

- **`art` streams its window** rather than materialising it, and the weight scale
  comes from a trip-count pass at 8 bytes an edge. Peak RSS on the `uk` window:
  density 479 -> 259 MB, strands 617 -> 312 MB. `publish.export_geojsonl` streams
  by `way_id`: 617 -> 372 MB on Wales.
- **All three art styles are byte-identical run to run**, which none of them were.

## Done — incremental rebuild

Ran 2026-08-07. Detail in CLAUDE.md. A monthly refresh now costs only the patterns
that are new.

- **`pattern_id` is an identity hash**, not a popularity rank recomputed every
  run. A second run against an existing database would have re-pointed matched
  edges at the wrong patterns; every region so far used a fresh data root, so
  nobody hit it.
- **`match_status` is a permanent cache.** `patterns` carries
  `first_seen`/`last_seen` and merges rather than deletes, and every consumer
  filters on the current feed version.
- **`--max-seconds`** spreads matching over nightly runs, checked between batches
  and paired with `ORDER BY n_trips DESC` so the busiest roads go first.
- **`match.pin_graph`** refuses to add to a database matched against a different
  Valhalla tileset; `--force-graph` overrides.
- **Old databases migrate in place** from the stored `pattern_stops`, aborting on
  a collision rather than merging two patterns.
- `tests/test_incremental.py` is new; 109 -> 123 tests.

## Done — rendering on the server

Added 2026-08-08. Detail in CLAUDE.md. Iterating on a style no longer means having
the data on the machine doing the iterating.

- **`GET /art`** renders a window in a style with its knobs as query parameters, and
  answers PNG or SVG. `GET /art/meta` reports the styles, presets, defaults and
  limits, so the UI is built from the server rather than compiled against it.
- **`web/art.html`** is a studio page: sliders, live preview at a cheap width,
  separate export width, and the whole parameter set in the URL hash.
- **Serving moved into the package** as `wayfare serve`, so the endpoint is under
  mypy and the test suite. `scripts/serve.py` is a shim.
- **`art.render_bytes`** shares `_render` with the file path; a `BytesIO` for a sink
  is the only difference.
- Bounded: one render at a time, 64 megapixels, a queue limit, and the database
  opened read-only for one render so the pipeline's write lock is never blocked.
  `WAYFARE_ART=off` switches the endpoint off entirely.

Not done: `edges` still has no spatial index, so a national window reads the whole
table however it is asked for. Measured only against Wales-scale data.

## In progress — Greater London

Running in a separate data root (`data-london`) against its own Valhalla instance
on port 8003, built from
`europe/united-kingdom/england/greater-london-latest.osm.pbf`. A second instance
rather than a rebuild, because rebuilding the shared graph invalidates every
`edge_id` in the Wales database.

- 304 MB zip, 1.5 GB unpacked, `stop_times.txt` 1.50 GB, 17,611,239 stop times.
- 480,412 trips -> **4,709 patterns**, a 102x collapse against Wales's 10.3x.
  London runs the same route all day at high frequency.
- Only **0.9% carry operator geometry** (44 shapes), against Wales's 85.2% and the
  national 48.3%. London is almost entirely the `stops` path, at two Valhalla
  calls per pattern.
- Matching at **1.0/s** against Wales's 3.6/s, ETA about 1h15m. This is the honest
  cost of `stops` at London road density, and a far better basis than Wales for
  extrapolating to GB.

## Next — GB

1. **Extrapolate from London, not Wales.** Wales's 3.6/s is a `shape`-path rate on
   sparse roads; London's 1.0/s is a `stops`-path rate on dense ones, and the
   nation at 48% `shape` sits between them. Re-measure on the first national batch
   regardless.
2. **Watch memory on `patterns`.** The partitioned aggregate handled London's 1.50
   GB `stop_times.txt` in 6 seconds inside an 8 GB limit; nationally the file is
   5.09 GB. `WAYFARE_MEM` defaults to 8 GB and DuckDB spills to `temp_directory` —
   make sure that path has room.
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

**Validate the `stops` strategy against the `shape` strategy.** Wales is 85%
`shape`, which makes it an unusually good validation set: those patterns are
ground truth for the synthesised ones. Match a
sample of them *both* ways and measure how often the synthesised route recovers
the same edge set. This is the single best available check on the primary code
path, and it costs almost nothing because the data is already there. Report it as
a coverage/agreement figure alongside `status`.

**Eyeball the coalescing on the rendered map.** The merge is lossless by
construction and the counts check out, but nobody has yet confirmed that the
merged segments read well on hover — a feature now spans a run of edges, so the
highlighted stretch and the info card cover more road than before. Look in
particular at forks, where chaining stops, and at long uniform ways.

**Direction.** Valhalla edges are directed, and `edge_id` distinguishes the two
directions of a one-way pair. Coincident pairs now collapse where the service sets
agree, which handles ordinary two-way streets, and a one-way pair carrying
different buses each way still renders as two lines. What remains unverified is
whether dual carriageways and one-way systems read well as two parallel lines on
the map. Decide after seeing Wales rendered.

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

**Feed churn is unmeasured.** The incremental path is built and `patterns` logs
new / carried over / departed on every run, but nobody has yet put two consecutive
BODS feeds through it. That number decides whether a monthly refresh takes minutes
or hours, and it is one refresh away from being known.

**A graph rebuild is still a full re-match.** Geofabrik rebuilds daily and every
`edge_id` depends on the build, so a new graph invalidates the whole edge table.
`match.pin_graph` refuses rather than silently mixing two GraphId spaces, but
nothing reuses matches across builds. `way_id` survives a rebuild, so re-anchoring
on it is the obvious thing to try.

**`art` still consumes raw directed edges.** Coalescing is a publish-stage
transform, so the renders are unaffected by it — no benefit, no regression. If a
future change moves coalescing upstream into `aggregate`, art becomes a second
consumer of that decision and would need its own check.

**`agency_id` is carried but unused.** Colouring or filtering by operator is
plausible for both the map and the art, and the data is already there.
