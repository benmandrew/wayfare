# PLAN

Numbers live in `docs/`; this file is what is done and what is next.

## Done

**Scaffold, end to end.** `acquire` (`.part` staging, archive validation, Range
resumption), `patterns` (GTFS to distinct ordered stop sequences in DuckDB), `match`
(Valhalla, two strategies, interruption-safe), `aggregate`, `publish` (GeoJSONL ->
tippecanoe -> PMTiles), `art` (three styles, PNG and SVG), the MapLibre viewer, and
Docker Compose for `valhalla`, `wayfare` and `matcher`.

**Wales, end to end** (2026-08-06). 95.6% of timetabled trips represented at 3.6/s.
Four defects the mini fixture could not reach: the GTFS size floor, the missing
`confidence_score`, the redundant OSM download, and retrying files that were complete
but invalid.

**Data representation** (2026-08-06). Tile coalescing, 169,857 directed edges ->
53,013 features, lossless. Archive 23.8 -> 9.5 MB and no features dropped at any
zoom. `refs` cap 12 -> 64 and the overflow sidecar deleted. Geometry as micro-degree
integer lists plus bbox columns. `patterns` partitioned on `hash(trip_id)` to get
round DuckDB's inability to spill an ordered list aggregate.

**Streaming and determinism** (2026-08-06). `art` streams its window rather than
materialising it; `publish.export_geojsonl` streams by `way_id`. All three art styles
became byte-identical run to run, which none of them were.

**Incremental rebuild** (2026-08-07). `pattern_id` is an identity hash rather than a
popularity rank recomputed every run; `match_status` is a permanent cache;
`--max-seconds` spreads matching over nightly runs; `match.pin_graph` refuses a
database matched against a different Valhalla tileset; old databases migrate in
place. A monthly refresh now costs only the patterns that are new.

**Greater London** (2026-08-07). 480,412 trips -> 4,709 patterns, a 102x collapse.
Only 0.9% carry operator geometry, so it is almost entirely the `stops` path, at
1.0/s — the honest cost of `stops` at London road density, and the basis GB was
extrapolated from.

**Great Britain** (2026-08-07). 52,554 patterns, 95.9% matched, 2,746,261 edges, 130
MB PMTiles.

**Rendering on the server** (2026-08-08). `GET /art` renders a window with its knobs
as query parameters; `GET /art/meta` reports styles, presets, defaults and limits, so
the UI is built from the server rather than compiled against it. `web/art.html` is a
studio page with the whole parameter set in the URL hash. Serving moved into the
package as `wayfare serve`. Bounded: one render at a time, 64 megapixels, a queue
limit, and a read-only handle held only for the length of one render.

**Data-side customisation** (2026-08-08). `art.QuerySpec` — weight, group, order and
filters as a closed vocabulary — so a style says how an edge is painted and the spec
says which edges there are. Defaults reproduce the previous output byte for byte. Two
bugs found on the way: `min_trips` reached the flat query but not the grouped one, so
`density` and `strands` drew different networks from one spec; and `strands` to SVG
was never deterministic, because the edges within a ribbon had no tiebreak and SCREEN
compositing hid it from every PNG comparison.

**Parallel rendering** (2026-08-09), measured against the real national database. A
render is drawn in horizontal bands, one process each, about three times faster over
`uk` and byte-identical. Bands cut on edge count rather than height; `Source.groups`
carries the window's group statistics into every band; `default_workers` reads the
cgroup quota and counts physical cores. Three bugs found on the way: `fork` with an
open DuckDB handle kills the child with no traceback; clipping to the band splits
strokes at a raster boundary and cairo's fixed-point tessellation does not re-add
exactly; and a band trusting `config.DB_PATH` would draw from a different database
than the connection it was given.

**Coalescing runs of edges into one stroke** (2026-08-09), as `RenderOpts.coalesce`,
off by default. London `density` at 4,000px 5.05s -> 2.54s. It is deliberately not
the default because it is not picture-preserving for `density` — see
`docs/rendering.md`.

**Republic of Ireland, acquire and patterns** (2026-08-09), feed
`20260808_b375dfac`. The first source in this project that is not BODS: `ireland` is a
region slug resolving through `config.FEEDS` to the National Transport Authority's
108 MB bundle, and `config.Feed` carries the URL, the licence, the credit, whether the
host resumes, and whether NaPTAN applies — it does not, since NaPTAN is the GB stop
register. 123,903 road-going trips collapse to 2,853 patterns in 2 seconds, a 43.4x
collapse, and 100% carry operator geometry against GB's 48.3%. The GUID feed version
is rewritten rather than worked around, and a declared `Content-Length` is now checked
because every host except BODS sends one. Numbers in `docs/data.md`. Everything
downstream of `patterns` ran later the same day, in the entry below.

