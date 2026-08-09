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

**Look at the skipped and errored patterns.** Wales skipped 4.2% on the
`MAX_STOP_GAP_M` bound; GB skipped 1,555 and errored 462. Some are certainly
TrawsCymru-style long-distance coaches, which genuinely have huge stop gaps and should
probably be matched rather than dropped. Others may be bad stop coordinates. This is a
concrete list to inspect rather than a hypothetical.

**Validate the `stops` strategy against the `shape` strategy.** Wales is 85% `shape`,
which makes it an unusually good validation set: those patterns are ground truth for
the synthesised ones. Match a sample of them *both* ways and measure how often the
synthesised route recovers the same edge set. This is the single best available check
on the primary code path, and it costs almost nothing because the data is already
there. Report it as a coverage/agreement figure alongside `status`.

**Check `break_through` at termini and on one-way pairs.** Still untested; the choice
over plain `through` is reasoned but unverified against real geometry.

**Tune the rejection bounds against real output.** `MIN_MATCH_CONFIDENCE` has still
never rejected anything on merit — the one shape-path rejection was on detour, not
score — so 0.30 remains an untested guess.

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
path except the matcher.

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
