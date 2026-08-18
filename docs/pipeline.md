# Pipeline

The stages, and the decision behind each one. What the feeds themselves hold is [docs/data.md](data.md), and how the whole thing runs unattended is [docs/deploy.md](deploy.md).

The stages run in this order, each reading what the last one wrote:

    acquire  -> raw downloads
    patterns -> trips collapse to distinct ordered stop sequences
    match    -> Valhalla; the stage that runs for a day or two
    trace    -> OSM route relations as geometry for patterns with none
    snap     -> OSM way ids for the rail shapes an operator already publishes
    routes   -> OSM route relations as services, for modes with no timetable
    aggregate-> invert pattern->edges into edge->services
    publish  -> GeoJSONL -> tippecanoe -> PMTiles

`wayfare all` chains acquire, patterns, match, the publish gate, trace, snap, routes, aggregate, prune, cluster and publish, in the same order as [`deploy/refresh.sh`](../deploy/refresh.sh), and a test parses that script to keep the two from drifting. `trace`, `snap` and `routes` are tolerated failures, because they ask Overpass, and each is tolerated separately because the three ask it different questions. Every stage checkpoints, and re-running the same command resumes.

## acquire

Downloads a region's timetable and whatever ships beside it into `raw/`, then unpacks the General Transit Feed Specification (GTFS) bundle into `work/gtfs`, which is what `patterns` reads. It touches no database.

Three publishers give three shapes of feed. The Bus Open Data Service (BODS) publishes one bundle per slug and `config.feed` builds the URL on demand, which is why only the two exceptions have an entry in `config.FEEDS`. The National Transport Authority (NTA) publishes the Republic of Ireland as one bundle, and skips the National Public Transport Access Nodes (NaPTAN) register, the Great Britain stop file, which has nothing to say about anywhere else. Northern Ireland downloads no feed at all, because none is published: the four OpenDataNI datasets are resolved through `package_show` at fetch time, since the resource id and the filename both move on every publication, and `translink.build_gtfs` assembles the bundle.

The check on a bundle is structural rather than a size floor. BODS sends no `Content-Length`, so a truncated download looks exactly like a complete one, and regional bundles run from 41 MB to 1.28 GB, so no size floor separates a cut-short national feed from a whole regional one. `check_gtfs` opens the zip and requires `stop_times.txt`, `trips.txt`, `routes.txt` and `stops.txt`, and a zip stores its central directory at the end, so a download cut short cannot be opened at all, at any feed size.

Retries are for connections that dropped, never for content that is wrong. A complete but unusable file raises `Invalid` and a 401 or 403 raises `Unauthorized`, and both stop the stage where it stands, because the same bytes come back next time and five attempts at a password prove nothing about it.

## patterns

Reads the unpacked feed and groups trips by `(route_id, direction, ordered stop sequence)`. Most trips are one physical journey repeated through the day, so this collapse is what makes national scale affordable. It writes `patterns`, `pattern_stops`, `routes`, `stops` and `shapes`, and rebuilds `meta.modes`.

`pattern_id` is an identity hash and not a rank: `hash(route_id || direction || ordered stop ids) >> 1` cast to BIGINT, from `db.pattern_id_sql`. Nothing that varies between feeds may enter it, not trip counts, not shape ids and not operator, or `match_status` misses on every run. `gtfs._check_unique_ids` refuses to build on a collision rather than merging two patterns.

`patterns` merges rather than deletes. Rows carry `first_seen` and `last_seen` feed versions, so a pattern that leaves the timetable keeps its match results and a seasonal service that returns is already matched. Every consumer filters on `db.current_feed()`.

Trips are weighted per week and `calendar_dates` exceptions are ignored, so a service running only on bank holidays is weighted as though it ran a normal week. That is safe because the number is only ever a rendering weight, and nothing routes, matches or gates on it.

The mode selection lives in `meta.modes` rather than in the invocation, so [`deploy/refresh.sh`](../deploy/refresh.sh) passes none and inherits the stored set. Narrowing it retires the deselected patterns, because rebuilding against the feed already on disk leaves them live.

A stop's coordinates being valid does not make it British. BODS carries international coach, so live stops sit between Calais and Warsaw at coordinates that are entirely correct, and `config.british_isles_sql` is the boundary. The whole pattern is dropped rather than the stop.

