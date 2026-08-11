# Data sources

What the upstream feeds actually contain, and how each one lies. Measured against
the live feeds on 2026-08-06, feed version `20260806_022608`.

## Geometry coverage

**Only 48.3% of trips carry a `shape_id`** — 748,087 of 1,549,590. This is the
single most important fact about the project. The split is strictly per-operator
and all-or-nothing, tracking whose scheduling software emits TransXChange
`TrackPoint`s: Stagecoach North East 100%, Go North East 0%, Arriva North East 0%.
So map matching is the primary path, not a fallback. Where shapes do exist they
are genuine road geometry (median 849 points, p90 2,109, max 3,705), not
stop-to-stop lines.

The Republic of Ireland inverts that figure outright — every trip in the National
Transport Authority's feed carries a `shape_id` — which makes it the sharpest
available contrast and the cheapest region this project has added. See Beyond Great
Britain below.

**Northern Ireland sits between the two, and its figure moves for a reason neither
of the others has.** 58.0% of its patterns and 62.0% of its journeys carry
geometry, against the 83.5% predicted from the previous timetable. A shape is all
of a journey's hops or none of them: stitching what exists and jumping the rest
hands `map_snap` a straight line across a town, which it lays down the wrong roads
with confidence, and bad geometry is worse than none. Individual hops are 96.1%
covered, so the loss is concentrated — 791 of 12,372 timetabled stops appear
nowhere in the geometry at all, and account for 1,408 of the 1,995 missing hops.
Falling back to the reversed polyline of the opposite hop was measured and
rejected: it exists for 23 of the 1,995, and a one-way system makes it wrong.
Translink publishes the two halves on unrelated cadences — the timetable every few
weeks, the road geometry not since 2025-09-23 — so this figure falls further until
`PtLinks` is republished.

An operator switching on `TrackPoint`s is an opt-in re-match. A pattern matched
from bare stops that later gains a `shape_id` is worth redoing — it turns a guess
into an observation — but it adds work to a queue meant to be predictable. The
count is always logged; `wayfare patterns --upgrade-shapes` is what acts on it.

## BODS

**BODS sends no `Content-Length`.** A truncated download looks exactly like a
complete one. Hence `MIN_GTFS_BYTES` and the `.part` staging in `acquire.py`.

**BODS blocks requests that look like generic scrapers.** A real User-Agent is
required; see `config.USER_AGENT`.

**Regional slugs** (`config.BODS_GTFS_URL`): `all`, `england`, `scotland`,
`wales`, `north_east`, `north_west`, `yorkshire`, `east_midlands`,
`west_midlands`, `east_anglia`, `london`, `south_east`, `south_west`. Use `wales`
(41 MB) for development. `ireland` is a slug too and is not one of these: it resolves
through `config.FEEDS` to the National Transport Authority instead, and skips NaPTAN.

**Sizes.** National GTFS: 1.28 GB zipped, 7.84 GB unpacked, `stop_times.txt` 5.09
GB, `shapes.txt` 2.53 GB, 1.55M trips. OSM Great Britain: 2.16 GB. NaPTAN CSV: 102
MB, 435k records. Budget ~40 GB of disk including the Valhalla graph.

## Mode filtering

**Ferries are the largest single error class in the GB run, and the matcher is not
the way to draw them.** All 52 `error` rows carrying Valhalla code 444 — 11,200
trips — are two-stop sea crossings on the `shape` path: CalMac, Orkney, the Shields
ferry. A further 41 `low_confidence` rows, 8,867 trips, are the same crossings
arriving through the `stops` path, and they are 63% of all low-confidence trip
weight.

**The reason given here used to be wrong, and the wrong reason is worth recording.**
This file said `map_snap` was "refusing to put water on a road". It is not, and
Valhalla's own source says so. `lua/graph.lua` admits exactly two non-`highway`
route types — `route=ferry` and `route=shuttle_train` — so a ferry way *is* in the
graph with a `way_id`. `class BusCost : public AutoCost` overrides only `Allowed`
and `AllowedReverse`, neither of which rejects a ferry, and not `EdgeCost`, which
handles `Use::kFerry` through `ferry_factor_`. So `bus` costing traverses ferries by
inheritance. Error 444 is Meili failing to snap the trace to *any* edge — most
likely because the supplied shape lies outside the search radius of the OSM ferry
way, or because no such way exists for that crossing. Checked against `master` on
2026-08-10. The conclusion held while the reasoning did not, which is the kind of
entry that survives longest unexamined.

