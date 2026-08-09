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

**A DuckDB connection holds one result at a time, and a second query abandons the
first silently.** Not an error, not a short read anything notices — a 200,000-row
stream interrupted after its first batch simply ends at 20,000 and looks complete.
This is why `Window.paths` resolves `self.weights` *before* it opens its stream:
making that scale lazy put its query inside the draw loop, where it truncated
every `density` and `spectrum` render to its first fetch. The renders were stable,
plausible, and wrong. Anything else that becomes lazy on this connection inherits
the same trap.

**Geometry comes out of DuckDB as Arrow, not as rows.** The scan was never the
problem and still is not — the London window scans in 4.4ms — but *materialising*
it was: 303ms to turn the same rows into Python lists of ints, because an
`INTEGER[]` column arrives over the row protocol as a list object per edge holding
an int object per vertex. In Arrow that column is a flat child buffer plus
offsets, which numpy adopts without copying. Over London at 3000px (197,276 edges,
585,287 vertices) the whole data path went 852ms → 358ms by fetching Arrow, and
→ 198ms by keeping it flat all the way to cairo — hence `Polyline`, which holds
indices into a fetch's shared coordinate buffers rather than a list of tuples per
edge. Whole renders: `strands` over London 5,449ms → 3,622ms, `density` 4,985ms →
4,423ms. The remainder really is cairo, as recorded below.

**The percentile pass belongs in SQL, and has to match `Weights.over` exactly.**
Pulling every weight into Python to take two order statistics cost 1,918ms of a
2,150ms pass at 4.2M edges. `bounds_query` reproduces the rank convention rather
than using `quantile_disc`, which interpolates differently and would shift a
render's contrast invisibly: `row_number()` and an explicit `floor(q * n)`,
because `CAST(x AS BIGINT)` rounds where Python's `int()` truncates. Verified
identical on 72 real database/window/spec combinations.

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
also matching; two would not finish either sooner. That holds all the more now one
render uses every core it is allowed — see banding below. The bound is `width` x derived
height x `scale`², because the window's aspect ratio sets the height and `scale`
multiplies both — `width=4000&scale=4` over a tall window is 200 megapixels and
looks modest. Past `QUEUE_LIMIT` waiters the answer is 503, since a studio page
re-rendering on every slider move would otherwise queue renders nobody will look at.

**There is still no spatial index on `edges`.** A national window therefore reads
the whole table, over HTTP as much as on the command line. The pixel cap does
nothing about that; the serialisation and the queue limit are the only protection.
See the clustering measurements further down for what would actually prune, and the
paragraph after that for why it is not where the time goes.

**A render is drawn in horizontal bands, one process each, and the output is
byte-identical.** Everything above optimises one core; the box has eight. The canvas
splits into bands, each band is drawn by its own process against its own read-only
handle, and the rasters are pasted back. Measured over the `uk` window on the real
2.75M-edge database at 2,000px: `density` 77–98s → 28–32s, `spectrum` 58–67s → 21–31s,
`strands` 71–72s → 37–40s; at 4,000px `density` 98s → 42s. Verified byte-identical for
all three styles, and on the awkward canvases — letterboxed, `scale=3`, filtered,
sampled, `line_scale=6`. `strands` gains least because its cost is the (service, edge)
fan-out, not vertices.

Four things had to be true and each was a bug first:

- **Cut on edge count, not on height.** Equal-height bands put 1,307,069 of 2,746,261
  edges into one of eight, so seven cores idled while the eighth ran 35s. Latitude
  quantiles over the window took the same render 37s → 27s.
- **Spawn, not fork.** The parent holds an open DuckDB handle when the pool starts, and
  DuckDB's background threads do not cross a fork. The child dies on first use and it
  presents as `BrokenProcessPool` with no traceback, because it is killed rather than
  raising.
- **One band per worker, not more.** 24 balanced bands measured *slower* than 8, 36.7s
  against 27.0s. This was first attributed to `edge_services` being unable to prune, so
  that every band scans all 8.3M rows whatever its height. **That floor is real and it
  is not the reason.** Timing a single band in isolation at 1, 2, 4, 8, 16 and 24 bands,
  with the drawing suppressed to separate the data path, the per-band data cost halves
  every time the bands double: 20.08s, 10.05s, 5.08s, 2.64s, 1.38s, 1.00s. Fitting a
  constant to that gives a floor of **0.16s a band** — 4.6% of a 24-way band, about one
  second of wall clock across all 24. Total CPU work across every band stays inside 10%
  from one band to 24. What actually costs the 10s is spawn, at about a second each and
  24 of them on four cores, plus the oversubscription itself. Same shape for `strands`
  (floor 0.18s) and for a filtered spec (0.13s), so it is not a quirk of one style.
