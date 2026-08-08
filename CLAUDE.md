# wayfare

UK-wide dataset of bus routes snapped to the road network, from DfT BODS open
data. Two consumers: an interactive web map (hover a road, see which buses use it)
and artistic renderings of areas.

## Hard-won facts — do not rediscover these

Measured against the live feeds on 2026-08-06, feed version `20260806_022608`.

**Only 48.3% of trips carry a `shape_id`.** 748,087 of 1,549,590. This is the
single most important fact about the project. The split is strictly per-operator
and all-or-nothing, tracking whose scheduling software emits TransXChange
`TrackPoint`s — Stagecoach North East 100%, Go North East 0%, Arriva North East
0%. So **map matching is the primary path, not a fallback**. Where shapes do
exist they are genuine road geometry (median 849 points, p90 2,109, max 3,705),
not stop-to-stop lines.

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

**BODS sends no `Content-Length`.** A truncated download looks exactly like a
complete one. Hence `MIN_GTFS_BYTES` and the `.part` staging in `acquire.py`.

**BODS blocks requests that look like generic scrapers.** A real User-Agent is
required; see `config.USER_AGENT`.

**Sizes.** National GTFS: 1.28 GB zipped, 7.84 GB unpacked, `stop_times.txt` 5.09
GB, `shapes.txt` 2.53 GB, 1.55M trips. OSM Great Britain: 2.16 GB. NaPTAN CSV:
102 MB, 435k records. Budget ~40 GB of disk including the Valhalla graph.

**Regional slugs** (`config.BODS_GTFS_URL`): `all`, `england`, `scotland`,
`wales`, `north_east`, `north_west`, `yorkshire`, `east_midlands`,
`west_midlands`, `east_anglia`, `london`, `south_east`, `south_west`. Use
`wales` (41 MB) for development.

**Geofabrik files Greater London under `england/`**, not at the top level like the
nations. The `london` slug therefore fell back silently to the 2.16 GB Great
Britain extract until `config.OSM_EXTRACTS` got an entry.

**In Mapbox Vector Tiles (MVT) the cost that matters is per feature, not per
attribute value.** MVT pools attribute values per layer per tile, so a feature
pays two varints to point into the pool. Long service lists are cheap; feature
counts are not. A Valhalla directed edge averages 4.14 coordinates and tens of
metres, so one feature per edge was the root cost — and at four points a feature
`--simplification=4` had nothing to remove, so low zooms carried full-detail
geometry and tippecanoe fell back on `--drop-densest-as-needed`, shedding whole
roads.

**DuckDB cannot spill an ordered list aggregate.** `list(x ORDER BY y)` pins its
per-group sort state in memory. Collapsing trips to stop sequences died on the
London feed (480,412 trips, 1.5 GB `stop_times.txt`) at "failed to pin block",
identically at a 7.4 GB limit and at a 10.2 GB limit on a 17 GB machine. Raising
the limit is not a fix, it only moves the wall. The fix is to project the needed
columns into a table in one streaming scan (which does spill), then aggregate in
16 partitions keyed on `hash(trip_id)`. London then collapsed 17,611,239 stop
times in 6 seconds inside an 8 GB limit. `stop_times.txt` is 5.09 GB nationally,
so this would have hit there too.

**OSM `route=bus` relations are not viable as a source.** 12,968 nationally, only
818 `route_master` relations, and Greater London alone is 13% of the total. BODS
is the authority for what services exist; OSM is only the geometry substrate.

**Northern Ireland has no GTFS.** BODS and NaPTAN are both GB-only. Translink
publishes ATCO.CIF via OpenDataNI, which carries no geometry at all. Not yet
covered — see PLAN.md.

## Architecture

Five stages, each reading what the last wrote, each independently re-runnable:

    acquire  -> raw downloads
    patterns -> 1.55M trips collapse to distinct ordered stop sequences
    match    -> Valhalla; the stage that runs for a day or two
    aggregate-> invert pattern->edges into edge->services
    publish  -> GeoJSONL -> tippecanoe -> PMTiles

