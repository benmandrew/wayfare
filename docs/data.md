# Data sources

Every region wayfare draws comes from a different publisher, under a different
licence, with a different amount of geometry attached. This file is the reference to
read before running one: which feed a region uses, how to point the pipeline at it,
what the data covers, where it is thin, and what obligations travel with it.

Every coverage figure here is a snapshot of a live feed. The dates and feed versions
are given because they move.

## Choosing a region

`wayfare acquire --region SLUG` selects the feed, and `WAYFARE_REGION` sets the
default, which is `all`. `config.feed()` resolves the slug: `config.FEEDS` holds the
two exceptions, `ireland` and `northern_ireland`, and every other slug is built as a
Bus Open Data Service (BODS) URL on demand.

The BODS slugs are `all`, `england`, `scotland`, `wales`, `north_east`, `north_west`,
`yorkshire`, `east_midlands`, `west_midlands`, `east_anglia`, `london`, `south_east`
and `south_west`. Use `wales` for development; the bundle is 41 MB.

Give each region its own `WAYFARE_DATA` root. `meta.feed_version` holds one value, so
a second region acquired into the first's database becomes the current feed and the
next `publish` overwrites the first region's archive. The Republic and Northern
Ireland are the one case where the *graph* is shared — Geofabrik's
`ireland-and-northern-ireland-latest.osm.pbf` is 409 MB and splits at the sea rather
than at the border, so both halves match against one build and one GraphId space.
Sharing the graph does not mean sharing the database.

Disk for a national run: the BODS national General Transit Feed Specification (GTFS)
bundle is 1.28 GB zipped and 7.84 GB unpacked, of which `stop_times.txt` is 5.09 GB and
`shapes.txt` 2.53 GB, over 1.55M trips. The Great Britain OpenStreetMap (OSM) extract is
2.16 GB and the National Public Transport Access Nodes (NaPTAN) CSV is 102 MB over 435k
records. Budget about 40 GB including the Valhalla graph.

## Geometry coverage

Whether a trip carries a `shape_id` decides which path the matcher takes, and it is
the largest difference between the three regions.

| Region | Trips with geometry | Measured |
| --- | --- | --- |
| Great Britain (BODS) | 48.3% of trips, 748,087 of 1,549,590 | 2026-08-06, feed `20260806_022608` |
| Republic of Ireland (NTA) | 100% of 129,405 trips, all 108 agencies | 2026-08-09, feed `20260808_b375dfac` |
| Northern Ireland (Translink) | 62.0% of journeys, 58.0% of patterns | 2026-08-09, feed `20260806_140751` |

Great Britain's split is strictly per-operator and all-or-nothing, tracking whose
scheduling software emits TransXChange `TrackPoint`s: Stagecoach North East 100%, Go
North East 0%, Arriva North East 0%. Map matching is therefore the primary path and
not a fallback. Where shapes exist they are genuine road geometry, at a median 849
points, a p90 of 2,109 and a maximum of 3,705.

An operator switching `TrackPoint`s on is an opt-in re-match. A pattern matched from
bare stops that later gains a `shape_id` is worth redoing, because it turns a guess
into an observation, but it adds work to a queue meant to be predictable. The count is
always logged and `wayfare patterns --upgrade-shapes` is what acts on it.

### Northern Ireland's gap, and why it widens

Northern Ireland's figure is the one that moves on its own. A shape there is all of a
journey's hops or none of them: stitching what exists and jumping the rest hands
`map_snap` a straight line across a town, which it lays down the wrong roads with
confidence, and bad geometry is worse than none. Individual hops are 96.1% covered, so
the loss is concentrated. 791 of 12,372 timetabled stops appear nowhere in the
geometry at all and account for 1,408 of the 1,995 missing hops. Falling back to the
reversed polyline of the opposite hop was measured and rejected: it exists for 23 of
the 1,995, and a one-way system makes it wrong.

Translink publishes the two halves on unrelated cadences. The timetable is republished
every few weeks; the road geometry has not moved since 2025-09-23. The coverage figure
above therefore falls with every timetable release until `PtLinks` is republished. The
83.5% predicted from the previous timetable is history rather than a forecast.

## Great Britain: BODS

BODS sends no `Content-Length`, so a truncated download looks exactly like a complete one.
`MIN_GTFS_BYTES` and the `.part` staging in [`acquire.py`](../wayfare/acquire.py) are what
catch it. BODS also answers anything that looks like a generic scraper with a block, so
`config.USER_AGENT` is load-bearing.

