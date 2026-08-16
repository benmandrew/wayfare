# Rendering (`art`)

`art` takes a longitude/latitude window onto the road network and draws every road
that carries a service, weighted by how much service it carries. The output is a PNG
or an SVG, written from the command line or served over HTTP by `wayfare serve`. It is
separate from the tile publishing code on purpose: tiles are for reading, these are
for looking at.

It needs a built database and the `art` extra — `pycairo`, `numpy` and `pyarrow`. The
pipeline and the tile server import none of the three.

## Drawing one

    wayfare art cardiff
    wayfare art --bbox=-3.32,51.42,-3.08,51.57 --style spectrum
    wayfare art uk --style strands --width 6000 --out /tmp/uk.png

The area is a preset name or a window written `minlon,minlat,maxlon,maxlat`. Twelve
presets ship: `bristol`, `cardiff`, `edinburgh`, `greater_glasgow`, `liverpool`,
`london`, `manchester`, `sheffield`, `tyne_and_wear`, `west_midlands`,
`west_yorkshire` and `uk`. A window that starts west of Greenwich begins with a minus,
which argparse reads as an option flag, so pass it attached —
`--bbox=-3.3,51.4,-3.0,51.6` — rather than as a separate token.

The flags that change the picture:

- **`--style`** — `density`, `spectrum` or `strands`; default `density`.
- **`--width`** — canvas width in pixels, default 4,000. The height follows from the
  window's aspect ratio.
- **`--scale`** — surface multiplier for print, and 2.0 is roughly 192 dpi. PNG only,
  since an SVG is resolution independent.
- **`--caption`** — a line of text in the bottom-left corner.
- **`--credit`** — burn the data credit into the corner too. Every render already
  carries it in the file's metadata; this is for anywhere the metadata will not
  survive the trip.
- **`--coalesce`** — join edges that meet end to end into one stroke, so a shared node
  is capped once rather than twice. `density` only, and it changes the picture.
- **`--out`** — the output file, `.png` or `.svg`. The suffix picks the format, and
  the default is `OUT/<area>-<style>.png`.
- **`--workers`** — bands drawn in parallel; the default is one per physical core, and
  `1` draws serially.

## The three styles

**`density`** paints weekly trip volume as light — a wide dim halo under a narrow
bright core, composited additively, so a corridor several services share burns out
white. It is the one to reach for first, the only one whose line widths follow the
canvas size, and the only one that reads `--coalesce`.

**`spectrum`** takes the hue of each segment from its compass bearing, which turns a
gridded city into blocks of colour and a radial one into a wheel. It strokes each
segment separately and never simplifies its geometry, so it is the most expensive of
the three per vertex.

**`strands`** gives every service its own translucent ribbon, screened over the
others, so overlapping routes weave rather than merge. It is the only style that reads
`QuerySpec.group`, which is what makes it more than one picture: grouped by operator or
by road class it draws a genuinely different map with no new drawing code.

Two `strands` behaviours are deliberate and neither is a bug to fix in passing. A
service is weighted by the total traffic on every road it uses rather than by its own
trips, so a minor route along a busy corridor keeps a wide ribbon. A service registered
by two operators covers each edge once, hence the DISTINCT on the service/edge pair.
Changing either changes the picture, so decide that first.

## Changing what is drawn

A render is a style and a *query spec*, and they know nothing about each other. The
style decides how an edge is painted; `art.QuerySpec` decides which edges exist, what
their weight means, and what a group is. `Style.needs_groups` is the only thing
crossing the line, and it names the *shape* of data a style consumes — flat edges or
grouped paths — never what the groups are.

`weight` is the scalar the colour and width ramps see, per edge:

| `weight` | what it measures |
|---|---|
| `trips` | weekly trips, the default |
| `services` | distinct services |
| `operators` | distinct operators |
| `patterns` | distinct patterns |
| `busiest` | the busiest single service on the edge |
| `density` | trips per metre, so a long rural link and a short city block carrying the same buses no longer weigh the same |

`group` is what one ribbon is, for `strands`: `service` (the default), `operator`,
`road_class`, `way` or `road_name`. A service key puts an edge in as many groups as it
has services; an edge-level key puts it in exactly one. `MAX_GROUPS` is 20,000, which
refuses a spec that would draw one composited stroke per OSM way.

`order` decides which group is drawn first, and so ends up underneath: `widest` (the
default), `narrowest`, `busiest`, `quietest` or `name`. Unlike the other two
vocabularies these are `(column, direction)` pairs rather than SQL fragments, because
the same ordering has to be written against two different sets of table aliases.

