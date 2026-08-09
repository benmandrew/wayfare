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
