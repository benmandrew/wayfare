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
because every host except BODS sends one. Numbers in `docs/data.md`. Nothing
downstream of `patterns` has been run.

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

## Next — the Republic of Ireland, end to end

1. **Build the Valhalla graph** from `europe/ireland-and-northern-ireland-latest.osm.pbf`
   (409 MB), in its own data root and its own instance, exactly as London was. The
   extract covers both halves of the island, so this same graph is what Northern
   Ireland will eventually match against — one build, one GraphId space, one database
   if it comes to that.
2. **Decide `MAX_STOP_GAP_M` for the `shape` path first.** 333 of the 2,853 patterns
   (11.7%, 8,395 of 148,255 weekly trips) have a stop gap over 25 km, and every one of
   them carries a dense operator trace. `match_one` checks the bound before it chooses
   a strategy, so all 333 would be skipped on a rule written for routing between bare
   stops. Wales's equivalent was 4.2%, and it is the same open question the
   correctness section above raises about TrawsCymru coaches — the Republic just makes
   it three times as expensive to leave alone.
3. **Expect minutes, not hours.** Wales matched 3,552 patterns at 3.6/s on the `shape`
   path; the Republic is 2,853 patterns and 100% `shape`.
4. **Carry the attribution into what gets published.** CC BY 4.0 makes crediting the
   NTA a condition, and `acquire` printing it in a log line is not that. The tiles and
   the viewer need it before an Irish archive is served.
5. `.env.example` has no `ireland` block; the README covers the two variables.

## Next — scale

**Subsequence reduction.** Many patterns are short workings: a contiguous subsequence
of a longer pattern on the same service. Matching the longest and deriving the rest
could cut Valhalla calls substantially. Worth measuring the share of patterns this
covers before building it.

## Known gaps

**Feed churn is unmeasured.** The incremental path is built and `patterns` logs new /
carried over / departed on every run, but nobody has yet put two consecutive BODS
feeds through it. That number decides whether a monthly refresh takes minutes or
hours, and it is one refresh away from being known.

**Northern Ireland.** BODS and NaPTAN are both GB-only. Translink publishes ATCO.CIF
via OpenDataNI, and it does carry geometry — see `docs/data.md`. It needs a separate
parser and the `ireland-and-northern-ireland` OSM extract (409 MB; there is no
standalone NI extract). Sequenced after GB, because it shares nothing with the GTFS
path except the matcher — and now after the Republic, which reaches that same extract
through the GTFS path and therefore builds the graph both halves of the island would
share.

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