BODS carries international coach, and its continental stops are correct. 41 live stops of
the August 2026 national feed stand between Calais and Warsaw, the furthest at 20.96°E,
so nothing that checks a coordinate for validity catches them. What they break is
anything that sizes a window off stop coordinates: a plain min/max over every live stop
asks Overpass for a box from Ireland to Poland, which exhausts the 8 GB limit and kills
the stage with no traceback.

`config.british_isles_sql` is the boundary, and `patterns` drops any pattern calling
outside it — 52 of 55,198, every one of them coach. Three of its four bounds are a box.
The fourth is a capped line through the Channel, because Calais is half a degree east of
Dover while Brittany is two degrees west of Cornwall, and no single straight line
separates both. The nearest British stop it keeps and the nearest continental stop it
drops are each about 15 km clear of it. The whole pattern goes rather than the offending
stop, since dropping the stop alone would leave a London-to-Warsaw coach in the dataset
as a London-to-Dover one, and `span_m`, the detour check and every bounding box would
read it as domestic.

## The Republic of Ireland: the NTA

The National Transport Authority (NTA) publishes the whole timetable as one bundle at
`https://www.transportforireland.ie/transitData/Data/GTFS_All.zip`, with no key and no
registration. It is the cheapest region this project has added. Measured 2026-08-09
against feed `20260808_b375dfac`: 108 MB zipped, 492 MB unpacked, 4,421,418 stop times.
The mode filter drops 2,847 rail trips over 19 routes and 2,655 tram trips over 2
routes before anything else runs, and the remaining 123,903 road-going trips collapse
to 2,853 patterns in 2 seconds — a 43.4x collapse against Wales's 10.3x and London's
102x — over 14,027 stops, 106 agencies and 759 distinct service numbers. 2,852 distinct
shapes serve those 2,853 patterns, so exactly one is shared, and every pattern takes
the `shape` path at one Valhalla call each.

The licence is Creative Commons Attribution 4.0 (CC BY 4.0) rather than the Open
Government Licence (OGL), so attribution is a condition rather than a courtesy.

NaPTAN is GB-only and a region outside GB must not fetch it, which is what
`config.Feed.stop_register` records. The NTA host is better behaved than BODS on every
count measured: it sends a `Content-Length`, answers a Range request with a 206, and
returns a real 404 on a bad path rather than an HTTP 200 error page. `_stream` therefore
checks the bytes written against the declared length, skipping the check on a
`Content-Encoding: gzip` body, and treats a short read as a retryable `OSError`.

The NTA's `feed_version` is a globally unique identifier (GUID) such as
`B375DFAC-C156-4A9E-A642-8DF76AAA2A51`, which cannot be ordered and says nothing about
when it was published. `acquire.feed_version` rewrites an opaque version as
`feed_start_date` plus the first eight hex digits, giving `20260808_b375dfac`. The date
alone will not do, because the NTA declares a year-long validity window and republishes
inside it, and a version that fails to move between publications leaves withdrawn
services looking live to every consumer of `patterns`. A BODS timestamp is passed
through untouched, since changing it would orphan every pattern in an existing database.

## Northern Ireland: Translink and OpenDataNI

Northern Ireland has no GTFS and, since 2026-08-06, no Association of Transport
Coordinating Officers common interface file (ATCO.CIF) either. BODS and NaPTAN are both
GB-only, so Translink publishes the province through OpenDataNI. Both timetable
datasets carry TransXChange 2.4, the same format BODS is built from, and TransXChange
`StopPoint`s carry World Geodetic System 1984 (WGS84) longitude and latitude directly,
so no reprojection and no converter sits anywhere near the pattern identity. Re-check
the format before trusting any figure here; it moved once with no announcement.

The source is four OpenDataNI datasets: two TransXChange timetables (Ulsterbus and
Goldline, 69.6 MB over two files; Metro and Glider, 104.6 MB) and two MapInfo route
bundles. Translink re-uploads rather than overwrites, so the CKAN resource id and the
filename both move on every publication — the timetable was `ulb-gle-16042026.zip` in
April and `ulsterbus-and-goldline-until-31st-august-26.zip` in August.
`translink.resource` resolves each dataset through CKAN `package_show` and takes the
most recently published ZIP, which is also what separates the current Metro routes from
the 2022 Glider ones filed in the same dataset. The slugs are historical names that
stopped describing their contents years ago, so do not tidy them: the one reading "metro
timetable valid from 18 June until 31 August 2016" is the live Metro and Glider feed.
Like BODS, OpenDataNI answers a generic scraper with a 403. Everything is OGL v3.0.

