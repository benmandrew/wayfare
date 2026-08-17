# Measured runs

Everything below is the output of one specific run, against a named feed version, on
the date in its heading. Nothing here is re-checked by a test or recomputed by a
build, and a later run against a later feed will not reproduce these numbers exactly.
Read them to size a run — how long a region takes, how much disk an archive wants, how
much of a feed survives each stage — rather than as current state.

## Wales, end to end, 2026-08-06

The first real run. Feed version `20260806_022608`, Valhalla 3.8.3, graph built
from `wales-latest.osm.pbf`.

| Stage | Result |
|---|---|
| acquire | 41 MB zip, 0.26 GB unpacked |
| patterns | 37,028 trips -> 3,584 patterns (10.3x), 2s |
| | 85.2% carry operator geometry, far above the 48.3% national figure |
| Valhalla graph | ~6 min for Wales |
| match | 3,552 patterns in 16m23s at 3.6/s, 6 workers |
| | ok 3,400 (94.9%) · skipped 148 · error 23 · low_confidence 13 |
| | 95.6% of timetabled trips represented |
| aggregate | 169,857 edges, 413,915 edge-service pairs, 478 distinct services |
| publish | 169,857 edges -> 53,013 features -> 9.5 MB PMTiles, no features dropped |
| art | 0.5s per 2400px render |

3.6/s is the honest throughput. An earlier 15.3/s was measured while the confidence
bug was rejecting most patterns instantly, and meant nothing.

Extrapolating to Great Britain from this is not a straight multiply. Wales is 2.4% of
national trips, which suggests roughly 12 hours — but Wales is 85% `shape` where the
nation is 48%, and the `stops` path costs two Valhalla calls instead of one.

## Greater London, 2026-08-07

Run in a separate data root (`data-london`) against its own Valhalla instance on port
8003, because rebuilding the shared graph invalidates every `edge_id` in the Wales
database.

- 304 MB zip, ~1.5 GB unpacked, of which `stop_times.txt` is 1.50 GB and 17,611,239 stop
  times. The bundle is almost entirely that one file.
- 480,412 trips collapse to 4,709 patterns, a 102x collapse against Wales's 10.3x.
  London runs the same route all day at high frequency.
- Only 0.9% carry operator geometry (44 shapes), against Wales's 85.2% and the
  national 48.3%. London is almost entirely the `stops` path, at two Valhalla calls
  per pattern.
- Matching runs at 1.0/s against Wales's 3.6/s. That is the honest cost of `stops` at
  London road density, and a far better basis than Wales for extrapolating to the
  nation.

## Great Britain, 2026-08-07

Complete end to end, on the server, feed `20260807_022616`.

| Stage | Result |
|---|---|
| patterns | 52,554 |
| match | ok 50,395 (95.9%) · skipped 1,555 (3.0%) · error 462 (0.9%) · low_confidence 142 (0.3%) |
| aggregate | 2,746,261 edges, 8,301,705 edge-service pairs |
| cluster | current — `meta.edges_clustered` = 2,746,261, the whole table |
| publish | 130 MB PMTiles |
| graph | pinned at `3.8.3/1786113507` |

These figures predate the stop-gap and chunking fixes recorded in
[docs/pipeline.md](pipeline.md), so the `skipped` and `error` counts are what those bounds
cost rather than what the pipeline now achieves. Re-matching the 1,555 skipped and 462 error
patterns is a `wayfare match --retry skipped,error` away and has not been run.

## Feed churn, Wales, 2026-08-08

The first measurement of churn against two real consecutive feeds. Wales data root
(`data`), previous stored feed `20260806_023912`, new feed `20260808_024504` — a
two-day gap rather than a month.

| Measure | Result |
|---|---|
| patterns before -> after | 3,584 -> 3,541 |
| `patterns` log | 30 new · 0 carried over still unmatched · 73 departed |
| accounting | 3,584 − 73 + 30 = 3,541, exact |
| `wayfare status` | `patterns_pending` 30, `patterns_departed` 73, 3,327 of 3,541 matched (94.0%) |
| share of the live set | 30 patterns = 0.85%, carrying 2,741 of 146,433 trips = 1.87% of service |
| catch-up cost | 30 patterns at Wales's measured 3.6/s, ~8s against 16m23s for the full run |

New patterns run busier than average: 0.85% of the live set carries 1.87% of
timetabled trips. The catch-up is roughly a 120x reduction in match time, which is
what makes an incremental run a routine operation rather than a rebuild.

All 73 departed patterns keep their `match_status` rows — 3,614 pattern rows against
3,584 status rows, 73 of 73 departed still cached — so a seasonal service that returns
is free.

