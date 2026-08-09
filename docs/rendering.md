# Rendering (`art`)

## The model

**A render is a style and a query spec, and they know nothing about each other.**
The style decides how an edge is painted; `art.QuerySpec` decides which edges
exist, what their weight means, and what a group is. Three styles then cover the
product of the two rather than three fixed pictures — `strands` grouped by operator
or by road class is a genuinely different map with no new drawing code.
`Style.needs_groups` is the only thing crossing the line: it says which *shape* of
data a style consumes, flat edges or grouped paths, never what the groups are.

**The spec is a closed vocabulary, not a query language.** `WEIGHTS`, `GROUPS` and
`ORDERS` are dicts of SQL fragments; substituted text is only ever a value looked up
in one of them, and anything a caller supplies is a bound parameter. This is not
fussiness: DuckDB's `read_only` applies to the database file and not the filesystem,
so `read_csv` and `ATTACH` still work and user SQL would be an arbitrary file read on
the server. There is a lockdown path (`enable_external_access=false`,
`disabled_filesystems`) but no statement timeout, so a runaway query would need
interrupting from another thread. Not worth it for four knobs. `MAX_GROUPS` refuses a
spec that would draw one composited stroke per OSM way.

**`Edge.weight`, not `Edge.n_trips`.** The field holds whatever `QuerySpec.weight`
asked for, which may be a count of operators or traffic per metre. A field named for
trips holding a count of operators is a lie, and the rename cost four lines.

**Two `strands` behaviours are deliberate.** A service is weighted by the total
traffic on every road it uses, not by its own trips, so a minor route along a busy
corridor keeps a wide ribbon. A service registered by two operators covers each edge
once, hence the DISTINCT on the service/edge pair. Neither is a bug to fix in
passing; changing either changes the picture, so decide that first.

## Streaming

**Nothing holds a whole window or a whole table.** `Window` pulls geometry in chunks,
and the percentile weight scale comes from a separate pass over trip counts alone — 8
bytes an edge, held as two bounds rather than a list of normalised values. Holding
every edge cost 439 MB for the `uk` preset on Wales alone, and the country is about
25x Wales. Each style needed a different accommodation: `density` walks the window
twice (ADD is commutative, so order is free); `spectrum` moved its quietest-first
ordering into SQL, which is sound because weight is monotonic in trip count;
`strands` strokes a service's edges as one cairo path, so the window hands back
(service, edge) pairs already grouped and one ribbon is live at a time.
`render(edges=...)` still works via `Held`, which presents a list through the same
interface. Peak RSS on the `uk` window: density 479 -> 259 MB, strands 617 -> 312 MB.
The Python side no longer grows with the window; what still grows is DuckDB's own
aggregate, which spills to disk rather than failing.

**Geometry comes out of DuckDB as Arrow, not as rows.** The scan was never the
problem and still is not — the London window scans in 4.4ms — but *materialising* it
was: 303ms to turn the same rows into Python lists of ints, because an `INTEGER[]`
column arrives over the row protocol as a list object per edge holding an int object
per vertex. In Arrow that column is a flat child buffer plus offsets, which numpy
adopts without copying. Over London at 3000px (197,276 edges, 585,287 vertices) the
whole data path went 852ms → 358ms by fetching Arrow, and → 198ms by keeping it flat
all the way to cairo — hence `Polyline`, which holds indices into a fetch's shared
coordinate buffers rather than a list of tuples per edge. Whole renders: `strands`
over London 5,449ms → 3,622ms, `density` 4,985ms → 4,423ms.

**The percentile pass belongs in SQL, and has to match `Weights.over` exactly.**
Pulling every weight into Python to take two order statistics cost 1,918ms of a
2,150ms pass at 4.2M edges. `bounds_query` reproduces the rank convention rather than
using `quantile_disc`, which interpolates differently and would shift a render's
contrast invisibly: `row_number()` and an explicit `floor(q * n)`, because `CAST(x AS
BIGINT)` rounds where Python's `int()` truncates. Verified identical on 72 real
database/window/spec combinations.