- **Draw past the cut and paste only the middle.** Clipping to the band splits a stroke
  at a raster boundary, and cairo tessellates in 24.8 fixed point, so the two halves'
  coverage does not always re-add to the whole shape's — one row of one Cardiff render
  came out 1/255 off. The band surface therefore carries a margin of half a line width,
  drawn and discarded, and the only clip is the serial path's own window rect.

**Two scales must be the window's, never the band's.** `Weights` is injected into each
band. So are the *group* statistics, through `Source.groups`, which now names an
optional pre-computed `(grp, n_edges, trips)` relation — registered as an Arrow table,
because inserting 20,000 rows through bound parameters would cost seven seconds a band.
`gstat` sets both ribbon width and draw order. Width being per-band is visibly wrong;
order is the subtle one, because SCREEN is commutative in real arithmetic and *rounds*
in eight-bit, so reordering moved 2.8% of the pixels by up to 4/255 — diffuse, across
the whole image, nothing like a seam, and exactly the kind of difference that gets
waved through.

**Banding declines rather than fails, and the cases matter.** SVG (nothing to paste),
`render(edges=...)` (the list lives in the parent), a window under `MIN_BAND_EDGES`
(spawn costs about a second whatever the picture — Cardiff at 1,200px is 0.75s serial
and banding made it twice as slow), and a connection a worker could not reopen.
`band_source` asks the *connection* for its path via `duckdb_databases()` rather than
assuming `config.DB_PATH` — a caller may hand `render` any database, and a band opening
the configured one instead would quietly draw a different picture in the parallel path
only — then probes it with a read-only open. That probe is what catches a writable
handle: DuckDB gives a writer an exclusive lock, so bands could not open the file at
all. The count that `MIN_BAND_EDGES` is compared against comes from the render's own
`WHERE`, so a spec filtered to one road class does not start eight processes for a
picture one core finishes in a tenth of a second.

**`default_workers` reads the cgroup, not `os.cpu_count()`.** The render service runs
at `cpus: 4` on an eight-core box and `os.cpu_count()` reports the host's, so it would
start eight processes to share four cores' quota and a 3 GB memory limit.
`WAYFARE_RENDER_WORKERS` overrides. Each band also does `SET threads=1`: DuckDB
defaults to a thread per core *per process*, and eight bands would put sixty-four on
eight cores.

**And it counts physical cores, not hardware threads.** The box is four cores of eight
threads, and the second thread of a core draws no faster: `uk` `density` at 2,000px is
78.1s on one worker, 44.9s on two, **26.9s on four**, 27.2s on six, 28.1s on eight,
30.9s on twelve, 33.2s on sixteen, 37.5s on 24. Speed-up tops out at 2.90x on four,
which is the core count and not the thread count — tessellating round caps is ALU- and
branch-bound, so there are no memory stalls for a sibling thread to fill. The 4.6%
between eight workers and four is the smaller half of it; eight interpreters and eight
DuckDB connections against a 3 GB container limit is the half that bites.
`_physical_cpus` reads distinct `(physical id, core id)` pairs from `/proc/cpuinfo` and
returns None anywhere that file is absent, where the logical count stands as before.

**Round caps are what a render costs, and this is where the rest of the time is.**
Measured by replaying the whole `uk` window through cairo under different settings:
butt caps and mitre joins take 55.4s to 25.5s — a 54% cut — because at national scale
an edge has already simplified to 2.08 vertices, so nearly every stroke is one tiny
segment whose cost is tessellating two round caps. `ctx.set_tolerance` coarsens that
arc: 1.0 gives 78.5%, 2.0 gives 73.7%. `Antialias.FAST` gives 74.4%; `GOOD` and
`DEFAULT` are byte-identical to `BEST` and buy nothing, so the antialias setting is not
a lever. None of these are taken — they all change the picture, and banding was
available and does not. Coalescing runs of edges into single subpaths the way `publish`
already does would keep round caps *and* remove them from every internal joint; that is
the next real win and it is unbuilt.