Three caveats travel with the number. Two days is not a month, so scaling linearly to
~12.8% over 30 days is an upper bound rather than an estimate: the same volatile
patterns flipping in and out repeatedly make the union over 30 days far smaller than 15
two-day windows. It is also Wales, at 85.2% operator geometry against the national
48.3%, and one region rather than the nation. And the run triggered
`_migrate_pattern_ids`, because this database still held rank ids. That migration
recomputes the identity hash from the stored `pattern_stops` with the same SQL
expression `build_patterns` uses, and aborts on any unmapped pattern or hash collision,
so old and new ids are directly comparable and the churn figure is not a migration
artefact.

## `trace` against Great Britain, 2026-08-12

The first run of the stage against real data. Bus Open Data Service (BODS) feed
`20260807_022616`, on a copy of the server's database, so production was untouched.

Work selected is every live non-road pattern carrying no `shape_id`. That is 1,737
patterns — metro 1,417, ferry 170, tram 77, rail 71 and aerial 2.

| Step | Result |
|---|---|
| Overpass | one query over 49.95,-5.27 to 54.13,1.59, 131 MB in 27s |
| relations | 1,022 — train 833, subway 97, tram 59, light rail 25, monorail 4, aerialway 2, funicular 2 |
| chaining | 861 of 1,022 chain cleanly, 117 break, about 40 carry fewer than two stop members |
| fit | ok 1,127 · no_stop_match 440 · no_relation 170, in 182s |
| drawn | 23,134 km of track; `segments` 629 rows -> 1,756 |

One query covers the nation. 27 seconds and 131 MB is the whole national Overpass
cost, far below what the per-way alternative was feared to cost. The 117 relations that
do not chain are 94 `route=train`, 12 tram, 7 subway, 2 light rail and 2 funicular.

| mode | patterns resolved | share of timetabled trips |
|---|---|---|
| metro | 1,040 of 1,417 | 86.9% |
| rail (the Docklands Light Railway) | 42 of 71 | 60.6% |
| tram | 43 of 77 | 34.2% |
| aerial | 2 of 2 | 100% |
| ferry | 0 of 170 | — |

Ferries are 166 of the 170 `no_relation` rows, and that is the intended outcome.
`route=ferry` is deliberately absent from `config.OSM_ROUTE_VALUES`. The project's
existing rule is that a ferry is drawn from the operator's own trace or not drawn at
all, because an OSM ferry way is a schematic between two terminals rather than a survey
of the crossing.

Every one of the eleven Underground lines comes out within a few percent of its
published length. Six can be quoted against a published figure:

| line | drawn | published |
|---|---|---|
| Victoria | 21.53 km | 21 km |
| Waterloo & City | 2.27 km | 2.37 km |
| Bakerloo | 23.34 km | 23.2 km |
| Circle | 27.33 km | 27 km |
| Hammersmith & City | 25.49 km | 25.5 km |
| Jubilee | 37.19 km | 36.2 km |

The other five draw District 43.42 km, Metropolitan 44.80 km, Piccadilly 47.85 km,
Central 54.81 km and Northern 36.60 km. Away from the Underground, the DLR's Lewisham
to Bank is 11.07 km, London Trams' Wimbledon to Beckenham Junction 19.16 km and the
cable car 1.13 km.

`trace` needs no detour guard, and that is measured rather than assumed. Against the
straight-line chain through its own stops, 1,102 of the 1,127 traces draw between 1.0
and 1.3 times it, 8 between 1.3 and 1.6, and 13 under 1.0. Four exceed 2.5, and all
four are the Piccadilly line's Heathrow Terminal 4 loop, which genuinely runs one way.
A guard of the kind `match` carries would reject those four correct traces and nothing
else.

The archive was built in the scratch data root and not deployed. It comes to 138.8 MB
against production's 137.9 MB, and London at z12 lights 5.3% of its pixels against the
current archive's 4.5%. The credit reads "Road and track geometry: © OpenStreetMap
contributors, Open Database License", which is the widened noun working.
`patterns_pending` stayed 0 throughout, so trace failures never reached the publish
gate.

Two naming conventions cost 414 patterns between them, and the run is what found both
(fixed in `5cd1435`).

**Platform and mode qualifiers.** A Public Transport version 2 (PTv2) stop member is a
node on the platform, so OpenStreetMap writes "Lewisham Platform 6" and "Canary Wharf
Platforms 5 & 6", where BODS qualifies the same station by mode and writes "Lewisham
DLR Station" and "Shadwell DLR". Neither qualifier appears on the other side, and one
mismatched stop refuses the whole contiguous run. That cost every one of the 71 DLR
patterns, against relations that chain with zero breaks. Fixing it took the DLR from 0
to 42 and the total from 713 to 755.