## Determinism

**Every query the render path streams needs an ORDER BY, including the ones whose
order looks irrelevant.** `edges_query` had none when not ordering by weight, and
DuckDB's parallel hash join returns rows in an order that varies between runs of the
same query against the same file — so `density` to SVG produced four distinct outputs
in four runs, on real data. PNG hid it completely, because cairo's ADD is saturating
and therefore commutative, so the buffer is identical whatever order the strokes
arrive in; SVG records the strokes in the order they were issued and shows it. That is
the same failure `_order_sql` was written to fix for `strands`, and the existing
"byte-identical across two calls" test could not catch this one: three edges never
reach a second thread, so an undefined order is a stable one. Test the order is
*defined* rather than that two runs agree. The sort is close to free — +1.2 ms over
cardiff, +10.1 ms over `uk`, +9.1 ms over London, against renders of 0.4 s to 4.4 s.

**Every ORDER BY needs a unique tiebreak**, and a commutative compositing operator
will hide a missing one from every check that looks at pixels. `strands` to SVG was
never deterministic: the group query ordered by group with no tiebreak *within* a
group, so the edges of one ribbon arrived in scan order. Three runs gave three
different files at a constant 293,842 bytes, differing in 180,365 of them. Fixed by an
`edge_id` tiebreak in `art._order_sql`. Same lesson as the refs ordering and `_chain`'s
starting point in `publish`.

**Which cairo you have decides whether an SVG is vectors or one embedded raster, and
it changes what an SVG test can see.** The dev shell's libcairo 1.18.4 writes all three
styles as real `<path>` strokes. The shipped image's 1.16.0 writes `spectrum` as 35,188
paths but falls back to a *single* `<image>` for `density` and `strands` — cairo cannot
express ADD or SCREEN in SVG, and 1.16 gives up where 1.18 does not. Two consequences.
Any stroke-order bug is invisible in a 1.16 `density` SVG, because there are no strokes
in it. And on 1.16 two SVGs rendered in *one process* never compare equal even when the
pixels are identical, because the fallback names its elements from a process-wide
counter: `id="image5"`/`id="surface1"` against `id="image11"`/`id="surface7"`, with
byte-identical base64 between them. Compare SVGs across processes, or compare the
payload rather than the file. Rendering the same window in three fresh 1.16 processes
gives one hash.

## Line widths

**A stroke width fixed in pixels is a different picture at every canvas size.** The map
shrinks with the canvas and the lines do not, so the same window at 1,600px carries the
4,000px line weight over 40% of the road length, and `density`'s two additive passes
then clip to white through every town centre. That is why the `/art` default (1,600px)
looked nothing like the CLI one (4,000px), and why a preview was a poor guide to the
render it stands in for. `draw_density` quotes its widths against `DENSITY_REF_PX`
(2,000) and multiplies by `width_px`. The ramp constants were halved to match, and
halving then doubling is exact in binary, so the 4,000px render is byte-identical to
the one before the change — verified on Cardiff — while every smaller canvas gets
proportionally lighter. No floor: a small canvas *should* draw hairlines, the same way
downsampling the big render would. `spectrum` and `strands` still hold their widths in
pixels, which is only defensible because neither stacks light additively the way
`density` does; the studio's Widths note says so.

**`Style.max_line_px` therefore has two regimes, and `ref_px` is which one.** The field
exists for banding: a band draws and queries a *collar* past its own rows, half the
widest stroke plus two pixels, so no stroke is ever cut at a raster boundary. Left
`None`, `ref_px` means `max_line_px` is absolute pixels and the collar is fixed —
`spectrum` at 4.0, `strands` at 3.9. Set, it means pixels at a canvas `ref_px` wide, and
the collar scales with `width_px`. `density` is the only style in the second regime:
`max_line_px=9.5`, `ref_px=DENSITY_REF_PX`, the halo ramp's `1.5 + 8.0` quoted against
the same 2,000 the ramp itself is.

