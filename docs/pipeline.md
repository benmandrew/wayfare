# Pipeline

Five stages, each reading what the last wrote, each independently re-runnable:

    acquire  -> raw downloads
    patterns -> 1.55M trips collapse to distinct ordered stop sequences
    match    -> Valhalla; the stage that runs for a day or two
    aggregate-> invert pattern->edges into edge->services
    publish  -> GeoJSONL -> tippecanoe -> PMTiles

## patterns

**A pattern is the unit of work.** Grouping trips by `(route_id, direction,
ordered stop sequence)` is what makes national scale affordable — most trips are
the same physical journey repeated through the day.

**`pattern_id` is an identity hash, not a popularity rank.** It used to be
`row_number() OVER (ORDER BY count(*) DESC)` in `gtfs.py`, recomputed every run, so
a journey got a different id whenever some other route gained a trip. `patterns`
also did `DELETE FROM patterns` while leaving `match_status` and `pattern_edges`
alone, so a second run against an existing database would have silently re-pointed
matched edges at the wrong patterns. Nobody had hit it because each region so far
used a fresh data root. It is now `hash(route_id || direction || ordered stop ids)
>> 1` cast to BIGINT — the shift keeps it inside a signed BIGINT — built by
`db.pattern_id_sql`. Nothing that varies between feeds may enter the identity: not
trip counts, not shape ids, not operator, or the cache misses.
`gtfs._check_unique_ids` refuses to build on a collision rather than merging two
patterns.

**`match_status` is a permanent cache keyed on pattern identity**, not a per-run
table. `patterns` carries `first_seen`/`last_seen` feed-version columns and merges
rather than deletes. A pattern that leaves the timetable keeps its row and its
match results; a seasonal service that returns is already matched and costs
nothing. Every consumer of `patterns` filters on `db.current_feed()` — match work
selection, aggregate, coverage, prune and shape loading — so departed patterns are
never matched, aggregated or rendered.

**Old databases migrate in place; they are not re-matched.**
`db._migrate_pattern_ids` recovers each pattern's identity from the `pattern_stops`
rows already stored and renumbers `patterns`, `pattern_stops`, `match_status` and
`pattern_edges` together. It aborts loudly on a collision or on a pattern whose
stops are missing. A national match run costs a day or two, so every migration must
be a rewrite rather than a re-run.

**The unmeasured number that decides everything is feed churn** — how many patterns
are new month to month. `patterns` logs new / carried over still unmatched /
departed every run, and `wayfare status` reports `patterns_pending` and
`patterns_departed`. It has not yet been measured against two real consecutive BODS
feeds.

**GTFS ids stay strings.** Route "07" must not become 7, or the join silently loses
every service with a leading zero. Hence `all_varchar=true` on every `read_csv`.

## match

**Valhalla is the only engine that returns OSM way ids from map matching** without
a custom graph build. `/trace_attributes` exposes `edge.way_id` directly. OSRM
discards way ids at extract time and can only return node ids; GraphHopper needs
`osm_way_id` added as an encoded value and the graph reimported.

**Way ids appear only in Valhalla's native response.** Asking for `format=osrm`
silently drops them. This is the kind of failure that looks like empty data rather
than an error.

**Valhalla `edge.id` is a GraphId, stable only within one graph build.** It is the
join key for the whole pipeline, so the OSM extract is pinned for the duration of a
run (`force_rebuild: "False"` in docker-compose.yml). Rebuild the graph and every
`edge_id` in the database becomes meaningless. `way_id` is the durable identity;
keep it. Geofabrik rebuilds daily, so this is not hypothetical.

**A graph rebuild is a full re-match, and is guarded rather than silent.**
`match.pin_graph` records Valhalla's `tileset_last_modified` in `meta.graph_id` on
the first run and refuses to add to the database against a different build;
`--force-graph` overrides. Mixing two GraphId spaces in one table renders fine and
is wrong. If Valhalla reports no tileset timestamp the guard warns and stands down
rather than pretending to work.

**Two matching strategies**, chosen per pattern in `valhalla.Client`:

- `shape`: operator geometry exists. Dense trace, `map_snap`, one call.
- `stops`: no geometry. Route the stops with `bus` costing and `break_through`
  locations to synthesise road geometry, then `edge_walk` that result to recover
  edges exactly. Two calls. Falls back to `map_snap` if `edge_walk` refuses on a
  chunk-stitch discontinuity.

Confidence from the `stops` path is deliberately reported as 0.0, not 1.0: it is a
guess about which roads the bus takes, not an observation of it, and `edge_walk`
returns 1.0 by construction.

**The match stage must survive interruption.** It runs for days on a server that
may reboot. Work is selected by the *absence* of a `match_status` row. This means
one batch is both the unit of concurrency and the unit of checkpointing, and they
cannot be separated: a batch still in flight is still selectable, so loading the
next batch before committing the last hands the same patterns out twice. This was a
real bug, caught in testing. Do not reintroduce pipelining across batch boundaries
without adding an in-flight exclusion.