**Cairo is 76% of a band at every band count**, which is what makes coalescing worth
building: 62.21s of 82.29s at one band, 8.23s of 10.87s at eight, 2.50s of 3.50s at 24.
Banding changed the wall clock and not the composition, so coalescing attacks the same
three quarters whether a render is banded or not.

**But coalescing is not picture-preserving for `density`, and that has to be a
decision rather than a discovery.** Two edges meeting end to end each lay a round cap at
the shared node, and ADD counts the overlap twice; one continuous subpath counts it
once. Measured at density's own widths and alphas, the junction pixel drops 85 → 53 at
t=0.25, **200 → 108 at t=0.5**, and 255 → 230 at t=1.0, while mid-edge is unchanged to
the byte. The effect peaks in the middle because a doubled value saturates at the top.
Nationally that is a bright dot at every one of millions of nodes, so what coalescing
removes is arguably an artefact of drawing per edge rather than anything in the data —
but every existing render changes, and `publish`'s chaining is not the precedent it
looks like: an MVT feature carries attributes, not additive light, so that merge really
was lossless and this one is not.

**A render costs per edge and per vertex, never per pixel.** The section below
measures the cairo half at 75% and asks for "fewer strokes: dropping sub-pixel
edges, or coalescing runs". This is that, carried out. Over a synthetic 1M edges,
`density` took 52.7s at 900px and 59.0s at 4,000px — a 20x cut in pixels bought
11%, because the cost is cairo tessellating round joins and caps once per *vertex*.
A smaller preview is therefore not a cheaper one, which is the whole reason
`sample` exists. Batching cairo state changes was tried and rejected: 128 weight
buckets delivered in bucket order by SQL, one path and one state change each, moved
50.2s to 45.6s — per-stroke setup was never the cost either.

The three things that did work, in order of how much they buy and how little they
cost: `RenderOpts.simplify_px` drops vertices within half a pixel of the last one
kept (36% of vertices survive at preview width, 30% off the clock, 0.05% of output
bytes changed); `density` draws its halo and core in one walk instead of two, which
is byte-identical because cairo's ADD is saturating and therefore commutative;
and `Projection.batch` projects a whole 20,000-row fetch with numpy at once. Per
edge numpy would lose — 4.14 vertices is far too few to pay for array setup — so
the batching is the point, not the library. Together, 53.2s to 25.8s.

**`spectrum` must never simplify its geometry.** Every other style would draw the
same line through fewer points. This one takes the *hue* from the angle between
consecutive points, so dropping a vertex merges two bearings into their average and
repaints that stretch a different colour. Half a pixel of tolerance moved 74% of the
output bytes, against 0.05% for `density`. `draw_spectrum` therefore passes `tol=0.0`
explicitly rather than reading `opts.simplify_px`. Any future style that derives
colour, width or order from geometry inherits this problem — check before enabling
simplification for it.

**Sampling is the only preview lever, and the weight scales must not see it.**
`QuerySpec.sample=n` adds `hash(edge_id) % n = 0` to the window CTE and is linear:
1/8 takes `density` from 50.5s to 6.6s. `hash` rather than `random` so a preview is
reproducible and does not flicker as it redraws. It lives on the spec rather than
in `RenderOpts` because it decides *which edges there are* — but it is deliberately
absent from `QuerySpec.selective`, since it narrows nothing semantically and must
not flip the `LEFT JOIN` that keeps serviceless edges in the picture.

`_Sql.window(sampled=True)` is asked for only by `edges_query`; `weights_query` and
`group_query` take the whole window, and `grouped_query` puts the thinning on its
final SELECT rather than in the shared CTE, because `gstat` is built from that CTE
and decides every ribbon's width and draw order. Sampling upstream of it would make
a preview weight its ribbons differently from the render it stands in for. This is
the trap to watch: anything statistical must read the unsampled window, and only
drawn geometry may be thinned. Alpha is compensated linearly (`alpha_scale * sample`, since
ADD is linear), but the core pass already runs at alpha up to 0.90, so 8x pins it at
1.0 and the preview still comes back at about 62% brightness. Widening the lines
would close that gap and destroy the point, since line weight is one of the knobs
being judged. Hence the studio page labels the sampled pass and follows it with the
real one.

