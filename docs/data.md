# Data sources

Wayfare draws three regions from three publishers, each under a different licence and each carrying a different amount of geometry. This file records the decisions those differences forced, and the reason behind each one.

## One region, one data root

`meta.feed_version` holds a single value, so a second region acquired into the first's database becomes the current feed and the next `publish` overwrites the first region's archive. Every region therefore gets its own data root. The Republic of Ireland and Northern Ireland are the one case where the Valhalla *graph* is shared, because Geofabrik's combined extract splits at the sea rather than at the border, so both halves match against one build and one GraphId space. Sharing the graph does not mean sharing the database.

## Geometry coverage

Whether a trip carries a `shape_id` decides which path the matcher takes, and it is the largest difference between the three regions. Great Britain is 48.3% of trips, the Republic of Ireland 100%, Northern Ireland 62%. Great Britain's split is strictly per-operator and all-or-nothing, tracking whose scheduling software emits TransXChange `TrackPoint`s, so an operator is either wholly covered or wholly bare. *Map matching* is the primary path.

Northern Ireland's shape is all of a journey's hops or none of them. Stitching what exists and jumping the rest hands the matcher a straight line across a town, which it lays down the wrong roads with confidence, and bad geometry is worse than missing geometry. Translink publishes the timetable and the road geometry on unrelated cadences, so the coverage figure falls on its own with every timetable release until the geometry bundle is republished.

## Great Britain: BODS

The Bus Open Data Service (BODS) sends no `Content-Length`, so a truncated download looks exactly like a complete one and the check has to be structural — a minimum byte count and `.part` staging in [`acquire.py`](../wayfare/acquire.py). BODS and OpenDataNI both answer anything that looks like a generic scraper with a block, so `config.USER_AGENT` is load-bearing.

BODS carries international coach, and its continental stops are entirely correct, so nothing that checks a coordinate for validity catches them. What they break is anything that sizes a window off stop coordinates: a plain min/max over every live stop asks Overpass for a box from Ireland to Poland, which dies with no traceback.

`config.british_isles_sql` is the boundary. Three of its four bounds are a box, and the fourth is a capped line through the Channel, because Calais is east of Dover while Brittany is west of Cornwall and no single straight line separates both. `patterns` drops the whole pattern rather than the offending stop, since dropping the stop alone leaves a London-to-Warsaw coach in the dataset as a London-to-Dover one, which every span and bounding-box check reads as domestic.

## The Republic of Ireland: the NTA

The National Transport Authority (NTA) publishes the whole timetable as one bundle, with no key and no registration, under Creative Commons Attribution 4.0 (CC BY 4.0). Its `feed_version` is a globally unique identifier (GUID), which cannot be ordered and says nothing about when it was published, so `acquire.feed_version` rewrites an opaque version as `feed_start_date` plus the first eight hex digits. The date alone will not do, because the NTA declares a year-long validity window and republishes inside it, and a version that fails to move between publications leaves withdrawn services looking live to every consumer of `patterns`. A BODS timestamp is passed through untouched, since changing it would orphan every pattern in an existing database.

The National Public Transport Access Nodes (NaPTAN) stop register covers Great Britain alone, and a region outside Great Britain must not fetch it, which is what `config.Feed.stop_register` records.

## Northern Ireland: Translink and OpenDataNI

Translink publishes the province through OpenDataNI, since BODS and NaPTAN are both Great Britain only. Translink re-uploads rather than overwrites, so the Comprehensive Knowledge Archive Network (CKAN) resource id and the filename both move on every publication, and `translink.resource` resolves each dataset through `package_show` and takes the most recently published ZIP. The dataset slugs are historical names that stopped describing their contents years ago, so do not tidy them: the one reading "metro timetable valid from 18 June until 31 August 2016" is the live Metro and Glider feed. Everything is Open Government Licence (OGL) v3.0.

Road geometry arrives separately, as a MapInfo Interchange Format (MIF) file with its companion MID attribute file, read by [`mapinfo.py`](../wayfare/mapinfo.py). The pair has no key of its own — one object per feature, one delimited attribute row per object, in order — so an unrecognised keyword shifts every later attribute row onto the wrong road and the result still draws. The parser therefore raises on any object type it does not know, and asserts the coordinate system, since Irish Grid eastings read as degrees would put Belfast in the Atlantic. `None` geometry on a line of its own is the trap.

