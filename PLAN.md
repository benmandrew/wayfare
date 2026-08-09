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

## Done — data-side customisation (prototype)

Added 2026-08-08. Numbers in CLAUDE.md. A style now says how an edge is painted and
`QuerySpec` says which edges there are, so three styles cover the product of the two.

- **`art.QuerySpec`** — `weight` (6), `group` (5), `order` (5), and filters on
  operator, service, road class and a trips floor. A closed vocabulary of SQL
  fragments with bound parameters, never a query language.
- **Exposed on `/art`** and in the studio page, which builds the controls from
  `/art/meta` — including the operators and road classes actually in the database.
- **Defaults reproduce the previous output byte for byte**, checked by rendering the
  pre-refactor and post-refactor modules against one database with the same cairo.
- **`MAX_GROUPS`** refuses a spec that would draw one composited stroke per OSM way.
- 188 -> 334 tests.

Two bugs found on the way, both fixed: `min_trips` reached the flat query but not the
grouped one, so `density` and `strands` drew different networks from one spec; and
`strands` to SVG was never deterministic, because the edges within a ribbon had no
tiebreak and SCREEN compositing hid it from every PNG comparison.

## Done — parallel rendering

Ran 2026-08-09, against the real national database on the server (2,746,261 edges,
8,301,705 edge-service rows) rather than a synthetic one. Numbers in CLAUDE.md.

- **A render is drawn in horizontal bands, one process each.** `uk` at 2,000px:
  `density` 77–98s -> 28–32s, `spectrum` 58–67s -> 21–31s, `strands` 71–72s -> 37–40s.
  At 4,000px `density` 98s -> 42s. **Byte-identical**, for all three styles and for
  letterboxed, scaled, filtered, sampled and wide-lined canvases.
- **Bands cut on edge count, not height.** Equal-height bands gave one of eight 48%
  of the country's edges. That alone was 37s -> 27s.
- **`Source.groups`** carries the window's `(grp, n_edges, trips)` into every band, so
  ribbon widths and draw order stay global. Without it `strands` differed by up to
  4/255 across 2.8% of the image — eight-bit rounding under a reordered SCREEN.
- **`default_workers` reads the cgroup quota**, so the `cpus: 4` render container does
  not start eight processes. `WAYFARE_RENDER_WORKERS` overrides; `--workers` on the
  CLI overrides both.
- 376 -> 391 tests.

Followed up 2026-08-09, after the merge. `default_workers` now counts **physical
cores**, not hardware threads: the box is four cores of eight threads and `uk`
`density` at 2,000px is 26.9s on four workers against 28.1s on eight. 391 -> 395
tests.

Three bugs found on the way: `fork` with an open DuckDB handle kills the child with
no traceback; clipping to the band splits strokes at a raster boundary and cairo's
fixed-point tessellation does not re-add exactly; and a band trusting `config.DB_PATH`
would draw from a different database than the connection it was given.

## Next — make the drawing cheaper, not the query

Banding bought a factor of three by using the cores. What is left is the work itself,
and it is now measured on real data: an edge in the `uk` window simplifies to **2.08
vertices**, so almost every stroke is one tiny segment and its cost is tessellating
two round caps. Replacing round caps with butt/mitre halves a render (55.4s -> 25.5s),
which says where the time is but is not a change anyone wants.

- **Coalescing runs of edges into single subpaths** is the one that keeps the picture:
  `publish` already chains by `way_id` (169,857 -> 53,013 features on Wales), and a
  chain pays two caps instead of two per edge. Needs breaking at weight changes.
  Unbuilt, and now the largest remaining win.
- `ctx.set_tolerance(1.0)` is 78.5% of cairo for a coarser cap arc; `Antialias.FAST`
  is 74.4%. Both change the output, neither is taken.
- `Antialias.GOOD`/`DEFAULT` are byte-identical to `BEST` — the antialias setting is
  not a lever at all.
- `edge_services` still cannot prune, and under banding that is worse than it was:
  it is a per-band floor, which is why more bands than cores is slower.

## Superseded — make the drawing cheaper, not the query

The prototype's measurements point somewhere other than where I expected. A render
is **75% cairo**; the whole database side is a quarter, and the percentile pass under
2%. London's 752,561-edge window is 28.6s, of which 21.5s is stroking.

- Clustering `edges` on a Morton/Hilbert code prunes real work (Cardiff reads 5.9% of
  the table, 22ms -> 4.4ms) and shrinks the file 528 -> 443 MB. Worth doing, but it
  is a 5x win on a quarter of the cost, and Wales is too small to show it.
- `edge_services` cannot prune at all — no bbox column, no pushdown through the join.
- Extracting the window to Parquet was tried and **rejected**: 2,347 -> 2,320 ms.
- The real lever is fewer strokes: drop sub-pixel edges, or coalesce runs the way
  `publish` already does for tiles. Unmeasured.

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