Four filters narrow the picture: `operator`, `service`, `road_class` and `min_trips`.
Any of them flips the join. Unfiltered, an edge with no services still draws, black and
at weight zero; filtered to one operator, an edge that operator does not use has to
vanish rather than render as a black line through the middle of the picture.
`Edge.weight` holds whatever `weight` asked for, which may be a count of operators, so
the field is not named for trips.

The spec is a closed vocabulary and never a query language. Substituted text is only
ever a value looked up in `WEIGHTS`, `GROUPS` or `ORDERS`, and anything a caller
supplies is a bound parameter. That matters because DuckDB's `read_only` applies to the
database file and not to the filesystem, so `read_csv` and `ATTACH` still work and user
SQL would be an arbitrary file read on the server. This is DuckDB's behaviour rather
than this code's, and its lockdown path (`enable_external_access=false`,
`disabled_filesystems`) carries no statement timeout, so a runaway query would still
need interrupting from another thread. Not worth it for four knobs.

## Serving

`wayfare serve` answers three things on one port, 8099 by default: the static viewer,
the PMTiles archives with byte ranges, and `GET /art`. `--no-art` switches the render
endpoint off and answers it 501, and `WAYFARE_ART=off` does the same for a deployment
that cannot change the command.

`/art` exists because the data is on the server and the design work is not: iterating on
a style otherwise means copying tens of gigabytes to a laptop. `art.render_bytes` is the
same `_render` as the file path with a `BytesIO` for a sink, so there is no second
drawing path to diverge.

The query string takes the window as `area=<preset>` or
`bbox=minlon,minlat,maxlon,maxlat`, plus `style`, `format` (`png` or `svg`), `width`
(default 1,600), `height`, `scale`, `caption`, `credit`, `background` (`#rrggbb` or
three 0–1 floats), `hue`, `line_scale`, `alpha_scale` and `coalesce`. The query spec
arrives through `weight`, `group`, `order`, `operator`, `service`, `class`, `min_trips`
and `sample`; `class` is short for `road_class`, because the query string is written by
hand often enough to be worth the mapping. Filters repeat or comma-separate, and are
sorted and deduplicated so that `operator=A,B` and `operator=B,A` are one cache entry.

Everything is range-checked at parse time, so a bad request costs a parse rather than a
window query. `width` caps at 12,000, `scale` at 4.0, a caption at 120 characters, a
filter at 64 values, `min_trips` at 1,000,000 and `sample` at 16. The bound that
binds is 64 megapixels, because the window's aspect ratio sets the height
and `scale` multiplies both: `width=4000&scale=4` over a tall window is 200 megapixels
and looks modest.

`GET /art/meta` serves what the studio page builds its controls from — the styles and their
blurbs, the presets, the three spec vocabularies, the defaults, the limits, the credit, and
this database's own operator and road-class lists, bounded at 500 values each. Served rather
than compiled into the page, so a style or preset added in [`art.py`](../wayfare/art.py)
reaches the interface without touching any HTML.

Renders are serialised one at a time, because a render is CPU-bound cairo over a full
scan of `edges` and the same box is usually also matching. A request waits up to 90
seconds for the slot, and past four waiters the answer is 503. Recent renders are cached
in 96 MB of memory, keyed on every parameter plus the database file's size and mtime.

The render server never holds the database open. DuckDB gives a writer an exclusive
lock on the file, so one read-only handle kept alive by a viewer nobody is looking at
would stop the next `match` or `aggregate` from starting. `/art` opens read-only for
the length of one render, closes it, and reports a held lock as 503 with the reason
rather than a traceback. Never cache the connection to save the open — the open is
metadata, the lock is the pipeline.

Every `/art` error is JSON, and the message is the interface. `send_error` writes an
HTML page, which an `<img>` renders as a broken-image icon with the reason nowhere
anyone can see it. A lat,lon-swapped window is the case that cannot raise, being a
legal window that draws nothing, so it comes back 200 with `X-Wayfare-Warning`. There
is still no spatial index on `edges`, so a national window reads the whole table over
HTTP as much as on the command line; the serialisation and the queue limit are the only
protection.

## The studio page

[`art.html`](../web/art.html), at `http://localhost:8099/art.html`, is the page for
iterating on a design. It drives `/art` from controls built out of `/art/meta`: style, area
or hand-drawn window, the whole query spec, the colour and line knobs, a caption, the credit
checkbox, and PNG and SVG download links. Only what differs from the defaults reaches the
URL hash, so a shared link says what was changed. It loads `vendor/maplibre-gl.js` and
`vendor/pmtiles.js` from disk, like the viewer does, so no content delivery network is
involved.

