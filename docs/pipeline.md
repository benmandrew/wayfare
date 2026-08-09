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

**Failures are recorded, not retried, and that needs "failed" to mean
"impossible".** A pattern whose stops cannot be connected by road will never
succeed. A matcher that retries it on every restart never finishes. Every outcome
gets a row: `ok`, `low_confidence`, `no_route`, `skipped` and `error` are all
permanent, and `transport_error` is the one that is not. Two of the three things
that used to share `error` were never permanent at all, and the national run is what
exposed both.

**`NoRoute` was never once raised.** `_post` tested for `"no route" in body.lower()`,
and Valhalla's 442 says "No path could be found for input", so `no_route` has zero
rows in Wales, in London and in Great Britain — 70 of 82 local error rows are a
permanent no-path filed as a fault. Every failure Valhalla reports arrives as HTTP
400. Its `src/exceptions.cc` gives 154, 170, 171, 172, 440, 441, 442, 443 and 444 the
same status, so the status line carries no meaning and `error_code` in the JSON body
carries all of it. That set is `valhalla.NO_PATH_CODES`, and 444 is the ferry code in
`docs/data.md`. Matching a third party's English is what broke this; do not replace
one prose match with another.

**A refused connection is `transport_error`, and it is retryable.** `match.py` caught
bare `Exception` and wrote `error`, so a `requests.ConnectionError` or `Timeout`
landed with the genuinely impossible: 262 of Great Britain's 462 error rows (56.7%),
carrying 4,127 trips — 120 connection refusals on `/trace_attributes` (2,107 trips),
107 on `/route` (1,696), 22 read timeouts (55) and 13 `RemoteDisconnected` (269), all
against one host. 227 refusals means Valhalla was down or restarting, and every
pattern handed out in that window became a silent hole in the map with nothing that
would ever retry it.

`valhalla.TransportError` is deliberately *not* a `ValhallaError`. `match_stops`
retries an `edge_walk` refusal as a `map_snap` on `except ValhallaError`, and a
second call down a dead socket is pointless. HTTP 5xx joins it, because codes 102,
203 and 402 all mean "The service is shutting down". `_match_batch` catches
`requests.RequestException` explicitly and leaves the bare `except` as the last
resort for a bug in this code, which stays permanent: a defect that retries for ever
is worse than one that records the traceback and moves on.

**`--retry transient` is the alias for the statuses safe to clear unattended**, and it
holds `transport_error` alone. A literal status is still accepted and is still for
after fixing the matcher. `run` warns when transport rows are present rather than
clearing them itself, so a run stays reproducible and the hole stays visible. Both
the retry and the reclassify below must land before the first batch loads, for the
in-flight reason above.

**An existing database needs `wayfare match --reclassify-transport` once.** The old
rows are told apart by the shape of the detail this codebase writes rather than by
anyone's wording: a reply from Valhalla is stored as `"<http status>: <json body>"`,
so `status = 'error' AND NOT regexp_matches(detail, '^[0-9]{3}: ')` is every row that
never got one. The `confidence_score` misconfiguration is the one ValhallaError
raised without a reply to quote, and it is excluded by name through
`valhalla.NO_SCORE_MESSAGE`. Reclassify, then `--retry transient`. It is a command
rather than a migration on connect because it decides what gets re-matched, and a
national match run costs a day or two.

**Bad geometry is worse than missing geometry.** A wrong match produces a
confident-looking line down a road no bus uses. `low_confidence` rows are kept (so
they are never retried) but their edges are dropped.

**The detour check needs both a ratio and an absolute slack.** On a short pattern a
ratio alone is meaningless — a one-way system that sends the bus around one block
triples a 300 m span. Both `MAX_DETOUR_RATIO` and `DETOUR_SLACK_M` must be
exceeded.

**The stop-gap bound was a bound on long-distance coach, not on bad data.**
`config.MAX_STOP_GAP_M` was 25 km, and a pattern with any consecutive stop pair
further apart than that was recorded `skipped` without ever being matched. Nationally
that skipped 1,555 patterns and 63,341 trips, 1.64% of every trip in the feed. Triage
of all 1,555 turned up no null-island stops, and the only stops outside GB are real
international coach halts — Paris Bercy, Amsterdam Sloterdijk, Brussels-North. 1,299
of the 1,555 are National Express or FlixBus, median 6 stops, median longest leg
147 km. The bound is now 180 km, and it is derived rather than chosen:
`config.VALHALLA_MAX_DISTANCE_M` (200,000) times `VALHALLA_DISTANCE_HEADROOM` (0.9).
Recovery measured against the completed national run, where *routable* means every
stop in GB and the chain inside the cap:

    50 km    356 patterns    15,566 trips    325 routable    14,186 routable trips
    100 km   769             32,122          619             24,074
    150 km   1,120           47,114          744             29,552
    180 km   1,319           56,720          808             34,851