**The `stops` strategy validated against the `shape` strategy, and `break_through`
fixed** (2026-08-09), against the Wales data root and its own Valhalla. A census
rather than a sample: all 3,052 Wales patterns that carry operator TrackPoints and
matched ok were matched both ways in one run, 84 s a pass. Pooled length recall 0.951,
pooled precision 0.892 — the synthesised route under-recovers by 4.4% and over-draws
by 7.4%. That found `break_through` forbidding the U-turn an out-and-back spur needs,
so `_location_types` relaxes the stops between a stop and its return; phantom road
falls 9,309 km -> 8,844 km for 21 km of real road. Numbers in `docs/pipeline.md`. Two
of the named tail cases turned out not to be this bug: patterns 1790 and 1346 route
95.9 km and 26.2 km because their stops fall outside the Wales extract, and both stay
rejected, which is the right answer.

**Northern Ireland, acquire and patterns** (2026-08-09), feed `20260806_140751`.
The first region this project assembles rather than downloads: `northern_ireland`
resolves through `config.FEEDS` to four OpenDataNI datasets, found by CKAN
`package_show` because resource ids and filenames rotate on every publication.
`config.Feed` gained `parts`, and `acquire.assemble` builds a GTFS bundle from the
TransXChange timetables and the MapInfo road geometry into `WORK`, skipping the
rebuild while the parts stand. `wayfare/translink.py` is the CKAN resolution, the
TransXChange reader and the GTFS writer; `wayfare/mapinfo.py` is the MIF/MID
parser. Translink dropped ATCO.CIF for TransXChange 2.4 on 2026-08-06, three days
before this was built, which removed the converter and the reprojection this was
planned around. 21,105 journeys collapse to 2,071 patterns in 3 seconds, a 10.2x
collapse, and shape coverage came in well under prediction for a reason nothing
here can fix — numbers in `docs/data.md`. 554 -> 585 tests, none of them needing
the real download. Everything downstream of `patterns` ran later the same day, in the
entry below.

**Both parts of Ireland, end to end** (2026-08-09), against one shared Valhalla graph
`3.8.3/1786309727`, built from `europe/ireland-and-northern-ireland-latest.osm.pbf` (409
MB) and living at `/home/ben/wayfare-ireland/valhalla`. The Republic, feed
`20260808_b375dfac` in `/home/ben/wayfare-ireland/data`: 2,853 patterns, 2,721 ok (95.4%),
132 `no_route`, 0 skipped, 0 pending; 97.2% of trips, 352,945 edges, 737 services, 16.4 MB
PMTiles; 18.4 patterns/s with 2 workers, 2m35s; mean 558.8 edges per matched pattern,
detour ratio 1.18. Northern Ireland, feed `20260806_140751` in `/home/ben/wayfare-ni/data`:
2,071 patterns, 2,061 ok (99.5%), 10 `no_route`, 0 skipped, 0 pending; 99.7% of trips,
121,384 edges, 638 services, 6.1 MB PMTiles; 18.1 patterns/s with 2 workers, under two
minutes; the 121,384 edges coalesced to 32,041 features, 45 of them over the 64-service
cap, and no tile hit the size limit at any zoom. Both runs took minutes, not hours. One
data root per region rather than the shared one item 1 of the old Northern Ireland section
left open: the extract and therefore the GraphId space is shared, so a shared root was
possible, but `meta.feed_version` is single-valued (`wayfare/db.py:184`), so acquiring the
second region into the first's database would make it the current feed and the next
`publish` would overwrite the first region's archive. Two predictions in this file did not
hold. Northern Ireland was expected to land between Wales's 3.6/s and London's 1.0/s
because it is 58% `shape` and 42% `stops`; it ran at 18.1/s, indistinguishable from the
Republic's 18.4/s at 100% `shape`, and why the mix made no difference is unexplained and
unmeasured. And `MAX_STOP_GAP_M` did not need deciding for the Republic ahead of matching:
the bound is 180 km and is not applied to a pattern carrying an operator trace, so none of
the 333 patterns (11.7%) with a stop gap over 25 km was skipped, and the run logged runs of
"N of 200 patterns carry operator geometry across a leg over 180 km" throughout. The viewer
now serves three regions from `/home/samba/sambashare/wayfare/out`: `great_britain.pmtiles`
(130.2 MB), `ireland.pmtiles` (16.4 MB) and `northern_ireland.pmtiles` (6.1 MB). Great
Britain's archive was renamed from `bus.pmtiles` because `web/index.html:358` builds each
dropdown label from the filename; the default view is unchanged, because
`web/index.html:315` takes `ARCHIVES[0]` and `great_britain` still sorts first.