`all_varchar=true` is on every GTFS `read_csv` in [`gtfs.py`](../wayfare/gtfs.py), because route "07" must not become 7.

## match

Map-matches every pending road pattern onto the Valhalla graph and writes `pattern_edges` and `edges`. This is the primary geometry path rather than a fallback, because only 48.3% of trips carry a `shape_id` and the split is per-operator and all-or-nothing.

Two strategies are chosen per pattern. With operator geometry, `shape` submits one dense trace to `/trace_attributes` under `map_snap`. Without it, `stops` routes the stops with `bus` costing and `break_through` locations to synthesise road geometry, then walks that result back with `edge_walk` to recover edges exactly, falling back to `map_snap` on a chunk-stitch discontinuity. Confidence from the `stops` path is reported as 0.0, since it is a guess about which roads the bus takes.

Failures are recorded rather than retried, which needs "failed" to mean "impossible", because a matcher that retries the unroutable never finishes. Every outcome gets a row.

    ok               matched, edges kept
    low_confidence   matched, edges dropped
    no_route         Valhalla found no path; permanent
    skipped          a stop gap past the bound, or too few stops
    error            a bug or a malformed reply; permanent until the code moves
    transport_error  the request never reached Valhalla; the one retryable status

Permanence is decided by Valhalla's numeric `error_code` and never its English. Every failure arrives as HTTP 400, and `src/exceptions.cc` gives codes 154, 170, 171, 172, 440, 441, 442, 443 and 444 the same status, so the status line carries no information. `valhalla.NO_PATH_CODES` is the set that means no path.

A refused connection is `transport_error`, the one status safe to clear unattended. `valhalla.TransportError` is deliberately not a `ValhallaError`, so `match_stops` does not retry an `edge_walk` refusal down a dead socket. HTTP 5xx joins it, because codes 102, 203 and 402 all mean the service is shutting down.

Bad geometry is worse than missing geometry, since a wrong match draws a confident-looking line down a road no bus uses. `low_confidence` rows are kept so they are never retried, and their edges are dropped. The detour check needs both a ratio and an absolute slack exceeded, because on a short pattern a ratio alone is meaningless: a one-way system around one block triples a 300 m span.

The stop-gap bound applies to unshaped patterns only. The reasoning behind `config.MAX_STOP_GAP_M` is about guesswork, so it holds for `stops` and not for `shape`: with an operator trace there is no routing, and the distance between two timing points says nothing about the recording's quality. [`match.py`](../wayfare/match.py) tests `if not p.shape and p.max_gap_m > config.MAX_STOP_GAP_M`, and counts shaped patterns over the bound separately.

Valhalla's two 200 km limits bite in different places. `service_limits.bus.max_distance` is checked against the straight-line chain through a `/route` request's locations, and `service_limits.trace.max_distance` along the shape a `/trace_attributes` request submits. Road is longer than the line it follows, so a stop chain that routes without complaint then fails on the shape that came back, and `valhalla._chunks` therefore runs twice, on the stops before routing and on the synthesised road before walking it.

A graph rebuild is a full re-match, and nothing reuses matches across builds. Valhalla's `edge.id` is a *GraphId*, valid only within one graph build, and it is the join key for the whole pipeline; `match.pin_graph` records Valhalla's `tileset_last_modified` in `meta.graph_id` and refuses to add rows against a different build, because mixing two GraphId spaces in one table renders fine and is wrong. `way_id` is the durable identity and is kept for that reason.

A batch is both the unit of concurrency and the unit of checkpointing. Work is selected by the *absence* of a `match_status` row, so a batch still in flight is still selectable and loading the next batch before committing the last hands the same patterns out twice. Do not reintroduce pipelining across batch boundaries without an in-flight exclusion, and note that `--retry` and `--reclassify-transport` both rewrite `match_status` before the first batch loads, for the same reason.

## trace

Draws the non-road patterns that carry no operator geometry, from OpenStreetMap (OSM) *route relations*. `db.matchable` keeps a metro away from Valhalla, since there is no road under a tube tunnel. It writes `traces`, `trace_status` and `ways`.