**Failures are recorded, not retried.** A pattern whose stops cannot be connected
by road will never succeed. A matcher that retries it on every restart never
finishes. Every outcome gets a row, including `no_route`, `error` and `skipped`.

**Bad geometry is worse than missing geometry.** A wrong match produces a
confident-looking line down a road no bus uses. `low_confidence` rows are kept (so
they are never retried) but their edges are dropped.

**The detour check needs both a ratio and an absolute slack.** On a short pattern a
ratio alone is meaningless — a one-way system that sends the bus around one block
triples a 300 m span. Both `MAX_DETOUR_RATIO` and `DETOUR_SLACK_M` must be
exceeded.

**Spreading the work is `--max-seconds`.** Checked between batches, never inside
one, because a batch is the unit of checkpointing — so the budget is a floor on run
length, not a ceiling. It composes with the existing absence-of-a-status-row work
selection with no other bookkeeping: a run stopped by the budget is
indistinguishable from one that was killed. Combined with `ORDER BY n_trips DESC`,
a nightly job with a half-hour budget spends it on the busiest roads first, so a
partially drained queue degrades gracefully. `publish` can be spread by rebuilding
one region's PMTiles per night, which the multi-region viewer already supports.

## Storage

**Storage is one DuckDB file** (`work/wayfare.duckdb`). DuckDB rather than SQLite
because the central operation is a group-by over a 5 GB CSV, done out of core. The
minimal-dependency discipline that governs `ontime` does not apply here — this is an
offline batch pipeline, not a 62 MB container.

**Geometry is stored as integer micro-degrees, not WKT.** `edges` carries
`lon_e6`/`lat_e6` INTEGER lists plus four bbox columns; 1e-6 of a degree is 11 cm.
This deleted the first-vertex regex, the pad-by-longest-edge collar and the Python
re-test that the art window query needed — the window test is now an exact integer
overlap. `shapes` is one row per shape rather than one row per point: 1.7M rows for
Wales, on the order of 100M nationally. It is input to `match` and nothing else, so
`wayfare prune` drops it once matching completes. Wales: 160 MB -> 114 MB compacted.
Migration runs on connect and rewrites in place, because a national match run costs
a day or two and a schema change it cannot survive is one nobody applies.

**DuckDB takes a single writer.** Match workers do HTTP only; the main thread
writes.

**DuckDB cannot spill an ordered list aggregate.** `list(x ORDER BY y)` pins its
per-group sort state in memory. Collapsing trips to stop sequences died on the
London feed (480,412 trips, 1.5 GB `stop_times.txt`) at "failed to pin block",
identically at a 7.4 GB limit and at a 10.2 GB limit on a 17 GB machine. Raising the
limit is not a fix, it only moves the wall. The fix is to project the needed columns
into a table in one streaming scan (which does spill), then aggregate in 16
partitions keyed on `hash(trip_id)`. London then collapsed 17,611,239 stop times in
6 seconds inside an 8 GB limit. `stop_times.txt` is 5.09 GB nationally, so this
would have hit there too. `WAYFARE_MEM` defaults to 8 GB and DuckDB spills to
`temp_directory`, so that path needs room.

**A DuckDB connection holds one result at a time, and a second query abandons the
first silently.** Not an error, not a short read anything notices — a 200,000-row
stream interrupted after its first batch simply ends at 20,000 and looks complete.
This is why `Window.paths` resolves `self.weights` *before* it opens its stream:
making that scale lazy put its query inside the draw loop, where it truncated every
`density` and `spectrum` render to its first fetch. The renders were stable,
plausible, and wrong. Anything else that becomes lazy on this connection inherits
the same trap.

**DuckDB inserts about 2,700 rows/s through executemany, and 1.6M/s from a file.**
It is columnar; every bound-parameter insert pays the full per-statement machinery.
This is not a small constant — roughly 450 of the Wales match run's 983 seconds went
on inserting rather than matching, with Valhalla under 1% CPU throughout. `match`
stages each batch to a file and reads it back: CSV for `pattern_edges`,
newline-delimited JSON for `edges` because it carries INTEGER[] geometry and road
names holding quotes and commas. Multi-row VALUES and unnest of parallel arrays are
no better than executemany. Never add a row-at-a-time insert loop on a table that
grows with the network.

## Clustering

**Clustering `edges` on a space-filling curve does prune, and it is `wayfare
cluster`.** DuckDB keeps min/max zonemaps per 122,880-row group, and `match` inserts
edges in batch order, which is spatially random, so unclustered they prune nothing.
Ordering the table by a Morton code over the bbox centre makes a city window touch a
handful of groups: Cardiff read 100% -> 11.7% of `edges`, 22 ms -> 4.4 ms; London
100% -> 26.3%, 30 ms -> 16 ms. It also shrinks the file, 528 -> 453 MB. Verified from
DuckDB's own `operator_rows_scanned`, not inferred from wall time. Hilbert reaches
5.9% on Cardiff but only beats Morton on the smallest window, so it is not worth the
`spatial` extension on its own and is benchmark-only. But this is a 5x improvement to
a quarter of the cost, and Wales at ~2 row groups cannot show it at all.