**A station needs more than one spelling.** Two Edgware Road stations sit a few hundred
metres apart, so BODS disambiguates in the name — "Edgware Road (Bakerloo)" — where
OpenStreetMap writes "Edgware Road" twice and lets the relation say which is which.
Flattening the brackets matches neither. `osm.spellings` now offers the bracketed and
the unbracketed form, and a stop matches if any spelling agrees. The looser join is safe
because the matched node still has to sit within `TRACE_STOP_MAX_M` of the timetable's
coordinate, which is what keeps Edgware Road apart from Edgware, 8 km up the Northern
line. This took the total from 755 to 1,127 and the Underground from 668 to 1,040.

440 `no_stop_match` rows and 4 `no_relation` rows remain unresolved. One cause is
diagnosed and it is an OpenStreetMap gap rather than a naming problem: the Northern line
pattern via Bank calls at Kennington, the relation's stop members omit it, so the run is
not contiguous and the pattern is refused. Whether the rest are the same shape is
unmeasured. Trams are the weakest mode at 34.2% of trips and nothing has been looked
into there. 117 relations do not chain, 94 of them `route=train`, which is where they
will matter once heavy rail has a timetable source.

The stage was designed against a survey of Greater London and ran against the nation
with no change to its design; what it turned out to need was two spellings of a station
name. Until the remaining 440 refusals are triaged there is no way to tell a mapping gap
from a convention neither publisher has written down.

## The Republic's rail, drawn twice, 2026-08-16

National Transport Authority feed `20260814_21a88e41`. The `segments` and
`track_services` partition had already stopped the 44 relation-built patterns being
drawn in both layers, and the archive still drew every line twice, because the two
copies are different patterns from different sources: 319 timetabled rail patterns with
the operator's shapes, and 44 patterns built from OSM route relations.

The two layers were rendered separately from the published archive and their ink
compared, each dilated three pixels so a small offset does not read as unique coverage.

| view | segments | track | segments only | track only |
|---|---|---|---|---|
| Dublin z11 | 23,859 px | 16,997 px | 36 (0.2%) | 2 (0.0%) |
| midlands z10 | 10,940 px | 10,270 px | 0 | 0 |

The relation track covers nothing the operator's shapes do not. What the two carry
differs, and that is the whole of the trade. In the Dublin z11 tile the 263 rail
segments all carry `trips` under labels that are the feed's route categories — 153
`rail`, 51 `InterCity`, 41 `DART`, 18 `Commuter` — while the 449 track features name
services (`Dublin - Cork`, `South Western Commuter: Grand Canal Dock -> Newbridge`) and
carry no counts at all. Journeys with weak names against names with no journeys, over
the same geometry, and the map shades by journeys a day.

`config.Feed.route_relations=()` on the Republic. One `routes` run after it:

| | before | after |
|---|---|---|
| relations drawn | 44 | 0 |
| live `osm:r` patterns | 44 | 0, all 44 retired |
| `segments` | rail 363, tram 40 | rail 319, tram 40 |
| `track_services` | 5,565 pairs over 2,761 ways | 0 |
| archive layers | `bus`, `segments`, `track` | `bus`, `segments` |
| archive | 16.0 MB | 14.7 MB |

The retire is the half worth keeping in mind. `routes` drew nothing that run, and the
early return on an empty result meant it also retired nothing, so the setting was inert
and the 44 stayed live. Great Britain keeps `ROUTE_MODES` and is unaffected: it has no
rail timetable, which is the case the stage exists for.

## Shaped rail through `trace`, the Republic of Ireland, 2026-08-16

The first run with `config.TRACE_OVER_SHAPE_MODES` holding `rail`, so heavy rail is
fitted against an OpenStreetMap relation even though the National Transport Authority
(NTA) publishes a shape for it. Feed `20260814_21a88e41`, on a copy of the server's
database, so production was untouched. Great Britain is unaffected and was not run: its
996 live rail patterns carry no `shape_id` at all, 905 of them being the `routes` stage's
own, so nothing there changes hands.

Work selected grows from 0 to 319 — every live shaped rail pattern in the Republic, which
between them run four services and hold 392,939 vertices of mainline drawn over itself.