The pattern identity is Translink's own operator code plus line name. The TransXChange `ServiceCode` was rejected because it carries an operating branch, a schedule tag and a registration revision, all of which move without the bus changing. `shape_id` is a hash of the stop sequence rather than the journey pattern id, because [`gtfs.py`](../wayfare/gtfs.py) collapses patterns with `mode(shape_id)` and that has no tiebreak.

A stop at latitude zero is not a null. Translink has shipped stops at exactly `0.0`/`0.0`, which passes `IS NOT NULL` and drags a pattern's span across two continents, so `gtfs.py` drops a zero latitude on load and [`translink.py`](../wayfare/translink.py) drops it before it is ever written.

## The stop gap bound

`config.MAX_STOP_GAP_M` is derived from the maximum distance Valhalla refuses a request at, with headroom, rather than written out, so it cannot drift past that limit. It applies to unshaped patterns only, because the reasoning is about guesswork: routing through a long leg produces a confident-looking line down a motorway the bus may not use, while an operator's recorded trace invents nothing, and how far apart two timing points sit says nothing about whether that trace is good.

## Modes

`gtfs.py` keeps a mode only if it was asked for, from the vocabulary in `config.MODES`, and `db.matchable` is what keeps whatever was kept away from Valhalla. Selecting a mode and map-matching it are separate decisions.

The trap is the kept set rather than the filter. `200` is the extended-GTFS code for coach, so a filter written as `route_type = '3'` deletes National Express and FlixBus while looking right. `config.MODES` therefore holds ranges rather than single codes, and `ROAD_ROUTE_TYPES` is derived from that vocabulary rather than written out, so the two cannot drift apart. Each remaining General Transit Feed Specification (GTFS) type is its own mode rather than grouped by guess, since a cable tram is not a tram whatever the names suggest.

An unrecognised type is a warning rather than an info line, because the way this goes wrong is a future feed publishing something road-going in a range nobody kept. A feed where every trip drops raises instead, since that means the join to `routes.txt` failed. A NULL `mode` on `patterns` is deliberately not backfilled to `bus`, because it means a database written before modes existed, which held road patterns only because that was what the filter left.

A ferry is drawn and never matched, because matching answers the wrong question. An OpenStreetMap (OSM) ferry way is a line drawn from one terminal to another, so snapping to it replaces the operator's recorded course with a schematic. The feed's own geometry is coarse but real, and it is copied into `segments` and drawn as it stands. Patterns carrying no trace are not drawn at all.

## OpenStreetMap

OSM `route=bus` relations are not viable as a source of what services exist, since there are far fewer of them than there are timetabled services. BODS is the authority for what runs, and OSM is the geometry substrate.

`route=subway` and `route=train` relations reverse the argument. A railway is a fixed and publicly documented alignment that does not move between timetables, so its *route relations* are among the best-maintained in the country. Transport for London (TfL) publishes no track geometry of its own, which leaves the relations as the only survey there is.

The tagging traps sit in the ways and the nodes rather than in the relations. The relation's `route` tag is the only reliable mode handle, and it does not say what the obvious names suggest, since the Elizabeth line is `route=train`. The way tags `ref` and `line` are not join keys: `ref` carries signalling codes rather than line names, and `line` is multi-valued on shared track with inconsistent separators. There is no `naptan:AtcoCode` on any Underground stop node, so the timetable's stop identifiers do not reach OSM at all and the join is by normalised station name with a coordinate check. [`pipeline.md`](pipeline.md) covers the stage and [`osm.py`](../wayfare/osm.py) the parsing.

### A relation is a poor source of track, and OSM's track is an excellent one

The stop members that make a relation a good join key for the Underground are what make it a bad one for heavy rail. OpenStreetMap models an intercity line with the stations that define it rather than every station on it, while the timetable's patterns are stopping services over branches, and `trace` needs a pattern's calling points to be a subsequence of a relation's.

Relations are a poor source of the track itself for the same reason. Measured against the Republic's rail shapes, the ways reachable through route relations cover 78.7% of the timetabled length, because nobody has drawn a route over the rest, while a bare `railway=*` query over the same window covers 100.0% within 25 m. That second query is what `snap` asks for.