A route relation is already ordered, which makes this an ingestion job rather than a snapping one. Its `role=""` way members chain end to end in the order the service runs them, so a pattern's geometry is a cut of that chain between its first and last calling points. Nothing is snapped, and there is no confidence score to fall back on.

Heavy rail is fitted even where the operator published a shape. `db.traceable` is the predicate and `config.TRACE_OVER_SHAPE_MODES` is the list, holding `rail` alone: a shape cannot carry way ids, and way ids are what `aggregate` inverts into shared track. The shape stays the fallback rather than being replaced, so a line whose relation is unmapped or does not chain draws exactly as it did before and loses only the sharing.

Overpass is used rather than the OpenStreetMap application programming interface (API), because the stage has to discover relations over an area and nothing in a timetable carries a relation id. One request per run is enough: `out body geom` inlines every member way's coordinates, and a second `out` returns the member nodes with their tags, which is where the station names come from, since a relation member carries a role and an id and never the tags of the thing it points at. The body is cached as raw bytes, so a parser fix applies to bytes already paid for.

The fit is a subsequence search over normalised station names. A pattern is tested against the relations calling at either of its ends, and the reverse order is tried too because a relation is per direction. Every placement is tried rather than the first, since a relation calling at one station twice offers more than one and only one projects in order along the track. What gets projected is the relation's own stop nodes, which are on the track by construction.

The join is by name with a coordinate check, because there is no `naptan:AtcoCode` on an Underground stop node. `osm.normalise` folds case, expands the ampersand, deletes apostrophes, turns remaining punctuation into spaces and strips qualifiers in a loop until the result stops changing, bounded at four passes and refusing any strip that would empty the string. The loop is not decoration, since "Edgware Road Station Station" is in one feed's stop register.

A sequence that turns round partway is refused rather than drawn. The matched stops must project one way along the chain, either way; what is refused is a reversal in the middle, because slicing between the two ends of a loop takes the wrong branch and draws confident track no service runs.

A resolved pattern records the ways under the cut and not the ways of the line. `osm.ways_between` reads `osm.Chain.way_at` at the same two distances `osm.slice_between` cuts the geometry, because `aggregate` inverts that list per way and the whole chain would say a short working from Edgware to Kennington runs the length of the Northern line.

Failure to reach Overpass at all is deliberately not written down. Nothing was learned about any pattern, so a permanent row would be a lie about all of them at once; the run logs a warning and carries on rather than throwing away a match run that has just cost a day or two.

    ok               the relation chained and carried the pattern's stop sequence
    no_relation      nothing fetched calls at either of this pattern's ends
    chain_break      the relation serving it does not chain into one path
    no_stop_match    a relation shares a terminus and not the sequence
    not_monotonic    the stops matched and project out of order along the chain
    skipped          fewer than two stops to fit
    error            a bug or a malformed response, permanent until the code moves
    transport_error  the request never got an answer, so nothing was learned

`chain_break` is written against the pattern, through `_STATUS_OF_REASON`, rather than merely counted per relation: `trace.prepare` chains every relation once and drops the broken ones before any pattern is fitted, so `resolve` tests the pattern's terminus names against `prepared.broken_names` to tell a line that is mapped and does not chain from one nobody has mapped.

Three traps each look like missing data rather than a mistake. Platform members must leave the way chain, since leaving `role=platform`, `platform_entry_only` and `platform_exit_only` in produces spurious breaks that read as broken mapping across the whole of London, and `config.OSM_STOP_ROLES` names the three roles that are calling points. The two publishers qualify a station name differently, a Public Transport version 2 (PTv2) stop member being a node on the platform, so OpenStreetMap writes "Lewisham Platform 6" where BODS qualifies by mode, "Lewisham DLR Station", and one mismatched stop refuses the whole contiguous run. And a station needs more than one spelling, BODS writing "Edgware Road (Bakerloo)" where OpenStreetMap writes "Edgware Road" twice and lets the relation say which is which, so `osm.spellings` offers both forms and a stop matches if any spelling agrees.

## snap

Gives an operator's own rail shape the OpenStreetMap way ids it does not carry, so overlapping services share the track they run over. It answers the same question as `trace` from the opposite end: the operator already published where the train goes, so nothing has to be inferred about the route and OpenStreetMap supplies only the identity. It writes `snap_status`, `traces` and `ways`.

