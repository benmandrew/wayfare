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

## Next — republish the three served archives with their credit

**The attribution code landed 38 minutes after the image that published these tiles was
built.** `publish` now stamps the credit into the archive's own tileset metadata
(`93623bc`), but `benmandrew/wayfare:latest` on the server was built at 21:04Z and the
commit landed at 21:42Z, so all three archives now being served — Great Britain, the
Republic and Northern Ireland — were written without it. The Republic's CC BY 4.0 and
Translink's Open Government Licence v3.0 both make the credit a condition of publication,
so this is a live breach for as long as those files are up. The code is done and nothing
needs re-matching: rebuild the image, run `wayfare publish --region <region>` against each
of the three data roots, and copy each archive back into
`/home/samba/sambashare/wayfare/out` under its region name. This is the most urgent item
in this file.

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
3. **Rendered PNG and SVG output carries no credit at all.** `publish` stamps the
   archive, but `art` stamps nothing, so a render leaves the credit behind.

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

**`publish.build_tiles` hardcodes `out/bus.pmtiles`** (`wayfare/publish.py:308`), and
`publish.build` passes no path, so a second region's archive is renamed and copied into
the served directory by hand. Nothing automates the step, and the viewer takes each
region's label from the filename (`web/index.html:358`), so that hand step is also where
the label on the map is decided.