## Done — the three served archives carry their credit

**Republished on 2026-08-10, all three, against image `sha256:ace6d450`.** The attribution
code (`93623bc`) had landed 38 minutes after the image that published the earlier tiles was
built, so Great Britain and Northern Ireland were being served with no credit at all, which
the Republic's CC BY 4.0 and Translink's Open Government Licence v3.0 both make a breach.
Ireland had already picked the credit up in its 9 August republish. `config.credit_html`
was right for all three regions throughout; nothing needed fixing before republishing, and
nothing was re-matched.

    # from /home/ben/wayfare-build, /home/ben/wayfare-ireland
    wayfare publish --region all --name-by-region              # great_britain.pmtiles
    wayfare publish --region ireland --out /served/ireland.pmtiles

Northern Ireland's data root has no `wayfare.duckdb`, only the `edges.geojsonl` its last
publish exported, so it took `--from-export` — tiles built from a GeoJSONL already on disk,
opening no database. The export is deterministic, so those are the tiles the missing
database would have produced.

    wayfare publish --region northern_ireland --from-export --out /served/northern_ireland.pmtiles

The same republish carried the `far` band, and it took four attempts and a revert.
A single national `trips` floor emptied 310 of Great Britain's 655 populated cells. A
per-cell floor sharing the cap out in proportion to cell size emptied none and drew 15
features per rural cell at z6 where Ireland drew 53. Weighting it by the square root of
cell size restored the countryside and flattened the cities. Moving to a 0.02-degree grid
took coverage of 1.4 km bins from 37.8% to 88.9%.

None of them helped. Rasterising the tile geometry and counting lit pixels — what a
reader actually sees — shows every cap losing ink at every zoom in every window, and at
z8 around London the capped archive lit 5.0% against 8.2% uncapped, with the city
hollowed to a radial skeleton. Great Britain was republished on 2026-08-11 with the
overview uncapped: 130.4 MB, 6.3% lit nationally at z6 against 4.8% capped. Uncapped
already exceeds Ireland's density at z6 and z7, and the deficit is z5 alone, 3.8% against
Dublin's 4.7%.

The counting mistake is the lesson. A cap keeps many short features spread over many
cells; no cap keeps fewer, longer ones. Feature counts, populated cells and bins holding
anything all reward the first, and only the second reaches the screen, so four rounds
were judged on numbers that rose while the map got worse.

Both regions also carry their non-road modes now, drawn from operator geometry: 629
patterns for Great Britain and 371 for Ireland. Great Britain's had been matched onto
roads before the mode filter existed, 1,726,822 `pattern_edges` for the Underground
alone and 16,833 edges reachable from no bus, because `aggregate` filtered on the live
feed and never on `matchable`. Northern Ireland has no non-bus routes and was not
touched. The replaced archives are at `work/previous-great_britain.pmtiles` and
`/home/ben/archive-backup-20260810/` on the server.

## Done — the Underground and the DLR drawn from OSM