The window is sized off the pending shapes' vertices rather than their stops, because a shape runs past the stops it calls at and a stop-sized window leaves the approach to a terminus with no track under it. Every vertex is tested against `config.british_isles_sql` rather than the corners alone, and `config.pad_and_clip` then clips to `config.Feed.bounds`, as `trace.bbox` and `osmroutes.bbox` do, because Northern Ireland's rail shapes reach Dublin Connolly.

The stage has its own Overpass cache, deliberately not the file `trace` or `routes` use, because sharing a body would let whichever stage ran first decide this one's coverage. A relation is the wrong instrument here in any case, since it covers only the track somebody drew a route over, while asking Overpass for bare `railway=*` ways covers the track itself.

The tolerance is a margin rather than a knob. The covered share does not move between 25 m and 50 m, so `config.SNAP_MAX_M` sits at 25 m in the middle of a range where the answer is the same, and a survey either follows the track or is somewhere else.

A partial cover is refused whole rather than trimmed, and that is the one way this stage could lie. Attributing the half of a shape that found track and dropping the half that did not reports a short working over a line the service runs the length of, and nothing downstream could tell. A refused pattern keeps its own shape in `segments` exactly as before, so widening the stage to a mode whose track is unmapped costs the sharing and never the line.

Parallel track is where a nearest-vertex answer flaps, because four tracks through a station throat sit within a few metres of each other and taking the nearest way at every vertex independently turns one line into a shredded list of ways. The snapper holds the way the run is already on, and `config.SNAP_HOLD_M` bounds that hold against the *nearest* way rather than against the tolerance: held to the tolerance, a way that has already diverged keeps the shape for another 25 m of track it does not carry.

`service=*` track is excluded in the query, because a siding, a yard road and a crossover sit within metres of the running line and a shape snaps onto one happily and reports a service running through a depot.

The stage's three writes are one transaction. Work is selected by the absence of a `snap_status` row, so a status committed without its geometry is a pattern marked resolved that nothing will ever ask about again.

    ok             every metre of the shape found track and the ways were stored
    partial_cover  under `SNAP_MIN_COVER` of it did; refused rather than trimmed
    no_track       nothing fetched came within tolerance of any vertex
    too_short      fewer than two points to snap
    error          a bug or malformed geometry, permanent until the code moves

A pattern `trace` already resolved is left alone, because `traces` holds one row per pattern and a relation fitted by stop sequence is the stronger evidence of the two. A snapped trace is told from a fitted one afterwards by its NULL `relation_id`.

## routes

Turns OpenStreetMap route relations into services in their own right, for modes with no timetable behind them at all. `trace` uses a relation as geometry for a pattern a feed already carries; `routes` uses the relation as the pattern. The module is [`wayfare/osmroutes.py`](../wayfare/osmroutes.py) because `routes` is already a GTFS table name, and the command line subcommand is `wayfare routes`.

`config.Feed.route_relations` narrows `osmroutes.ROUTE_MODES` per region, `None` taking the default and `()` drawing none, because a region that publishes the mode itself has the operator's own shape for the same line and only the shape knows how often a service runs ([docs/data.md](data.md) has the three gates). An empty selection skips the Overpass fetch and still runs `write`, which retires the previous run whether or not it found anything.

The stage has no incremental path. `run()` chains every relation and rewrites every pattern and way on every invocation, consulting no existing `traces` or `trace_status` row, which is what stops a line retired in OpenStreetMap being drawn for ever.

The scheduled path never re-queries Overpass. [`deploy/refresh.sh`](../deploy/refresh.sh) runs `wayfare routes` with no `--refresh` and nothing deletes the cached body, so every run re-parses the same bytes and the rail layer's OpenStreetMap content is frozen at whatever was last cached until someone passes `--refresh` by hand.

`write_ways` upserts rather than clearing, because `trace` writes into the same table and a blanket delete would take the tube's track out of the archive on the next `routes` run, with `publish.export_track_geojsonl` joining `ways` inside so nothing would raise. `osmroutes.prune_ways` runs after every writer and drops the ways no `traces` row runs over.

## aggregate

Inverts pattern-to-edges into edge-to-services and rebuilds the two tables that draw the non-road modes: `build` writes `edge_services`, then `build_segments`, then `build_track_services`. Each table is deleted and rebuilt in one statement.

