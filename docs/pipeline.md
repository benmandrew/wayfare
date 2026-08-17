# Pipeline

An operator's reference. Each section covers one stage: what it does, how to run it, the
flags that change the outcome, what it costs, how it fails and whether it is safe to
interrupt.

The stages run in this order, each reading what the last one wrote:

    acquire  -> raw downloads
    patterns -> trips collapse to distinct ordered stop sequences
    match    -> Valhalla; the stage that runs for a day or two
    trace    -> OSM route relations as geometry for patterns with none
    snap     -> OSM way ids for the rail shapes an operator already publishes
    routes   -> OSM route relations as services, for modes with no timetable
    aggregate-> invert pattern->edges into edge->services
    publish  -> GeoJSONL -> tippecanoe -> PMTiles

What the feeds themselves hold is [docs/data.md](data.md). `wayfare all` chains acquire, patterns, match, the
publish gate, trace, snap, routes, aggregate, prune, cluster and publish — the same stages in
the same order as [`deploy/refresh.sh`](../deploy/refresh.sh), and a test parses that script
to keep the two from drifting. `trace`, `snap` and `routes` are tolerated failures, because
they ask Overpass, and each is tolerated separately because the three ask it different
questions. Every stage checkpoints, and an interrupt is
caught and reported; re-running the same command resumes.

## acquire

**What it does.** Downloads a region's timetable and whatever ships beside it into `raw/`,
then unpacks the General Transit Feed Specification (GTFS) bundle into `work/gtfs`, which
is what `patterns` reads. It touches no database. What each feed covers, where it is thin
and what obligations travel with it is [docs/data.md](data.md); this section is how to run
the stage.

    wayfare acquire
    wayfare acquire --region wales
    wayfare acquire --region ireland --force

**Flags.** `--region` takes a region slug and defaults to `WAYFARE_REGION`, itself `all`:
a Bus Open Data Service (BODS) slug, `ireland` for the Republic of Ireland bundle from the
National Transport Authority (NTA), or `northern_ireland` for Translink's four OpenDataNI
datasets. `--force` re-downloads, re-assembles and re-unpacks, since nothing here is
fetched again while a good copy is on disk. `--with-osm` also archives the Geofabrik
extract; Valhalla fetches its own copy, so this only records which extract a set of edge
ids belongs to. `wayfare all` passes neither flag, and withholds
`--force` deliberately: an attended first run should not re-fetch 1.28 GB it already has.

**Cost.** The transfer, and then the unzip. Nationally the BODS bundle is 1.28 GB zipped
and 7.84 GB unpacked, of which `stop_times.txt` is 5.09 GB; the National Public Transport
Access Nodes (NaPTAN) register adds 102 MB and `--with-osm` a 2.16 GB extract. Budget
about 40 GB for a national run including the Valhalla graph. Wales is 41 MB, and the
Republic's bundle 108 MB zipped and 492 MB unpacked.

**Interrupting.** Safe for the transfers. Bytes go to `<name>.part` and the file is
renamed only once it is complete and has passed its check, so a half-file is never
mistaken for a finished one. The partial is kept only where the host answers a Range
request with a 206, which Geofabrik and the NTA do and BODS does not; a server that
ignores the header sends the whole file from byte zero, so the partial is discarded rather
than concatenated into something the right shape and the wrong size. Unpacking is the half
that is not safe.

**Three publishers, three shapes of feed.** BODS publishes one bundle per slug and
`config.feed` builds the URL on demand, which is why only the two exceptions have an entry
in `config.FEEDS`. `--region ireland` takes the NTA's single `GTFS_All.zip` and skips
NaPTAN, the Great Britain stop register, which has nothing to say about anywhere else.
`--region northern_ireland` downloads no feed at all, because none is published: the four
OpenDataNI datasets are resolved through `package_show` at fetch time, since the resource
id and the filename both move on every publication, and `translink.build_gtfs` assembles
the bundle into `work/`. That assembly is redone only when a part's size or timestamp
moves, which a `.manifest` beside it records.

**BODS sends no `Content-Length`, so a truncated download looks exactly like a complete
one.** `config.MIN_GTFS_BYTES`, 1 MiB, rejects an empty body or an error page cheaply and
nothing else: regional bundles run from 41 MB to 1.28 GB, so no size floor separates a
cut-short national feed from a whole Welsh one. The real test is structural. `check_gtfs`
opens the zip and requires `stop_times.txt`, `trips.txt`, `routes.txt` and `stops.txt`,
and a zip stores its central directory at the end, so a download cut short cannot be
opened at all, at any feed size. A host that does declare a length has its bytes counted
against it, skipping a `Content-Encoding` body that requests has already decoded.

**Retries are for connections that dropped, never for content that is wrong.** A complete
but unusable file raises `Invalid` and a 401 or 403 raises `Unauthorized`, and both stop
the stage where it stands: the same bytes come back next time, and five attempts at a
password prove nothing about it. Everything else retries `config.DOWNLOAD_RETRIES` times,
5, waiting `DOWNLOAD_BACKOFF` of 30 seconds times the attempt number — 30, 60, 90 and 120
seconds, or 300 in total, before it gives up.

**What goes wrong.**

- *A 403 that is not about credentials.* BODS blocks anything that looks like a generic
  scraper and OpenDataNI answers the same way, so `config.USER_AGENT` is load-bearing and
  `WAYFARE_UA` overrides it. It arrives as `Unauthorized` and is refused rather than
  retried.
- *A bundle missing a member.* `check_gtfs` names the missing files, the `.part` is
  deleted and the stage stops. Re-running fetches the same bytes, so the feed itself has
  to change.
- *An interrupt during unpacking.* `unpack_gtfs` writes members straight into `work/gtfs`
  with no staging, and its marker is `stop_times.txt` itself, so a run killed after that
  member and before the ones behind it in the zip reports "already unpacked" next time and
  hands `patterns` a truncated feed. `wayfare acquire --force` re-extracts.
- *A second region acquired into the first's data root.* `raw/`, `work/gtfs` and the
  database are all per data root, and `meta.feed_version` holds one value, so the second
  region becomes the current feed and the next `publish` overwrites the first region's
  archive. Give each region its own `WAYFARE_DATA`.

## patterns

**What it does.** Reads the unpacked GTFS feed from `work/gtfs` and groups trips by
`(route_id, direction, ordered stop sequence)`. Most trips are one physical journey
repeated through the day, so this collapse is what makes national scale affordable. It
writes `patterns`, `pattern_stops`, `routes`, `stops` and `shapes`, and rebuilds
`meta.modes`.

    wayfare patterns
    wayfare patterns --modes bus,coach,tram --memory 12GB

**Flags.** `--modes` is a comma-separated selection out of `config.MODES`, defaulting to
whatever this database was last built with. The selection lives in `meta.modes`, never in
the invocation, so [`deploy/refresh.sh`](../deploy/refresh.sh) passes none and inherits the
stored set. Narrowing it retires the deselected patterns, because rebuilding against the
feed already on disk leaves them live. `--memory` overrides `WAYFARE_MEM`, itself 8GB.
`--upgrade-shapes` re-matches patterns matched from bare stops that have since gained
operator geometry.

**Cost.** Minutes. London's feed, 480,412 trips over a 1.5 GB `stop_times.txt`, collapses
17,611,239 stop times in 6 seconds inside an 8 GB limit. `stop_times.txt` is 5.09 GB
nationally.

**Interrupting.** Safe. The stage is a rebuild, so a killed run leaves the previous
database and the next run redoes it from the start.

**`pattern_id` is an identity hash, not a rank.** It is
`hash(route_id || direction || ordered stop ids) >> 1` cast to BIGINT, from
`db.pattern_id_sql`. Nothing that varies between feeds may enter it: not trip counts, not
shape ids, not operator, or `match_status` misses on every run. `gtfs._check_unique_ids`
refuses to build on a collision rather than merging two patterns.

**`patterns` merges rather than deletes.** Rows carry `first_seen` and `last_seen` feed
versions, so a pattern that leaves the timetable keeps its match results and a seasonal
service that returns is already matched. Every consumer filters on `db.current_feed()`.

**Trips are weighted per week, and `calendar_dates` exceptions are ignored.** `n_trips`
sums `calendar.txt`'s days-per-week over the pattern's trips. An exception that adds or
removes a single date is not read, so a service running only on bank holidays is weighted
as though it ran a normal week. The number is only ever a rendering weight, and nothing
routes, matches or gates on it.