While a control is being dragged the page asks for `sample=8` and labels the pass, then
follows it with the real one, because a preview is only cheap if it draws fewer edges.
The preview width is measured from the stage rather than guessed at from the viewport:
`fitWidth` takes the frame's box and the window's own aspect through `canvasHeight`,
the server's arithmetic, so the picture is drawn at the size it is displayed at.
Cardiff on a 2,200px screen went from a 1,842x1,849 render shown inside an 1,842x1,112
box to a 1,108x1,112 one drawn 1:1. A typed width switches the fitting off and is the
only width that reaches a shared link, since an automatic one is the sender's screen
rather than a decision.

## How long a render takes

A render costs per edge and per vertex, never per pixel. Over a synthetic 1M edges,
`density` took 52.7s at 900px and 59.0s at 4,000px — a 20x cut in pixels bought 11%,
because the cost is cairo tessellating round joins and caps once per *vertex*. A
smaller preview is therefore not a cheaper one, which is the whole reason `sample`
exists. `QuerySpec.sample=n` adds `hash(edge_id) % n = 0` to the window CTE and is
linear: 1/8 takes `density` from 50.5s to 6.6s. It hashes rather than randomises so a
preview is reproducible and does not flicker as it redraws. Alpha is compensated
linearly, but the core pass already runs at alpha up to 0.90, so 8x pins it at 1.0 and
the preview comes back at about 62% brightness.

A render is drawn in horizontal bands, one process each, and the output is
byte-identical to the serial path. Measured over the `uk` window on the real 2.75M-edge
database at 2,000px: `density` 77–98s to 28–32s, `spectrum` 58–67s to 21–31s, `strands`
71–72s to 37–40s; at 4,000px `density` 98s to 42s. Byte-identity was verified for all
three styles and on the awkward canvases — letterboxed, `scale=3`, filtered, sampled,
`line_scale=6`. `strands` gains least, because its cost is the (service, edge) fan-out
rather than vertices.

`default_workers` reads the worker count off the cgroup rather than `os.cpu_count()`,
which reports the host's cores and not the render service's quota.
`WAYFARE_RENDER_WORKERS` overrides it. Each band also does `SET threads=1`, because
DuckDB defaults to a thread per core *per process* and eight bands would put sixty-four
threads on eight cores.

It counts physical cores rather than hardware threads, and that is measured. On the
four-core, eight-thread box that serves this, `uk` `density` at 2,000px is 78.1s on one
worker, 44.9s on two, 26.9s on four, 27.2s on six, 28.1s on eight, 30.9s on twelve,
33.2s on sixteen and 37.5s on 24. Speed-up tops out at 2.90x, at the core count rather
than the thread count, because tessellating round caps is ALU- and branch-bound and
leaves no memory stalls for a sibling thread to fill. The 4.6% between eight workers
and four is the smaller half of the reason to stop at four; eight interpreters and
eight DuckDB connections against the container's memory limit is the half that bites.
`_physical_cpus` reads `/proc/cpuinfo` and returns None where that file is absent, so
the logical count stands elsewhere.

Banding declines rather than fails, and the cases are SVG (nothing to paste),
`render(edges=...)` (the list lives in the parent), a window under `MIN_BAND_EDGES` of
150,000, and a connection a worker could not reopen. Spawning a worker costs about a
second whatever the picture, so Cardiff at 1,200px is 0.75s serial and banding made it
twice as slow. The count comes from the render's own `WHERE`, so a spec filtered to one
road class does not start eight processes for a tenth of a second's work.

Memory is bounded rather than proportional to the window. Peak RSS on the `uk` window
is 259 MB for `density` and 312 MB for `strands`, down from 479 MB and 617 MB before
the streaming rewrite. What still grows is DuckDB's own aggregate, which spills to disk
rather than failing.

## Line widths and canvas size

A stroke width fixed in pixels is a different picture at every canvas size. The map
shrinks with the canvas and the lines do not, so the same window at 1,600px carries the
4,000px line weight over 40% of the road length, and `density` then clips to white
through every town centre. That is why the `/art` default at 1,600px looked nothing like
the command line's 4,000px. `draw_density` quotes its widths against `DENSITY_REF_PX`
(2,000) and multiplies by `width_px`; the ramp constants were halved to match, and
halving then doubling is exact in binary, so the 4,000px render is byte-identical to the
one before the change while every smaller canvas gets proportionally lighter. There is
no floor, because a small canvas *should* draw hairlines. `spectrum` and `strands` still
hold their widths in pixels, which is defensible only because neither stacks light
additively.