200 km is Valhalla's own `service_limits.bus.max_distance`, past which it refuses with
error 154, and 630 of the 1,555 span more than 200 km and cannot be routed at any
setting. Filling the cap exactly buys nothing and only converts an honest `skipped`
row into an `error` one.

**The 200 km cap bites in two places, and it is the second one that actually bit.**
Two Valhalla limits ship at 200 km. `service_limits.bus.max_distance` is checked by
`/route` against the straight-line chain through the request's locations;
`service_limits.trace.max_distance` is checked by `/trace_attributes` along the shape
the request submits. The `stops` path calls both — route the stops to synthesise road,
then walk that road. The expectation was that a 40-location chunk of a long pattern
would blow the route cap, so `valhalla._chunks` gained a cumulative-distance bound
alongside its existing location count (40, which exists for request size and is a
different constraint). That is right and necessary — 40 coach stops are half the
country and 40 city stops are a suburb — but it is not what was failing. What fails is
the walk. Road is longer than the straight line it follows, by 1.26x and 1.58x on the
two long Welsh patterns tested. A Welsh pattern with a 183.7 km stop chain routes
without complaint as one 17-location request, then fails 154 on the trace of the
232.2 km shape that came back; another, 173.4 km of chain, produced 273.8 km of road.
So `_chunks` is now used twice, on the stops before routing and on the synthesised
road before walking it, with the same ceiling both times. Chunking only the route
would have converted `skipped` rows into `error` rows, which is the net loss the whole
change exists to avoid. Parts overlap by one point, so a boundary falling inside an
edge puts that edge at the end of one walk and the start of the next; the merge keeps
the first occurrence, which loses the tail of one edge's geometry and no edge
identity. Counting it twice would inflate `road_m` instead.

Splitting the walk moves the `edge_walk` fallback rather than adding to it. Over 50 of
Wales's 348 `stops` patterns, every single one falls back to `map_snap` today — the
exact walk refuses on all 50 — and every single one still comes back as one part under
the distance bound, reproducing its stored `pattern_edges` row for row. Where the walk
does split, a chunk-stitch discontinuity now downgrades the part it falls in instead
of the whole trace, so the fallback covers less geometry than it did rather than more.

**Route 461's error 154 is the Wales extract, not the chunking.** All 10 of the Wales
run's error-154 rows are route 461, 55 to 63 stops over 60-70 km chains. It is a
cross-border service, Llandrindod Wells to Hereford, and the Wales-only OpenStreetMap
extract has no roads east of the border, so its English stops snap back to the nearest
Welsh road. Per-leg routing shows six legs of 21 to 78 km of road for 0.5 to 7 km of
straight line, and several consecutive legs of 0.00 km where two stops snap to the
same node. The whole 63 km chain routes as 260 km. Chunking cannot shorten it:
measured at chunk sizes 40, 20, 10, 5, 3 and 2 the road stays 260.0 km, so the length
is the graph's and not the request's. With the walk split it now traces rather than
erroring — 1,266 edges — and lands in `low_confidence` at a detour ratio of 4.1, edges
dropped, row kept. That is the right answer for geometry that is an artefact of the
extract, and a better one than `error`, which reads as a bug.

**The gap bound guards guesswork, so it must not be applied before the strategy is
chosen.** `match_one` tested the gap several lines before it chose between
`match_shape` and `match_stops`, so the bound was deciding for a path it was never
written for. The reasoning on it — routing a long leg invents a plausible-but-wrong
motorway — holds for `stops` and does not hold at all for `shape`: with an operator
trace there is no routing and no guess, `map_snap` follows geometry the operator
recorded, and the distance between two timing points says nothing about whether that
geometry is good. This qualifies the "bad geometry is worse than missing geometry"
entry above: that reasoning is about a guess, not about a recording. The test is now
gated on `p.shape`. GB was losing 153 patterns and 6,062 trips to it, and the Republic
of Ireland's feed loses 333 of 2,853 patterns, 11.7%, and 8,395 of 148,255 weekly
trips — proportionally far worse only because that feed carries a shape on every trip,
so it cannot lose the argument on the other path. The detour check does not catch
them, which was the obvious risk and was measured. Wales shape-path detour ratio by
longest-leg band: under 2 km, median 1.17, max 3.78; 2-5 km, 1.13 and 1.86; 5-10 km,
1.13 and 1.60; 10-25 km, 1.14 and 1.34. A long leg makes a traced match straighter,
not wilder, and the one Welsh shape-path `low_confidence` sits in the under-2 km band,
which is the regime `DETOUR_SLACK_M` was written for. The two long patterns the raised
bound admits measure 1.26 and 1.58 against a ratio of 3.0, so `MAX_DETOUR_RATIO` and
`DETOUR_SLACK_M` are unchanged. `p.max_gap_m` is still computed for every pattern, and
`load_batch` logs how many carry a trace across a leg past the bound, because a leg
that long is still the thing that drops a pattern on the other path and a trace does
not rule out the stop coordinate that produced it being wrong. The bound was never
wrong about long legs; it was wrong about what a long leg is evidence of.

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