**The decision, on the corrected reasoning: a ferry is drawn, never matched.** Not
because Valhalla refuses, but because matching answers the wrong question. An OSM
ferry way is, by the wiki's own description, a line drawn from one terminal to
another — snapping to it would replace the operator's recorded course with a
schematic, and it would put the crossing on shared "infrastructure" that two
operators may or may not have in common. The feed's own geometry is better: 244 of
the 416 GB ferry patterns carry a trace, coarse but real, CalMac at a median 8
vertices. Those are copied into `segments` and drawn as they are. The other 172 are
not drawn at all, because a straight line between two ports is a schematic this
pipeline would be inventing rather than repeating.

`gtfs.py` keeps a mode only if it was asked for — see `config.MODES` — and
`db.matchable` is what keeps whatever it kept away from Valhalla. Selecting a mode
and map-matching it are separate decisions, and for ferries the answers differ.

**The trap is the kept set, not the filter.** GB `routes.txt` carries `3` on
12,709 routes, `200` on 316, `4` on 119, `1` on 54, `0` on 43, `2` on 3 and `6` on
1. `200` is the extended-GTFS code for coach, so those 316 are National Express and
FlixBus — real long-distance road services, and already the bulk of the
long-distance `skipped` patterns. A filter written as `route_type = '3'` deletes
them and looks right. `config.MODES` therefore holds ranges rather than values:
`3`, `11` and `800` plus `700`–`716` under `bus`, and `200`–`209` under `coach`.
`ROAD_ROUTE_TYPES` is the union of the two, derived rather than written out, so the
vocabulary and the road filter cannot drift apart. Nothing speculative is added
beyond what a feed publishes — a mode nobody publishes is a line that cannot be
checked against one — and each remaining GTFS type is its own mode rather than
grouped by guess: a cable tram is not a tram, whatever the names suggest.

Everything dropped is logged by type with its route and trip counts, and a type
this codebase does not name is a warning rather than an info line, because the way
this goes wrong is a future feed publishing something road-going in a range nobody
kept. A feed where *every* trip drops raises instead: that means the join to
`routes.txt` failed, not that the timetable is all water.

`routes` gained a `route_type` column and `patterns` gained a `mode`; both
migrations add the column empty, since nothing already stored can supply it, and the
next `patterns` run fills it in. A NULL `mode` is deliberately *not* backfilled to
`bus`: it means a database written before modes existed, which held road patterns
only because that was what the filter left, and `db.matchable` reads NULL as
matchable for exactly that reason. Backfilling would assert a mode the feed never
recorded; leaving it empty keeps a national match run going across the upgrade.

Already-matched ferry patterns are not deleted — they stop appearing in the current
feed and become departed, keeping their `match_status` rows exactly as a withdrawn
bus route does. So an existing database keeps its ferry edges until the next
`aggregate`, which joins on `db.current_feed()` and drops them from
`edge_services`.

## OSM

**Geofabrik files Greater London under `england/`**, not at the top level like the
nations. The `london` slug therefore fell back silently to the 2.16 GB Great
Britain extract until `config.OSM_EXTRACTS` got an entry.

**One Geofabrik extract covers the whole island of Ireland.**
`ireland-and-northern-ireland-latest.osm.pbf` is 409 MB and splits at the sea
rather than at the border, so the Republic and Northern Ireland match against one
graph build and therefore one GraphId space. Nothing forces the two feeds into one
database and nothing prevents it: the island can share a data root.

**OSM `route=bus` relations are not viable as a source.** 12,968 nationally, only
818 `route_master` relations, and Greater London alone is 13% of the total. BODS is
the authority for what services exist; OSM is only the geometry substrate.

**OSM `route=subway` and `route=train` relations *are* viable, and the coverage
argument above reverses.** A Public Transport version 2 (PTv2) route relation holds
the ways a service runs over, in the order it runs them, plus its calling points as
node members with roles. Buses fail on population, at 12,968 relations and 818 route
masters against tens of thousands of registered services. Rail inverts every term of
that. Roughly 1,000 relations cover roughly 1,000 GB rail services, and they are
among the best-maintained relations in the country, because a railway is a fixed and
publicly documented alignment that does not move between timetables. The Greater
London bounding box holds 556 route relations, and every one of the eleven
Underground lines has a `route_master`, as do the Docklands Light Railway (DLR, 5),
London Trams (3) and the Elizabeth line (5).