| Step | Result |
|---|---|
| Overpass | one query over 51.65,-9.90 to 54.79,-5.74, 67 relations in 4s |
| chaining | 51 of 67 chain cleanly, 15 break |
| fit | ok 50 · no_stop_match 269, in 23.9s |
| `segments` rail | 319 rows -> 269 |
| `track_services` | 1,434 rows over 1,297 distinct ways |
| vertices moved | 35,160 drawn per pattern become 10,950 drawn per way |

50 of 319 is the honest yield and it is an OpenStreetMap coverage limit rather than a
naming one. The names normalise and agree on both sides: the feed writes "Cork (Kent)"
and the relation "Cork Kent", and `osm.normalise` folds both to `cork kent`. What the
Republic's relations model is the intercity line with sparse stop members — `Cork - Dublin`
carries four, Cork Kent, Mallow, Limerick Junction and Dublin Heuston — while the feed's
patterns are stopping services over branches. The Cobh line's seven calling points are a
subsequence of no relation fetched, so the pattern is refused. Great Britain's Underground
resolved at 86.9% because a tube line and its timetabled service are the same list of
stations, and heavy rail is where that stops being true.

Nothing was lost to the 269 refusals. `build_segments` partitions on whether a cut trace
exists rather than on whether a shape does, so a refused pattern keeps the operator's own
recording and draws exactly as it did before. `aggregate` reports them as "319 on an
operator shape the tracer was offered and could not fit" before the run and 269 after,
and "have no geometry from either source" stayed 0 throughout.

One thing the run found that is not this change's. Overpass answered the first attempt
with a 504, which `wayfare trace` reported as `transport_error` and wrote no status row
for, leaving all 319 patterns pending and the map unchanged — the retryable path working.
The Republic's 44 relation-derived patterns were live at the time of this run and are the
ones the section above retires; nothing here draws them either way.

## `snap` against the Republic of Ireland, 2026-08-16

The first run of the stage against real data. Feed `20260814_21a88e41`, on a copy of the
server's database, so production was untouched. Great Britain and Northern Ireland were
not run and have nothing here to run against: Great Britain's 996 live rail patterns carry
no `shape_id` at all, 905 of them being `routes`' own, and Northern Ireland's database
currently holds only bus and coach.

Work selected is every live shaped rail pattern: 319, running 19 routes, holding 392,939
vertices of mainline drawn as 319 polylines over each other.

| Step | Result |
|---|---|
| Overpass | one query over 51.75,-9.80 to 54.69,-5.84, 4,901 ways in 5.2 MB |
| index | 45,986 track segments |
| snap | ok 319, in 149.3s |
| covered | 100.0% of 41,418.0 km of shape, worst vertex 21.6 m off track |
| `segments` | 359 rows → 40, every one of them tram |
| `track_services` | 3,914 rows over 3,040 ways |
| vertices | 392,939 drawn per pattern → 30,604 drawn per way |

Every pattern resolved, which is the difference between snapping and fitting. The relation
fit reached 50 of the same 319 four days earlier, and the two failures are unrelated: a
relation lists the stations that define a line and the timetable lists the ones a service
calls at, while the track under a shape is just there.

Sharing at the way level: 2,357 ways carry one service, 492 carry two and 191 carry three,
and the busiest way carries 62 patterns. The median is 1.19 ways per kilometre.

**Two bugs, both found by running it rather than by reading it, and both in this stage.**

*The three writes were not one transaction.* Killed between them, the run left 319 patterns
marked `ok` against 271 `traces` rows, and the `ways` write is third so 2,436 way ids
referenced geometry never stored — which `publish.export_track_geojsonl` joins away without
a word. Work is selected by the absence of a `snap_status` row, so those 48 patterns were
marked resolved for ever and would have stopped being drawn with nothing to show for it.
`snap.commit` is now one transaction with a rollback. `match` survives two statements
because a lost batch is reselected; nothing reselects here.

*The anti-flapping hold was bounded against the wrong thing.* Holding the previous way until
it left `SNAP_MAX_M` gave a way that had already diverged another 25 m of track it does not
carry. The headline hid it — 319 of 319 `ok`, 100.0% covered — and the distribution did not:
every one of the 319 reported its worst vertex in the 20–25 m band, over track with
something inside 5 m of 99.5% of it. Each was a junction where the run should have changed
way. `SNAP_HOLD_M` now bounds the hold at 3 m against the *nearest* way, and the same run
returns:

| worst vertex | held to `SNAP_MAX_M` | held to `SNAP_HOLD_M` |
|---|---|---|
| under 2 m | 0 | 1 |
| 2–5 m | 0 | 75 |
| 5–10 m | 0 | 107 |
| 10–20 m | 0 | 104 |
| 20–25 m | 319 | 32 |
| ways found | 2,436 | 3,040 |
| `track_services` rows | 3,136 | 3,914 |