**Every query the render path streams needs an ORDER BY, including the ones whose
order looks irrelevant.** `edges_query` had none when not ordering by weight, and
DuckDB's parallel hash join returns rows in an order that varies between runs of
the same query against the same file — so `density` to SVG produced four distinct
outputs in four runs, on real data. PNG hid it completely, because cairo's ADD is
saturating and therefore commutative, so the buffer is identical whatever order the
strokes arrive in; SVG records the strokes in the order they were issued and shows
it. That is the same failure `_order_sql` was written to fix for `strands`, and the
existing "byte-identical across two calls" test could not catch this one: three
edges never reach a second thread, so an undefined order is a stable one. Test the
order is *defined* rather than that two runs agree. The sort is close to free —
+1.2 ms over cardiff, +10.1 ms over `uk`, +9.1 ms over London, against renders of
0.4 s to 4.4 s — which is worth knowing, because it was left alone once on the
assumption that ordering the hottest query in the render path would be expensive.
It is about 0.2%, measured after the Arrow fetch above, so against the faster
numerator rather than the one it was originally waved away with.

**Which cairo you have decides whether an SVG is vectors or one embedded raster,
and it changes what an SVG test can see.** The dev shell's libcairo 1.18.4 writes all
three styles as real `<path>` strokes. The shipped image's 1.16.0 writes `spectrum` as
35,188 paths but falls back to a *single* `<image>` for `density` and `strands` — cairo
cannot express ADD or SCREEN in SVG, and 1.16 gives up where 1.18 does not. Two
consequences, both of which cost time to work out. Any stroke-order bug the paragraph
above is about is invisible in a 1.16 `density` SVG, because there are no strokes in
it. And on 1.16 two SVGs rendered in *one process* never compare equal even when the
pixels are identical, because the fallback names its elements from a process-wide
counter: `id="image5"`/`id="surface1"` against `id="image11"`/`id="surface7"`, with
byte-identical base64 between them. Compare SVGs across processes, or compare the
payload rather than the file. Rendering the same window in three fresh 1.16 processes
gives one hash.

**A render is 75% cairo, and the scan is not the problem.** Measured on a synthetic
4.2M-edge / 10.25M-service database (`scripts/bench_window.py`), `density` at 800px:
Cardiff 56,251 edges takes 2,363 ms — weights pass 55 ms, two window walks 532 ms,
cairo 1,776 ms. London 752,561 edges takes 28,589 ms, split 516 / 6,558 / 21,515 the
same way. So the whole database side is about a quarter of a render and the
percentile pass under 2%. Optimise the drawing, not the query. What would actually
help is fewer strokes: dropping sub-pixel edges, or coalescing runs the way
`publish` already does for tiles.

That conclusion held and the reasoning behind it did not, which is worth keeping
both halves of. "The scan is not the problem" is true — but the quarter that is not
cairo was almost none of it *scanning*. It was DuckDB rows becoming Python objects,
which is a different thing with a different fix, and the reason a "query
optimisation" like clustering `edges` moved 14ms while changing how the rows are
fetched moved 650ms. See the Arrow entry above. What remains is genuinely cairo.

**Clustering `edges` on a space-filling curve does prune, and it is `wayfare
cluster`.** DuckDB keeps min/max zonemaps per 122,880-row group, and `match` inserts
edges in batch order, which is spatially random, so unclustered they prune nothing.
Ordering the table by a Morton code over the bbox centre makes a city window touch a
handful of groups: Cardiff read 100% -> 11.7% of `edges`, 22 ms -> 4.4 ms; London
100% -> 26.3%, 30 ms -> 16 ms. It also shrinks the file, 528 -> 453 MB. Verified from
DuckDB's own `operator_rows_scanned`, not inferred from wall time. Hilbert reaches
5.9% on Cardiff but only beats Morton on the smallest window, so it is not worth the
`spatial` extension on its own and is benchmark-only. **But this is a 5x improvement
to a quarter of the cost**, and Wales at ~2 row groups cannot show it at all.

`db.morton_sql` is the one implementation; `scripts/bench_window.py` calls it rather
than keeping its own, so what the benchmark measures and what the command does
cannot drift. `db.CLUSTER_BOX` is a fixed grid over Great Britain rather than the
data's extent, so a region's layout does not depend on which region it is; the code
is a physical row order and never an identity, so changing the box is harmless
beyond needing a re-run.