**The tagging traps sit in the ways and the nodes rather than in the relations.**
The relation's `route` tag is the only reliable mode handle, and it does not say what
the obvious names suggest — the Elizabeth line is `route=train`. Way tags do not help
either. `ref` is on 2.4% of subway ways and carries signalling codes rather than line
names, and `line` reaches 62.1% and is multi-valued on shared track with inconsistent
separators. On the stop side there is no `naptan:AtcoCode` on any Underground stop
node, so the timetable's stop identifiers do not reach OSM at all and the join is by
normalised station name with a coordinate check. `docs/pipeline.md` covers the stage
that does it and `wayfare/osm.py` the parsing.

**Transport for London (TfL) publishes no track geometry, so the relations are the
only survey there is.** `api.tfl.gov.uk/Line/{id}/Route/Sequence/{dir}` returns a
`lineStrings` field whose vertex count equals the station count, which is a straight
line between consecutive stations rather than an alignment. The only geographic asset
on the portal is a station point layer. The operator's own open data therefore cannot
draw the Underground, and OSM is the first source rather than the fallback.

## Beyond Great Britain

**Northern Ireland has no GTFS, and since 2026-08-06 it has no ATCO.CIF either.**
BODS and NaPTAN are both GB-only, so Translink publishes the province itself
through OpenDataNI. Both timetable datasets now carry TransXChange 2.4, the same
format BODS is built from, and that deleted the plan this file used to record: a
third-party ATCO.CIF converter, and a six-figure Irish Grid reprojection to get
stop positions out of `QB` records. TransXChange `StopPoint`s carry World Geodetic
System 1984 (WGS84) longitude and latitude directly, so there is no conversion, no
`pyproj`, and no converter anywhere near the pattern identity — where a version
bump would have invalidated the whole match cache silently. Re-check the format
before trusting any figure in this section; it moved once with no announcement.

**The source is four OpenDataNI datasets, and their slugs are the only stable
handle.** Two TransXChange timetables (Ulsterbus and Goldline, 69.6 MB over two
files; Metro and Glider, 104.6 MB) and two MapInfo route bundles. Translink
re-uploads rather than overwrites, so the CKAN resource id and the filename both
move on every publication — the timetable was `ulb-gle-16042026.zip` in April and
`ulsterbus-and-goldline-until-31st-august-26.zip` in August. `translink.resource`
therefore resolves each dataset through CKAN `package_show` and takes the most
recently published ZIP, which is also what separates the current Metro routes from
the 2022 Glider ones filed in the same dataset. The slugs are historical names that
stopped describing their contents years ago — the one that says "metro timetable
valid from 18 June until 31 August 2016" is the live Metro and Glider feed — so do
not tidy them. Like BODS, OpenDataNI answers a generic scraper with a 403, so
`config.USER_AGENT` is load-bearing here too. Everything is OGL v3.0.

**Road geometry comes separately, and `None` is a real object type.** The `PtLinks`
bundle is MapInfo Interchange Format (MIF) with its companion MID attribute file:
37,913 stop-to-stop polylines, read by `wayfare/mapinfo.py` in about 155 lines with
no new dependency. The `.MIF` holds one object per feature and the `.MID` one
delimited row per object, in order, with no key of its own, so an unrecognised
keyword does not degrade — it shifts every later attribute row onto the wrong road
and the result still draws. The parser therefore raises on any object type it does
not know, and checks that both files run out together. `None` is the trap: an
attribute row with no geometry, on a line of its own, that looks like nothing.
Translink writes 152 of them among the 37,913. `CoordSys Earth Projection 1` is
asserted for the same class of reason — Irish Grid eastings read as degrees are
still numbers, and would put Belfast in the Atlantic without raising.

**`StoppingPoints.GlobalId` is the NaPTAN ATCO code, and it is the whole join.**
The timetable knows a stop only by ATCO code and the geometry only by a numeric
triple; that column is the one place the two meet. It checks out: of 14,754 hops
whose polyline could be tested against both stops, 14,747 start nearer the
from-stop than the to-stop, at a median 6 m and a p90 of 13 m from it. A hop
usually has several polylines, one per line and branch that runs it, and the kept
one is the *shortest* rather than the first, because a pick made from file order
changes every shape when Translink re-exports.