Road geometry arrives separately. The `PtLinks` bundle is MapInfo Interchange Format (MIF)
with its companion MID attribute file: 37,913 stop-to-stop polylines, read by
[`wayfare/mapinfo.py`](../wayfare/mapinfo.py) in 182 lines with no new dependency. The
`.MIF` holds one object per feature and the `.MID` one delimited row per object, in order,
with no key of its own, so an unrecognised keyword shifts every later attribute row onto the
wrong road and the result still draws. The parser therefore raises on any object type it
does not know, checks that both files run out together, and asserts `CoordSys Earth
Projection 1`, since Irish Grid eastings read as degrees would put Belfast in the Atlantic.
`None` is the trap: an attribute row with no geometry, on a line of its own, and Translink
writes 152 of them among the 37,913.

`StoppingPoints.GlobalId` is the NaPTAN ATCO code and it is the whole join between the
two halves. Of 14,754 hops whose polyline could be tested against both stops, 14,747
start nearer the from-stop than the to-stop, at a median 6 m and a p90 of 13 m. A hop
usually has several polylines, one per line and branch that runs it, and the kept one is
the shortest rather than the first, because a pick made from file order changes every
shape when Translink re-exports.

The pattern identity is Translink's own fields. `route_id` is the operator code plus the
line name, `ULB-40`. The TransXChange `ServiceCode` `2-40-_-y18-1` was rejected: it carries
an operating branch, a schedule dataset tag and a registration revision, all of which move
without the bus changing, and it splits what should be one route, since eight lines are
registered twice under different branch numbers. `shape_id` is a hash of the stop sequence
rather than the journey pattern id, because [`gtfs.py`](../wayfare/gtfs.py) collapses
patterns with `mode(shape_id)` and that has no tiebreak. `calendar.txt` ids are prefixed
`NI-`, since Translink's day patterns and the Republic's service ids are both numeric.

Run through `patterns` the province is Wales-shaped. Measured 2026-08-09 against feed
`20260806_140751`: 21,105 journeys over 687 routes collapse to 2,071 patterns in 3
seconds, a 10.2x collapse against Wales's 10.3x, because Translink runs long rural
services that repeat few times a day. Six operators (ULB 1,123 patterns, MET 479, UTS
193, GLE 154, FY 110, GDR 12) and 1,178 distinct shapes. The window reaches 53.35°N and
8.27°W, which is Dublin and Donegal, because Goldline runs across the border.

A stop at latitude zero is not a null. Translink has shipped stops at exactly `0.0`/`0.0`,
which is in the Gulf of Guinea, passes `IS NOT NULL`, and drags a pattern's span across two
continents. [`gtfs.py`](../wayfare/gtfs.py) drops a zero latitude on load and
[`translink.py`](../wayfare/translink.py) drops it before it is ever written.

## The stop gap bound

`config.MAX_STOP_GAP_M` is 180 km, derived as `VALHALLA_MAX_DISTANCE_M` (200,000) times
`VALHALLA_DISTANCE_HEADROOM` (0.9) rather than written out, so it cannot drift past the
limit Valhalla refuses a request at.

It applies to unshaped patterns only (`match.py:305`). Routing through a long leg
produces a confident-looking line down a motorway the bus may not use; following an
operator's recorded trace invents nothing, and how far apart two timing points sit says
nothing about whether that trace is good. A shaped pattern over the bound is matched
and the count is logged.

The figures the old 25 km bound produced are historic and are recorded here only so
nobody re-derives them. That bound excluded 1,555 patterns nationally, of which 180 km
admits 1,319 carrying 56,720 trips, and triage found no bad data among them — 1,299
were National Express or FlixBus, at a median 6 stops and a median longest leg of 147
km. It also skipped 4.2% of Welsh patterns, 4.7% of Northern Irish ones, and 333 of the
Republic's 2,853 (11.7%), every one of which carried a dense operator trace.

## Modes

[`gtfs.py`](../wayfare/gtfs.py) keeps a mode only if it was asked for, from the vocabulary
in `config.MODES`, and `db.matchable` is what keeps whatever was kept away from Valhalla.
Selecting a mode and map-matching it are separate decisions.