`db.morton_sql` is the one implementation; `scripts/bench_window.py` calls it rather
than keeping its own, so what the benchmark measures and what the command does cannot
drift. `db.CLUSTER_BOX` is a fixed grid over Great Britain rather than the data's
extent, so a region's layout does not depend on which region it is; the code is a
physical row order and never an identity, so changing the box is harmless beyond
needing a re-run.

**DuckDB never gives space back below a file's high-water mark, so `cluster` has to
write a new file.** Reordering in place is the obvious implementation and it makes
the database *bigger*: the dropped table's blocks stay allocated, and neither
`CHECKPOINT` nor `VACUUM` reclaims them — measured at 505 MB going to 730. `COPY FROM
DATABASE` into a freshly attached file is what actually reclaims them, and it
preserves row order, so the curve survives the copy. `db.cluster` therefore reorders
in place, copies to `<db>.compacting`, reopens and counts it, re-asserts the indexes
(the copy carries data, not necessarily every index), and only then does an atomic
rename. 529 MB -> 471 MB on the benchmark's 4.2M edges. It needs room for a second
copy while it runs, and an interruption at any point leaves the original untouched.

**Clustering goes stale rather than off, and `wayfare status` says so.**
`cluster_edges` records the row count it sorted in `meta.edges_clustered`. Rows
`match` adds afterwards land unsorted on the end, where no zonemap can help, so
`status` reports `yes`, `no`, or `stale (N of M edges sorted)`. It is a separate
command rather than a step in `aggregate` for the same reason `prune` is: it rewrites
the whole table, needs room for a second copy of `edges` while it does, and is worth
doing once after a match run rather than on every re-aggregation.

**`edge_services` cannot prune, and giving it a bbox column is not worth it.** It
carries no bbox column and DuckDB pushes no min/max filter through the join, so the
weights pass reads all 10.25M rows under every layout. The bbox column was the obvious
way through and has been measured: it prunes almost exactly as hoped — cardiff
10,250,638 rows → 614,400, London → 2,457,600 — and buys 40.9ms → 31.1ms on cardiff,
356.1ms → 357.6ms on London, and nothing nationally. Four `INTEGER` columns on the
largest table in the database for 10ms on the smallest window. The scan was never what
cost anything; see docs/rendering.md.

## publish

**In Mapbox Vector Tiles (MVT) the cost that matters is per feature, not per
attribute value.** MVT pools attribute values per layer per tile, so a feature pays
two varints to point into the pool. Long service lists are cheap; feature counts are
not. A Valhalla directed edge averages 4.14 coordinates and tens of metres, so one
feature per edge was the root cost — and at four points a feature `--simplification=4`
had nothing to remove, so low zooms carried full-detail geometry and tippecanoe fell
back on `--drop-densest-as-needed`, shedding whole roads.

**Tile features are coalesced, and coalescing must stay lossless.** Runs of edges that
share every tile attribute (`way_id`, road name, service set, trip count) and meet end
to end merge into one feature. Chaining stops where three of a group's edges meet: at a
fork, picking a continuation draws a line that doubles back. Directed pairs collapse
only where the service sets agree, so a one-way pair carrying different buses each way
still renders as two lines, while an ordinary two-way street no longer renders one line
invisibly under another. Wales: 169,857 directed edges -> 102,925 after collapsing
pairs -> 53,013 after chaining. This is a publish-stage concern only; `art` reads raw
directed edges and is unaffected.

**The `refs` cap is 64 and there is no overflow sidecar.** Only 1,405 of Wales's
169,857 edges held more than 12 services and the longest held 53, so 64 clears Wales
outright. Raise the cap again rather than reintroducing a sidecar — pooling makes the
list cheap. Card-only attributes are confined to z11+. Bucketing `trips` to a log scale
was tried and rejected: it saves a further 2.1%, because MVT already pools the 1,759
distinct values, and costs an approximate figure in the info card.

**The export is deterministic, and must stay that way.** Two things made it not be:
`list(short_name ORDER BY n_trips DESC)` with no tiebreak, so equally busy services came
back in arbitrary order and that order was part of the coalescing key; and `_chain`
starting a closed loop wherever the scan happened to begin. A rebuild now produces
byte-identical output, which is what makes tiles cacheable and two runs comparable.

**Archive size, same data throughout** (Wales): 23.8 MB baseline -> 20.9 MB with the
edge id moved into the MVT feature id field (`--use-attribute-for-id`) -> 13.7 MB with
coalescing -> 11.1 MB with card-only attributes confined to z11+ -> 9.5 MB once the
refs-ordering bug stopped splitting segments. 60% in total. Tippecanoe reports no
features dropped and every zoom holds the full network; the first build was thinning
the densest tiles to 27% of their features.

`publish.export_geojsonl` streams by `way_id` rather than materialising the table: 617
-> 372 MB peak RSS on Wales.
