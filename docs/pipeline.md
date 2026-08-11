# Pipeline

Six stages, each reading what the last wrote, each independently re-runnable:

    acquire  -> raw downloads
    patterns -> 1.55M trips collapse to distinct ordered stop sequences
    match    -> Valhalla; the stage that runs for a day or two
    trace    -> OSM route relations for the modes with no road and no shape
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

**The number that decides everything is *feed churn*** — how many patterns are new
from one feed to the next — and it is now measured. Two Wales feeds two days apart,
`20260806_023912` then `20260808_024504`, took 3,584 patterns to 3,541. `patterns`
logs new / carried over still unmatched / departed every run: 30 new, 0 carried over
still unmatched, 73 departed, and 3,584 − 73 + 30 = 3,541 closes the accounting
exactly. `wayfare status` then reported `patterns_pending` 30 and
`patterns_departed` 73. Catching up cost 30 patterns at Wales's measured 3.6/s,
about 8 seconds, against 16m23s for the original full run. That is a two-day delta
on one region rather than a month across the nation; the caveats are in
docs/results.md.

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

**The `stops` path recovers 95.1% of the road an operator trace covers, and the way it
fails is by inventing.** Wales is 85% `shape`, which makes it ground truth for the
strategy that has to carry the other half of the country. All 3,052 Wales patterns
that carry operator geometry and matched ok on the `shape` path were matched *both*
ways in one run against one live Valhalla — once normally, once with the geometry
withheld so `match_one` takes the `stops` branch — and the edge sets compared. Pooled
length recall 0.951, 0.960 trip-weighted; pooled precision 0.892. So the synthesised
route under-recovers by 4.4% and over-draws by 7.4%, which is 1,456 km of phantom road
at network level. Invention is the larger error, and bad geometry is worse than
missing geometry — the entry below. Span predicts quality and stop spacing does not:
under 2 km scores 0.907 and over 20 km scores 0.959, because a short urban pattern's
length is dominated by a town centre where one wrong block is a large share of the
route. That is not reassuring for London, which is entirely `stops` path and entirely
dense urban. The harness is a census rather than a sample and takes 84 s a pass, so
re-running it is the cheap way to judge any change to the matcher.

**`break_through` is wrong wherever a pattern doubles back, and the answer is 50 m
wide.** Valhalla's `break_through` location type forbids a U-turn at the stop, which
is what a bus does at an ordinary stop and not what it does on an out-and-back spur.
Refused the turn, the router replies with a lap of the block: service 86B in Newtown
routed 24.3 km over a 5.9 km span, was thrown out by the detour guard, and drew
nothing at all across three patterns. `_location_types` now relaxes the stops
*between* a stop and its return to plain `break`, which permits the turn without
asking for one — Valhalla still pays the turn cost. Everything else keeps
`break_through`, because that is what lets the `edge_walk` second pass recover edges
exactly.

**The two visits are separate stops tens of metres apart, so an exact coordinate test
finds none of them.** 86B returns to Montgomeryshire Infirmary 28 m from where it left
it and to Tan-y-Graig 7 m away, on different NaPTAN ids — the two kerbs of one road.
`REVISIT_M` is 50 m, wider than a road and well inside the gap between stops — Wales's
118,676 consecutive pairs are 126 m apart at the fifth percentile, and only 0.38% of
stop-to-stop-after-next pairs, the closest two the rule can even consider, fall within
50 m. Adjacent stops are excluded, since those are a junction rather than a turn, and
so is the first-to-last pair, or every circular in the country would relax end to end.
1,018 of Wales's 3,584 patterns carry at least one revisit.

**The fix trades 21 km of real road for 465 km of invented road, and that is the whole
argument for it.** Measured by re-running the census above with the shipped matcher
under each rule, paired pattern by pattern in one process against one graph build:
pooled length recall 0.9513 → 0.9510, trip-weighted 0.9601 → 0.9601, pooled precision
0.8748 → 0.8802. Phantom road falls 9,309 km → 8,844 km and three patterns stop being
rejected. Recall alone reads as a loss — 191 patterns improve and 268 regress —
because the relaxation mostly *removes* road, and removing road can only lower recall.
The symmetric measures are the ones to read: Jaccard improves on 353 patterns against
191, the harmonic mean of precision and recall on 354 against 182, and its
trip-weighted mean goes 0.9212 → 0.9242. Nothing predicts which patterns regress.
Bucketed by revisit span, by number of revisits and by the fraction of stops relaxed,
every bucket shows a small negative recall delta and a positive precision delta.