**A pattern is the unit of work.** Grouping trips by `(route_id, direction,
ordered stop sequence)` is what makes national scale affordable — most trips are
the same physical journey repeated through the day.

**Two matching strategies**, chosen per pattern in `valhalla.Client`:
- `shape`: operator geometry exists. Dense trace, `map_snap`, one call.
- `stops`: no geometry. Route the stops with `bus` costing and `break_through`
  locations to synthesise road geometry, then `edge_walk` that result to recover
  edges exactly. Two calls. Falls back to `map_snap` if `edge_walk` refuses on a
  chunk-stitch discontinuity.

Confidence from the `stops` path is deliberately reported as 0.0, not 1.0: it is a
guess about which roads the bus takes, not an observation of it, and `edge_walk`
returns 1.0 by construction.

**Storage is one DuckDB file** (`work/wayfare.duckdb`). DuckDB rather than SQLite
because the central operation is a group-by over a 5 GB CSV, done out of core. The
minimal-dependency discipline that governs `ontime` does not apply here — this is
an offline batch pipeline, not a 62 MB container.

**Geometry is stored as integer micro-degrees, not WKT.** `edges` carries
`lon_e6`/`lat_e6` INTEGER lists plus four bbox columns; 1e-6 of a degree is 11 cm.
This deleted the first-vertex regex, the pad-by-longest-edge collar and the Python
re-test that the art window query needed — the window test is now an exact integer
overlap. `shapes` is one row per shape rather than one row per point: 1.7M rows
for Wales, on the order of 100M nationally. It is input to `match` and nothing
else, so `wayfare prune` drops it once matching completes. Wales: 160 MB -> 114 MB
compacted. Migration runs on connect and rewrites in place, because a national
match run costs a day or two and a schema change it cannot survive is one nobody
applies.

**Serving is `wayfare serve`, in the package, not a script.** `server.py` answers
three things on one port: the static viewer, the PMTiles archives with byte ranges,
and `GET /art`. It moved out of `scripts/serve.py` when it gained the render
endpoint — serving bytes off disk is fine unchecked, taking parameters from a URL
and running cairo is not, and pyproject puts only `wayfare` under mypy and ruff.
`scripts/serve.py` is a deprecated shim so a deployed compose file keeps working.

**A render is a style and a query spec, and they know nothing about each other.**
The style decides how an edge is painted; `art.QuerySpec` decides which edges exist,
what their weight means, and what a group is. Three styles then cover the product of
the two rather than three fixed pictures — `strands` grouped by operator or by road
class is a genuinely different map with no new drawing code. `Style.needs_groups`
(was `needs_services`) is the only thing crossing the line: it says which *shape* of
data a style consumes, flat edges or grouped paths, never what the groups are.

**The spec is a closed vocabulary, not a query language.** `WEIGHTS`, `GROUPS` and
`ORDERS` are dicts of SQL fragments; substituted text is only ever a value looked up
in one of them, and anything a caller supplies is a bound parameter. This is not
fussiness: DuckDB's `read_only` applies to the database file and not the filesystem,
so `read_csv` and `ATTACH` still work and user SQL would be an arbitrary file read on
the server. There is a lockdown path (`enable_external_access=false`,
`disabled_filesystems`) but no statement timeout, so a runaway query would need
interrupting from another thread. Not worth it for four knobs.

**`Edge.weight`, not `Edge.n_trips`.** The field holds whatever `QuerySpec.weight`
asked for, which may be a count of operators or traffic per metre. A field named for
trips holding a count of operators is a lie, and the rename cost four lines.

**`/art` exists because the data is on the server and the design work is not.**
Every expensive stage runs where the disk is, so iterating on a style used to mean
copying tens of gigabytes to a laptop or editing a style and watching a deploy.
`art.render_bytes` is the same `_render` as the file path with a `BytesIO` for a
sink, deliberately — there is no second drawing path to diverge.

## Constraints