Getting this wrong is a seam, and it is one-sided. A collar wider than the stroke only
costs work, so the old fixed 19.0 was merely wasteful below 4,000px and silently broken
above it: half the stroke is `9.5 * line_scale * width_px / 4000` and the collar was
`9.5 * line_scale + 2`, which crosses at `width_px > 4000 * (1 + 2 / (9.5 *
line_scale))` — 4,842px at `line_scale=1`, 4,421px at 2, 4,211px at 4. The two-pixel
slack is the only margin and it shrinks in relative terms as the lines widen, so a large
export with the width knob raised sits closest to the edge. An edge whose centreline
falls past the collar is never fetched, so the paint it owes the band is simply absent.
Every banding test drew at 150 or 200px and passed;
`test_banding_holds_at_a_canvas_wider_than_the_style_reference` renders at 6,000px and
compares bytes. Its fixture alternates busy and quiet edges every row on purpose — only
the busiest draw at the full width, and a fixture that puts those at one end makes the
seam a matter of luck.

## Simplification and sampling

**`spectrum` must never simplify its geometry.** Every other style would draw the same
line through fewer points. This one takes the *hue* from the angle between consecutive
points, so dropping a vertex merges two bearings into their average and repaints that
stretch a different colour. Half a pixel of tolerance moved 74% of the output bytes,
against 0.05% for `density`. `draw_spectrum` therefore passes `tol=0.0` explicitly
rather than reading `opts.simplify_px`. Any future style that derives colour, width or
order from geometry inherits this problem — check before enabling simplification for it.

**Sampling is the only preview lever, and the weight scales must not see it.**
`QuerySpec.sample=n` adds `hash(edge_id) % n = 0` to the window CTE and is linear: 1/8
takes `density` from 50.5s to 6.6s. `hash` rather than `random` so a preview is
reproducible and does not flicker as it redraws. It lives on the spec rather than in
`RenderOpts` because it decides *which edges there are* — but it is deliberately absent
from `QuerySpec.selective`, since it narrows nothing semantically and must not flip the
`LEFT JOIN` that keeps serviceless edges in the picture.

`_Sql.window(sampled=True)` is asked for only by `edges_query`; `weights_query` and
`group_query` take the whole window, and `grouped_query` puts the thinning on its final
SELECT rather than in the shared CTE, because `gstat` is built from that CTE and decides
every ribbon's width and draw order. Sampling upstream of it would make a preview weight
its ribbons differently from the render it stands in for. This is the trap to watch:
anything statistical must read the unsampled window, and only drawn geometry may be
thinned. Alpha is compensated linearly (`alpha_scale * sample`, since ADD is linear),
but the core pass already runs at alpha up to 0.90, so 8x pins it at 1.0 and the preview
still comes back at about 62% brightness. Widening the lines would close that gap and
destroy the point, since line weight is one of the knobs being judged. Hence the studio
page labels the sampled pass and follows it with the real one.

## Banding

**A render is drawn in horizontal bands, one process each, and the output is
byte-identical.** Everything else optimises one core; the box has eight. The canvas
splits into bands, each band is drawn by its own process against its own read-only
handle, and the rasters are pasted back. Measured over the `uk` window on the real
2.75M-edge database at 2,000px: `density` 77–98s → 28–32s, `spectrum` 58–67s → 21–31s,
`strands` 71–72s → 37–40s; at 4,000px `density` 98s → 42s. Verified byte-identical for
all three styles, and on the awkward canvases — letterboxed, `scale=3`, filtered,
sampled, `line_scale=6`. `strands` gains least because its cost is the (service, edge)
fan-out, not vertices.

Four things had to be true and each was a bug first:

- **Cut on edge count, not on height.** Equal-height bands put 1,307,069 of 2,746,261
  edges into one of eight, so seven cores idled while the eighth ran 35s. Latitude
  quantiles over the window took the same render 37s → 27s.
- **Spawn, not fork.** The parent holds an open DuckDB handle when the pool starts, and
  DuckDB's background threads do not cross a fork. The child dies on first use and it
  presents as `BrokenProcessPool` with no traceback, because it is killed rather than
  raising.