The distance distribution is what makes snapping safe rather than a threshold to tune. The covered share is 99.5% at 5 m and 100.0% at both 25 m and 50 m, so a survey either follows the track or is somewhere else, and there is no near miss to adjudicate. `service=*` is excluded, because a siding sits within metres of the running line and a shape snaps onto one happily.

The operator's shape is not thrown away by any of this. It stays the evidence, with OpenStreetMap supplying only the way id, and it stays the fallback for every pattern refused, so a region whose track is unmapped loses the sharing and never the line.

### Three gates on what a region draws

[`osmroutes.py`](../wayfare/osmroutes.py) discovers route relations over a window and turns each into a pattern, which is how Great Britain's National Rail is drawn at all and is Northern Ireland's only source of rail. A window is a box and a border is not, so two of the three gates are about where a relation is, and the first is about whether the region wants relations at all.

`config.Feed.route_relations` is the OSM `route` values a region draws, where `None` takes the default and `()` draws none. The stage exists for a mode with no timetable behind it, and a region publishing its own timetable for that mode gets the same line from both sources. The shapes win, because they are the operator's own recording and because they carry the journey counts the relations have no way to know. An empty selection still retires the previous run, since a region that has stopped drawing relations is the one whose last run most needs retiring.

`config.Feed.bounds` is the per-region window, intersected with the box the region's own stops draw. Northern Ireland's stops reach Dublin, so without those bounds its window drew the Republic's rail into Northern Ireland's archive, and the viewer draws every archive it is offered onto one map. Bounds that never meet a region's live stops raise, rather than querying an empty box and reporting that nothing was discovered.

`config.Feed.operators` is the names a region's own rail carries in an OSM `operator` tag. A tag naming only another region's operators is refused, while a tag naming nobody any region claims is left to the window, which keeps every BODS slug drawing what it always drew. A region that draws no relations keeps its `operators` regardless, since that claim is what makes another region refuse the same relations.

## Attribution

An archive owes the publisher always and OpenStreetMap conditionally. Both are licence conditions rather than courtesies. The timetable is the publisher's, under whatever licence that publisher chose, and where a route was map-matched onto OSM ways or drawn from OSM track the geometry is OpenStreetMap's under the Open Database License (ODbL, carrying the licence's own American spelling because that is its name).

The ODbL claim is wrong for a mode drawn from the operator's own trace, which involves no OSM way, since asserting share-alike over somebody else's survey imposes a condition its publisher never chose. `config.credit_parts` therefore takes three flags, and `publish.contents` reads all three off the database, because only the thing that built an archive knows what went into it.

- `road` — whether `edge_services` has rows.
- `operator` — whether `segments` has rows. It widens the publisher's noun to name operator geometry as well as routes and timetables, since the trace arrives in the same bundle under the same licence.
- `track` — whether anything drawn came from a route relation.

Everything else about a licence lives in [`licences.py`](../wayfare/licences.py): the names, the table from each name to its URI, the frozen `Credit` dataclass and the renderers over it. A licence with no URL entry raises at publish time rather than dropping the URI and publishing anyway. The renderers take a tuple of credits and know nothing about where it came from, so the dependency runs one way and has to, with [`feeds.py`](../wayfare/feeds.py) importing `licences` and never the reverse.

The credit lives in the tiles, because that is the one place it travels with the data. [`publish.py`](../wayfare/publish.py) stamps it into every tippecanoe pass, tippecanoe writes it to the tileset metadata, and PMTiles carries that block verbatim, so an archive copied to a bucket takes its credit with it where a sidecar file would be left behind. `pmtiles.Protocol` is the trap: without `{ metadata: true }` the plugin answers MapLibre's TileJSON request from the PMTiles header alone and the attribution never leaves the file, which looks like a viewer crediting only its basemap rather than like an error. A render is the case tileset metadata cannot reach, since a PNG is passed around on its own, so every render stamps the same credit into its own file.

### The licence this project cannot express

TfL publishes under Open Government Licence v2.0 with amendments for Transport for London, which requires three verbatim attribution strings rather than a name, a licence and a URI. `licences.Credit` carries four fields and cannot hold that. Nothing here uses TfL data, so the note exists only so that the first change reaching for the TfL portal knows the cost before writing any code.

The credit travels with the bytes rather than with the page that displays them. The way to keep testing that is to copy an archive somewhere the page it was built against cannot reach, and open it there.