**`wayfare trace`, a sixth stage between `match` and `aggregate`** (2026-08-11). Great
Britain's `route_type=1` is 54 routes, 61,288 trips and 1,525 patterns, and 1,417 of
those patterns (92.9%) carry no `shape_id`; `route_type=2` is three routes, all of them
the Docklands Light Railway (DLR), 71 patterns, none with a shape. Seventeen named
lines arrive with a full stop sequence and no geometry — the Underground (41 line
records, 11 named lines, 58,560 trips), the DLR (6,630 trips), London Trams, West
Midlands Metro, Blackpool, the Air-Rail Link and the IFS Cloud Cable Car — and both
existing geometry paths refuse them, since there is no road under a tube tunnel and
nothing in `shapes` to copy. The new stage draws them from OpenStreetMap route
relations, whose `role=""` members are already in route order and already join end to
end, so a pattern's geometry is a cut of its line's chain: no snapping, no shortest
path, no Markov model. `wayfare/osm.py` is the Overpass client, the relation parser,
the chain walk, the name normalisation and the projection; `wayfare/trace.py` is the
stage. `trace_status` and `traces` are new tables on `match_status`'s design, a
permanent cache selected by the absence of a row, with `transport_error` as the one
retryable status. `aggregate.build_segments` now unions `shapes` and `traces`, kept
disjoint by `shape_id IS NULL` so `segments` keeps its primary key, and the operator's
own recording always wins where there is one. `publish.contents` gained `track`,
computed from `segments JOIN traces`, which widens the ODbL noun to "Track geometry" or
"Road and track geometry" — an archive of tube tunnels is derived from OSM with no
matched edge in it. `deploy/refresh.sh` runs `wayfare trace --retry transient` after
the publish gate and lets it fail.

The relations were surveyed before the stage was written, against BODS
`20260806_022608` and one Overpass query over Greater London. 556 route relations;
every Underground line, the DLR (5 masters), London Trams (3) and the Elizabeth line
(5) carry a `route_master`; chain walks with zero breaks on Victoria (24 ways, 21.69 km
against an official 21 km), Central (125 ways, 54.75 km), Jubilee (59 ways, 37.15 km)
and the DLR's Lewisham to Stratford (81 ways, 11.02 km). The 1,417 Underground patterns
are 459 distinct station sequences over 11 lines, every one a contiguous sub-path of
its line. There is no `naptan:AtcoCode` on any Underground stop node, so the join is by
normalised name with a coordinate check: 16 of 16 Victoria line stops match, 15 of them
within 150 m, the worst being Highbury & Islington at 216 m where the timetable's point
is the National Rail entrance and the node is the tube platform.

**Run against Great Britain** (2026-08-12), feed `20260807_022616`, on a copy of the
server's database so that production stayed untouched. 1,737 live non-road patterns
carry no `shape_id`, and one Overpass query over their bounding box returned 131 MB and
1,022 route relations in 27 seconds — the whole national fetch cost. 861 of those
relations chain cleanly. The fit resolved 1,127 patterns in 182 seconds, 23,134 km of
track, and took `segments` from 629 rows to 1,756: metro 1,040 of 1,417 patterns and
86.9% of its trips, the DLR 42 of 71 and 60.6%, tram 43 of 77 and 34.2%, the cable car
2 of 2. Ferries draw nothing, which is 166 of the 170 `no_relation` rows and is correct
— `route=ferry` is deliberately outside `config.OSM_ROUTE_VALUES`, because an OSM ferry
way is a schematic between terminals. All eleven Underground lines come out within a
few percent of published length, and the archive built from the scratch root is 138.8
MB against production's 137.9 MB, credit widened to "Road and track geometry". It has
not been deployed. 655 tests pass, ruff and mypy are clean. Figures in
`docs/results.md`.

The run found two naming conventions, both now fixed in `5cd1435`. A stop member is a
node on the platform, so OSM writes "Lewisham Platform 6" where BODS qualifies by mode,
"Lewisham DLR Station"; that cost all 71 DLR patterns and took the total from 713 to
755. And a station needs more than one spelling, because BODS disambiguates the two
Edgware Roads in the name where OSM lets the relation do it; offering both the
bracketed and the unbracketed form took the total from 755 to 1,127.

**What the run leaves open.** 440 `no_stop_match` and 4 `no_relation` rows remain, and
only one cause is diagnosed: the Northern line pattern via Bank calls at Kennington and
the relation's stop members omit it, so the run is not contiguous and is refused. That
is an OSM gap rather than a naming problem, and whether the other 439 are the same
shape is unmeasured. Trams are the weakest mode at 34.2% of trips and nothing has been
looked into there. 117 relations do not chain, 94 of them `route=train`, which is the
mode where they will matter.

## Done — rail drawn per way

**Rail is drawn per way rather than per pattern** (2026-08-15). One polyline per
pattern meant that many services over one stretch of track were many coincident lines,
which is ugly and puts a hover on an arbitrary one of them.
`aggregate.build_track_services` inverts a trace's way list into one row per way and
mode, the way `edge_services` inverts a matched pattern into one row per edge, and
`aggregate.build_segments` keeps only what cannot be inverted. The two partition on a
new column, `traces.ways_cut`. Segments draws the operator's own shapes plus any trace
whose way ids are still the whole line's chain; track draws everything whose way ids
are cut to its own pattern. `mode` joins the track key, so a way carrying both a tube
line and a National Rail service is two features drawn twice, deliberately, because
they are different networks.