Two filters on `edge_services` are both load-bearing. The join to `patterns` under `db.current_feed()` drops departed services, because `pattern_edges` keeps the matched geometry of a pattern that has left the timetable and a road nobody runs on must not still be drawn. `db.matchable` is the same rule pointed at the past, dropping the rows of a database matched before the mode filter existed, which hold `pattern_edges` for patterns that should never have reached Valhalla.

`segments` holds one polyline per non-road pattern, and is rebuilt outright rather than merged, so it holds the current feed only and a departed tram stops being drawn on the next run. A pattern with no geometry gets no row: the stops are known, and a straight line between two of them would draw perfectly happily down the wrong side of a river.

`build_track_services` inverts relation track per way, keyed on `(way_id, short_name, agency_id, mode)`, because one polyline per pattern cannot answer which services use a piece of track and most rail ways carry more than one service. The key is a `way_id` rather than an `edge_id` because nothing routed this track and a GraphId is valid only within one graph build. `mode` is in the key on purpose, so a way carrying both a tube line and a National Rail service is two features and is drawn twice. `n_trips` sums to NULL rather than zero where no timetable has been attributed, because zero trips a week and an unknown number are different claims.

The two arms partition on `traces.ways_cut`, and a pattern in both is drawn twice. A trace cut to its own pattern is inverted per way; a trace holding the whole line's chain keeps its polyline in `segments`, because inverting it would attribute a short working to track it never reaches. What a pattern in both arms looks like is a hover on a National Rail way answering with one relation's card rather than the way's service list.

The shape arm is a fallback and not a claim, which is why it tests the trace rather than the `shape_id`. A mode in `config.TRACE_OVER_SHAPE_MODES` carries both a shape and a cut trace, so only the trace says which layer draws the pattern, and every pattern the tracer did not resolve is drawn from the operator's own recording exactly as before.

## publish

Streams the network out as GeoJSONL, builds up to six tippecanoe passes and joins them into one PMTiles archive. Four road bands cover the zoom range — `far` z5–z7, `mid` z8–z9, `near` z10 and `detail` z11–z14 — and the fifth and sixth passes are `segments` and `track`, each one band over z5–z14. `tile-join` concatenates whatever exists, and either of the last two can be absent.

`_DETAIL_ONLY` is `("way", "refs", "name")`, stripped from the three overview bands, because those are the attributes only the info card opens and the card does not open below `DETAIL_ZOOM`. `trips` is published at every zoom, because the viewer's colour ramp reads it and an attribute a paint property cannot find is not an error MapLibre reports: it takes the fallback out of `to-number` and draws a whole country in the first ramp colour.

The layer names and the colours are one file, [`wayfare/map.toml`](../wayfare/map.toml). The name tippecanoe is handed comes out of it, MapLibre asks the archive for the same name, and `coverage.draw` paints with the same ramp. Nothing either side reports when they drift, because a layer the viewer names and the archive does not carry is a layer that draws nothing.

The viewer reads a generated `web/palette.js` rather than the file itself, since a browser has no Tom's Obvious Minimal Language (TOML) parser and the page has to work on a static host. `scripts/palette_js.py` generates that file, it is committed, and CI runs `--check` on every push, because a colour edited in the TOML and not regenerated is a page painting the old one and nothing at run time can notice. The *OKLab* derivation that turns each non-road mode's seed colour into a six-step ramp lives in [`wayfare/palette.py`](../wayfare/palette.py) and nowhere else, and the page holds finished arrays and computes no colour at all.

The detail band's feature id is the OpenStreetMap way id, and the overview bands' is the Valhalla edge id. `way` is therefore an attribute of no band, and the viewer tells the two ranges apart by reading `refs`. Put `way` back as an attribute, or strip `refs` from a band, and the viewer hovers in the wrong id space with no error to show for it.

Tile features are coalesced, and coalescing must stay lossless. Runs of edges sharing every tile attribute and meeting end to end merge into one feature, chaining stops where three of a group's edges meet, since picking a continuation at a fork draws a line that doubles back, and directed pairs collapse only where the service sets agree.