**Two narrower rules were measured and rejected, and the tempting one is the retry.**
Relaxing the two visits as well as the stops between them costs 36.6 km of real road
against 20.8 km for the same phantom saving, and regresses 283 patterns rather than
268. Applying the relaxation only after the detour guard fires has a blast radius of
exactly three patterns and no regressions at all, which sounds ideal until the numbers
land: it rescues the same three 86B patterns and forfeits the entire 465 km of
precision gain. Three rescues are worth 85 trips. The precision gain is worth 5% of
every invented metre in Wales.

**Half the named tail is not this bug, and one case was misfiled.** Patterns 1790 and
1346 route 95.9 km and 26.2 km because stops fall outside the Wales extract, not
because they double back — 1346 carries no revisit within 50 m at all, and six of its
ten stops are Gloucestershire ATCO codes. Both are unchanged by the fix and both stay
rejected, which is correct: `map_snap` on an operator shape degrades gracefully when
the graph does not cover the route and `break_through` cannot, so the detour guard is
the only thing standing between a regional extract and 95 km of confident-looking
phantom. Pattern 1 (Cardiff service 6, 1,360 trips, recall 0.808) is unchanged too. It
takes Callaghan Square where the bus takes Bute Street, two ways round one block, both
plausible to a router. Nothing detects that.

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

## trace

**A mode can publish a complete stop sequence and no geometry at all, and the
largest one in the country does.** Great Britain's `routes.txt` carries
`route_type=1` on 54 routes, 61,288 trips over 1,525 patterns, and 1,417 of those
patterns (92.9%) have no `shape_id`. `route_type=2` is three routes, all of them
the Docklands Light Railway (DLR), 71 patterns, not one with a shape. Seventeen
named lines arrive this way. The London Underground contributes 41 line records
over 11 named lines and 58,560 trips, the DLR 6,630 trips, and beside them sit
London Trams, West Midlands Metro, Blackpool, the Air-Rail Link and the IFS Cloud
Cable Car. Measured against Bus Open Data Service (BODS) feed `20260806_022608`.

**Both of the other geometry paths refuse them.** `db.matchable` keeps a metro away
from Valhalla, because there is no road under a tube tunnel to match onto, and
`aggregate.build_segments` copies an operator trace that the feed does not carry.
Those patterns were counted by mode and drawn nowhere. `wayfare trace` is the third
path, and its source is OpenStreetMap *route relations*.

**A route relation is already ordered, which makes this an ingestion job.** Its
`role=""` way members chain end to end into one continuous path in the order the
service runs them, so a pattern's geometry is a cut of that chain between the first
stop it calls at and the last. Nothing is snapped. There is no shortest path, no
*hidden Markov model* and nothing to disambiguate, and `wayfare/osm.py` is a fetch, a
walk and a projection.

**The relations exist and the chains are clean.** The Greater London bounding box
holds 556 route relations. Each of the eleven Underground lines has a
`route_master`, as do the DLR (5), London Trams (3) and the Elizabeth line (5).
Walking the `role=""` ways in member order breaks nowhere on any line tested:
Victoria 24 ways over 21.69 km against an official 21 km, Central 125 ways over
54.75 km, Jubilee 59 ways over 37.15 km, and the DLR's Lewisham to Stratford
relation 81 ways over 11.02 km.

**Work selection is `match`'s, and its three conditions are each load-bearing.** A
pattern is owed a trace when it is in the current feed, when `db.matchable` is false
for it, and when its `shape_id` is NULL. The first keeps departed journeys out, the
second leaves the road to Valhalla, and the third keeps the operator's own recording
ahead of anything reassembled from OpenStreetMap — a feed trace records where the
vehicle goes, a relation records where the track is, and the two differ at a depot
or a turnback. Work is then selected by the absence of a `trace_status` row, exactly
as `match` selects on the absence of a `match_status` row. Patterns are handed out
busiest first, so a run cut short leaves the busiest lines drawn.