**`trace` records the ways under the slice it cut** rather than the whole candidate
chain. `osm.ways_between` already existed for `railtrips` and `osm.Chain.way_at` is
what makes the cut recoverable; `slice_between` answers the same question in geometry,
and this is the same cut in identity. `trace_status.n_ways` counts the slice's ways
now. `traces.ways_cut` records which of the two a stored row holds, because nothing
recoverable distinguishes them afterwards — the way boundaries are gone once the
polyline is stored. The migration sets it TRUE for `osm:r%` patterns, where the
relation is the pattern and the two lists are the same by construction, and FALSE for
everything else; a FALSE row keeps being drawn per pattern, unchanged, until `wayfare
trace --retry ok` re-cuts it.

**Every `osmroutes` pattern had been drawn twice.** The segments trace arm selects
live, non-matchable, shapeless patterns, and that is exactly what an `osm:r` pattern
is, so each one came out as a whole polyline over the very ways the per-way inversion
had just collapsed it into. It had been correct until the inversion landed after it.
The visible half was two coats of the same track. The worse half was the hover, because
the viewer queries segments before track, so a hover on National Rail answered with one
relation's card reading "operator geometry" rather than the way's service list.

**Three quieter faults came out with it.** `ways` is written by two stages now,
`routes` and `trace`, so `osmroutes.write_ways` upserts instead of clearing the table
and `osmroutes.prune_ways` drops the ways no `traces` row runs over; a blanket delete
would have taken the tube's track out of the archive on the next `routes` run, and the
track export joins `ways` inside, so that failure would have been track quietly not
drawn rather than anything raising. The track tippecanoe pass now passes
`--use-attribute-for-id=way_id`, where it had been taking the default `id`, which the
track export does not write, so every track feature reached the viewer with no id at
all and the hover highlight never lit, the id field being what `setFeatureState`
addresses. And the viewer's search dim on the track layer tested `ref`, a property no
track feature has, so a search dimmed the whole rail network to 0.1 without ever
lighting the line it named; it reads the comma-joined `refs` now.

**The viewer colours track by mode.** Each feature takes the middle of its own mode's
ramp, with `rail` as the fallback for archives published before `mode` was on the
feature. The legend gains a flat row per track mode ("Rail track", "Metro track"), and
each switches its mode by filter. The card prints journeys a day where a timetable is
attributed and says so where none is.

**Nothing here has been run, measured or published.** There is no database in this
worktree, so no served archive has been rebuilt and no new figure has been taken.
Taking the Underground per-way on a served region is `wayfare trace --retry ok`, then
`aggregate`, then `publish`. The figures the change is aimed at are already in the
repo. 75.8% of Great Britain's rail ways carry two or more relations, and the National
Rail relations are 1,569,495 vertices drawn per pattern against 443,126 per way. The
national trace run resolved 1,127 patterns, 1,040 of 1,417 metro patterns over eleven
Underground lines. 804 tests pass, ruff and mypy are clean.

## Done — a draft of the network under the network

**The viewer reads each archive twice** (2026-08-15). Pop-in on a slow connection is
the detail band arriving tile by tile over a blank map. MapLibre already refines within
a source, holding a loaded parent tile on screen and overzooming it until the child
lands, so what is missing is the cold view that holds no parent at all. The vendored
MapLibre GL JS 4.7.1 has no `prefetchZoomDelta`, so it never fetches a coarser tile on
purpose. A second vector source over the same PMTiles file, capped at `maxzoom` 9,
covers that for the cost of one tile: it overzooms the published mid band from z9 up to
z18 rather than requesting tiles it has been told do not exist.

**The cap is `config.MID_ZOOM - 1` and the layer starts at `config.MID_ZOOM`.** Both
are band edges `publish` owns and `web/index.html` restates, so
`test_the_draft_layer_tracks_the_bands_publish_actually_writes` reads them back out of
the page. Capped higher, the second source fetches the same detail tiles as the first
and doubles the cost of the view it exists to make cheaper. Drawn lower, it re-requests
the tiles already on screen. Both still draw a map, which is why a test holds them.