**The match stage must survive interruption.** It runs for days on a server that
may reboot. Work is selected by the *absence* of a `match_status` row. This means
one batch is both the unit of concurrency and the unit of checkpointing, and they
cannot be separated: a batch still in flight is still selectable, so loading the
next batch before committing the last hands the same patterns out twice. This was
a real bug, caught in testing. Do not reintroduce pipelining across batch
boundaries without adding an in-flight exclusion.

**Failures are recorded, not retried.** A pattern whose stops cannot be connected
by road will never succeed. A matcher that retries it on every restart never
finishes. Every outcome gets a row, including `no_route`, `error` and `skipped`.

**`pattern_id` is an identity hash, not a popularity rank.** It used to be
`row_number() OVER (ORDER BY count(*) DESC)` in `gtfs.py`, recomputed every run, so
a journey got a different id whenever some other route gained a trip. `patterns`
also did `DELETE FROM patterns` while leaving `match_status` and `pattern_edges`
alone, so a second run against an existing database would have silently re-pointed
matched edges at the wrong patterns. Nobody had hit it because each region so far
used a fresh data root. It is now `hash(route_id || direction || ordered stop ids)
>> 1` cast to BIGINT — the shift keeps it inside a signed BIGINT — built by
`db.pattern_id_sql`. Nothing that varies between feeds may enter the identity:
not trip counts, not shape ids, not operator, or the cache misses.
`gtfs._check_unique_ids` refuses to build on a collision rather than merging two
patterns.

**`match_status` is a permanent cache keyed on pattern identity**, not a per-run
table. `patterns` gained `first_seen`/`last_seen` feed-version columns and merges
rather than deletes. A pattern that leaves the timetable keeps its row and its
match results; a seasonal service that returns is already matched and costs
nothing. Every consumer of `patterns` filters on `db.current_feed()` — match work
selection, aggregate, coverage, prune and shape loading — so departed patterns are
never matched, aggregated or rendered.

**A graph rebuild is still a full re-match, and is now guarded rather than
silent.** `match.pin_graph` records Valhalla's `tileset_last_modified` in
`meta.graph_id` on the first run and refuses to add to the database against a
different build; `--force-graph` overrides. Mixing two GraphId spaces in one table
renders fine and is wrong. If Valhalla reports no tileset timestamp the guard
warns and stands down rather than pretending to work.

**Spreading the work is `--max-seconds`.** Checked between batches, never inside
one, because a batch is the unit of checkpointing — so the budget is a floor on
run length, not a ceiling. It composes with the existing absence-of-a-status-row
work selection with no other bookkeeping: a run stopped by the budget is
indistinguishable from one that was killed. Combined with `ORDER BY n_trips DESC`,
a nightly job with a half-hour budget spends it on the busiest roads first, so a
partially drained queue degrades gracefully. `publish` can be spread by rebuilding
one region's PMTiles per night, which the multi-region viewer already supports.

**Old databases migrate in place; they are not re-matched.**
`db._migrate_pattern_ids` recovers each pattern's identity from the
`pattern_stops` rows already stored and renumbers `patterns`, `pattern_stops`,
`match_status` and `pattern_edges` together. It aborts loudly on a collision or on
a pattern whose stops are missing. Same rule as the geometry migration above: a
national match run costs a day or two, so every migration must be a rewrite rather
than a re-run.

**An operator switching on `TrackPoint`s is an opt-in re-match.** A pattern
matched from bare stops that later gains a `shape_id` is worth redoing — it turns
a guess into an observation — but it adds work to a queue meant to be predictable.
The count is always logged; `wayfare patterns --upgrade-shapes` is what acts on
it.

**The unmeasured number that decides everything is feed churn** — how many
patterns are new month to month. `patterns` now logs new / carried over still
unmatched / departed every run, and `wayfare status` reports `patterns_pending`
and `patterns_departed`. It has not yet been measured against two real consecutive
BODS feeds.

**Bad geometry is worse than missing geometry.** A wrong match produces a
confident-looking line down a road no bus uses. `low_confidence` rows are kept (so
they are never retried) but their edges are dropped.