The trap is the kept set rather than the filter. Great Britain's `routes.txt` carries `3`
on 12,709 routes, `200` on 316, `4` on 119, `1` on 54, `0` on 43, `2` on 3 and `6` on 1.
`200` is the extended-GTFS code for coach, so those 316 are National Express and FlixBus,
and a filter written as `route_type = '3'` deletes them while looking right.
`config.MODES` therefore holds ranges: `3`, `11` and `800` plus `700`–`716` under `bus`,
and `200`–`209` under `coach`. `ROAD_ROUTE_TYPES` is the union of those two, derived
rather than written out, so the vocabulary and the road filter cannot drift apart. Each
remaining GTFS type is its own mode rather than grouped by guess, since a cable tram is
not a tram whatever the names suggest.

Everything dropped is logged by type with its route and trip counts, and an unrecognised
type is a warning rather than an info line, because the way this goes wrong is a future
feed publishing something road-going in a range nobody kept. A feed where every trip
drops raises instead: that means the join to `routes.txt` failed.

A NULL `mode` on `patterns` is deliberately not backfilled to `bus`. It means a database
written before modes existed, which held road patterns only because that was what the
filter left, and `db.matchable` reads NULL as matchable for that reason. Already-matched
patterns of a deselected mode are not deleted either; they become departed, keep their
`match_status` rows, and the next `aggregate` drops them from `edge_services`.

Ferries are the largest single error class in the Great Britain run, and the matcher is
not the way to draw them. All 52 `error` rows carrying Valhalla code 444 — 11,200 trips
— are two-stop sea crossings on the `shape` path, and a further 41 `low_confidence`
rows, 8,867 trips, are the same crossings arriving through the `stops` path, which is
63% of all low-confidence trip weight. Valhalla's graph does admit `route=ferry`, read
from its `lua/graph.lua` on 2026-08-10, so the earlier explanation here — that the
matcher refuses to put water on a road — does not hold.

A ferry is drawn and never matched, because matching answers the wrong question. An OSM
ferry way is a line drawn from one terminal to another, so snapping to it would replace
the operator's recorded course with a schematic. The feed's own geometry is better: 244
of the 416 Great Britain ferry patterns carry a trace, coarse but real, CalMac at a
median 8 vertices, and those are copied into `segments` and drawn as they are. The other
172 are not drawn at all.

## OpenStreetMap

Geofabrik files Greater London under `england/` rather than at the top level like the
nations, so the `london` slug fell back silently to the 2.16 GB Great Britain extract
until `config.OSM_EXTRACTS` got an entry. Anything without its own extract still falls
back to Great Britain.

OSM `route=bus` relations are not viable as a source. There are 12,968 nationally
against only 818 `route_master` relations, and Greater London alone is 13% of the total.
BODS is the authority for what services exist and OSM is the geometry substrate.

`route=subway` and `route=train` relations are viable, and the coverage argument
reverses. A Public Transport version 2 (PTv2) route relation holds the ways a service
runs over, in the order it runs them, plus its calling points as node members with
roles. Roughly 1,000 relations cover roughly 1,000 Great Britain rail services, and they
are among the best-maintained relations in the country, because a railway is a fixed and
publicly documented alignment that does not move between timetables. The Greater London
bounding box holds 556 route relations, and every one of the eleven Underground lines
has a `route_master`, as do the Docklands Light Railway (DLR, 5), London Trams (3) and
the Elizabeth line (5). Transport for London (TfL) publishes no track geometry of its
own — `api.tfl.gov.uk/Line/{id}/Route/Sequence/{dir}` returns a `lineStrings` field whose
vertex count equals the station count — so the relations are the only survey there is.

The tagging traps sit in the ways and the nodes rather than in the relations. The relation's
`route` tag is the only reliable mode handle, and it does not say what the obvious names
suggest, since the Elizabeth line is `route=train`. `ref` is on 2.4% of subway ways and
carries signalling codes rather than line names, and `line` reaches 62.1% and is
multi-valued on shared track with inconsistent separators. There is no `naptan:AtcoCode` on
any Underground stop node, so the timetable's stop identifiers do not reach OSM at all and
the join is by normalised station name with a coordinate check.
[`docs/pipeline.md`](pipeline.md) covers the stage and [`wayfare/osm.py`](../wayfare/osm.py)
the parsing.

### A relation is a poor source of track, and OSM's track is an excellent one