**Overpass rather than the OpenStreetMap application programming interface (API),
because the stage has to *discover* relations over an area.** The API answers for an
id that is already known, and nothing in a timetable carries a relation id. One
request per run is enough. The first `out body geom` statement inlines every member
way's coordinates, which is what replaces thousands of per-way fetches, and a second
`out` statement returns the member nodes with their tags. That second statement is
not redundant. A relation member carries a role and an id, never the tags of the
thing it points at, and the station names are the join key. The window asked for is
the pending patterns' stop extent padded by 0.2 degrees, about 22 km, because a line
runs well past the box its stops sit in — the Central line reaches Epping. The
response body is cached to `raw/osm_relations.json` as raw bytes, so a parser fix
applies to bytes already paid for.

**The fit is a subsequence search over normalised station names.** Great Britain's
1,417 Underground patterns come from 459 distinct station sequences over 11 lines,
and every one of them is a contiguous sub-path of its line, since a short working of
the Northern line still runs on Northern line track. A pattern is therefore tested
against the relations calling at either of its ends, its stop names are looked for
as a contiguous run through the relation's, and the reverse order is tried as well
because a relation is per direction. Every placement is tried rather than the first.
A relation calling at one station twice offers more than one, and only one of them
projects in order along the track.

**There is no `naptan:AtcoCode` on an Underground stop node, so the join is by name
with a coordinate check.** The identifier that would settle it is absent, which
leaves the station name, and the two publishers spell it differently. BODS writes
"Blackhorse Road Station", "Pimlico Station" and "King's Cross St. Pancras
Underground Station" where OpenStreetMap has "Blackhorse Road station", "Pimlico"
and "King's Cross St Pancras". `osm.normalise` folds case, expands the ampersand,
deletes apostrophes, turns the remaining punctuation into spaces and strips a
station suffix twice — twice, because "Edgware Road Station Station" is in one
feed's stop register. Measured on the Victoria line, that takes the join from
partial to 16 of 16.

**The coordinate check is what stops a name matching the wrong line, and it is set
at 400 m.** 15 of the Victoria line's 16 stops sit within 150 m of the node they
matched. The exception is Highbury & Islington at 216 m, where the timetable's point
is the National Rail entrance and the OpenStreetMap node is the tube platform. A
large interchange is where the two publishers disagree most, so `TRACE_STOP_MAX_M`
has to clear that case, and 400 m does while staying well under the roughly 1.2 km
spacing between Underground stations.

**What gets projected onto the chain is the relation's own stop nodes.** The node is
on the track by construction, where the feed's point can be a station entrance 216 m
away on the National Rail side of an interchange. Projecting the further of the two
risks landing on a parallel line and cutting the chain in the wrong place. The
feed's coordinate does the other job, which is checking that the name join found the
right station.

**A sequence that turns round partway is refused rather than drawn.** The matched
stops must project one way along the chain, and either way will do, since a relation
is per direction and half a line's patterns run from its far end back towards its
first way. What is refused is a sequence that reverses in the middle, which is a
loop — the New Addington branch is one — or a placement that doubles back. Slicing
between the two ends of such a sequence takes the wrong branch and draws confident
track no service runs on. `TRACE_MONOTONIC_SLACK_M` is 250 m, wide enough for a
station node sitting slightly behind its neighbour's projection and far short of a
turn. This is "bad geometry is worse than missing geometry" applied to a stage with
no confidence score to fall back on.

**Four traps, and each one looks like missing data rather than a mistake.**

- **Platform members must leave the way chain.** Leaving `role=platform`,
  `platform_entry_only` and `platform_exit_only` in produces 11 to 25 spurious
  breaks per relation, which reads as broken mapping across the whole of London.
  `config.OSM_STOP_ROLES` names the three roles that are calling points, and the
  chain takes `role=""` and nothing else.