`Style.max_line_px` therefore has two regimes and `ref_px` is which one. The field
exists for banding: a band draws and queries a *collar* past its own rows, half the
widest stroke plus two pixels, so no stroke is cut at a raster boundary. Left `None`,
`max_line_px` is absolute pixels and the collar is fixed — `spectrum` at 4.0, `strands`
at 3.9. Set, it means pixels at a canvas `ref_px` wide and the collar scales with
`width_px`, which is `density` at `max_line_px=9.5`. Getting it wrong is a one-sided
fault: a collar wider than the stroke only costs work, so the old fixed 19.0 was merely
wasteful below 4,000px and silently broken above it, crossing over at 4,842px with
`line_scale=1`. An edge whose centreline falls past the collar is never fetched, so the
paint it owes the band is simply absent, and every banding test drew at 150 or 200px and
passed.

## Provenance

Every render carries its credit in the file's metadata, unconditionally, and the
visible caption is opt-in. That is two mechanisms rather than one, deliberately.
Metadata costs nothing, cannot alter the picture, and means every render leaving the
server carries its provenance even where nobody thought about it. A credit burned into
a corner *is* a change to the artwork, and the person who knows whether an image is
going somewhere public is the one asking for it — hence `RenderOpts.credit`, `wayfare
art --credit` and `credit=1` on `/art`.

What leaves the machine is a derivative work of the publisher's timetable data and of
Open Database License (ODbL) road geometry. Which timetable licence applies follows the
region: `config.feed()` with no region returns the Bus Open Data Service (BODS) feed
under Open Government Licence v3.0 (OGL), so that is what a default render owes.
Creative Commons Attribution 4.0 (CC BY 4.0) covers only the Republic's National
Transport Authority feed. The ODbL credit is conditional on the render holding matched
road or traced track.

Four fields go in. `Title` is "wayfare density: cardiff", `Description` carries the
window as `minlon,minlat,maxlon,maxlat`, `Software` is "wayfare" bare, and `Copyright`
is `licences.text(config.credit_parts())`. Style and window are in there because a render that has been
through a chat client and back is otherwise a picture of somewhere nobody can name. The
feed version was left out: it would make a render's bytes a function of when the
timetable was downloaded rather than of what was asked for.

Nothing in the metadata may move, because renders are asserted byte-identical run to
run: no timestamp, no hostname, no output path and no version string, which is why
`Software` is the bare name. `test_the_metadata_holds_nothing_that_moves` checks all
four fields for a date, a year, a dotted version and the output path.

pycairo writes neither format's metadata, so both are post-processes on finished bytes.
The PNG `tEXt` chunk is written by hand and spliced in after IHDR, where a reader
looking for a copyright expects one; an unencodable publisher name widens it to `iTXt`
(UTF-8) rather than raising in the middle of a render. An SVG gets a Resource
Description Framework (RDF) `<metadata>` block of Dublin Core elements after the
opening `<svg>` tag.

The caption and the credit are drawn once, in the serial parent, after every band has
been pasted in. Laid down inside `_draw_band` they would appear once per band, each
clipped to its own rows, and they have to be the last thing to touch the surface
because they composite with OVER — the additive and screening styles would otherwise
take the text as light to accumulate.

The credit is one line per thing credited, bottom left, below the user's own caption.
It starts at `max(CREDIT_MIN_PX, proj.width / CREDIT_REF_PX)` — 6.5px, or `width_px`
over 220 where that is larger — so a narrow canvas begins at 6.5px rather than at a
fraction too small to see. That starting size is then shrunk to fit between the margins,
and *that* shrink has no floor, for the reason `density`'s line widths have none: a
thumbnail should look like the render reduced, and the metadata carries the obligation
once the text is a grey mark. The licence URIs are dropped through
`licences.lines(config.credit_parts(), links=False)`, because a URI in 7px text is unclickable and is
spelled out in full in the same file's metadata. `credit` reaches the `/art` cache key,
since a credited render and a plain one are different pictures.

## Design notes

These are the constraints the code is shaped by. Each was a bug before it was a rule.

**Nothing holds a whole window or a whole table.** `Window` pulls geometry in chunks of
20,000 rows, because holding every edge cost 439 MB for the `uk` preset on Wales alone
and the country is about 25x Wales. It arrives from DuckDB as Arrow rather than as rows:
materialising an `INTEGER[]` column over the row protocol builds a list object per edge
and an int object per vertex, and dropping that took London's data path from 852ms to
198ms.