**The pattern identity is Translink's own fields.** `route_id` is the operator code
plus the line name, `ULB-40`. The obvious choice, the TransXChange `ServiceCode`
`2-40-_-y18-1`, carries an operating branch, a schedule dataset tag and a
registration revision, and all three move without the bus changing. It also splits
what should be one route: eight lines are registered twice under different branch
numbers, and `ULB-40` merges them where the `ServiceCode` would not. Stop ids are
the ATCO codes verbatim and direction is TransXChange's own, so nothing in the
identity is invented here. `shape_id` is a hash of the stop sequence rather than
the journey pattern id, because `gtfs.py` collapses patterns with `mode(shape_id)`
and that has no tiebreak — the tie is removed rather than resolved. `calendar.txt`
ids are prefixed `NI-`, since Translink's day patterns and the Republic's service
ids are both numeric and collide.

**Run through `patterns`, the province is Wales-shaped.** Measured 2026-08-09
against feed `20260806_140751`: 21,105 journeys over 687 routes collapse to
**2,071 patterns in 3 seconds**, a **10.2x** collapse — Wales's 10.3x rather than
the Republic's 43.4x or London's 102x, because Translink runs long rural services
that repeat few times a day. Earlier research predicted 2,348 patterns from a trip
base of 18,975; the trip base was wrong, so treat the collapse ratio rather than
the count as the thing that carried over. Six
operators (ULB 1,123 patterns, MET 479, UTS 193, GLE 154, FY 110, GDR 12), 1,178
distinct shapes, and 4.7% of patterns over `MAX_STOP_GAP_M` — Wales's 4.2%, not the
Republic's 11.7%. The window reaches 53.35°N and 8.27°W, which is Dublin and
Donegal: Goldline runs across the border and those stops are in the feed. The
assembled bundle is byte-identical between two builds of one publication,
timestamps included, because this is the one feed here whose bytes are ours to
make comparable.

**A stop at latitude zero is not a null, and the old guard missed it.** Translink
has shipped stops at exactly `0.0`/`0.0`, which is in the Gulf of Guinea, passes
`IS NOT NULL`, and drags a pattern's span across two continents. `gtfs.py` now
drops a zero latitude on load, and `translink.py` drops it before it is ever
written — belt and braces, because the two guards catch it at different costs.

**The Republic of Ireland inverts the fact at the top of this file.** The National
Transport Authority's Transport for Ireland feed
(`https://www.transportforireland.ie/transitData/Data/GTFS_All.zip`) carries a
`shape_id` on all 129,405 of its trips, across all 108 agencies, with none of the
per-operator split that holds GB at 48.3%. Median shape is 992 points against GB's
849. The licence is CC BY 4.0 rather than the Open Government Licence (OGL), so
attribution is a condition rather than a courtesy. It also carries 2,847 Irish Rail
and 2,655 LUAS tram trips over 371 patterns, which is the other half of why the
mode filter above exists.

**Run through `patterns`, the Republic is the cheapest region this project has
added.** The region slug is `ireland` and it is not a BODS slug: `config.FEEDS` holds
the exceptions and `config.feed` builds every BODS region on demand, so the Republic
is the only entry. Measured 2026-08-09 against feed `20260808_b375dfac`: 108 MB
zipped, 492 MB unpacked, 4,421,418 stop times, and the mode filter drops 2,847 rail
trips over 19 routes and 2,655 tram trips over 2 routes before anything else runs.
123,903 road-going trips collapse to **2,853 patterns in 2 seconds** — a **43.4x**
collapse against Wales's 10.3x and London's 102x — over 14,027 stops, 106 agencies and
759 distinct service numbers. Every one of those 2,853 patterns carries operator
geometry, so the whole region is the `shape` path at one Valhalla call each and the
`stops` path is never reached. 2,852 distinct shapes serve 2,853 patterns, so exactly
one shape is shared.

**NaPTAN is GB-only and a region outside GB must not fetch it.** `config.Feed` carries
`stop_register` for that reason, along with the licence, the credit and whether the
host resumes. The NTA host is better behaved than BODS on every count measured: it
sends a `Content-Length`, answers a Range request with a 206, and returns a real 404
on a bad path rather than an HTTP 200 error page. `_stream` therefore checks the bytes
written against the declared length — skipped when the body arrived `Content-Encoding:
gzip`, because requests decodes it and the decoded size is not the declared one. That
check is a retryable `OSError` and not an `Invalid`: a short read hands back different
bytes next time.