- **One band per worker, not more.** 24 balanced bands measured *slower* than 8, 36.7s
  against 27.0s. This was first attributed to `edge_services` being unable to prune, so
  that every band scans all 8.3M rows whatever its height. That floor is real and it is
  not the reason. Timing a single band in isolation at 1, 2, 4, 8, 16 and 24 bands, with
  the drawing suppressed to separate the data path, the per-band data cost halves every
  time the bands double: 20.08s, 10.05s, 5.08s, 2.64s, 1.38s, 1.00s. Fitting a constant
  to that gives a floor of **0.16s a band** — 4.6% of a 24-way band, about one second of
  wall clock across all 24. Total CPU work across every band stays inside 10% from one
  band to 24. What actually costs the 10s is spawn, at about a second each and 24 of them
  on four cores, plus the oversubscription itself. Same shape for `strands` (floor 0.18s)
  and for a filtered spec (0.13s), so it is not a quirk of one style.
- **Draw past the cut and paste only the middle.** Clipping to the band splits a stroke
  at a raster boundary, and cairo tessellates in 24.8 fixed point, so the two halves'
  coverage does not always re-add to the whole shape's — one row of one Cardiff render
  came out 1/255 off. The band surface therefore carries a margin of half a line width,
  drawn and discarded, and the only clip is the serial path's own window rect. That
  margin is `_band_pad`; how wide it has to be depends on the width regime above.

**Two scales must be the window's, never the band's.** `Weights` is injected into each
band. So are the *group* statistics, through `Source.groups`, which names an optional
pre-computed `(grp, n_edges, trips)` relation — registered as an Arrow table, because
inserting 20,000 rows through bound parameters would cost seven seconds a band. `gstat`
sets both ribbon width and draw order. Width being per-band is visibly wrong; order is
the subtle one, because SCREEN is commutative in real arithmetic and *rounds* in
eight-bit, so reordering moved 2.8% of the pixels by up to 4/255 — diffuse, across the
whole image, nothing like a seam, and exactly the kind of difference that gets waved
through.

**Banding declines rather than fails, and the cases matter.** SVG (nothing to paste),
`render(edges=...)` (the list lives in the parent), a window under `MIN_BAND_EDGES`
(spawn costs about a second whatever the picture — Cardiff at 1,200px is 0.75s serial
and banding made it twice as slow), and a connection a worker could not reopen.
`band_source` asks the *connection* for its path via `duckdb_databases()` rather than
assuming `config.DB_PATH` — a caller may hand `render` any database, and a band opening
the configured one instead would quietly draw a different picture in the parallel path
only — then probes it with a read-only open. That probe is what catches a writable
handle: DuckDB gives a writer an exclusive lock, so bands could not open the file at
all. The count that `MIN_BAND_EDGES` is compared against comes from the render's own
`WHERE`, so a spec filtered to one road class does not start eight processes for a
picture one core finishes in a tenth of a second.

**`default_workers` reads the cgroup, not `os.cpu_count()`.** The render service runs at
`cpus: 4` on an eight-core box and `os.cpu_count()` reports the host's, so it would start
eight processes to share four cores' quota and a 3 GB memory limit.
`WAYFARE_RENDER_WORKERS` overrides. Each band also does `SET threads=1`: DuckDB defaults
to a thread per core *per process*, and eight bands would put sixty-four on eight cores.

**And it counts physical cores, not hardware threads.** The box is four cores of eight
threads, and the second thread of a core draws no faster: `uk` `density` at 2,000px is
78.1s on one worker, 44.9s on two, **26.9s on four**, 27.2s on six, 28.1s on eight,
30.9s on twelve, 33.2s on sixteen, 37.5s on 24. Speed-up tops out at 2.90x on four,
which is the core count and not the thread count — tessellating round caps is ALU- and
branch-bound, so there are no memory stalls for a sibling thread to fill. The 4.6%
between eight workers and four is the smaller half of it; eight interpreters and eight
DuckDB connections against a 3 GB container limit is the half that bites.
`_physical_cpus` reads distinct `(physical id, core id)` pairs from `/proc/cpuinfo` and
returns None anywhere that file is absent, where the logical count stands as before.