**The cap survives because MapLibre writes the source spec over the TileJSON**, as
`pick(extend(tilejson, options))`. The other order would leave the archive header's own
maxZoom of 14 standing and turn the draft into an uncapped copy of the layer above it,
silently. `protocol.add` shares the PMTiles instance, so the second TileJSON costs no
request, and the attribution control deduplicates by exact string, so an archive read
by two sources is credited once.

**The draft is drawn in the same colours at the same widths.** `publish._DETAIL_ONLY`
strips `way`, `refs` and `name` from every band below the detail one and leaves `trips`
and `n`, which are the two attributes the colour ramp and the width ramp read. So the
real tiles refine the geometry and leave the map looking the same. What the missing
three cost is every query: a draft feature carries neither the id the hover addresses
nor the `refs` the search reads, so `OVERVIEW` is kept out of `BUS`, `SEG`, `TRACK` and
`MATCH` and `liveLayers` never sees it. It dims to 0.12 with the road network under a
search, being the one road layer a service number cannot be applied to.

**Three events, because no one of them is both edges.** `sourcedata` is the falling
edge, firing as each tile lands. It is useless as the rising edge: 4.7.1 has no
`sourcedataloading`, so the first report that tiles were missing would arrive at the
moment they stopped being missing. `move` is the rising edge, reading one frame behind
the render that requests the tiles. `idle` is the backstop for a view that needed no
new tiles and fired neither. `map.isSourceLoaded` asks about the current viewport
rather than the whole archive, which is the question being asked, and an early-out on
an unchanged target keeps a per-frame handler down to a lookup per region.

**Measured against the served archives, driven in headless Chrome.**
`ireland.pmtiles` and `northern_ireland.pmtiles` were copied off the server, served
with `wayfare serve`, and opened at `#13/53.3498/-6.2603` over a connection throttled
through the Chrome DevTools Protocol. The overview source caches tiles at z9 and
nothing deeper while the region source caches z13, so the cap holds against the
header's own maxZoom of 14. The draft renders 3,219 features at that viewport against
the detail band's 3,560, and its features carry `n` and `trips` and neither `refs` nor
`name`, which is `_DETAIL_ONLY` doing what the paint expressions depend on. Drawn on
its own it is Dublin's road network in the ramp's own colours at the ramp's own widths.

**The draft stood in for 9.2 seconds at 60 KB/s and 14.7 seconds at 40 KB/s.** Its z9
tile was drawable 8.6 s into the load where the region's detail tiles were not complete
until 17.8 s, and 12.1 s against 26.8 s at the slower rate. The two regions faded
independently, Northern Ireland's draft clearing at 12.8 s while Ireland's stayed up to
26.8 s, which is the per-source test rather than a global one working as intended.

**The draft covers the roads and not the other modes.** `segments` and `track` are
single-pass z5–14 layers with no band structure to cap, so a tram or a railway has no
coarse copy to stand in for it and arrives when its own tile does. Those layers are
hundreds of features rather than millions, so they were left alone. It is a viewer
change throughout — no republish and no re-match. 817 tests pass, ruff and mypy are
clean.

## Next — National Rail

**GB heavy rail is absent from BODS.** All three `route_type=2` routes in the national
bundle are the DLR, so nothing in the timetable this project ingests describes a
National Rail service. Adding one means a second timetable source, and the two halves
of the problem are in very different shape.

The geometry is the solved part. Network Rail's Infrastructure Network Model is OGL
v3.0, free and needs no account: 49,274 track links and 1,411 Engineer's Line References
(ELRs) in EPSG:27700, quoted at ±0.5 m. OSM corroborates it — `ref` is a genuine ELR on
98.3% of the 65,972 `usage=main|branch` running-line ways — so there are two independent
surveys of where the track goes.

The blocker is the timetable join. A Common Interface File (CIF) schedule keys every
location on a Timing Point Location (TIPLOC) code, and TIPLOC is on 34 of 90,811 rail
ways and 164 of 3,648 station nodes. `ref:crs`, the three-letter Computer Reservation
System station code, is on 2,857 station nodes (78.3%) and on no ways at all. So the
chain is TIPLOC to CRS to station node to track, and its first hop needs a crosswalk —
Network Rail's CORPUS or the Rail Delivery Group's RSPS5046 — which has never been
verified to exist openly.

The licence question comes before any of that. RDG's licence is not OGL and has a
separate production tier, and Network Rail caps access at 1,000 users. Everything this
project ingests today is OGL v3.0 or CC BY 4.0.