The stop members that make a relation a good join key for the Underground are what make it
a bad one for heavy rail. OpenStreetMap models an intercity line with the stations that
define it rather than every station on it: `Cork - Dublin` lists four, Cork Kent, Mallow,
Limerick Junction and Dublin Heuston, while the National Transport Authority's patterns are
stopping services over branches. `trace` needs a pattern's calling points to be a
subsequence of a relation's, so 269 of the Republic's 319 shaped rail patterns match none of
the 67 relations fetched. Nothing is wrong with the names — the feed writes "Cork (Kent)"
and the relation "Cork Kent", and `osm.normalise` folds both to `cork kent`.

Relations are a poor source of the track *itself* for the same reason. Measured against the
Republic's 3,000.6 km of rail shape, the ways reachable through route relations cover 78.7%
of it, with Dublin–Belfast at 7.1% and Limerick–Waterford at 3.3%: nobody has drawn a route
over them. A bare `way[railway~"rail|light_rail|subway|narrow_gauge|tram"]` query over the
same window covers **100.0% within 25 m** and costs 7.2 MB in one request. That is what
`snap` asks for, and why it keeps its own cache.

The distance distribution is what makes snapping safe rather than a threshold to tune. The
covered share is 99.5% at 5 m, 99.8% at 10 m and 100.0% at 25 m and at 50 m, so a survey
either follows the track or is somewhere else and there is no near miss to adjudicate.
`service=*` is excluded because a siding sits within metres of the running line and a shape
snaps onto one happily.

The operator's shape is not thrown away by any of this. It stays the evidence — the snap
follows it and OpenStreetMap supplies only the way id — and it stays the fallback for every
pattern refused, so a region whose track is unmapped loses the sharing and never the line.

### Three gates on what a region draws

`osmroutes` discovers route relations over a window and turns each into a pattern. It is
how Great Britain's National Rail is drawn at all, since BODS does not carry it, and it
is Northern Ireland's only source of rail. A window is a box and a border is not, so two
of the gates are about where a relation is, and the first is about whether the region
wants relations at all.

`config.Feed.route_relations` is the OSM `route` values a region draws, narrowing
`osmroutes.ROUTE_MODES`. `None` takes that default and `()` draws none. The stage exists
for a mode with no timetable behind it, and a region publishing its own gets the same
line from both sources: the Republic's rail is in the National Transport Authority feed
with shapes, and at z10 and z11 none of the relation track's ink fell outside those
shapes. The shapes win, because they are the operator's own recording and because they
carry the journey counts the relations have no way to know, so the Republic sets `()`.
It keeps its `operators` regardless — that claim is what makes Great Britain refuse the
same relations, and it outlives the Republic's own reason for naming them. An empty
selection skips the Overpass fetch and still retires the previous run, since a region
that has stopped drawing relations is the one whose last run most needs retiring.

