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
(41 MB) for development.

**Sizes.** National GTFS: 1.28 GB zipped, 7.84 GB unpacked, `stop_times.txt` 5.09
GB, `shapes.txt` 2.53 GB, 1.55M trips. OSM Great Britain: 2.16 GB. NaPTAN CSV: 102
MB, 435k records. Budget ~40 GB of disk including the Valhalla graph.

## Mode filtering

**Ferries are the largest single error class in the GB run, and `route_type` is
what removes them.** All 52 `error` rows carrying Valhalla code 444 — 11,200 trips
— are two-stop sea crossings on the `shape` path: CalMac, Orkney, the Shields
ferry. `map_snap` is refusing to put water on a road, which is correct behaviour
answering a question that should never have been asked. A further 41
`low_confidence` rows, 8,867 trips, are the same crossings arriving through the
`stops` path, and they are 63% of all low-confidence trip weight. `gtfs.py` drops
every trip whose route is not road-going before patterns are built.

**The trap is the kept set, not the filter.** GB `routes.txt` carries `3` on
12,709 routes, `200` on 316, `4` on 119, `1` on 54, `0` on 43, `2` on 3 and `6` on
1. `200` is the extended-GTFS code for coach, so those 316 are National Express and
FlixBus — real long-distance road services, and already the bulk of the
long-distance `skipped` patterns. A filter written as `route_type = '3'` deletes
them and looks right. `config.ROAD_ROUTE_TYPES` therefore holds ranges rather than
values: `3`, `11` and `800` for bus and trolleybus, `200`–`209` for coach,
`700`–`716` for the extended bus codes. Nothing speculative is added beyond that —
a mode nobody publishes is a line that cannot be checked against a feed.

Everything dropped is logged by type with its route and trip counts, and a type
this codebase does not name is a warning rather than an info line, because the way
this goes wrong is a future feed publishing something road-going in a range nobody
kept. A feed where *every* trip drops raises instead: that means the join to
`routes.txt` failed, not that the timetable is all water.

`routes` gained a `route_type` column; the migration adds it empty, since nothing
already stored can supply it, and the next `patterns` run fills it in.
Already-matched ferry patterns are not deleted — they stop appearing in the current
feed and become departed, keeping their `match_status` rows exactly as a withdrawn
bus route does. So an existing database keeps its ferry edges until the next
`aggregate`, which joins on `db.current_feed()` and drops them from
`edge_services`.

## OSM

**Geofabrik files Greater London under `england/`**, not at the top level like the
nations. The `london` slug therefore fell back silently to the 2.16 GB Great
Britain extract until `config.OSM_EXTRACTS` got an entry.

**OSM `route=bus` relations are not viable as a source.** 12,968 nationally, only
818 `route_master` relations, and Greater London alone is 13% of the total. BODS is
the authority for what services exist; OSM is only the geometry substrate.

## Beyond Great Britain

**Northern Ireland has no GTFS, but it does have geometry.** BODS and NaPTAN are
both GB-only, and Translink publishes ATCO.CIF through OpenDataNI. That file is not
geometry-free: its `QB` records carry each stop's position as a six-figure Irish
Grid reference (EPSG:29903), and only 13 of 11,090 stops lack one. Round-tripping
those against Translink's own lat/lon list gives a median error of 3.1 m, which
pins the projection rather than assuming it. Translink also publishes
road-following geometry separately, as MapInfo MIF/MID `PtLinks`: 37,913
stop-to-stop polylines covering 97.5% of hops and 83.5% of trips. Not yet covered —
see PLAN.md.

**The Republic of Ireland inverts the fact at the top of this file.** The National
Transport Authority's Transport for Ireland feed
(`https://www.transportforireland.ie/transitData/Data/GTFS_All.zip`) carries a
`shape_id` on all 129,405 of its trips, across all 108 agencies, with none of the
per-operator split that holds GB at 48.3%. Median shape is 992 points against GB's
849. The licence is CC BY 4.0 rather than the Open Government Licence (OGL), so
attribution is a condition rather than a courtesy. It also carries 2,847 Irish Rail
and 2,655 LUAS tram trips over 371 patterns, which is the other half of why the
mode filter above exists.