**The detour check needs both a ratio and an absolute slack.** On a short pattern
a ratio alone is meaningless — a one-way system that sends the bus around one
block triples a 300 m span. Both `MAX_DETOUR_RATIO` and `DETOUR_SLACK_M` must be
exceeded.

**GTFS ids stay strings.** Route "07" must not become 7, or the join silently
loses every service with a leading zero. Hence `all_varchar=true` on every
`read_csv`.

**DuckDB takes a single writer.** Match workers do HTTP only; the main thread
writes.

**Tile features are coalesced, and coalescing must stay lossless.** Runs of edges
that share every tile attribute (`way_id`, road name, service set, trip count) and
meet end to end merge into one feature. Chaining stops where three of a group's
edges meet: at a fork, picking a continuation draws a line that doubles back.
Directed pairs collapse only where the service sets agree, so a one-way pair
carrying different buses each way still renders as two lines, while an ordinary
two-way street no longer renders one line invisibly under another. Wales: 169,857
directed edges -> 102,925 after collapsing pairs -> 53,013 after chaining. This is
a publish-stage concern only; `art` reads raw directed edges and is unaffected.

**Nothing holds a whole window or a whole table.** `art` streams its window:
`Window` pulls geometry in chunks, and the percentile weight scale comes from a
separate pass over trip counts alone — 8 bytes an edge, held as two bounds rather
than a list of normalised values. Holding every edge cost 439 MB for the `uk`
preset on Wales alone, and the country is about 25x Wales. Each style needed a
different accommodation: `density` walks the window twice (ADD is commutative, so
order is free); `spectrum` moved its quietest-first ordering into SQL, which is
sound because weight is monotonic in trip count; `strands` strokes a service's
edges as one cairo path, so the window hands back (service, edge) pairs already
grouped and one ribbon is live at a time. `render(edges=...)` still works via
`Held`, which presents a list through the same interface. Peak *resident set size*
(RSS) on the `uk` window: density 479 -> 259 MB, strands 617 -> 312 MB.
`publish.export_geojsonl` streams by `way_id` for the same reason: 617 -> 372 MB
on Wales. The Python side no longer grows with the window; what still grows is
DuckDB's own aggregate, which spills to disk rather than failing.

**Two `strands` behaviours are deliberate.** A service is weighted by the total
traffic on every road it uses, not by its own trips, so a minor route along a busy
corridor keeps a wide ribbon. A service registered by two operators covers each
edge once, hence the DISTINCT on the service/edge pair. Neither is a bug to fix in
passing; changing either changes the picture, so decide that first.

**The `refs` cap is 64 and there is no overflow sidecar.** Only 1,405 of Wales's
169,857 edges held more than 12 services and the longest held 53, so 64 clears
Wales outright. Raise the cap again rather than reintroducing a sidecar — pooling
makes the list cheap. Card-only attributes are confined to z11+. Bucketing `trips`
to a log scale was tried and rejected: it saves a further 2.1%, because MVT
already pools the 1,759 distinct values, and costs an approximate figure in the
info card.

**The render server never holds the database open.** DuckDB gives a writer an
exclusive lock on the file, so one read-only handle kept alive by a viewer nobody is
looking at would stop the next `match` or `aggregate` from starting. `/art` opens
read-only for the length of one render and closes it, and reports a held lock as 503
with the reason rather than a traceback. Never cache the connection to save the
open — the open is metadata, the lock is the pipeline.

**Renders are serialised, and the cap is pixels, not width.** One at a time because
a render is CPU-bound cairo over a full scan of `edges` and the same box is usually
also matching; two would not finish either sooner. The bound is `width` x derived
height x `scale`², because the window's aspect ratio sets the height and `scale`
multiplies both — `width=4000&scale=4` over a tall window is 200 megapixels and
looks modest. Past `QUEUE_LIMIT` waiters the answer is 503, since a studio page
re-rendering on every slider move would otherwise queue renders nobody will look at.