## Where a render's time goes

**A render costs per edge and per vertex, never per pixel.** Over a synthetic 1M edges,
`density` took 52.7s at 900px and 59.0s at 4,000px — a 20x cut in pixels bought 11%,
because the cost is cairo tessellating round joins and caps once per *vertex*. A smaller
preview is therefore not a cheaper one, which is the whole reason `sample` exists.
Batching cairo state changes was tried and rejected: 128 weight buckets delivered in
bucket order by SQL, one path and one state change each, moved 50.2s to 45.6s —
per-stroke setup was never the cost either.

The three things that did work, in order of how much they buy and how little they cost:
`RenderOpts.simplify_px` drops vertices within half a pixel of the last one kept (36% of
vertices survive at preview width, 30% off the clock, 0.05% of output bytes changed);
`density` draws its halo and core in one walk instead of two, which is byte-identical
because cairo's ADD is saturating and therefore commutative; and `Projection.batch`
projects a whole 20,000-row fetch with numpy at once. Per edge numpy would lose — 4.14
vertices is far too few to pay for array setup — so the batching is the point, not the
library. Together, 53.2s to 25.8s.

**Round caps are what a render costs, and this is where the rest of the time is.**
Measured by replaying the whole `uk` window through cairo under different settings: butt
caps and mitre joins take 55.4s to 25.5s — a 54% cut — because at national scale an edge
has already simplified to 2.08 vertices, so nearly every stroke is one tiny segment whose
cost is tessellating two round caps. `ctx.set_tolerance` coarsens that arc: 1.0 gives
78.5%, 2.0 gives 73.7%. `Antialias.FAST` gives 74.4%; `GOOD` and `DEFAULT` are
byte-identical to `BEST` and buy nothing, so the antialias setting is not a lever. None
of these are taken — they all change the picture, and banding was available and does not.

**Cairo is 76% of a band at every band count**: 62.21s of 82.29s at one band, 8.23s of
10.87s at eight, 2.50s of 3.50s at 24. Banding changed the wall clock and not the
composition, so coalescing attacks the same three quarters whether a render is banded or
not.

**A render is 75% cairo, and the scan is not the problem.** Measured on a synthetic
4.2M-edge / 10.25M-service database (`scripts/bench_window.py`), `density` at 800px:
Cardiff 56,251 edges takes 2,363 ms — weights pass 55 ms, two window walks 532 ms, cairo
1,776 ms. London 752,561 edges takes 28,589 ms, split 516 / 6,558 / 21,515 the same way.
So the whole database side is about a quarter of a render and the percentile pass under
2%. Optimise the drawing, not the query.

That conclusion held and the reasoning behind it did not, which is worth keeping both
halves of. "The scan is not the problem" is true — but the quarter that is not cairo was
almost none of it *scanning*. It was DuckDB rows becoming Python objects, which is a
different thing with a different fix, and the reason a "query optimisation" like
clustering `edges` moved 14ms while changing how the rows are fetched moved 650ms.

**Rejected read-path changes**, so they do not get tried again:

- **Extracting a window to Parquet.** The idea was that iterating on a design should cost
  the window rather than the national table. Cardiff went 2,347 ms -> 2,320 ms and London
  28,978 -> 28,619, and a filtered spec got *slower* (37 -> 101 ms) because the extract
  cost more than the scan it saved. `art.Source` survives as the substitution seam; do
  not reintroduce the extract without first making the drawing cheaper.
- **Materialising the shared `_grouped_base` CTEs into temp tables** so the two grouped
  queries stop recomputing them: 1.20x on London and 1.04x on `uk`, worth about 48ms of a
  3.6s render, against three temp tables and a second copy of SQL whose parameter
  ordering is already the fragile part of the builder.