`config.Feed.bounds` is the per-region window, `(south, west, north, east)`, intersected
with the box the region's own stops draw. Northern Ireland gets `(54.0, -8.35, 55.35,
-5.35)`, the six counties padded, and Dublin at 53.35°N falls outside it. Without those
bounds its window drew 17,549 ways of track against the Republic's own 4,156, both
archives carried the Republic's rail, and the viewer draws every archive it is offered
onto one map. Bounds that never meet a region's live stops raise, rather than querying an
empty box and reporting that nothing was discovered. The Republic gets no bounds at all,
because Donegal reaches further north than any part of Northern Ireland.

`config.Feed.operators` is the second gate, the names a region's own rail carries in an
OSM `operator` tag, read by `osmroutes.ours`. A tag naming nobody any region claims is
left to the window, which keeps every BODS slug drawing what it always drew. A tag
naming only another region's operators is refused. A tag naming both goes to whichever
name comes first inside the tag, so the Enterprise — run jointly by Iarnród Éireann and
NI Railways, with both regions' runs reading the one tag — lands in exactly one archive.
Operator names fold case and accents, because `Iarnród Éireann` is tagged both ways
across the network, and they split on `;` and on `/`.

The operator gate bites Great Britain deliberately. Its window is clipped to the British
Isles, so Iarnród Éireann's relations always reached it and 70 chained relations outside
Great Britain were being drawn into its archive. Great Britain names no operators of its
own and does not need to, since naming another region's is enough to refuse. A
continental relation names an operator no region claims, so the British Isles window is
still the only thing holding those out.

## Attribution

An archive owes the publisher always and OpenStreetMap conditionally, and both are
licence conditions rather than courtesies. The timetable is the publisher's, under
whatever licence that publisher chose: OGL v3.0 for BODS and Translink, CC BY 4.0 for the
NTA. Where a route was map-matched, the
geometry is OpenStreetMap's under the Open Database License (ODbL, carrying the
licence's own American spelling because that is its name). That second obligation is the
easy one to miss, because every matched edge is an OSM way that Valhalla matched a route
onto, so such an archive is a derived database whatever the timetable's licence says.
The viewer's basemap credit says nothing about the lines drawn on top of it, which is
why the wording names what each credit covers.

The ODbL claim is wrong for a mode that was never matched. A tram, metro or ferry drawn
from the trace in the feed involves no OSM way, and asserting share-alike over an
operator's own survey imposes a condition its publisher never chose. Track drawn from a
route relation is the opposite case: `wayfare trace` copies an OSM relation's own
geometry, so an archive of nothing but Underground track is wholly derived from
OpenStreetMap while `edge_services` is empty.

`config.credit_parts` therefore takes three flags, and `publish.contents` reads all
three off the database, because only the thing that built an archive knows what went
into it.

- `road` — whether `edge_services` has rows.
- `operator` — whether `segments` has rows. It widens the publisher's noun from "Routes
  and timetables" to "Routes, timetables and operator geometry", since the trace arrives
  in the same bundle under the same licence.
- `track` — whether anything drawn came from a route relation. `_has_traced_segments`
  short-circuits on `track_services` having rows, which is rebuilt every `aggregate`
  against the live patterns, and only then falls back to `segments JOIN traces`. The
  join matters on that arm, because `traces` is a cache that keeps its rows when a
  service leaves the timetable, and a credit has to describe the bytes being published.

`road` and `track` are independent and either alone sets the ODbL credit; together they
only widen its noun, which the `licences.openstreetmap(what=...)` factory takes as an
argument; its default is "Road geometry". An archive with no matched edges and no track
credits nobody but the publisher.

Everything else about a licence lives in [`wayfare/licences.py`](../wayfare/licences.py):
the names, the `URLS` table from each name to its URI, the frozen `Credit` dataclass of
`what`, `who`, `licence` and `who_url`, and the three renderers `html`, `lines` and `text`.
A licence with no entry in `URLS` raises a `KeyError` at publish time rather than dropping
the URI and publishing anyway. The renderers take a tuple of credits and know nothing about
where it came from, so the dependency runs one way and has to: [`wayfare/feeds.py`](../wayfare/feeds.py)
imports `licences`, never the reverse. Adding a source to `FEEDS` credits it everywhere; the
feed definitions live in `feeds` and `config` re-exports every name, so `config.FEEDS` and
`config.credit_parts` still read as they always did.

### Where the credit travels

The credit lives in the tiles, because that is the one place it travels with the data.
[`publish.py`](../wayfare/publish.py) passes `--attribution=<credit_html>` to every
tippecanoe pass, tippecanoe writes it to the tileset metadata, PMTiles carries that block
verbatim, and MapLibre reads a source's own attribution into the control with no help from
the page. An archive copied to a bucket takes its credit with it, where a sidecar file would
be left behind. Against tippecanoe 2.79.0, `tile-join` carries an input's attribution
through in either argument order; every pass is stamped regardless, since any of them can be
opened alone.

`pmtiles.Protocol` is the trap, because its `metadata` option defaults to false. Without
`{ metadata: true }` the plugin answers MapLibre's TileJSON request from the PMTiles
header alone, and the attribution never leaves the file, which looks like a viewer
crediting only its basemap rather than like an error.

A render is the case tileset metadata cannot reach, since a PNG is passed around on its own.
Every render stamps `licences.text(config.credit_parts())` into its own file, and
`licences.lines(config.credit_parts(region), links=False)` is the same credit shortened for a caption drawn
on the canvas; [`docs/rendering.md`](rendering.md) covers how. The studio page states the
credit at the download control, served from `/art/meta`, because the download is the moment
the obligation attaches to somebody.

An archive built before any of this carries no attribution and degrades quietly, since
MapLibre falls back to the basemap line alone. Bringing it up to date is a `publish` run,
with no re-match and no re-aggregate.

### The licence this project cannot express

TfL publishes under Open Government Licence v2.0 with amendments for Transport for
London, which requires three verbatim attribution strings rather than a name, a licence
and a URI. `licences.Credit` carries four fields and cannot hold that. Nothing here uses
TfL data — the Underground timetable is BODS and the geometry is OpenStreetMap — so the
note is here only so that the first change reaching for the TfL portal knows the cost
before writing any code.

The credit travels with the bytes rather than with the page that displays them. The way
to keep testing that is to copy an archive somewhere the page it was built against
cannot reach, and open it there.