**There is still no spatial index on `edges`.** A national window therefore reads
the whole table, over HTTP as much as on the command line. The pixel cap does
nothing about that; the serialisation and the queue limit are the only protection.

**A render is 75% cairo, and the scan is not the problem.** Measured on a synthetic
4.2M-edge / 10.25M-service database (`scripts/bench_window.py`), `density` at 800px:
Cardiff 56,251 edges takes 2,363 ms — weights pass 55 ms, two window walks 532 ms,
cairo 1,776 ms. London 752,561 edges takes 28,589 ms, split 516 / 6,558 / 21,515 the
same way. So the whole database side is about a quarter of a render and the
percentile pass under 2%. Optimise the drawing, not the query. What would actually
help is fewer strokes: dropping sub-pixel edges, or coalescing runs the way
`publish` already does for tiles.

**Clustering `edges` on a space-filling curve does prune, and it is worth doing for
its own sake.** DuckDB keeps min/max zonemaps per 122,880-row group, and `match`
inserts edges in batch order, which is spatially random, so today they prune
nothing. Ordering the table by a Morton or Hilbert code over the bbox centre makes a
city window touch a handful of groups: Cardiff read 100% -> 11.7% (Morton) or 5.9%
(Hilbert) of `edges`, 22 ms -> 4.4 ms; London 100% -> 26.3%, 30 ms -> 16 ms. It also
shrinks the file, 528 -> 453 -> 443 MB. Verified from DuckDB's own
`operator_rows_scanned`, not inferred from wall time. Hilbert only beats Morton on
the smallest window, so it is not worth the `spatial` extension on its own. **But
read the previous paragraph before spending the effort:** this is a 5x improvement to
a quarter of the cost, and Wales at ~2 row groups cannot show it at all.

**`edge_services` cannot prune and never will, as things stand.** It carries no bbox
column and DuckDB pushes no min/max filter through the join, so the weights pass
reads all 10.25M rows under every layout. That caps what clustering can do. Giving
it a bbox column, or clustering it by `edge_id` alongside a clustered `edges`, is
the only way through — unmeasured.

**Extracting a window to Parquet was tried and rejected.** The idea was that
iterating on a design should cost the window rather than the national table. It does
not pay, for the reason above: Cardiff went 2,347 ms -> 2,320 ms and London 28,978
-> 28,619, and a filtered spec got *slower* (37 -> 101 ms) because the extract cost
more than the scan it saved. `art.Source` survives as the substitution seam, with
the numbers recorded on it; do not reintroduce the extract without first making the
drawing cheaper.

**Every `/art` error is JSON, and the message is the interface.** `send_error`
writes an HTML page, which an `<img>` renders as a broken-image icon with the reason
nowhere anyone can see it. A lat,lon-swapped window is the case that cannot raise —
it is a legal window that draws nothing — so it comes back 200 with
`X-Wayfare-Warning`, which is the CLI's log line put somewhere a browser can read.

## Standards

- Python 3.12, ruff at line-length 92, mypy strict on `wayfare`. `ruff format` owns the
  layout and `nixfmt` owns `flake.nix`; both are enforced, so do not hand-tune spacing
  back. `.github/workflows/check.yml` runs format, lint, types and tests in this same
  devShell on every push and pull request.
- The dev environment is the nix flake and nothing else. direnv enters it (`.envrc` is
  `use flake` plus `dotenv_if_exists .env`, the same file Compose reads); `nix develop`
  is the same shell without direnv. It supplies Python 3.12, uv, cairo, pkg-config,
  felt/tippecanoe and the duckdb CLI, and its hook builds `.venv` with uv, re-syncing
  when `pyproject.toml` or the nixpkgs Python moves. Dependencies stay in
  `pyproject.toml`, never in the flake.
- `.venv/bin` is on `PATH` in the shell, so the commands are bare: `pytest -q`,
  `ruff check .`, `mypy`. Outside the shell they are not on `PATH` at all — get into it
  rather than reaching for a system Python or a hand-made venv.