**The NTA's `feed_version` is a GUID, and the incremental design keys on feed
version.** `B375DFAC-C156-4A9E-A642-8DF76AAA2A51` cannot be ordered against another
one and says nothing about when it was published. So `acquire.feed_version` rewrites
an opaque version as `feed_start_date` plus the first eight hex digits —
`20260808_b375dfac` — which reads like the BODS timestamp it sits beside. The date
alone would not do: the NTA declares a year-long validity window (20260808 to
20270808) and republishes inside it, so two feeds would collide on the start date.
Distinctness is the half that matters, because every consumer of `patterns` filters on
`last_seen`, and a version that fails to move between publications leaves withdrawn
services looking live and reports a quiet month that never happened. The version is
rewritten only where it is opaque; a BODS timestamp is passed through untouched, since
changing it would orphan every pattern in an existing database.

**11.7% of Irish patterns exceed `MAX_STOP_GAP_M`, and they all have real geometry.**
333 of 2,853, carrying 8,395 of 148,255 weekly trips (5.7%), have a consecutive stop
gap over 25 km — against Wales, where the same bound skipped 4.2% of patterns.
`match_one` applies that bound before choosing a strategy, so those 333 would be
skipped despite the operator having supplied a dense trace of the road the coach
actually takes. The bound exists because *routing* through a 40 km gap invents
plausible-but-wrong roads; a supplied shape invents nothing. Decide this before the
Republic is matched, not after — see PLAN.md.

## Attribution

**An archive owes the publisher always and OpenStreetMap conditionally.** The timetable
is the publisher's, under whatever licence that publisher chose: OGL v3.0 for BODS and
Translink, CC BY 4.0 for the NTA. Where a route was map-matched, the geometry is
OpenStreetMap's under the Open Database License (ODbL, carrying the licence's own
American spelling because that is its name). That second obligation is the one that is
easy to miss: every matched edge is an OSM way that Valhalla matched a route onto, so
such an archive is a derived database whatever the timetable's licence says, and that
holds for an OGL region as much as for the Irish one. The viewer's pre-existing
OpenStreetMap line credits the *basemap tiles* and says nothing about the lines drawn
on top of them, so the wording names what each credit covers.

**The ODbL claim is wrong for a mode that was never matched, and wrong in the direction
that is harder to notice.** A tram, metro or ferry is drawn from the trace in the feed:
no OSM way is involved, and asserting share-alike over an operator's own survey imposes
a condition its publisher never chose. So `credit_parts` takes `road=` and `operator=`,
and `publish.contents` reads both off the database — `road` from whether `edge_services`
has rows, `operator` from whether `segments` does — because only the thing that built an
archive knows what went into it. A region with no matched edges credits nobody but the
publisher.

**"Bus routes" was the other half of the same mistake.** It was accurate while a bus was
all there was, and an archive holding trams and ferries credited as bus routes
misdescribes its own contents. The noun is now "Routes and timetables", widening to
"Routes, timetables and operator geometry" when a non-road trace is present — the trace
arrives in the same bundle under the same licence, so it is named inside the publisher's
credit rather than given a third line of its own.

**The licences live in `wayfare/licences.py`, and nothing else does.** The names, the
`URLS` table from each name to its URI, `OSM_COPYRIGHT`, the frozen `Credit` dataclass
of `what`, `who`, `licence` and `who_url`, the one `OPENSTREETMAP` credit that is the
same for every region, and the three renderers — `html`, `lines` and `text`. The list
only grows: Transport for London publishes under an amended Open Government Licence
v2.0 with three attribution strings of its own, and Network Rail's feeds are not open
at all, so this stopped being something to keep beside paths and tunables. A licence
with no entry in `URLS` raises a `KeyError` at publish time rather than dropping the
URI and publishing anyway.

**The credit is still derived from `config.Feed` rather than written out per region.**
`config.credit_parts(region)` is the one part of crediting that belongs in `config`,
because it needs the feed and a feed is configuration — what a licence is called and
how a credit is written are not. `credit_html`, `credit_lines` and `credit_text` are
thin wrappers that hand its result to the matching renderer. Adding a source to
`FEEDS` credits it.

The dependency runs one way and has to: `config` imports `licences` for the two names
it declares feeds with, and `licences` imports nothing of ours. Putting `credit_parts`
in `licences` would have needed `Feed`, and `Feed` needs the licence names.