**Only queries that produce drawn geometry may be sampled**, plus the band-cut count
that has to agree with them. Everything statistical reads the *unsampled* window, which
is why `grouped_query` thins on its final SELECT rather than in the shared CTE that
`gstat` is built from, and why `bounds_query` reproduces `Weights.over`'s rank
convention rather than using `quantile_disc`, whose interpolation would shift a render's
contrast invisibly.

**Every query the render path streams needs an ORDER BY with a unique tiebreak**, even
where the order looks irrelevant. DuckDB's parallel hash join returns rows in a varying
order — third-party behaviour, and enough to make `density` to SVG produce four distinct
outputs in four runs on real data. PNG hid it completely, because cairo's ADD is
saturating and therefore commutative. Test that the order is *defined* rather than that
two runs agree.

**Two scales must be the window's and never the band's.** `Weights` is injected into
each band, and so are the group statistics through `Source.groups`, registered as an
Arrow table because inserting 20,000 rows through bound parameters would cost seven
seconds a band. Width being per-band is visibly wrong; draw order is the subtle one,
because SCREEN is commutative in real arithmetic and *rounds* in eight-bit, so
reordering moved 2.8% of the pixels by up to 4/255.

**Which cairo is installed decides whether an SVG is vectors or one embedded raster**,
which is libcairo's behaviour rather than `art`'s. The dev shell's 1.18.4 writes all
three styles as real `<path>` strokes; the shipped image's 1.16.0 writes `spectrum` as
35,188 paths and falls back to a single `<image>` for `density` and `strands`, since
cairo cannot express ADD or SCREEN in SVG. So a stroke-order bug is invisible in a 1.16
`density` SVG, and two SVGs from one 1.16 process never compare equal even with
identical pixels, because the fallback names its elements from a process-wide counter.

**Bands cut on edge count, spawn rather than fork, and paste only their middles.**
Equal-height bands put 1,307,069 of 2,746,261 edges into one of eight, so latitude
quantiles took the same render 37s to 27s. DuckDB's background threads do not cross a
fork, so a forked child dies on first use as a `BrokenProcessPool` with no traceback.
And a stroke clipped at a raster boundary does not always re-add to the whole shape's
coverage in cairo's 24.8 fixed point, so each band draws a discarded margin.

**Round caps are what a render costs.** Replaying the `uk` window through cairo, butt
caps and mitre joins take 55.4s to 25.5s, because at national scale an edge has already
simplified to 2.08 vertices and nearly every stroke is one tiny segment with two round
caps. Coarser tolerance and `Antialias.FAST` buy a further quarter each and are not
taken, since they change the picture where banding does not.

**Optimise the drawing, not the query — and the quarter that is not cairo was never
scanning.** It was DuckDB rows becoming Python objects, which is why clustering `edges`
moved 14ms while changing how the rows are fetched moved 650ms. Three changes bought
time, together taking 53.2s to 25.8s: `simplify_px` drops vertices within half a pixel
of the last one kept, `density` draws its halo and core in one walk, and
`Projection.batch` projects a whole fetch with numpy at once. `spectrum` is exempt from
simplification and must stay so, since half a pixel of tolerance moved 74% of its output
bytes. Four other read-path changes were tried and rejected: a Parquet extract of the
window, which made a filtered spec *slower* at 37 ms to 101 ms; temp tables for the
shared `_grouped_base` CTEs, worth about 48ms of a 3.6s render; 128 cairo state-change
buckets, 50.2s to 45.6s; and a bounding box on `edge_services`, covered in
[docs/pipeline.md](pipeline.md).

**Coalescing is not picture-preserving for `density`, which is why it is off by
default.** Two edges meeting end to end each lay a round cap at the shared node and ADD
counts the overlap twice, so the junction pixel drops 200 to 108 at mid-weight while
mid-edge is unchanged to the byte. The halo pass matters more than the junction dot:
consecutive halos overlap along the *whole* road, so coalescing drops mean brightness to
82% on a Cardiff crop and 63% on a West End one, with the lit footprint unchanged.
Compare at matched exposure — `alpha_scale` about 1.35 for Cardiff, 1.9 for London — or
the judgement is about the exposure. What it buys is speed: serial `density` at 4,000px
goes 0.88s to 0.73s over Cardiff and 5.05s to 2.54s over London. The parent ships the
chain assignment through `Source.chains`, because a band inferring it from its own
collar sees a fork as a through node.

Most of what is written down here began as a picture that looked right and was not — a
seam at a band edge, a glow that turned out to be an artefact of how finely Valhalla
chopped the road, an SVG that differed from itself between two runs. Drawing the
geometry and looking at it is still the only check that catches those, and every number
above exists because looking came first.