**What goes wrong.**

- *No unpacked feed.* The stage names the directory it looked in, `work/gtfs`, rather than
  the `stop_times.txt` inside it that it tested for, and exits 1. Run `wayfare acquire`
  first.
- *An empty `--modes`.* Refused rather than read as "everything", which would build a
  database with no patterns and report success.
- *A leading zero lost from a GTFS id.* Route "07" must not become 7. `all_varchar=true`
  on every GTFS `read_csv` in [`gtfs.py`](../wayfare/gtfs.py) is the only thing stopping it.
- *A stop outside these islands.* BODS carries international coach, so live stops sit
  between Calais and Warsaw at coordinates that are entirely correct.
  `config.british_isles_sql` is the boundary, and the whole pattern is dropped rather than
  the stop.

**Feed churn is small.** Two Wales feeds two days apart, `20260806_023912` then
`20260808_024504`, took 3,584 patterns to 3,541: 30 new, 73 departed. Catching up cost about
8 seconds of matching at Wales's measured 3.6 patterns/s, against 16m23s for the original
full run. That is a two-day delta on one region; [docs/results.md](results.md) has the
caveats.

## match

**What it does.** Map-matches every pending road pattern onto the Valhalla graph and
writes `pattern_edges` and `edges`. This is the primary geometry path rather than a
fallback, because only 48.3% of trips carry a `shape_id` and the split is per-operator and
all-or-nothing.

    wayfare match
    wayfare match --max-seconds 1800 --workers 8
    wayfare match --retry transient

**Flags.** `--workers` defaults to `WAYFARE_WORKERS`, itself 6. `--limit N` stops after N
patterns and `--max-seconds` stops at the next batch boundary, so the budget is a floor on
run length rather than a ceiling. `--valhalla` overrides the base URL. `--retry` takes
comma-separated statuses or the alias `transient`.

**Cost.** Days nationally. Wales measured 3.6 patterns/s over 983 seconds, of which
roughly 450 seconds went on inserting rather than matching, with Valhalla under 1% CPU
throughout. Work is handed out `ORDER BY n_trips DESC`, so a partially drained queue has
already drawn the busiest roads.

**Interrupting.** Safe, and expected. Results commit every `config.CHECKPOINT_EVERY`
patterns, currently 200, and work is selected by the *absence* of a `match_status` row, so
a run stopped by the budget is indistinguishable from one that was killed.

**A batch is both the unit of concurrency and the unit of checkpointing.** A batch still
in flight is still selectable, so loading the next batch before committing the last hands
the same patterns out twice. Do not reintroduce pipelining across batch boundaries without
an in-flight exclusion. `--retry` and `--reclassify-transport` both rewrite `match_status`
and both land before the first batch loads, for the same reason.

**Two strategies, chosen per pattern.** With operator geometry, `shape` submits a dense
trace to `/trace_attributes` under `map_snap`, one call. Without it, `stops` routes the
stops with `bus` costing and `break_through` locations to synthesise road geometry, then
`edge_walk` that result to recover edges exactly, falling back to `map_snap` on a
chunk-stitch discontinuity. Confidence from the `stops` path is reported as 0.0, since it
is a guess about which roads the bus takes. Measured against Wales's 3,052 shaped patterns
with the geometry withheld, the `stops` path has pooled length recall 0.951 and precision
0.892, so it over-draws by 7.4%, or 1,456 km of phantom road at network level.

**Failures are recorded rather than retried, which needs "failed" to mean "impossible".**
A matcher that retries the unroutable never finishes. Every outcome gets a row.

    ok               matched, edges kept
    low_confidence   matched, edges dropped
    no_route         Valhalla found no path; permanent
    skipped          a stop gap past the bound, or too few stops
    error            a bug or a malformed reply; permanent until the code moves
    transport_error  the request never reached Valhalla; the one retryable status

**Permanence is decided by Valhalla's numeric `error_code`, never its English.** Every
failure arrives as HTTP 400, and `src/exceptions.cc` gives codes 154, 170, 171, 172, 440,
441, 442, 443 and 444 the same status, so the status line carries no information.
`valhalla.NO_PATH_CODES` is the set that means no path. An earlier test for
`"no route" in body.lower()` matched nothing Valhalla ever says, which is why Wales,
London and Great Britain all hold zero `no_route` rows.

**A refused connection is `transport_error`, the only status safe to clear unattended.**
`valhalla.TransportError` is deliberately not a `ValhallaError`, so `match_stops` does not
retry an `edge_walk` refusal down a dead socket. HTTP 5xx joins it, because codes 102, 203
and 402 all mean the service is shutting down. Great Britain's national run recorded 462
error rows of which 262 were transport faults carrying 4,127 trips, all against one host,
and 227 of those were connection refusals: Valhalla was down or restarting while patterns
were being handed out.

**Bad geometry is worse than missing geometry.** A wrong match draws a confident-looking
line down a road no bus uses. `low_confidence` rows are kept so they are never retried,
and their edges are dropped. The detour check needs both `MAX_DETOUR_RATIO` (3.0) and
`DETOUR_SLACK_M` (1,000 m) exceeded, because on a short pattern a ratio alone is
meaningless, a one-way system around one block tripling a 300 m span.