## Next — the picture

**Decide whether `coalesce` becomes the default for `density`.** The flag exists and
banding survives it; what has not happened is a review of before-and-after renders at
matched exposure. What it removes is arguably an artefact of drawing per edge, but
every existing render changes, so this is a decision about the picture rather than an
optimisation to merge quietly.

**Eyeball the tile coalescing on the rendered map.** The merge is lossless by
construction and the counts check out, but nobody has confirmed that the merged
segments read well on hover — a feature now spans a run of edges, so the highlighted
stretch and the info card cover more road than before. Look in particular at forks,
where chaining stops, and at long uniform ways.

**Direction.** Coincident directed pairs collapse where the service sets agree, which
handles ordinary two-way streets, and a one-way pair carrying different buses each
way still renders as two lines. What remains unverified is whether dual carriageways
and one-way systems read well as two parallel lines on the map.

## Next — correctness

**Re-match the patterns the old bounds dropped.** GB's 1,555 `skipped` and 462 `error`
rows have been triaged and the bounds fixed — the stop gap is 180 km and is not
applied at all to a pattern carrying an operator trace, and a refused connection is
`transport_error` rather than a permanent fault — but nothing has been re-matched.
`wayfare match --reclassify-transport`, then `--retry transient,skipped,error`, is the
run, and it has not been done. Wales's 4.2% skipped is the same list at regional
scale.

**Re-run the census on a graph that covers the whole route.** The 22 low-confidence
patterns in the Wales census are almost all stops over the England border, where
`map_snap` on an operator shape matches the Welsh half and stops while `break_through`
has to reach every stop and loops the extract boundary to do it. That is an artefact
of a regional extract, so the national graph should clear it — but nobody has checked,
and the asymmetry is real. It also means the Wales figures understate the strategy: on
the clean subset of 2,950 patterns pooled recall is 0.955 rather than 0.951.

**The census has never been run against dense urban geometry.** Span predicts quality
and short urban patterns are the worst of it — under 2 km scores 0.907 against 0.959
over 20 km — and London is entirely `stops` path and entirely dense urban. Running the
same harness against the London data root would cost one pass, except that London has
almost no `shape` patterns to be ground truth. GB is the only place both halves exist.

**Tune the rejection bounds against real output.** `MIN_MATCH_CONFIDENCE` has still
never rejected anything on merit — the one shape-path rejection was on detour, not
score — so 0.30 remains an untested guess.

## Next — the two Irish regions

1. **`.env.example` still has no `ireland` block.** The README covers the two variables.
2. **Neither region is clustered.** `edges_clustered` reads `"no"` for both, so
   `wayfare cluster` has never run against either data root.

## In progress — Greater London

Running in a separate data root (`data-london`) against its own Valhalla instance
on port 8003, built from
`europe/united-kingdom/england/greater-london-latest.osm.pbf`. A second instance
rather than a rebuild, because rebuilding the shared graph invalidates every
`edge_id` in the Wales database.

- 304 MB zip, 1.5 GB unpacked, `stop_times.txt` 1.50 GB, 17,611,239 stop times.
- 480,412 trips -> **4,709 patterns**, a 102x collapse against Wales's 10.3x.
  London runs the same route all day at high frequency.
- Only **0.9% carry operator geometry** (44 shapes), against Wales's 85.2% and the
  national 48.3%. London is almost entirely the `stops` path, at two Valhalla
  calls per pattern.
- Matching at **1.0/s** against Wales's 3.6/s, ETA about 1h15m. This is the honest
  cost of `stops` at London road density, and a far better basis than Wales for
  extrapolating to GB.

## Next — GB

1. **Extrapolate from London, not Wales.** Wales's 3.6/s is a `shape`-path rate on
   sparse roads; London's 1.0/s is a `stops`-path rate on dense ones, and the
   nation at 48% `shape` sits between them. Re-measure on the first national batch
   regardless.
2. **Watch memory on `patterns`.** The partitioned aggregate handled London's 1.50
   GB `stop_times.txt` in 6 seconds inside an 8 GB limit; nationally the file is
   5.09 GB. `WAYFARE_MEM` defaults to 8 GB and DuckDB spills to `temp_directory` —
   make sure that path has room.
3. **Pin the OSM extract.** Set `force_rebuild: "False"` and leave the graph alone
   for the whole run; every `edge_id` in the database depends on it.
4. **Budget the disk.** ~40 GB including the graph.