- **The Elizabeth line is `route=train`, not `route=subway`.** A mode filter written
  from the obvious names misses it outright. `config.OSM_ROUTE_VALUES` is therefore
  deliberately wide — subway, light rail, tram, train, monorail, funicular and
  aerialway — and the stop-sequence join is what decides which relation a pattern
  belongs to. A few thousand extra relations in one request is the cheaper mistake.
- **Way tags are not a join key.** `ref` is on 2.4% of subway ways and carries
  signalling codes rather than line names. `line` reaches 62.1% and is multi-valued
  on shared track, with separators that vary by mapper. Ways are reached through the
  relation in member order, never through their own tags.
- **The coverage argument against OpenStreetMap bus relations does not transfer.**
  docs/data.md rejects `route=bus` as a source on 12,968 relations and 818 route
  masters against the whole bus network. Rail inverts every term of it: roughly
  1,000 relations cover roughly 1,000 GB rail services, every London line carries a
  `route_master`, and they are among the best-maintained relations in the country.

**`segments` now has two sources, and they are disjoint by construction.**
`aggregate.build_segments` unions the operator shape of every live non-road pattern
with the `traces` row of every live non-road pattern whose `shape_id` is NULL. That
NULL is what keeps the two arms from overlapping, and it is what lets `segments`
keep its primary key on `pattern_id`. A non-road pattern with neither source still
gets no row and is still not drawn, so the log line reports all three counts and a
region drawing fewer segments than it has patterns says so.

**Every pattern gets a `trace_status` row, and one status is retryable.** The
vocabulary is `match_status`'s for the reason the stage's shape is. A tracer that
re-fetches every unresolvable pattern on every run never finishes, which needs
"failed" to mean "impossible".

    ok               the relation chained and carried the pattern's stop sequence
    no_relation      nothing fetched calls at either of this pattern's ends
    chain_break      the relation's ways do not form one continuous path
    no_stop_match    a relation shares a terminus and not the sequence
    not_monotonic    the stops matched and project out of order along the chain
    skipped          fewer than two stops to fit
    error            a bug or a malformed response, permanent until the code moves
    transport_error  the request never got an answer, so nothing was learned

`chain_break` describes a relation rather than a pattern, and the run reports it as
a count. `trace.prepare` chains every relation once and drops the ones that break
before any pattern is fitted, so a pattern whose only candidate broke is recorded
`no_relation`. `wayfare trace --retry transient` clears `transport_error` and
nothing else, exactly as `match --retry transient` does, and both must land before
the first work is selected: a row deleted while its pattern is in flight hands that
pattern out twice.

**A failure to reach Overpass at all is deliberately not written down.** Nothing was
learned about any pattern, so a permanent row would be a lie about all of them at
once. `wayfare all` logs a warning and carries on rather than throwing away a match
run that has just cost a day or two, and the patterns keep no status row, so the
next `wayfare trace` picks them up unchanged. Trace failures also stay out of the
publish gate, which counts matchable patterns only; docs/deploy.md has that
reasoning.

**None of the numbers above came from running this stage.** The census of the feed
and the survey of the relations were both taken before it was written, against BODS
`20260806_022608` and one Overpass query over Greater London. The stage is built and
tested and has not yet run against the national database, which lives on the server.
What it resolves, refuses and draws nationally belongs in docs/results.md, once
there is a run to report.

## aggregate

**A non-road mode has no road under it, so it is never matched and is drawn from the
operator's own trace.** `db.matchable` is the predicate that keeps a tram, a metro, a
train or a ferry away from Valhalla, and `aggregate.build_segments` is what draws it
instead: one row in `segments` for each live non-road pattern, holding the shape its
operator published, in the same integer micro-degrees `edges` uses. That path has no
matcher, no routing and no snapping in it. A recording is the best geometry available
for a mode with no way in the graph, and it is a survey rather than a schematic —
Metrolink's traces run to a median 474 points against the bus feed's 849. A route
therefore gets its geometry one of two ways. The mode decides which.