**The stop-gap bound applies to unshaped patterns only.** `config.MAX_STOP_GAP_M` is 180 km,
derived as `VALHALLA_MAX_DISTANCE_M` (200,000) times `VALHALLA_DISTANCE_HEADROOM` (0.9). The
reasoning is about guesswork, so it holds for `stops` and not for `shape`: with an operator
trace there is no routing, and the distance between two timing points says nothing about the
recording's quality. [`match.py`](../wayfare/match.py) tests `if not p.shape and p.max_gap_m
> config.MAX_STOP_GAP_M`, and counts shaped patterns over the bound separately. Any figure
quoted against the older 25 km bound is historic.

**Valhalla's two 200 km limits bite in different places.**
`service_limits.bus.max_distance` is checked against the straight-line chain through a
`/route` request's locations; `service_limits.trace.max_distance` is checked along the
shape a `/trace_attributes` request submits. Road is longer than the line it follows, by
1.26x and 1.58x on the two long Welsh patterns measured, so a 183.7 km stop chain routes
without complaint and then fails 154 on the 232.2 km shape that came back.
`valhalla._chunks` therefore runs twice, on the stops before routing and on the
synthesised road before walking it. Parts overlap by one point and the merge keeps the
first occurrence, which loses the tail of one edge's geometry and no edge identity.

**A graph rebuild is a full re-match, and nothing reuses matches across builds.**
Valhalla's `edge.id` is a *GraphId*, valid only within one graph build, and it is the join
key for the whole pipeline. `match.pin_graph` records Valhalla's `tileset_last_modified`
in `meta.graph_id` on the first run and refuses to add rows against a different build;
`--force-graph` overrides, and mixing two GraphId spaces in one table renders fine and is
wrong. Where Valhalla reports no tileset timestamp the guard warns and stands down. The
extract is pinned for the duration of a run (`force_rebuild: "False"` in
docker-compose.yml) and Geofabrik rebuilds daily, so this is not hypothetical. `way_id` is
the durable identity and is kept for that reason.

**Recovery on a database matched before transport faults had a status of their own.**

    wayfare match --reclassify-transport
    wayfare match --retry transient

Old rows are told apart by the shape of the stored detail rather than by anyone's wording.
A reply from Valhalla is stored as `"<http status>: <json body>"`, so
`status = 'error' AND NOT regexp_matches(detail, '^[0-9]{3}: ')` is every row that never
got one, and the `confidence_score` misconfiguration is excluded by name through
`valhalla.NO_SCORE_MESSAGE`. `run` warns when transport rows are present rather than
clearing them itself, so a run stays reproducible and the hole stays visible.

**A regional extract makes a cross-border service look like a matcher fault.** Wales's
error-154 rows were all route 461, whose English stops snap back to the nearest Welsh road
because the extract has no roads east of the border. Its 63 km chain routes as 260 km at
every chunk size from 40 down to 2, so the length belongs to the graph. It now traces and
lands in `low_confidence` at a detour ratio of 4.1, edges dropped and row kept.

## trace

**What it does.** Draws the non-road patterns that carry no operator geometry, from
OpenStreetMap *route relations*. `db.matchable` keeps a metro away from Valhalla, since
there is no road under a tube tunnel, and `aggregate.build_segments` can only copy a trace
the feed carries. Great Britain's `route_type=1` patterns are 92.9% shapeless and the
Docklands Light Railway (DLR) publishes no shape at all. It writes `traces`,
`trace_status` and `ways`.

    wayfare trace
    wayfare trace --refresh
    wayfare trace --retry transient

**Flags.** `--relations PATH` moves the Overpass cache, default `raw/osm_relations.json`.
`--refresh` re-queries Overpass even with a cached body present, for after the relations
themselves have moved. `--limit N` stops after N patterns. `--retry` takes statuses or
`transient`; `--retry ok` redraws what already worked, which is what re-cuts a trace
stored before the tracer kept the ways under its own slice.

**Cost.** Minutes. The national run of 2026-08-12, against BODS `20260807_022616` on a
copy of the server's database, fetched 131 MB and 1,022 relations in 27 seconds and spent
182 seconds fitting. It resolved 1,127 of 1,737 pending patterns: 86.9% of Underground
trips, 60.6% of the DLR's and 34.2% of trams.

**Interrupting.** Safe. Work is selected by the absence of a `trace_status` row, exactly
as `match` selects on `match_status`, and patterns are handed out busiest first.

**A route relation is already ordered, which makes this an ingestion job.** Its `role=""`
way members chain end to end in the order the service runs them, so a pattern's geometry
is a cut of that chain between its first and last calling points. Nothing is snapped, and
there is no confidence score to fall back on.

**Heavy rail is fitted even where the operator published a shape.** `db.traceable` is the
predicate and `config.TRACE_OVER_SHAPE_MODES` is the list, holding `rail` alone. A shape is
normally the better of the two recordings, being a survey of where the vehicle goes against
a relation's survey of where the track is, and it stays the better one for a tram with
street running and a depot. What it cannot do is carry way ids, and way ids are what
`aggregate` inverts into shared track: the Republic's 363 rail patterns run four services,
and 319 of them were 392,939 vertices of mainline drawn over itself. The shape is kept as
the fallback rather than replaced, so a line whose relation is unmapped or does not chain
draws exactly as it did before and loses only the sharing.

**Overpass rather than the OpenStreetMap application programming interface (API), because
the stage has to discover relations over an area.** Nothing in a timetable carries a
relation id. One request per run is enough: `out body geom` inlines every member way's
coordinates, and a second `out` returns the member nodes with their tags, which is where
the station names come from, since a relation member carries a role and an id and never
the tags of the thing it points at. The window is the pending patterns' stop extent padded
by 0.2 degrees, about 22 km, because a line runs past the box its stops sit in. The body
is cached as raw bytes, so a parser fix applies to bytes already paid for.

**The fit is a subsequence search over normalised station names.** A pattern is tested
against the relations calling at either of its ends, and the reverse order is tried too
because a relation is per direction. Every placement is tried rather than the first, since
a relation calling at one station twice offers more than one and only one projects in
order along the track. What gets projected is the relation's own stop nodes, which are on
the track by construction; the feed's coordinate only checks that the name join found the
right station.

**The join is by name with a coordinate check, because there is no `naptan:AtcoCode` on an
Underground stop node.** `osm.normalise` folds case, expands the ampersand, deletes
apostrophes, turns remaining punctuation into spaces and strips qualifiers in a loop until
the result stops changing, bounded at four passes, refusing any strip that would empty the
string. The loop is not decoration, since "Edgware Road Station Station" is in one feed's
stop register. `config.TRACE_STOP_MAX_M` is 400 m, which clears Highbury & Islington at
216 m, where the timetable's point is the National Rail entrance and the OpenStreetMap
node is the tube platform, while staying under the roughly 1.2 km station spacing.

**A sequence that turns round partway is refused rather than drawn.** The matched stops
must project one way along the chain, either way. What is refused is a reversal in the
middle, which is a loop or a doubled-back placement, and slicing between the two ends of
one takes the wrong branch and draws confident track no service runs.
`config.TRACE_MONOTONIC_SLACK_M` is 250 m, wide enough for a station node sitting slightly
behind its neighbour's projection and far short of a turn.

**A resolved pattern records the ways under the cut, not the ways of the line.**
`osm.ways_between` reads `osm.Chain.way_at` at the same two distances `osm.slice_between`
cuts the geometry. Storing the whole candidate chain became a confident lie once
`aggregate` began inverting the list per way: a Northern line short working from Edgware
to Kennington was stored against every way of the Northern line. `traces.ways_cut` is TRUE
on every row this stage writes.

**Statuses.**

    ok               the relation chained and carried the pattern's stop sequence
    no_relation      nothing fetched calls at either of this pattern's ends
    chain_break      the relation serving it does not chain into one path
    no_stop_match    a relation shares a terminus and not the sequence
    not_monotonic    the stops matched and project out of order along the chain
    skipped          fewer than two stops to fit
    error            a bug or a malformed response, permanent until the code moves
    transport_error  the request never got an answer, so nothing was learned

`chain_break` is written against the pattern, through `_STATUS_OF_REASON`, and not merely
counted per relation. `trace.prepare` chains every relation once and drops the broken ones
before any pattern is fitted, so `resolve` tests the pattern's terminus names against
`prepared.broken_names` to tell a line that is mapped and does not chain from one nobody
has mapped. `_REASON_RANK` picks the most specific near-miss where several relations
refuse one pattern.

**Failure to reach Overpass at all is deliberately not written down.** Nothing was learned
about any pattern, so a permanent row would be a lie about all of them at once. `wayfare
all` logs a warning and carries on rather than throwing away a match run that has just cost
a day or two, and the patterns keep no status row. Trace failures also stay out of the
publish gate, which counts matchable patterns only; [docs/deploy.md](deploy.md) has that
reasoning.

**Six traps, each of which looks like missing data rather than a mistake.**

- **Platform members must leave the way chain.** Leaving `role=platform`,
  `platform_entry_only` and `platform_exit_only` in produces 11 to 25 spurious breaks per
  relation, which reads as broken mapping across the whole of London.
  `config.OSM_STOP_ROLES` names the three roles that are calling points.
- **The two publishers qualify a station name differently.** A Public Transport version 2
  (PTv2) stop member is a node on the platform, so OpenStreetMap writes "Lewisham Platform
  6" where BODS qualifies by mode, "Lewisham DLR Station". One mismatched stop refuses the
  whole contiguous run, which cost all 71 DLR patterns. `osm.normalise` strips both forms.
- **A station needs more than one spelling.** BODS writes "Edgware Road (Bakerloo)" where
  OpenStreetMap writes "Edgware Road" twice and lets the relation say which is which.
  `osm.spellings` offers both forms and a stop matches if any spelling agrees, which is
  safe because the node still has to sit within `TRACE_STOP_MAX_M` of the feed's point.
- **The Elizabeth line is `route=train`, not `route=subway`.** A mode filter written from
  the obvious names misses it, so `config.OSM_ROUTE_VALUES` is deliberately wide and the
  stop-sequence join decides which relation a pattern belongs to.
- **Way tags are not a join key.** `ref` is on 2.4% of subway ways and carries signalling
  codes; `line` reaches 62.1% and is multi-valued on shared track. Ways are reached
  through the relation in member order.
- **Ferries resolve to nothing by design.** `route=ferry` sits outside
  `config.OSM_ROUTE_VALUES`, so 166 of the national run's 170 `no_relation` rows are
  ferries.

## snap

**What it does.** Gives an operator's own rail shape the OpenStreetMap way ids it does not
carry, so overlapping services share the track they run over. `trace` fits a pattern to a
route relation by station sequence and `snap` takes the other side of the same problem: the
operator already published where the train goes, so nothing has to be inferred about the
route, and OpenStreetMap supplies only the identity. It writes `snap_status`, `traces` and
`ways`.

    wayfare snap
    wayfare snap --refresh
    wayfare snap --retry partial_cover

**Flags.** `--track PATH` moves the Overpass cache, default `raw/osm_track.json`,
deliberately not the files `trace` and `routes` use: this stage asks for bare `railway=*`
ways rather than route relations, and sharing a body would let whichever ran first decide
this one's coverage. `--refresh` re-queries. `--limit N` stops after N patterns. `--retry`
takes statuses; `partial_cover` is the one worth redoing after the track is better mapped.

**Cost.** Seconds. Against the Republic's feed `20260814_21a88e41` the Overpass query
returned 6,147 ways in 7.2 MB, and indexing and snapping 319 patterns is arithmetic over a
grid.

**Interrupting.** Safe. Work is selected by the absence of a `snap_status` row, exactly as
`match` selects on `match_status`, and patterns are handed out busiest first.

**The window is the pending patterns' shape vertex extent padded by 0.05 degrees, where
`trace`'s is their stop extent padded by 0.2.** The shape is what is being snapped and it
already runs past the stops it calls at, so a stop-sized window leaves the approach to a
terminus with no track under it. Every vertex is tested against `config.british_isles_sql`
rather than the corners alone, since a feed carrying international coach holds correct
coordinates in Warsaw and one of those in the min/max asks Overpass for every railway
between here and Poland. A vertex outside the bounds leaves the window and not the pattern,
so track it cannot reach surfaces as `partial_cover`, which is already refused.
`config.pad_and_clip` then clips to `config.Feed.bounds` off `WAYFARE_REGION`, as
`trace.bbox` and `osmroutes.bbox` do, because Northern Ireland's rail shapes reach Dublin
Connolly. Bounds that meet no pending shape raise, and a run with no pending shape in the
British Isles gets None back, which the stage logs as a warning before returning.

**Why a relation is the wrong instrument here.** A relation covers only the track somebody
drew a route over. Measured against the Republic's rail, the ways `trace` and `routes` left
in the `ways` table cover 78.7% of the timetabled shape length, with Dublin–Belfast at 7.1%
and Limerick–Waterford at 3.3%. Asking Overpass for the track itself covers 100.0% within
25 m, which is why the stage has its own query and its own cache.

**The tolerance is a margin rather than a knob.** Over 3,000.6 km of Irish rail shape the
covered share is 99.5% at 5 m, 99.8% at 10 m and 100.0% at 25 m and at 50 m. A survey
either follows the track or is somewhere else, so `config.SNAP_MAX_M` sits at 25 m in the
middle of a range where the answer does not move.

**A partial cover is refused rather than trimmed**, and that is the one way this stage
could lie. Attributing the half of a shape that found track and dropping the half that did
not reports a short working over a line the service runs the length of, and nothing
downstream could tell. `config.SNAP_MIN_COVER` is 0.98, and a refused pattern keeps its own
shape in `segments` exactly as before — so widening the stage to a mode whose track is
unmapped costs the sharing and never the line.

**Parallel track is where a nearest-vertex answer flaps.** Four tracks through a station
throat sit within a few metres of each other, and taking the nearest way at every vertex
independently hops between them, turning one line into a shredded list of ways each
carrying a fragment of the service. The snapper holds the way the run is already on, and
`config.SNAP_HOLD_M` is what bounds the hold: 3 m against the *nearest* way rather than 25 m
against nothing. The first run held to the tolerance and every one of the 319 patterns came
back with a worst vertex in the 20–25 m band, over track with something inside 5 m of 99.5%
of it — each one a junction where the run should have changed way and instead gave the old
way another 25 m of track it does not carry.

**`service=*` track is excluded in the query.** A siding, a yard road and a crossover sit
within metres of the running line, so a shape snaps onto one happily and reports a service
running through a depot.

**Statuses.**

    ok             every metre of the shape found track and the ways were stored
    partial_cover  under `SNAP_MIN_COVER` of it did; refused rather than trimmed
    no_track       nothing fetched came within tolerance of any vertex
    too_short      fewer than two points to snap
    error          a bug or malformed geometry, permanent until the code moves

A pattern `trace` already resolved is left alone, because `traces` holds one row per pattern
and a relation fitted by stop sequence is the stronger evidence of the two. A snapped trace
is told from a fitted one afterwards by its NULL `relation_id`.

## routes

**What it does.** Turns OpenStreetMap route relations into services in their own right, for
modes with no timetable behind them at all. Great Britain's National Rail is the case: BODS
does not carry it and every timetable source sits behind a login or a licence negotiation.
`trace` uses a relation as geometry for a pattern a feed already carries; `routes` uses the
relation as the pattern. The module is [`wayfare/osmroutes.py`](../wayfare/osmroutes.py),
the command line interface (CLI) subcommand is `wayfare routes`, and `routes` is also a GTFS
table name; the subcommand is what this section means throughout.

    wayfare routes
    wayfare routes --refresh
    wayfare routes --cif schedule.cif --on 2026-08-15

**Flags.** `--relations PATH` moves the Overpass cache, default `raw/osm_routes.json`,
deliberately not the file `trace` uses: the two stages ask for different windows, and
sharing one body would let whichever ran first decide the other's coverage. `--refresh`
re-queries. `--cif` attributes trips from a Network Rail Common Interface File (CIF)
schedule and is optional, with `--stops` naming the NaPTAN CSV that turns a TIPLOC into a
place and `--on` the date whose service to count. Without `--cif` the track draws and
`trips` stays null.

**Which relations.** `config.Feed.route_relations` narrows `osmroutes.ROUTE_MODES` per
region: `None` takes the default, `()` draws none. A region that publishes the mode
itself needs `()`, because the relation and the operator's own shape are the same line
and only the shape knows how often a service runs — see the three gates in
[docs/data.md](data.md). An empty selection skips the Overpass fetch and still runs
`write`, which retires the previous run whether or not it found anything.

**Cost.** 54m43s nationally when profiled on the server against a cached 121.5 MB Overpass
body, of which 99.8% was two insert loops: `write_ways` took 2,733 seconds for 68,369 ways
and `write` 543 seconds for 935 patterns. Fetching, parsing and chaining the whole body
together took 6.3 seconds. That profile predates the territory gate, so 68,369 rows is an
upper bound rather than the figure; `write_ways` is handed only the relations that became
patterns. `write_ways` has no `BEGIN`/`COMMIT` around its `executemany`, which is 90% of
its cost, and `write` is per-point rather than per-row bound, its 935 rows carrying 1.79M
list elements across the binding layer one at a time.

**Interrupting.** The stage has no incremental path. `run()` chains every relation and
rewrites every pattern and way on every invocation, consulting no existing `traces` or
`trace_status` row, which is what stops a line retired in OpenStreetMap being drawn for
ever. An interrupted run leaves the previous rows and the next run redoes all of it.

**The scheduled path never re-queries Overpass.**
[`deploy/refresh.sh`](../deploy/refresh.sh) runs `wayfare routes` with no `--refresh`, and
nothing in the script or in `acquire` deletes `raw/osm_routes.json`, since `acquire`'s `osm`
source is the Geofabrik pbf for Valhalla. So every weekly run re-parses the same bytes and
rewrites the same rows, and the only thing that legitimately differs is `last_seen` moving
to the new feed version. The rail layer's OpenStreetMap content is frozen at whatever was
last cached until someone passes `--refresh` by hand.

**What goes wrong.**

- *No live patterns.* `bbox` returns None and the stage raises rather than querying an
  unbounded window.
- *Another region's rail.* `config.Feed.bounds` narrows the window per region and
  `config.Feed.operators` refuses a relation whose `operator` names only another region's
  rail. The stage logs the region it thinks it is in, because a run against the wrong data
  root draws another region's rail into this one's archive.
- *Track that quietly stops being drawn.* `write_ways` upserts rather than clearing,
  because `trace` writes into the same table. A blanket delete would take the tube's track
  out of the archive on the next `routes` run, and `publish.export_track_geojsonl` joins
  `ways` inside, so nothing would raise. `osmroutes.prune_ways` runs after every writer and
  drops the ways no `traces` row runs over.

## aggregate

**What it does.** Inverts pattern-to-edges into edge-to-services and rebuilds the two
tables that draw the non-road modes. `build` writes `edge_services`, then `build_segments`,
then `build_track_services`. It is a handful of SQL statements, costs minutes and takes no
flags. Safe to interrupt: each table is deleted and rebuilt in one statement.

    wayfare aggregate

**Two filters on `edge_services`, and both are load-bearing.** The join to `patterns` under
`db.current_feed()` drops departed services, because `pattern_edges` keeps the matched
geometry of a pattern that has left the timetable and a road nobody runs on must not still
be drawn. `db.matchable` is the same rule pointed at the past: a database matched before
the mode filter existed holds `pattern_edges` for patterns that should never have reached
Valhalla, 1,726,822 of them for the Underground alone, plus ferries snapped to coast roads.

**`segments` holds one polyline per non-road pattern, and a pattern with no geometry gets no
row.** It is rebuilt outright rather than merged, being derived from `patterns` and `shapes`
at the cost of one `INSERT ... SELECT`, so it holds the current feed only and a departed
tram stops being drawn on the next run. Missing geometry is left missing: the stops are
known, and a straight line between two of them would draw perfectly happily down the wrong
side of a river. Great Britain's ferries are the worked example, and [docs/data.md](data.md)
has the reasoning for that mode.

**`build_track_services` inverts relation track per way, keyed on `(way_id, short_name,
agency_id, mode)`.** One polyline per pattern cannot answer which services use a piece of
track: 75.8% of Great Britain's rail ways carry two or more relations, so drawing per
pattern puts coincident lines over most of the network and a hover lands on an arbitrary
one. The key is a `way_id` rather than an `edge_id` because nothing routed this track and a
GraphId is valid only within one graph build. `mode` is in the key on purpose, so a way
carrying both a tube line and a National Rail service is two features and is drawn twice.
`n_trips` sums to NULL rather than zero where no timetable has been attributed, because
zero trips a week and an unknown number are different claims.

**The two arms partition on whether a cut trace exists, and a pattern in both is drawn
twice.** A trace cut to its own pattern is inverted per way. A trace holding the whole
line's chain keeps its polyline in `segments`, because inverting it would attribute a short
working to track it never reaches, until `wayfare trace --retry ok` re-cuts it.
`traces.ways_cut` is what tells the two apart, since nothing recoverable does once the
polyline is stored. What a pattern in both arms looks like is a hover on a National Rail way
answering with one relation's card rather than the way's service list.

**The shape arm is a fallback and not a claim, which is why it tests the trace.** A mode in
`config.TRACE_OVER_SHAPE_MODES` carries both a shape and a cut trace, so a `shape_id` says
nothing about which layer draws the pattern and only the trace does. Every pattern the
tracer did not resolve is drawn from the operator's own recording exactly as before, which
is what makes adding a mode to that set unable to take a line off the map. The log line
counts them: "on an operator shape the tracer was offered and could not fit" is the share
of a mode still drawn one polyline per pattern, and a mode whose relations are unmapped
appears there in full rather than as track that never arrived.

**What goes wrong.** Every fault here recovers by running `wayfare aggregate` again. The
stage is a full recomputation — three DELETEs and three `INSERT ... SELECT`s over
`pattern_edges`, `patterns`, `shapes` and `traces`, all of them already on disk — with no
network call, no external tool and nothing to resume, so redoing it costs the minutes it
cost the first time. What it gets wrong is therefore what it draws rather than whether it
finishes.

- *Nothing to invert.* An `aggregate` run before `match` has written anything logs "0
  edges carry 0 edge-service pairs" and succeeds. The counts in the log are the check; the
  `publish` that follows is what raises.
- *A non-road pattern with geometry from neither source.* It gets no row and is simply not
  drawn, which the log counts rather than raises on. `wayfare trace` is what fills it, and
  a straight line between two known stops is the thing that must not be invented.
- *A trace written before the tracer learned to cut.* `traces.ways_cut` is FALSE, so the
  pattern keeps its polyline in `segments` and contributes nothing per way, and a hover on
  that track answers with one relation's card. `wayfare trace --retry ok` re-cuts it and
  the next `wayfare aggregate` moves it into the track layer.
- *A database matched before the mode filter existed.* `db.matchable` drops the
  `pattern_edges` rows that should never have reached Valhalla, so those roads stop being
  drawn on the first run of this stage and the edge count falls with them.

## publish

**What it does.** Streams the network out as GeoJSONL, builds up to six tippecanoe passes
and joins them into one PMTiles archive, with the data credit stamped into the archive's
own tileset metadata.

    wayfare publish
    wayfare publish --region ireland --name-by-region
    wayfare publish --from-export

**Flags.** `--region` decides which feed's credit is stamped, defaulting to the
`WAYFARE_REGION` the data root was acquired with; the licence is a condition rather than a
label, so it has to match the data. `--out PATH` names the archive outright and
`--name-by-region` writes `<name>.pmtiles` through `config.archive_name`, with region `all`
becoming `great_britain.pmtiles`; the two are mutually exclusive. Given neither, the
archive is `bus.pmtiles` in the data root's `out/`, and `publish.default_out` raises only
where a region-named archive already exists there, since writing the default beside it
would update nothing anyone serves. `--from-export` builds from a GeoJSONL a previous
publish wrote and needs no database, for a data root whose database has been pruned away;
it rebuilds the same tiles and does not refresh the region.

**Cost.** Tens of minutes nationally, dominated by tippecanoe. `export_edges_geojsonl` streams by
`way_id` rather than materialising, measured at 617 MB down to 372 MB peak resident set
size on Wales. `tippecanoe` and `tile-join` must be on `PATH`, from felt/tippecanoe; the
mapbox fork cannot write PMTiles.

**Interrupting.** Safe. Every intermediate goes in a scratch directory under the output
directory and only the finished archive is renamed into place, atomically and on one
filesystem, so a killed run leaves whatever is being served untouched.

**Four road bands, then segments, then track.** `far` covers z5-z7, `mid` z8-z9, `near` z10
alone and `detail` z11-z14. The fifth pass is `segments` and the sixth is `track`, each one
band over z5-z14, and `tile-join` concatenates whatever exists. The archive holds three
layers: the banded road layer, `segments` and `track`. Either of the last two can be
absent, and each is skipped rather than joined in empty.

**`_DETAIL_ONLY` is `("way", "refs", "name")`, stripped from the three overview bands.**
Those are the attributes only the info card opens, and the card does not open below
`DETAIL_ZOOM`. `trips` is published at every zoom, because the viewer's colour ramp reads
it and an attribute a paint property cannot find is not an error MapLibre reports: it takes
the fallback out of `to-number` and draws a whole country in the first ramp colour. An
archive published before that change is handled by the viewer's `["has", "trips"]` guard,
which never fires against a newer build.

**The layer names and the colours are one file, [`wayfare/map.toml`](../wayfare/map.toml).**
The name tippecanoe is handed comes out of it, MapLibre asks the archive for the same name,
and `coverage.draw` paints with the same ramp, so `bus`, `segments` and `track` are written
in one place rather than three. Nothing either side reports when they drift, because a layer
the viewer names and the archive does not carry is a layer that draws nothing.

The viewer reads `web/palette.js` rather than the file itself, since a browser has no Tom's
Obvious Minimal Language (TOML) parser and cannot fetch one either: the page has to work on
a static host, and a palette landing a round trip after the page would repaint the map once
it arrived. So `scripts/palette_js.py` generates that file from the TOML, it is committed,
and CI runs `--check` on every push. A colour edited in the TOML and not regenerated is a
page painting the old one, which nothing at run time can notice. The *OKLab* derivation that
turns each of the eight non-road mode seed colours into a six-step ramp lives in
[`wayfare/palette.py`](../wayfare/palette.py) and nowhere else, and the page holds finished
arrays and computes no colour at all.

**The detail band's feature id is the OpenStreetMap way id, and the overview bands' is the
Valhalla edge id.** `way` is therefore an attribute of no band and neither is `id`, and the
viewer tells the two ranges apart by reading `refs`. Put `way` back as an attribute, or
strip `refs` from a band, and the viewer hovers in the wrong id space with no error to show
for it. A way whose service set changes along it is several features sharing one id, so a
hover selects the whole way, which the Mapbox Vector Tile (MVT) specification asks for and
does not require.

**Tile features are coalesced, and coalescing must stay lossless.** Runs of edges sharing
every tile attribute and meeting end to end merge into one feature. Chaining stops where
three of a group's edges meet, since picking a continuation at a fork draws a line that
doubles back, and directed pairs collapse only where the service sets agree. Wales: 169,857
directed edges to 102,925 after collapsing pairs to 53,013 after chaining. `art` reads raw
directed edges and is unaffected.

**The export is deterministic and must stay that way.** Every ORDER BY needs a unique
tiebreak, including the ones whose order looks irrelevant, because DuckDB's parallel hash
join returns rows in a varying order. Two things broke this: `list(short_name ORDER BY
n_trips DESC)` with no tiebreak, whose arbitrary order was part of the coalescing key, and
`_chain` starting a closed loop wherever the scan began. A rebuild now produces
byte-identical output.

**Measured savings on the current build.** Great Britain published from a copy of the
server's database, against an archive built the old way from byte-identical inputs, goes
166,165,053 bytes to 136,786,795, or 17.7%. Per zoom, z5-z10 are unchanged, z11 saves
30.9%, z12 28.7%, z13 26.7% and z14 14.6%, since `-D` never touches a band's maximum zoom.
`config.SIMPLIFICATION` is the largest single geometry saving: turning simplification off
costs 25.89% on Ireland's detail band. Feature ordering, tippecanoe's coalescing flags and
gzip are all exhausted, landing between +0.00% and +1.29%.

**What a low zoom holds is left to tippecanoe, and four attempts to take that decision away
all made the map worse.** `config.OVERVIEW_CAP_FAR` and `config.OVERVIEW_CAP_MID` are both
`None`, and the quota machinery under them is switched off, kept only because z5 is still
thinner than Ireland. Read the block in `config` before reviving any of it.
`--drop-densest-as-needed` picks by density rather than service level, which is a real
fault, but it thins only the tiles that will not fit: 18 at z5-z7 and 4 at z8-z9, where
every cap tried thinned the whole country to spare those. A fifth attempt is recorded
below, whose lossless half ships and whose cap failed for a reason none of the first four
ran into.

**Judge a low zoom by lit pixels, never by feature counts.** A cap keeps many short
features spread over many cells and no cap keeps fewer, longer ones. Features per zoom,
populated cells, features per cell and bins-holding-anything all reward the first, and only
the second reaches the screen, so every round shipped on numbers that rose while the map
got worse. `wayfare coverage` counts the same way and inherits the same blind spot. Draw
the geometry and look at it. Rasterised around London, the uncapped archive lights 3.8% of
the window at z5, 7.0% at z6 and 9.3% at z7 against the capped build's 2.7%, 3.7% and 3.7%;
at z8 it is 8.2% against 5.0%, where the capped render hollowed the city into a skeleton.

**The fifth attempt changed the unit of removal from a feature to a contiguous run.**
[`wayfare/corridors.py`](../wayfare/corridors.py) links export features end to end by
*good continuation*. At a junction two feature ends join only where each is the other's
straightest onward choice, the turn is at most 60 degrees, and the onward direction is
measured over 50 m rather than over the adjacent vertex. The cartographic literature calls
a run built that way a *stroke* (Thomson and Richardson, 1999), and the module renames it a
corridor because the viewer already calls a drawn line's width its stroke. Half of that
work ships. The lossless merge under it is `publish.merge_overview` in
[`wayfare/publish.py`](../wayfare/publish.py), and the cap on top of it is a bench no
publish calls.

**Every publish now builds the merged file.** `config.MERGE_OVERVIEW` is `True`, so the
three bands below z11 read it while the detail band goes on reading the road export, which
spends `way` on the feature id and carries `refs` and `name` for the info card. The merged
file lands in the publish scratch directory rather than beside the export, deliberately,
because it carries none of the info card's attributes and a later `--from-export` that
picked it up would publish a region with no road names and no service lists. Setting
`config.MERGE_OVERVIEW` false builds an archive the way it was built before, which is what
a comparison against the old build needs.

**The cap is still a bench.** `wayfare corridors EXPORT OUT [--merge] [--cap N]` runs the
corridor builder over a GeoJSONL a publish wrote, where `--merge` writes out the same
merged file a publish builds, for inspection or for capping on top of, and `--cap N` drops
whole corridors. That is the idea under test, because a cap that removes contiguous runs
rather than scattered features cannot leave a road drawn as dashes.
`publish --overview-export GEOJSONL` hands the three overview bands a source file of one's
own choosing, which is how a thinned overview is measured against the merged default.

**`coalesce` will not join two edges on different ways, because `way_id` is in its key.**
The overview bands carry neither `way` nor `refs` nor `name`, so every way boundary along a
corridor with the same services throughout is a feature break those bands pay for and
cannot show. The merge joins runs across it where `n` and `trips` are both equal, through a
point where exactly two of the group's lines meet, which is `publish._chain`'s rule. Great
Britain goes from 868,984 features to 244,679, or 28.2% of the count, and Ireland from
87,691 to 17,571, or 20.0%. On Great Britain that costs 15 seconds and 275 MB peak resident
set size.

**Fewer features means smaller tiles at every overview zoom.** Built over z5-z10 in
`benmandrew/wayfare:latest` on Linux with the flags a publish uses, the overview goes from
34 MB to 19 MB on Great Britain and from 4.3 MB to 1.6 MB on Ireland. The published Great
Britain archive is 127.5 MB, and the detail band is the rest of it and is untouched. Worst
tile against the 977 KB limit, by zoom, before and after: z5 952 KB to 862 KB, z6 893 to
838, z7 786 to 732, z8 903 to 346, z9 627 to 224, z10 422 to 135. So z8-z10 stop being
under any size pressure at all, and z5-z7 gain headroom while drawing more. End to end on
Ireland the whole archive goes from 13.60 MB to 10.75 MB, with the detail band unchanged
inside it.

**Lit pixels rise wherever the tiles were being thinned to fit.** Around London, z5 goes
5.771% to 8.062%, z6 7.413% to 8.625% and z7 7.670% to 8.598%. Around Manchester, z5 goes
3.321% to 4.934%, z6 4.529% to 5.654% and z7 4.810% to 5.829%. Over Wales at z5 it is
2.361% to 2.929%. The whole-country window loses between 0.07 and 0.17 percentage points at
z6-z10, and drawn and looked at that is simplification working on longer lines rather than
a network going missing. At 600 m a pixel the two renders are indistinguishable.

The merge changes one thing below z11. A merged run is one feature sharing one id, so a
hover lights the whole run rather than one way's worth of it.

**On the merged Great Britain file, 244,679 features form 117,445 corridors, the longest
115.0 km and the median one feature.** The median is one feature because the merge has
already joined what could be joined, and the junctions left over rarely pass mutual best
fit. Corridor building adds little on top of the merge. A cap of 120,000 keeps 55.0% of the
features and 81.9% of the drawn kilometres, and takes the overview from 18.75 MB to 12.75
MB.

**The cap costs ink everywhere.** Around London it loses 0.4 percentage points at z5 and
0.97 at z8-z10, around Manchester 0.40 at z5 and 1.0 at z7, and over Wales 0.09 at z5 and
0.24 at z10. Nothing comes out dashed. The network stays continuous and the towns simply
draw thinner, which is what the corridor unit was for, and it is a real improvement on the
four earlier attempts, whose capped renders hollowed London into a radial skeleton.

**It still is not worth it, and the reason is new.** At z5, the zoom the cap exists for,
the worst tile went from 862 KB to 899 KB, and tippecanoe's own thinning report over the
far band says which way round that is. Against the merged file with no cap, two z5 tiles
exceeded the 977 KB ceiling and `--drop-densest-as-needed` cut them to 58.40% and 43.20% of
their features, leaving 105,117 features standing at z5. Against the capped file no z5 tile
reaches the ceiling, nothing is dropped, and 112,357 features stand. So 862 KB is a thinned
tile and 899 KB is a complete one. The cap adds no bytes to any tile. It removes the
thinning that was taking bytes off two of them, and does its own thinning across the whole
country instead.

**That is the paragraph above happening again, on the fifth attempt.** The capped build
leaves more features standing at z5 than the uncapped one, 112,357 against 105,117, and
lights fewer pixels in every window measured. Judged on features per zoom the cap wins at
the only zoom it was built for, and the render it wins with is the thinner one, which is
what "judge a low zoom by lit pixels, never by feature counts" is there to catch. It caught
it in tippecanoe's drop report, where none of the first four attempts looked.

After the merge, z8-z10 have no size pressure left for a cap to relieve. A cap also filters
the input to all three overview bands at once, so it thins z10, where there was never a
problem. Applying it to the `far` band alone was not measured.

**Every figure here is from the Great Britain and Ireland exports of 2026-08-16.** The
lit-pixel comparisons were built on macOS with `config.SIMPLIFY_SHARED_NODES` off, which is
the documented SIGTRAP workaround, both arms identical. The byte figures were rebuilt on
Linux in the published image with the flag on and came out the same, 34 MB to 19 MB.

The merge is the corridor work's one shipped outcome. The cap does draw the continuous
network the four earlier caps destroyed, and it arrives at a zoom where the thinning it
replaces was already cutting deeper than the ceiling asked for.

**What goes wrong.** Three of these are silent, and each of the three took an archive to
the point of being served before anyone noticed.

- *`tippecanoe` or `tile-join` not on `PATH`.* `build_tiles` names the missing tool and
  where to get it. The check runs after `export_edges_geojsonl`, so the export has already been
  paid for and `--from-export` is what skips repeating it.
- *An empty input.* Tippecanoe exits 110 rather than writing an empty archive, so a pass
  with no features is skipped instead of joined in: a region with no matched edges gets no
  road bands at all. With nothing to write at all, `build_tiles` raises rather than
  publishing a blank archive, which loads without complaint and reads as a broken viewer.
- *A filter naming an excluded attribute.* Tippecanoe applies `-x` before `-j`, so the
  filter matches nothing and the band comes out empty, measured on London as a 2.4 KB
  archive holding no tiles and no error.
- *A band that grew past its own top zoom.* `--extend-zooms-if-still-dropping` treats `-z`
  as a ceiling it may raise, and a `far` band asked for z5-z7 came back covering z5-z9 on
  Great Britain, overlapping the band above it and having `tile-join` merge both copies of
  every road. The flag is passed to the last band only.
- *A longitude within about a kilometre of the prime meridian.* It is written `-1.1e-05`,
  and Great Britain has 63 such features around Greenwich, which a number pattern without
  an exponent skips without a word. `_NUM` allows the exponent, and `_features` raises on
  a line it cannot read rather than passing over it.
- *A default-named archive beside a region-named one.* `default_out` refuses, since
  writing `bus.pmtiles` next to `great_britain.pmtiles` would leave the served file stale
  while reporting success. Pass `--name-by-region` or `--out`.

**Checking a built archive needs no database.**

    wayfare coverage out/great_britain.pmtiles
    wayfare draw out/great_britain.pmtiles look.png --zoom 6 --window -6 50 2 59

`coverage` reports features per cell for each quarter of the country, ranked by how much
the cell holds at z14, and the figure to read is the emptiest quarter's against a region
that is not filtered. `draw` rasterises a zoom so it can be looked at, which is the check
the feature counts cannot make.

**`draw` takes several archives and composites them into one buffer**, because these
islands are three archives and the viewer draws every one it is offered onto the one map.
The buffer is red, green and blue plus a fourth channel carrying the compositing weight,
and the PNG is truecolour rather than the 8-bit greyscale it used to be. `--theme light`
or `--theme dark` paints the map's own colours out of `map.toml`, and without it the
diagnostic greys are what they always were. Compositing ranks by layer first and trip
count second, in the order the viewer stacks its layers — road underneath, then track,
then segments — since ranking on trips alone put a trunk road over the tram line crossing
it. Reading a feature's colour means reading its tags, and a layer's keys and values
tables may be written after the features that refer to them, so the walker resolves the
tables before it resolves any feature.

Two faults came out of that work, and neither said anything. The greys were `{"bus": 255,
"segments": 90}` with no `track` entry, so every track feature fell to the other-layer
grey; they are keyed on the shared layer names now, and a layer `publish` writes cannot be
missing from the table. And weight 0 meant "unlit", so the quietest road of the bottom
layer was drawn and then counted as background, and a national render reported 0.2% lit
against the greyscale's 5.1%. Every drawn weight is offset by one.

**Every line `draw` lays down is a one-pixel *Bresenham* run, so a road off the axes comes
out a staircase.** The committed README picture was made of those, and at national scale
they read as pixelation rather than as thin roads. `draw` takes `supersample` now, exposed
as `--supersample` on `wayfare draw`. It draws into a buffer that many times wider and
taller, with a nib that many pixels square so a line still comes out one *output* pixel
wide, and averages each block back down when it resolves the buffer to PNG scanlines. Unlit
buffer pixels count as background in that average, which is what grades an edge into the
ground. Averaging at the resolve step rather than while drawing is what keeps the
compositing rule intact, since every buffer pixel still states one feature whole and a mixed
hue can only appear where two of them fall inside one output pixel, which is the width of
the blend *antialiasing* is. Blending while drawing would have put a third hue naming
neither mode along every crossing. The default is 1, and at 1 the output is byte-identical
to the bytes of the picture already committed, so the diagnostics keep the render they were
judged on. The lit fraction counts pixels of the *picture* rather than of the buffer and so
means the same thing at any factor, but it moves: these islands at z11 read 9.7% lit at 1
and 16.7% at 6, because an antialiased edge lights pixels Bresenham left dark. Cost is the
square of the factor in memory and much less in time, the whole-islands z11 render going
from 15s to 42s at 6, since about 8s of either is spent before any of it is drawn: `draw`
walks one zoom, so these three archives are 16.5 MB of z11 tiles rather than the 147.7 MB
they hold, and 0.7s of that is the read and the gunzip against 7.2s of parsing 1.14M
features out of the wire format. An antialiased edge is what does not compress, and the
committed `docs/banner.png` is 0.84 MB. `scripts/readme_map.py` passes 6, and it is the
only caller that asks for more than 1.

**`scripts/readme_map.py` draws the picture the README embeds.** `python
scripts/readme_map.py` writes `docs/banner.png`, and `--zoom`, `--width`, `--supersample`,
`--theme` and `--out` are what vary it. It has no `--check` and CI cannot run it, since
every data root is gitignored and a national build is a match run of a day or two, so the
picture is committed and the script is run by hand when the archives move. The three
regions are named rather than globbed, because `$WAYFARE_DATA/out` accumulates whatever has
been published into it, and `wales.pmtiles` beside `great_britain.pmtiles` is the same
roads twice. It defaults to z11 for its geometry rather than its attributes: a current
archive carries `trips` at every zoom, so the ramp draws at any of them, and z11 is the
finest band whose whole-country pass is seconds. The check under it is not dead, since an
archive built before `trips` reached the overview bands has it in the detail band alone and
comes out every road in the "no answer" grey below z11. `coverage.layer_attributes` is what
lets it warn about the band it was given rather than write that map.

It draws 1800 wide, which is the two device pixels a retina screen draws each of the
roughly 900 CSS pixels GitHub's readme column gives a full-width picture. The archives have
far more than that to give. This window's 12.4 degrees over 1800 pixels is about 6.9e-3
degrees each, against the 360 / (2048 * 4096) degrees of a z11 tile's coordinate grid,
about 4.3e-5, so the raster runs out long before the tile geometry does and a wider draw is
a real picture rather than an upscale. `--width` is what asks for one.

**The frame is a letterbox across the three archives**, Kerry at -10.6 east to Lowestoft at
1.8, at 2.5:1 and 1800 by 720 pixels, drawn at z11 and supersample 6 in the dark theme over
the coastline underlay. It comes out 0.84 MB, reads 22.5% lit and takes 36s to draw. It
drops Scotland and the north of England, and no arrangement of it does otherwise. The whole
islands are 13 degrees of longitude over 9 of latitude, nowhere near 2.5:1 in Mercator, so
a letterbox of them is a crop either way and the only choice is which end. The ferries that
leave the window are drawn up to its edge, because `draw` clips per pixel rather than per
line.

Its north and south edges are derived rather than written down, because what fixes them is
where London falls. London is the brightest thing on this map by a wide margin, so it is
placed on the lower third line rather than in the middle, and the frame is built outwards
from that latitude, the longitude span and the aspect. Four hard-coded latitudes would stop
meaning that the moment the aspect moved. The derivation runs in *Mercator* rather than in
degrees, since a third of the picture is a third of the projected height and the two differ
by about 0.1 degrees over this frame, which is enough to move London off the line it is
being placed on. `_mercator_y` is the script's own rather than `coverage._mercator`, which
is private, takes a longitude it does not need and has no inverse, and that inverse is what
the frame is built out of.

**The lit fraction is comparable at one width and at no other.** A drawn road stays about
one pixel wide whatever the width, so quadrupling the pixels roughly halves the fraction of
them a fixed network lights. The 22.5% above and the 1200-wide figures below were drawn at
different widths, and neither says anything about the other. `coverage`'s argument that the
lit fraction is the statistic worth comparing holds across archives and across builds drawn
at one width. Across two widths it says nothing.

**2.5:1 is as tall as the crop goes before it takes something.** At 3:1 Dublin goes over
the top edge, and at 3.5:1 Wales loses Snowdonia and the south coast is cut through. Both
are visible in the picture and neither is reported by any number. The lit fraction rises as
the crop tightens, 17.6% at 2.5:1 against 18.0% at 3:1 and 18.7% at 3.5:1, because a
tighter crop keeps proportionally more of England. All three were measured unsupersampled
at 1200 wide, which is what makes them a comparison with each other and with nothing else.
That is the blind spot the low zoom bands have, and it has the same answer. Draw the frame
and look.

**The coastline under it is a committed file, not a basemap.** `draw` takes an `underlay`
of longitude/latitude polylines and paints them below every feature, at a weight of 1
against the quietest road's 2. Without one the only thing saying where the land is, is
where the buses are, so a coast with no service on it — most of Sutherland, most of Kerry —
is not drawn at all. `scripts/coastline.py` clips Natural Earth's 1:10m coastline to
`map.toml`'s roam box and writes `docs/coastline.json`: 69 runs, 9,339 points, 144 KB at
three decimal places, which is about 70 m against a pixel that is about 480 m. Clipped to
the roam box rather than to the banner's frame, so the frame moves without the coastline
being rebuilt. The 10 MB source is cached in `RAW` like every other download and the
clipped file is committed, so a redraw makes no request.

The alternative was the viewer's own CARTO backdrop, and the numbers were not the
objection: the backdrop only has to match the output resolution rather than the vector
zoom, so 1800 pixels over the banner's 12.4 degrees is z8 and 50 tiles rather than z11 and
2,088. What ruled it out is that a raster backdrop puts a licence condition on a PNG that
travels without the page it was made for, and that baking a static asset out of a free tile
service is not what the service is provisioned for. Natural Earth is public domain and owes
nothing.

**A licence condition travels with the data, not with the page.** The credit is derived
from `config.Feed` and written into the archive's tileset metadata, so a copied archive
keeps it. `publish.contents` reads off the database which of `road`, `operator` and `track`
the archive holds, and the Open Database License (ODbL) credit to OpenStreetMap is owed
only where a route was matched onto its ways or drawn from its track. The viewer needs
`pmtiles.Protocol({ metadata: true })` or MapLibre never sees any of it, which looks like a
viewer crediting only its basemap rather than an error.

## Storage

**One DuckDB file**, `work/wayfare.duckdb`. DuckDB rather than SQLite because the central
operation is a group-by over a 5 GB comma-separated values (CSV) file, done out of core.
Geometry is integer micro-degrees rather than well-known text: `edges` carries
`lon_e6`/`lat_e6` INTEGER lists plus four bbox columns, and 1e-6 of a degree is 11 cm, so
the art window test is an exact integer overlap. Migrations run on connect and rewrite in
place, never re-running the pipeline, because a national match run costs a day or two and a
schema change it cannot survive is one nobody applies.

**DuckDB takes a single writer.** Match workers do HTTP only and the main thread writes.

**A connection holds one result at a time, and a second query abandons the first
silently.** Not an error and not a short read anything notices: a 200,000-row stream
interrupted after its first batch ends at 20,000 and looks complete. This is why
`Window.paths` resolves `self.weights` before it opens its stream. Making that scale lazy
put its query inside the draw loop, where it truncated every `density` and `spectrum`
render to its first fetch, and the renders were stable, plausible and wrong.

**DuckDB cannot spill an ordered list aggregate.** `list(x ORDER BY y)` pins its per-group
sort state in memory, and collapsing trips to stop sequences died on the London feed at
"failed to pin block", identically at a 7.4 GB limit and at a 10.2 GB limit on a 17 GB
machine. Raising the limit moves the wall. The fix is to project the needed columns into a
table in one streaming scan, which does spill, then aggregate in `WAYFARE_SEQ_PARTITIONS`
partitions keyed on `hash(trip_id)`, 16 by default. DuckDB spills to `temp_directory`, so
that path needs room.

**Never add a row-at-a-time insert loop on a table that grows with the network.** DuckDB
inserts about 2,700 rows/s through `executemany` inside a transaction, 25 rows/s without
one, and 1.6M rows/s from a file. `match` stages each batch to a file and reads it back:
CSV for `pattern_edges`, newline-delimited JSON for `edges` because it carries INTEGER[]
geometry and road names holding quotes and commas. Multi-row VALUES and unnest of parallel
arrays are no better than `executemany`.

**`wayfare prune` drops operator geometry, and it spares what is still drawn.**
`maintenance.prune_shapes` refuses while any matchable pattern is unmatched, counting matchable
patterns only, since a tram never gets a `match_status` row and would otherwise block it
for ever. The delete then spares every shape a live non-matchable pattern points at,
because for a tram or a ferry `shapes` is the only geometry there is and `segments` is a
derived copy of it. Wales measured 160 MB down to 114 MB compacted. Run it after
`aggregate` and before `cluster`, which is the only thing that gives the space back.

**Departed patterns are counted and never evicted.** `patterns` keeps the row,
`match_status` keeps the result, and nothing removes a departed pattern's `pattern_edges`
rows, so that table grows monotonically as a database is kept current. `prune_shapes` drops
operator geometry and nothing else. The rows are harmless to what is drawn, since every
consumer filters on `db.current_feed()`, and they are dead weight on disk that no command
currently reclaims.

## Clustering

**What it does.** `wayfare cluster` reorders `edges` on a space-filling curve and compacts
the file. It takes no flags and reports the row count, the time and the size either side.
It is safe to interrupt at any point, leaving the original untouched, and it needs room for
a second copy of the database while it runs.

**Why it prunes.** DuckDB keeps min/max zonemaps per 122,880-row group, and `match` inserts
edges in batch order, which is spatially random, so unclustered they prune nothing. Ordering
by a Morton code over the bbox centre makes a city window touch a handful of groups: Cardiff
reads 100% of `edges` down to 11.7%, 22 ms to 4.4 ms, and London 100% to 26.3%, 30 ms to 16
ms. Both verified from DuckDB's own `operator_rows_scanned` rather than inferred from wall
time. Hilbert reaches 5.9% on Cardiff and only beats Morton on the smallest window, so it
stays benchmark-only rather than earning the `spatial` extension. Wales, at roughly 2 row
groups, cannot show any of this. `maintenance.morton_sql` is the one implementation and
[`scripts/bench_window.py`](../scripts/bench_window.py) calls it, so the benchmark and the
command cannot drift. `maintenance.CLUSTER_BOX` is a fixed grid over Great Britain rather than the
data's extent, and the code is a physical row order and never an identity, so changing the
box costs a re-run.

**It has to write a new file.** DuckDB never gives space back below a file's high-water
mark: the dropped table's blocks stay allocated and neither `CHECKPOINT` nor `VACUUM`
reclaims them, so reordering in place makes the database bigger, measured at 505 MB going to
730 MB. `COPY FROM DATABASE` into a freshly attached file reclaims them and preserves row
order, so the curve survives the copy. `maintenance.cluster` reorders in place, copies to
`<db>.compacting`, reopens and counts it, re-asserts the indexes and only then renames
atomically. Sorted neighbours also compress better, which is the other half of the win: 528
MB to 453 MB on the benchmark's 4.2M edges.

**Clustering goes stale rather than off.** `cluster_edges` records the row count it sorted
in `meta.edges_clustered`, so rows a later `match` appends land unsorted on the end where no
zonemap can help. `wayfare status` reports `yes`, `no` or `stale (N of M edges sorted)`. It
is a separate command rather than a step in `aggregate` for the same reason `prune` is,
being worth doing once after a match run rather than on every re-aggregation.

**`edge_services` cannot prune, and a bbox column is not worth adding.** It carries no bbox
column and DuckDB pushes no min/max filter through the join, so the weights pass reads all
10.25M rows under every layout. The column was measured: it prunes almost exactly as hoped,
Cardiff 10,250,638 rows down to 614,400 and London to 2,457,600, and buys 40.9 ms to 31.1 ms
on Cardiff, 356.1 ms to 357.6 ms on London and nothing nationally. That is four INTEGER
columns on the largest table in the database for 10 ms on the smallest window; the scan was
never what cost anything.

Most of what is written above began as a bug, and the entries that read as arbitrary
constants are usually the second answer to a question the first answer got wrong. The
figures here are worth re-measuring against a run of your own before they are relied on to
size anything.