- Tools that must be found by hand outside the shell: `tippecanoe` for `publish`, cairo
  for `art`. That is what the flake exists to stop.
- `LD_LIBRARY_PATH` in the flake is load-bearing: duckdb's manylinux wheel wants a distro
  `libstdc++`, and the nix interpreter has none. It fails at `import duckdb`, not install.
- Tests must not need the real datasets. `tests/conftest.py` holds a mini GTFS
  feed; mark anything needing real data `slow` or `valhalla`.
- Comment the non-obvious and leave the obvious alone.
- Use `wayfare.db.row` / `db.scalar` rather than `.fetchone()[0]`.

## Measured — Wales, end to end, 2026-08-06

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

3.6/s is the honest throughput. An earlier 15.3/s was measured while the
confidence bug was rejecting most patterns instantly, and meant nothing.

**Archive size, same data throughout.** 23.8 MB baseline -> 20.9 MB with the edge
id moved into the MVT feature id field (`--use-attribute-for-id`) -> 13.7 MB with
coalescing -> 11.1 MB with card-only attributes confined to z11+ -> 9.5 MB once
the refs-ordering bug stopped splitting segments. 60% in total. Tippecanoe now
reports no features dropped and every zoom holds the full network; the first build
was thinning the densest tiles to 27% of their features, so Wales renders complete
at every zoom for the first time.

**The export is deterministic, and must stay that way.** Two things made it not
be: `list(short_name ORDER BY n_trips DESC)` with no tiebreak, so equally busy
services came back in arbitrary order and that order was part of the coalescing
key; and `_chain` starting a closed loop wherever the scan happened to begin. A
rebuild now produces byte-identical output, which is what makes tiles cacheable
and two runs comparable.

All three art styles are byte-identical run to run as well, which none of them
were. Ties previously fell in whatever order the scan returned, and two runs of
the old `spectrum` differed by 426 bytes. Verified by rendering Cardiff before and
after the streaming rewrite: `density` byte-identical, `strands` differing by 7
bytes out of 5.8M at delta 1, `spectrum` differing more because its ties now
resolve differently.

**That claim was PNG-only, and `strands` to SVG was never deterministic.** The group
query ordered by group with no tiebreak *within* a group, so the edges of one ribbon
arrived in scan order. A PNG cannot show it — `strands` composites with SCREEN, which
is commutative, so the image is identical either way — and an SVG records strokes in
the order they were issued. Three runs gave three different files at a constant
293,842 bytes, differing in 180,365 of them. Fixed by an `edge_id` tiebreak in
`art._order_sql`. The general lesson is the one this codebase keeps relearning, after
the refs ordering and `_chain`'s starting point: **every ORDER BY needs a unique
tiebreak**, and a commutative compositing operator will hide a missing one from every
check that looks at pixels.

**DuckDB inserts about 2,700 rows/s through executemany, and 1.6M/s from a file.**
It is columnar; every bound-parameter insert pays the full per-statement
machinery. This is not a small constant -- roughly 450 of the Wales match run's
983 seconds went on inserting rather than matching, with Valhalla under 1% CPU
throughout. `match` stages each batch to a file and reads it back: CSV for
`pattern_edges`, newline-delimited JSON for `edges` because it carries INTEGER[]
geometry and road names holding quotes and commas. Multi-row VALUES and unnest of
parallel arrays are no better than executemany. Never add a row-at-a-time insert
loop on a table that grows with the network.

**Extrapolating to GB is not a straight multiply.** Wales is 2.4% of national
trips, which suggests roughly 12 hours -- but Wales is 85% `shape` and the nation
is 48%, and the `stops` path costs two Valhalla calls instead of one. Budget
appreciably more, and re-measure on the first national batch rather than trusting
this number.

## Current state

Wales complete end to end. Greater London matching in progress in its own data
root against its own Valhalla instance. 123 tests pass, ruff and mypy clean. GB
not yet attempted. See PLAN.md.