The export is deterministic and must stay that way. Every ORDER BY needs a unique tiebreak, including the ones whose order looks irrelevant, because DuckDB's parallel hash join returns rows in a varying order; a `list(short_name ORDER BY n_trips DESC)` with no tiebreak put an arbitrary order inside the coalescing key, and `_chain` started a closed loop wherever the scan began.

What a low zoom holds is left to tippecanoe, and five attempts to take that decision away all made the map worse. `config.OVERVIEW_CAP_FAR` and `config.OVERVIEW_CAP_MID` are both `None` and the quota machinery under them is switched off; read the block in `config` before reviving any of it. `--drop-densest-as-needed` picks by density rather than service level, which is a real fault, but it thins only the tiles that will not fit, where every cap tried thinned the whole country to spare those.

Judge a low zoom by lit pixels, never by feature counts. A cap keeps many short features spread over many cells and no cap keeps fewer, longer ones, so features per zoom, populated cells, features per cell and bins-holding-anything all reward the first while only the second reaches the screen, and every round shipped on numbers that rose while the map got worse. `wayfare coverage` counts the same way and inherits the same blind spot. Draw the geometry and look at it.

The one thing that shipped out of that work is `publish.merge_overview` in [`wayfare/publish.py`](../wayfare/publish.py), under `config.MERGE_OVERVIEW`. `coalesce` keeps `way_id` in its key because the detail band spends the way id on its feature id, so every way boundary along a road whose services never change is a feature break the overview bands pay for and cannot show. The merge joins runs across it wherever `n` and `trips` are both equal, through a point where exactly two of the group's lines meet, which moves no point and averages nothing, and it makes the overview both smaller and denser.

The merged file lands in the publish scratch directory rather than beside the export, deliberately, because it carries none of the info card's attributes and a later `--from-export` that picked it up would publish a region with no road names and no service lists. Below z11 a merged run is one feature sharing one id, so a hover lights the whole run rather than one way's worth of it.

A pass with no features is skipped rather than joined in, because tippecanoe exits 110 rather than writing an empty archive, so a region with no matched edges gets no road bands at all. With nothing to write at all, `build_tiles` raises rather than publishing a blank archive, which loads without complaint and reads as a broken viewer.

Three things here fail silently, and each took an archive to the point of being served before anyone noticed.

- Tippecanoe applies `-x` before `-j`, so a filter naming an excluded attribute matches nothing and the band comes out empty.
- `--extend-zooms-if-still-dropping` treats `-z` as a ceiling it may raise, so a `far` band asked for z5–z7 came back covering z5–z9, overlapping the band above it and having `tile-join` merge both copies of every road. The flag is passed to the last band only.
- A longitude within about a kilometre of the prime meridian is written `-1.1e-05`, which a number pattern without an exponent skips without a word. `_NUM` allows the exponent, and `_features` raises on a line it cannot read rather than passing over it.

A licence condition travels with the data, not with the page. The credit is derived from `config.Feed` and written into the archive's own tileset metadata, so a copied archive keeps it, and `publish.contents` reads off the database which of `road`, `operator` and `track` the archive holds, since the Open Database License (ODbL) credit to OpenStreetMap is owed only where a route was matched onto its ways or drawn from its track. The viewer needs `pmtiles.Protocol({ metadata: true })` or MapLibre never sees any of it, which looks like a viewer crediting only its basemap rather than an error.

## Checking an archive

`wayfare coverage` reports features per cell for each quarter of the country and inherits the same blind spot as every count-based statistic, and `wayfare draw` rasterises a zoom so it can be looked at, which is the check the counts cannot make. Neither needs a database.

`draw` takes several archives and composites them into one buffer, because these islands are three archives and the viewer draws every one it is offered onto the one map. Compositing ranks by layer first and trip count second, in the order the viewer stacks its layers — road underneath, then track, then segments — since ranking on trips alone put a trunk road over the tram line crossing it.

Supersampling draws into a buffer several times wider and taller, with a nib that many pixels square so a line still comes out one *output* pixel wide, and averages each block back down at the resolve step rather than while drawing. That is what keeps the compositing rule intact, since every buffer pixel still states one feature whole; blending while drawing would have put a third hue naming neither mode along every crossing.