**DuckDB never gives space back below a file's high-water mark, so `cluster` has to
write a new file.** Reordering in place is the obvious implementation and it makes
the database *bigger*: the dropped table's blocks stay allocated, and neither
`CHECKPOINT` nor `VACUUM` reclaims them — measured at 505 MB going to 730. `COPY
FROM DATABASE` into a freshly attached file is what actually reclaims them, and it
preserves row order, so the curve survives the copy. `db.cluster` therefore reorders
in place, copies to `<db>.compacting`, reopens and counts it, re-asserts the indexes
(the copy carries data, not necessarily every index), and only then does an atomic
rename. 529 MB -> 471 MB on the benchmark's 4.2M edges, with Cardiff at 11.7% and
London at 26.3% of rows scanned — the bench's own figures, reproduced through the
shipped command. It needs room for a second copy while it runs, and an interruption
at any point leaves the original untouched.

**Clustering goes stale rather than off, and `wayfare status` says so.**
`cluster_edges` records the row count it sorted in `meta.edges_clustered`. Rows
`match` adds afterwards land unsorted on the end, where no zonemap can help, so
`status` reports `yes`, `no`, or `stale (N of M edges sorted)`. It is a separate
command rather than a step in `aggregate` for the same reason `prune` is: it
rewrites the whole table, needs room for a second copy of `edges` while it does, and
is worth doing once after a match run rather than on every re-aggregation.

**`edge_services` cannot prune, and giving it a bbox column is not worth it.** It
carries no bbox column and DuckDB pushes no min/max filter through the join, so the
weights pass reads all 10.25M rows under every layout. That caps what clustering can
do. The bbox column was the obvious way through and has now been measured: it prunes
almost exactly as hoped — cardiff 10,250,638 rows → 614,400, London → 2,457,600 —
and buys 40.9ms → 31.1ms on cardiff, 356.1ms → 357.6ms on London, and nothing
nationally. Four `INTEGER` columns on the largest table in the database for 10ms on
the smallest window. The reason the pruning does not convert into time is the same
one as everywhere else here: the scan was never what cost anything. The weights pass
is now two order statistics computed in SQL rather than 4.2M floats crossing into
Python, which is where its 1,918ms actually went.

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

**Great Britain is complete end to end**, on the server, feed `20260807_022616`.
Wales and Greater London were the two rehearsals for it and both stand. 395 tests
pass, ruff and mypy clean. See PLAN.md.

| Stage | Result |
|---|---|
| patterns | **52,554** |
| match | ok 50,395 (95.9%) · skipped 1,555 (3.0%) · error 462 (0.9%) · low_confidence 142 (0.3%) |
| aggregate | 2,746,261 edges, 8,301,705 edge-service pairs |
| cluster | current — `meta.edges_clustered` = 2,746,261, the whole table |
| publish | 130 MB PMTiles |
| graph | pinned at `3.8.3/1786113507` |

`patterns` holds exactly one feed version, so **feed churn is still unmeasured** —
the number this file has called the one that decides everything. It now costs one
`acquire` and one `patterns` against a second national feed; the incremental
machinery has been built and waiting since 2026-08-07.

The banding numbers above are the first `art` measurements taken against real
national data rather than a synthetic database: 2,746,261 edges and 8,301,705
edge-service rows, on the four-core, eight-thread box that serves it.

`pyarrow` joined the `art` extra alongside `pycairo` and `numpy`, and only that
extra — the pipeline and the tile server do not import it. It is what makes the
geometry fetch above possible; there is no row-protocol way to get at an
`INTEGER[]` column's buffer.

Two read-path changes were measured and **rejected**, so they do not get tried
again. Materialising the shared `_grouped_base` CTEs into temp tables so the two
grouped queries stop recomputing them: 1.20x on London and 1.04x on `uk`, worth
about 48ms of a 3.6s render, against three temp tables and a second copy of SQL
whose parameter ordering is already the fragile part of the builder. And giving
`edge_services` a bounding box so the weights pass can prune: it prunes exactly as
hoped — 94% of rows on cardiff, 76% on London — and buys 10ms on cardiff and
nothing at all on London, on the largest table there is. That last one answers the
open question this file used to record as unmeasured.

The render speed-ups above were measured against a synthetic 1M-edge database, not
real data, because the timings that prompted them came from a window far larger
than Wales. The ratios are structural and should hold, but two numbers are worth
re-taking on the real thing: the 62% preview brightness, which depends on how much
the network actually overlaps, and `strands`, whose cost is dominated by the
(service, edge) fan-out rather than by vertices.