**`segments` is rebuilt outright rather than merged, and a pattern with no shape gets
no row.** The table is derived from `patterns` and `shapes` and costs one
`INSERT ... SELECT` to recompute, where a match costs a Valhalla call and is cached for
ever, so it holds the current feed only exactly as `pattern_stops` does and a departed
tram stops being drawn on the next run. Missing geometry is left missing for the reason
the match section already gives: the stops are known, so a straight line between two of
them would draw perfectly happily down the wrong side of a river. Great Britain's
ferries are the worked example, 244 of 416 patterns carrying a trace and the other 172
drawing nothing at all, and docs/data.md has the reasoning for that mode in particular.

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
had nothing to remove, so low zooms carry full-detail geometry. What a low zoom holds
is therefore decided by a feature quota applied to the input, not by
`--drop-densest-as-needed` shedding whole roads.

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

**The archive is three tippecanoe builds joined by `tile-join`, not two.** `far` covers
z5-z7 and reads a filtered copy of the export, `near` covers z8-z10 and reads all of it,
`detail` covers z11-z14. The first two exclude the card-only attributes, which is how the
z11+ confinement above is implemented. Splitting the low zooms into their own *band* is
what lets them take a different input from the rest.

**A region with non-road modes gets a fourth pass, and its segments go in a layer of
their own.** The two layers carry different attributes and are drawn differently: the
viewer colours a road by how many distinct services use it, and a segment by its mode,
with a legend listing the modes actually drawn. `tile-join` keeps distinct layer names,
so the second layer costs one tippecanoe pass and nothing else. That pass covers z5-z14
in one band rather than three. Banding exists to keep info-card attributes off millions
of edges at zooms nobody reads them at and to thin the quietest roads out of the far
view; Great Britain holds 630 segments against 2.7M edges, and a tram line thinned out
of its own layer would only be missing. Either half can be absent, and each is skipped
rather than joined in empty: a bus-only region gets no segments pass, and a region with
no matched edges gets no road bands, because tippecanoe exits 110 on an empty input
rather than writing an empty archive. Irish Rail on its own is 331 patterns and not one
of them is a road.

**`--drop-densest-as-needed` chooses by spatial density, so it thins cities hardest and
leaves a rural road carrying two buses a week alone.** On the Great Britain archive
published 2026-08-07, tippecanoe's own `strategies` metadata records it shedding 922,505
features at z5, 841,401 at z6, 779,546 at z7, 538,485 at z8 and 298,823 at z9, and nothing
from z10 up. Decoding the finished archive gives z5 holding 5.1% of what z14 held, z6
9.7%, z7 14.3%, z8 37.6%, z9 58.3% and z10 86.1%. Ireland's archive carries no
`strategies` key at all, and neither does Northern Ireland's, so nothing was thinned at any
zoom in either, and Ireland holds 41.5% at z5 and 75.7% at z8. Great Britain carries
1,095,684 features at z14 against Ireland's 115,853, 9.5x the network, but at z5 carried
55,998 against 48,043, only 1.17x. Density was the wrong criterion.

**One national `trips` floor emptied half the map, and it shipped.** The first answer
ranked the whole region on `trips` and kept the highest 214,000 features. `trips` is an
absolute count, so ranking a country on one scale ranks it by how urban it is. At the
703-trip floor that produced, measured over 0.25-degree cells on Great Britain, 310 of the
655 cells holding any bus road lost every feature they had, 47.3% of them. The ten busiest
cells held 45.9% of the survivors, and the top tenth of cells went from 48.7% of the map's
features to 81.7%. On the map that drew the dense cities with black between them. The
archive was republished uncapped as soon as it was seen, so the fault was live only
briefly.

**The overview is not capped, and four attempts to cap it all made the map worse.**
`config.OVERVIEW_CAP_FAR` and `config.OVERVIEW_CAP_MID` are both `None`. The quota
machinery beneath them — a per-cell `trips` floor, a weighting exponent and a cell size
— is switched off and kept only because z5 is still thinner than Ireland, which is the
one place a selection might still earn its keep.

The attempts, in order, were a single national `trips` floor, which took every feature
from 310 of Great Britain's 655 populated cells; a per-cell floor sharing the cap out in
proportion to cell size, which emptied nothing and drew 15 features per rural cell at z6
where Ireland drew 53; the same weighted by the square root of cell size, which restored
the countryside and flattened the cities; and the same again on a 0.02-degree grid, which
took coverage of 1.4 km bins from 37.8% to 88.9% and was still worse on screen than no cap.