The coastline under a render is a committed Natural Earth file rather than the viewer's raster backdrop. `scripts/coastline.py` clips the 1:10m coastline to `map.toml`'s roam box and `draw` paints it below every feature, because without an underlay the only thing saying where the land is, is where the buses are, and a coast with no service on it is not drawn at all. A raster backdrop was ruled out because it puts a licence condition on a PNG that travels without the page it was made for.

The lit fraction is comparable at one width and at no other. A drawn road stays about one pixel wide whatever the width, so quadrupling the pixels roughly halves the fraction of them a fixed network lights: the statistic holds across archives and across builds drawn at one width, and says nothing across two.

## Storage

Storage is one DuckDB file, `work/wayfare.duckdb`, rather than SQLite, because the central operation is a group-by over a 5 GB comma-separated values (CSV) file done out of core. Geometry is integer micro-degrees rather than well-known text: `edges` carries `lon_e6`/`lat_e6` INTEGER lists plus four bbox columns, and 1e-6 of a degree is 11 cm, so the art window test is an exact integer overlap.

Migrations run on connect and rewrite in place, never re-running the pipeline, because a national match run costs a day or two and a schema change it cannot survive is one nobody applies.

DuckDB takes a single writer, so match workers do HTTP only and the main thread writes. A connection also holds one result at a time, and a second query abandons the first silently, mid-stream, so the truncated result looks complete. This is why `Window.paths` resolves `self.weights` before it opens its stream; making that scale lazy put its query inside the draw loop, and the renders came out stable, plausible and wrong.

DuckDB cannot spill an ordered list aggregate. `list(x ORDER BY y)` pins its per-group sort state in memory and raising the memory limit only moves the wall, so the collapse projects the needed columns into a table in one streaming scan, which does spill, then aggregates in `WAYFARE_SEQ_PARTITIONS` partitions keyed on `hash(trip_id)`.

Never add a row-at-a-time insert loop on a table that grows with the network. DuckDB inserts about 2,700 rows/s through `executemany` inside a transaction and 1.6M rows/s from a file, so `match` stages each batch to a file and reads it back: CSV for `pattern_edges`, newline-delimited JSON for `edges` because it carries INTEGER[] geometry and road names holding quotes and commas.

`wayfare prune` drops operator geometry and spares every shape a live non-matchable pattern points at, because for a tram or a ferry `shapes` is the only geometry there is and `segments` is a derived copy of it. `maintenance.prune_shapes` also refuses while any matchable pattern is unmatched, counting matchable patterns only, since a tram never gets a `match_status` row and would otherwise block it for ever.

Departed patterns are counted and never evicted, so `pattern_edges` grows monotonically as a database is kept current. The rows are harmless to what is drawn, since every consumer filters on `db.current_feed()`, and they are dead weight on disk that no command currently reclaims.

## Clustering

`wayfare cluster` reorders `edges` on a *space-filling curve* and compacts the file. DuckDB keeps min/max zonemaps per 122,880-row group and `match` inserts edges in batch order, which is spatially random, so unclustered they prune nothing; ordering by a Morton code over the bbox centre makes a city window touch a handful of groups.

Morton rather than Hilbert, which reaches a smaller share on the smallest window alone and would earn the `spatial` extension for that. `maintenance.morton_sql` is the one implementation and [`scripts/bench_window.py`](../scripts/bench_window.py) calls it, so the benchmark and the command cannot drift, and `maintenance.CLUSTER_BOX` is a fixed grid over Great Britain rather than the data's extent, because the code is a physical row order and never an identity.

It has to write a new file. DuckDB never gives space back below a file's high-water mark, so the dropped table's blocks stay allocated and reordering in place makes the database bigger; `COPY FROM DATABASE` into a freshly attached file reclaims them and preserves row order, so the curve survives the copy.

Clustering goes stale rather than off. `cluster_edges` records the row count it sorted in `meta.edges_clustered`, since rows a later `match` appends land unsorted on the end where no zonemap can help, and `wayfare status` reports `yes`, `no` or `stale (N of M edges sorted)`.

`edge_services` cannot prune, and a bbox column is not worth adding. It carries no bbox column and DuckDB pushes no min/max filter through the join, so the weights pass reads every row under every layout; the column was measured and prunes almost exactly as hoped, for four INTEGER columns on the largest table in the database and 10 ms on the smallest window. The scan was never what cost anything.