- **A bounding box on `edge_services`** so the weights pass can prune — see
  docs/pipeline.md.

## Coalescing

**Coalescing runs of edges into one stroke is `RenderOpts.coalesce`, off by default.**
`art` strokes one path per directed edge and an edge is 4.14 coordinates over tens of
metres, so a road is dozens of short strokes end to end, each with a round cap at both
ends, and `density` composites with ADD. Joining runs that meet head to tail into one
stroke removes the duplicate. Cardiff 11,644 edges → 2,113 runs, London 197,276 →
28,597, `uk` over Wales 169,857 → 59,309. Serial `density` at 4,000px: Cardiff 0.88s →
0.73s, London 5.05s → 2.54s. Peak RSS on `uk` 277 → 281 MB — the assignment is ~20 bytes
an edge in Arrow and is released before the drawing stream opens, so the streaming rule
holds.

**It is not picture-preserving for `density`, and that has to be a decision rather than
a discovery.** Two edges meeting end to end each lay a round cap at the shared node, and
ADD counts the overlap twice; one continuous subpath counts it once. Measured at
density's own widths and alphas, the junction pixel drops 85 → 53 at t=0.25, 200 → 108 at
t=0.5, and 255 → 230 at t=1.0, while mid-edge is unchanged to the byte. The effect peaks
in the middle because a doubled value saturates at the top. Nationally that is a bright
dot at every one of millions of nodes, so what coalescing removes is arguably an artefact
of drawing per edge rather than anything in the data — but every existing render changes,
and `publish`'s chaining is not the precedent it looks like: an MVT feature carries
attributes, not additive light, so that merge really was lossless and this one is not.

Three more things are worth knowing before looking at a render.

*The junction dot is not the biggest thing it removes.* `density`'s halo pass is 9.5px
wide at `DENSITY_REF_PX` and an edge is a couple of pixels long at city scale, so
consecutive halos overlap along the *whole* road, not just at the node. Coalescing
therefore drops mean brightness to 82% on a Cardiff crop and 63% on a West End one, and
the lit footprint is unchanged (0.6% and 0.9% of lit pixels lost, all fringe). So the
plain render's glow is partly a picture of how finely Valhalla chopped the road. Compare
at matched exposure — `alpha_scale` about 1.35 for Cardiff, 1.9 for London — or you are
judging the exposure and not the change.

*Chaining is directed, unlike `publish._chain`.* A two-way street is two coincident edges
pointing opposite ways, so at a node where two roads meet there are four incidences and
the undirected "exactly two meet here" rule fires on nothing. Head to tail, each
direction chains independently, and the doubling-back trap `publish` records cannot
arise. Directed pairs are *not* collapsed the way `publish` does: that would halve the
light on every two-way road, which is a different picture rather than a repaired one. The
group key is the drawn weight, because that is what the paint is a function of.

*Banding survives, but only because the parent ships the assignment.* Whether two edges
share a stroke is a fact about the graph, and a band that worked it out from its own
collar sees a fork as a through node wherever the third edge fell outside — and under ADD
that is not a local error, since two strokes double-count wherever they overlap.
Measured: London at 6,000px differed in 356 pixels by up to 89/255, all within 40 rows of
a cut, and was byte-identical at 2,000px, which is exactly the kind of fault that gets
waved through. `Source.chains` is the fix and it is the same arrangement `Source.groups`
already uses for `gstat`. With it, serial and banded are byte-identical on the real
databases at 2,000px, 4,000px and 6,000px, at 4 and 8 workers, under `scale`,
`line_scale=6`, a letterbox and `sample=4`. Simplification stays *per edge* rather than
per run for the same family of reason: `simplify` compares against the last vertex kept,
so a run-level pass would depend on where the run started, and a band's runs start where
its collar cut them.

*`spectrum` and `strands` both decline, for opposite reasons.* `spectrum` strokes each
segment separately to colour it by its own bearing, so it has a cap at every vertex and
no chaining removes them. `strands` already puts a service's edges into one cairo path,
and cairo fills a stroke's outline once with nonzero winding, so caps overlapping inside
a single stroke never accumulated. `Style.coalesces` says which is which, and a request
against a style that ignores it warns.