**Lit pixels are what a reader sees, and every cap loses them at every zoom.** Measured
by rasterising the tile geometry into a window and counting:

| window              | z5   | z6   | z7   |
| ------------------- | ---- | ---- | ---- |
| Ireland, Dublin     | 4.7% | 4.6% | 4.5% |
| GB uncapped, London | 3.8% | 7.0% | 9.3% |
| GB capped, London   | 2.7% | 3.7% | 3.7% |
| GB uncapped, Wales  | 1.1% | 1.2% | 1.3% |
| GB capped, Wales    | 1.0% | 1.0% | 1.0% |

At z8 around London the capped archive lit 5.0% against 8.2% uncapped, and the render
showed the city hollowed into a radial skeleton. Uncapped Great Britain already exceeds
Ireland's density at z6 and z7; the deficit is z5 alone, 3.8% against 4.7%.

**The counting mistake is the part worth keeping.** A cap keeps many short features
spread over many cells, and no cap keeps fewer, longer ones. Features per zoom, populated
cells, features per cell, and bins holding any feature all reward the first, and only the
second reaches the screen — so four rounds shipped on numbers that rose while the map got
worse. `wayfare coverage` counts features and inherits the same blind spot.

`--drop-densest-as-needed` remains the only thing thinning a low zoom. It chooses by
density rather than by service level, which is a genuine fault: on the 2026-08-07 archive
it shed 922,505 features at z5. It thins only the tiles that will not fit, 18 at z5-z7 and
4 at z8-z9, where a cap thins the whole country to spare them.

**Tippecanoe applies `-x` before `-j`, so a feature filter naming an excluded attribute
matches nothing.** Measured on London, `-x trips` with a `-j` filter on `trips` built a
2.4 KB archive holding no tiles, and reported no error. Filtering the input file avoids the
ordering entirely.

**`--extend-zooms-if-still-dropping` treats `-z` as a ceiling it may raise.** A `far` band
asked for z5-z7 came back covering z5-z9, which overlaps `near`, and `tile-join` would then
merge both copies of every road into those tiles. The flag is passed to the last band only.

**A longitude within about a kilometre of the prime meridian is written in scientific
notation.** It comes out as `-1.1e-05`, and Great Britain has 63 such features, around
Greenwich. A number pattern of `-?\d+\.?\d*` matches every other line in the export and
skips exactly those, so the filter would have put them in the wrong cell or dropped them
without a word. The pattern now allows an exponent.

All three failures produce an archive that builds, uploads and opens without complaint.
Counting features, per zoom in the finished archive and per cell in the filtered export, is
what catches them, and it is the measurement the whole banding rests on.

**A feature count cannot see a hole, and neither can a cell that draws anything at all.**
Both weightings above passed the checks that were being run. The proportional one drew every
one of Great Britain's 655 populated cells and carried 1.76x the features of the uncapped
build at z5, and it still looked like cities in a black field, because a cell holding one
road counts the same as a cell holding eighty. What separates them is decoding the *Mapbox
Vector Tile* geometry back to longitude and latitude and counting drawn features per cell
per zoom, split by how much the cell holds at z14. Under the proportional weight that
measurement reads 15 features per rural cell against 407 per urban one; under the square
root it reads 41 against 323.

`wayfare coverage <archive>` is that measurement, and it needs the archive and nothing
else — no database, no export — so a published file can be checked wherever it ended up.
It reports features per cell for each quarter of the country, ranked by how much the cell
holds at z14.

The figure to read is the emptiest quarter's, in features, against a region that is not
filtered. Great Britain draws 36 features per rural cell at z5 and 52 at z8, and Ireland
draws 44 and 62. The proportional weight drew 15 at z6 where Ireland drew 53, with 28 rural
cells under five features against Ireland's 6. The ratio between the busiest quarter and
the emptiest is reported beside it and is the weaker signal of the two: Great Britain's
cities are dense enough that its unfiltered z10 sits at 52.2x where Ireland's sits at 10.8x,
so the two regions cannot be compared on it, and the build that put black between the
cities read 27.1x at z6 — lower than its own z10, and no worse than what replaced it.