**The credit lives in the tiles because that is the one place it travels with the
data.** `publish.py` passes `--attribution=<credit_html>` to both tippecanoe passes,
tippecanoe writes it to the tileset metadata, PMTiles carries that block verbatim, and
MapLibre reads a source's own attribution into the control with no help from the page.
An archive copied to an R2 bucket — which `web/index.html` supports through `?tiles=` —
takes its credit with it, where a sidecar file or a field in `/archives.json` would be
left behind. It is also what makes the credit per-archive: the viewer serves several
regions from one page and switches between them at runtime, so a single hardcoded line
would be wrong for whichever region is not showing. `build_tiles` gained an
`attribution` parameter, and `publish.build(con, region)` and `wayfare publish --region`
pass it through.

**`tile-join` carries an input's attribution through, measured rather than assumed.**
Against tippecanoe 2.79.0 the joined archive keeps it, including where only one of the
two inputs has one, and in either argument order. Both passes are stamped regardless,
since either archive can be opened on its own.

**The trap was `pmtiles.Protocol`, whose `metadata` option defaults to false.** Without
`{ metadata: true }` the plugin answers MapLibre's *TileJSON* request from the PMTiles
header alone — bounds and zoom range — and the attribution written into the metadata
block never leaves the file. Both pages now construct the protocol with it. This failure
looks like a viewer crediting only its basemap rather than like an error, which is how
it was found: the credit was in the archive and absent from the control. The basemap
line itself was the same string typed into `web/index.html` and `web/art.html` character
for character, and now lives once in `web/credits.js` as `BASEMAP_CREDIT`; the data
credit is deliberately not there, because it comes from the archive.

**An archive built before this change carries no attribution, and degrades quietly
rather than blankly.** MapLibre ignores an absent attribution, so the control falls back
to the basemap line alone. Bringing an old archive up to date is the `publish` stage
only — no re-match, no re-aggregate.

**The tiles were the first half of it and a render was the visible other half.** The
art page credited the data on screen and then emitted images that carried none of it,
which is the case the tileset metadata cannot reach: an archive is read through a viewer
that shows the control, and a PNG is passed around on its own. A render now stamps
`config.credit_text()` into its own file, and `config.credit_lines(region, links=False)`
is the same credit shortened for a caption drawn on the canvas, one line per part. Both
are built from `credit_parts` like the other two renderers, so adding a source to
`FEEDS` still credits it everywhere and there is still no second hardcoded string. How a
PNG chunk and an SVG `<metadata>` block are assembled is in docs/rendering.md; what
matters here is what is owed and where it now travels.

**The studio page states the credit at the download control**, not only beside the
toggle that turns the caption on. The download is the moment the obligation attaches to
somebody, and a flag nobody knows about does not discharge it. The line is served in
`/art/meta` rather than written into the page, so it follows whichever region the
server's database holds — the same reason the tiles carry theirs in the archive rather
than in `web/index.html`.

**Track drawn from a route relation owes ODbL, and it owes it with no matched edge
anywhere in the archive.** `wayfare trace` copies an OSM relation's own geometry, so
an archive of nothing but Underground track is wholly derived from OpenStreetMap
while `edge_services` is empty. `publish.contents` therefore returns a third flag,
`track`, and `credit_parts` widens the ODbL noun to "Road geometry", "Track geometry"
or "Road and track geometry" from the two flags together. The flag is computed from
`segments JOIN traces` rather than from `traces` alone, because `traces` is a cache
that keeps its rows when a service leaves the timetable, and a credit has to describe
the bytes being published rather than the database they came out of.

**Transport for London's licence is the one `licences.Credit` cannot express, and
nothing here uses it.** Everything this project ingests is OGL v3.0 or CC BY 4.0, and
both fit the four fields `Credit` carries. TfL publishes under Open Government Licence
v2.0 *with amendments for Transport for London*, which requires three verbatim
attribution strings rather than a name, a licence and a URI. The Underground work
touches no TfL data at all: the timetable is BODS and the geometry is OpenStreetMap.
The note is here so that the first change reaching for the TfL portal knows what it
costs before it writes any code.

The credit travels with the bytes. That is the whole design, and the reason to keep
testing it by copying an archive somewhere the page it was built against cannot reach,
rather than by looking at the control on the machine that produced it.