## Next — correctness

**Look at the 148 skipped and 23 errored patterns.** Wales skipped 4.2% on the
`MAX_STOP_GAP_M` bound. Some are certainly TrawsCymru long-distance coaches, which
genuinely have huge stop gaps and should probably be matched rather than dropped.
Others may be bad stop coordinates. This is now a concrete list to inspect rather
than a hypothetical.

**Check `break_through` at termini and on one-way pairs.** Still untested; the
choice over plain `through` is reasoned but unverified against real geometry.

**Validate the `stops` strategy against the `shape` strategy.** Wales is 85%
`shape`, which makes it an unusually good validation set: those patterns are
ground truth for the synthesised ones. Match a
sample of them *both* ways and measure how often the synthesised route recovers
the same edge set. This is the single best available check on the primary code
path, and it costs almost nothing because the data is already there. Report it as
a coverage/agreement figure alongside `status`.

**Eyeball the coalescing on the rendered map.** The merge is lossless by
construction and the counts check out, but nobody has yet confirmed that the
merged segments read well on hover — a feature now spans a run of edges, so the
highlighted stretch and the info card cover more road than before. Look in
particular at forks, where chaining stops, and at long uniform ways.

**Direction.** Valhalla edges are directed, and `edge_id` distinguishes the two
directions of a one-way pair. Coincident pairs now collapse where the service sets
agree, which handles ordinary two-way streets, and a one-way pair carrying
different buses each way still renders as two lines. What remains unverified is
whether dual carriageways and one-way systems read well as two parallel lines on
the map. Decide after seeing Wales rendered.

**Tune the rejection bounds against real output.** Wales rejected 13 patterns as
low confidence and skipped 148 on stop gap. `MIN_MATCH_CONFIDENCE` in particular
has still never rejected anything on merit -- the one shape-path rejection was on
detour, not score -- so 0.30 remains an untested guess.

## Next — scale

**Subsequence reduction.** Many patterns are short workings: a contiguous subsequence
of a longer pattern on the same service. Matching the longest and deriving the rest
could cut Valhalla calls substantially. Worth measuring the share of patterns this
covers before building it.

## Known gaps

**Departed patterns are counted but never evicted.** `prune_shapes` drops operator
geometry only; nothing removes a departed pattern's `pattern_edges` rows. Wales
departed 73 of 3,584 patterns across a two-day feed gap, about 2%, and the edges of
all 73 stay in the table, so `pattern_edges` grows monotonically however long a
database is kept current. Keeping the `match_status` rows is deliberate — a seasonal
service that returns is then free — but no policy decides when the edges of a pattern
that is not coming back should go, and none has been written.

**Northern Ireland's two halves are published on unrelated cadences.** The
timetable is republished every few weeks and the road geometry has not moved since
2025-09-23, so hundreds of timetabled stops exist that the geometry has never heard
of, and journey-level shape coverage sits far below hop-level coverage as a result.
It will drift further until Translink republishes `PtLinks`. Nothing here can fix
it; the pipeline just has to report it honestly and let the `stops` path take the
rest.

**A graph rebuild is still a full re-match.** Geofabrik rebuilds daily and every
`edge_id` depends on the build. `match.pin_graph` refuses rather than silently mixing
two GraphId spaces, but nothing reuses matches across builds. `way_id` survives a
rebuild, so re-anchoring on it is the obvious thing to try.

**`edges` has no spatial index.** A national window reads the whole table however it
is asked for. `wayfare cluster` prunes it to a fraction, but the scan was never where
a render's time went.

**`calendar_dates` exceptions are ignored** when weighting patterns by trips per week.
They shift individual days rather than the shape of the week, and the number is only
ever a rendering weight — but a service that runs only on bank holidays is weighted as
if it ran a normal week.

**`agency_id` is carried but unused.** Colouring or filtering by operator is plausible
for both the map and the art, and the data is already there.

**Cross-border services reach Dublin and Donegal.** Goldline stops sit in the Republic,
which the shared extract covers, so they match — but the two regions are now separate
databases on one GraphId space, and if the feeds ever merge, those stops are the four
known `stop_id` collisions and they are the same physical stops. `service_id` is the
only other measured collision, and NI already prefixes it `NI-`.

**Northern Ireland has no Compose project.** It was driven by hand with `docker run`
against the Ireland project's network, so `/home/ben/wayfare-ni` is host state that
Ansible does not know about and that no committed file reproduces.