## Serving

**Serving is `wayfare serve`, in the package, not a script.** `server.py` answers three
things on one port: the static viewer, the PMTiles archives with byte ranges, and `GET
/art`. It moved out of `scripts/serve.py` when it gained the render endpoint — serving
bytes off disk is fine unchecked, taking parameters from a URL and running cairo is not,
and pyproject puts only `wayfare` under mypy and ruff. `scripts/serve.py` is a deprecated
shim so a deployed compose file keeps working.

**`/art` exists because the data is on the server and the design work is not.** Every
expensive stage runs where the disk is, so iterating on a style used to mean copying tens
of gigabytes to a laptop or editing a style and watching a deploy. `art.render_bytes` is
the same `_render` as the file path with a `BytesIO` for a sink, deliberately — there is
no second drawing path to diverge.

**The render server never holds the database open.** DuckDB gives a writer an exclusive
lock on the file, so one read-only handle kept alive by a viewer nobody is looking at
would stop the next `match` or `aggregate` from starting. `/art` opens read-only for the
length of one render and closes it, and reports a held lock as 503 with the reason rather
than a traceback. Never cache the connection to save the open — the open is metadata, the
lock is the pipeline.

**Renders are serialised, and the cap is pixels, not width.** One at a time because a
render is CPU-bound cairo over a full scan of `edges` and the same box is usually also
matching; two would not finish either sooner. That holds all the more now one render uses
every core it is allowed. The bound is `width` x derived height x `scale`², because the
window's aspect ratio sets the height and `scale` multiplies both — `width=4000&scale=4`
over a tall window is 200 megapixels and looks modest. Past `QUEUE_LIMIT` waiters the
answer is 503, since a studio page re-rendering on every slider move would otherwise
queue renders nobody will look at.

**There is still no spatial index on `edges`.** A national window therefore reads the
whole table, over HTTP as much as on the command line. The pixel cap does nothing about
that; the serialisation and the queue limit are the only protection. Clustering is what
prunes — see docs/pipeline.md — and the scan is not where the time goes anyway.

**Every `/art` error is JSON, and the message is the interface.** `send_error` writes an
HTML page, which an `<img>` renders as a broken-image icon with the reason nowhere anyone
can see it. A lat,lon-swapped window is the case that cannot raise — it is a legal window
that draws nothing — so it comes back 200 with `X-Wayfare-Warning`, which is the CLI's log
line put somewhere a browser can read.

## The studio page

**The preview is measured from the stage, not guessed at from the viewport.** It used to
be a fraction of `innerWidth` capped at 1,400px, and a cap is what leaves empty ground: an
`<img>` only ever shrinks to its container, so on a wide screen the render sat in the
middle of a much larger panel. `fitWidth` takes the frame's box and the window's own
aspect — through `canvasHeight`, which is the server's arithmetic — so the picture is
drawn at the size it is displayed at. Cardiff on a 2,200px screen went from a 1,842x1,849
render shown inside an 1,842x1,112 box to a 1,108x1,112 one drawn 1:1. A typed width
switches the fitting off (`previewWidthAuto`) and is the only width that reaches a shared
link, since an automatic one is the sender's screen rather than a decision.

**A percentage height against an implicit grid track silently becomes `auto`.** `.frame`
was `display: grid; place-items: center` with no `grid-template`, so its row was
content-sized and therefore indefinite, and both `max-height: 100%` and `height: 100%` on
the picture resolved to nothing. A render taller than the panel hung out of the bottom of
it, scrolled to the top, which reads as a cropped and off-centre picture rather than as a
layout bug. `grid-template: minmax(0, 1fr) / minmax(0, 1fr)` is the definite version.
Found by measuring the computed boxes in a headless Chrome rather than by reading the CSS:
the img reported its natural 1,849px height inside a 1,112px frame, which no reading of
`max-height: 100%` would have predicted.