The 24.8% more ways are the ones the run was driving past. The 32 patterns still in the top
band each hold at least one vertex genuinely that far from any mapped track, which is what
the band is for.

## The overview merge against the served archive, 2026-08-17

The confirmation run of `publish.merge_overview`, now on by default through
`config.MERGE_OVERVIEW`. It ran on the production host against the real Great Britain
data root, in `benmandrew/wayfare:latest` on Linux x86_64, which is the image and the
flags a real publish uses. Unlike the earlier bench figures, no simplification
workaround is in play.

Both arms were built by `publish.build_tiles` from the same three exports of 2026-08-16
already on disk: `edges.geojsonl` at 308 MB, `segments.geojsonl` and `track.geojsonl`.
Nothing was re-exported and the database was never opened. The two arms differ in
`config.MERGE_OVERVIEW` and in nothing else, and everything was mounted read-only except
a scratch output directory, so the served archive was never a write target.

The control is what makes the comparison airtight. The merge-off arm came out
byte-identical to the archive currently served, at every zoom, with the same tile counts,
the same totals and the same maxima, and 127.49 MB against the served 127,486,895 bytes.
So the difference in the other arm is the merge and nothing else.

The merged arm comes to 112.43 MB, a saving of 15.06 MB or 11.8%. It also builds faster,
3.6 minutes against 4.2, because tippecanoe has fewer features to place and that outweighs
the extra pass.

| zoom | served and unmerged | merged |
|---|---|---|
| z5 | 1.8 MB, max 1251 KB | 1.7 MB, max 1159 KB |
| z6 | 2.8 MB, max 1011 KB | 3.1 MB, max 1113 KB |
| z7 | 4.0 MB, max 1041 KB | 3.7 MB, max 998 KB |
| z8 | 8.9 MB, max 1046 KB | 4.5 MB, max 506 KB |
| z9 | 10.3 MB, max 743 KB | 5.2 MB, max 351 KB |
| z10 | 11.5 MB, max 516 KB | 6.1 MB, max 234 KB |
| z11-z14 | 14.5 / 17.1 / 21.6 / 34.6 MB | identical |

z11-z14 are identical because the detail band is built from the road export in both arms.
That is the design working. The merge reaches the three bands below z11 and nothing else,
and the numbers say so.

**z6 goes up**, 2.8 MB to 3.1 MB, and its worst tile with it, 1011 KB to 1113 KB. Those
tiles were already against the size ceiling and being thinned by
`--drop-densest-as-needed`, so the merge bought them more of the network for about the
same money. The lit fraction rises at z5-z7 for the same reason, measured earlier around
London at 5.771% to 8.062% at z5. At the zooms under pressure the saving arrives as
content, and at the zooms with headroom, z8-z10, it arrives as bytes, roughly halving
each.

The per-zoom maxima above exceed the 977 KB figure this report measures against, in both
arms, because `--maximum-tile-bytes` binds each tippecanoe pass and `tile-join` then
concatenates the three layers into one tile. It is not new and it is the same on both
sides.

An arm that reproduces the served archive byte for byte was worth the 4.2 minutes it
cost, since it leaves every other number here with one cause. What the merge is worth
still differs by zoom, bytes where there is headroom and drawn network where there is
none.

## Determinism

All three `art` styles are byte-identical run to run, which none of them were. Ties
previously fell in whatever order the scan returned, and two runs of the old `spectrum`
differed by 426 bytes. Verified by rendering Cardiff before and after the streaming rewrite:
`density` byte-identical, `strands` differing by 7 bytes out of 5.8M at delta 1, `spectrum`
differing more because its ties now resolve differently. That claim was PNG-only until the
`strands`-to-SVG ordering bug was found.

## What the render numbers were measured against

The banding, coalescing and cairo figures are the first
`art` measurements taken against real national data — 2,746,261 edges and 8,301,705
edge-service rows, on the four-core, eight-thread box that serves it.

The earlier render speed-ups (simplification, Arrow, one-walk `density`) were measured
against a synthetic 1M-edge database, because the timings that prompted them came from a
window far larger than Wales. The ratios are structural and should hold, but two numbers
are worth re-taking on the real thing: the 62% preview brightness, which depends on how
much the network overlaps in practice, and `strands`, whose cost is dominated by the
(service, edge) fan-out rather than by vertices.

Read together, these runs say that the expensive stage is matching and everything after
it is cheap, and that the cost of a region is set by how much of its feed carries
operator geometry rather than by how large it is.
