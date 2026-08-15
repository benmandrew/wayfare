# Serving the viewer, the archives and renders

`wayfare serve` answers three things on one port: the viewer, the tile archives with the
byte ranges they are read in, and renders on demand at `/art`.

Nothing here is built or installed. [`index.html`](index.html) and [`art.html`](art.html)
load MapLibre GL JS and the *PMTiles* protocol plugin (PMTiles is a single-file archive of
map tiles) as local files out of `web/vendor/`, where unmodified `dist` builds of MapLibre
GL JS 4.7.1 and pmtiles 4.4.1 are committed, both under the 3-Clause BSD licence.
[`web/vendor/README.md`](vendor/README.md) records the versions and how to update them. The
one thing still fetched from a third party is the raster basemap, from
`basemaps.cartocdn.com`; point `BASEMAP` at a local tile source if panning the map should
not be visible to anyone else.

## Serve locally

From the repository root, after `wayfare publish`:

```
wayfare serve
```

Open http://localhost:8099, which is the default port. There is nothing to copy — the
server reads the pages from `web/` and every `*.pmtiles` file it finds in `data/out/`, so
a rebuild is visible on refresh.

Use `wayfare serve` rather than `python3 -m http.server`. PMTiles reads slices of one
large archive with HTTP `Range` requests, and Python's built-in server does not implement
Range: it answers `200` with the whole file. The viewer then re-fetches the whole archive
for every tile it wants, so the symptom is a map that is unbearably slow rather than one
that is obviously broken. `wayfare serve` answers `206` properly, including the suffix
ranges PMTiles uses to locate its footer. A plain `file://` open does not work either,
because the browser blocks `fetch` against file URLs.

The server used to be [`scripts/serve.py`](../scripts/serve.py), which now prints a notice
and forwards, so a Compose file already on a server keeps working.

## Hold several regions side by side

The viewer loads every archive the server can see, all at once and into one map: one
MapLibre vector source and one pair of layers per archive. The regions barely overlap, so
several archives read as a single network, and there is nothing to switch between.

```
data/out/wales.pmtiles
data/out/ireland.pmtiles
```

`wayfare publish --region ireland --name-by-region` writes `ireland.pmtiles` straight into
the output directory. `--out` names a path outright, the two options are mutually
exclusive, and with neither the archive is `bus.pmtiles`. Publishing the default into a
directory that already holds this region's named archive would update nothing anyone
serves, so that case stops and names both flags. `--region` also chooses whose attribution
is stamped into the tileset metadata, which is what makes the archive lawful to serve, and
`--region all` writes `great_britain.pmtiles`, because the Bus Open Data Service (BODS)
slug `all` is a scope rather than a place.

`wayfare serve` answers `GET /archives.json` with a JavaScript Object Notation (JSON) list
of the archive filenames it can see, since the viewer is a static page and cannot list a
directory. The header dropdown is "Go to…": choosing a region calls `fitBounds` on that
archive's bounds rather than reloading. An archive that fails to open no longer takes the
map down with it, and the strapline reports the count instead of a name: "3 regions, 1
unavailable — bus services by road segment".

Three things follow from holding a set rather than one archive. `minZoom` is the deepest
floor of the set, the maximum of the loaded headers, because at a zoom one archive cannot
answer, that region alone goes blank. Feature ids are unique within an archive and not
across archives, so hover highlighting is keyed on the source as well as the id. And where
two archives overlap — Northern Ireland and the Republic were matched against one island
graph, so every cross-border road is in each — the road is drawn twice, while the match
count and the info card treat it as one, keyed on the OpenStreetMap (OSM) way id. Below
the *detail zoom* the tiles carry no way id, so the count falls back to the per-archive id
there.

The viewer frames itself from the union of the bounds in every loaded archive's PMTiles
header. A `#hash` in the URL still wins, because that is someone sharing a place. Each
archive carries its own attribution in its own tileset metadata, and MapLibre gathers
every loaded credit into the one control.

## Point at a remote archive

The `?tiles=` query parameter names exactly one archive by URL, and the page loads that
one alone rather than everything in `/archives.json`. Its purpose is pointing the page at
a bucket.

```
http://localhost:8099/?tiles=https://pub-xxxx.r2.dev/bus.pmtiles
```

The identical file therefore works both locally and in production.

## Hosting on Cloudflare R2 or Amazon S3

Upload `bus.pmtiles` as a single object; there is no tile directory to sync. The bucket
must allow cross-origin resource sharing (CORS) from the origin of the viewer. Put `range`
in `AllowedHeaders`, expose `ETag` and `Content-Range`, and allow `GET` and `HEAD`:

```json
[{"AllowedOrigins": ["https://your.site"], "AllowedMethods": ["GET","HEAD"], "AllowedHeaders": ["range","if-match"], "ExposeHeaders": ["ETag","Content-Range"], "MaxAgeSeconds": 3600}]
```

The bucket must also serve range requests. R2 and S3 both do this natively, but a content
delivery network or proxy in front of the bucket can strip them, and the two faults look
the same from the browser: an empty map, and a console error on the first fetch.

## Render over HTTP

`wayfare art` draws only against a database on the same machine, so iterating on a design
meant copying tens of gigabytes to a laptop, or editing a style and watching a deploy.
`GET /art` renders a window on demand instead, which turns a design into a query string.

```
curl -o cardiff.png 'http://localhost:8099/art?area=cardiff&style=strands&width=2000'
```

The picture:

- `bbox=minlon,minlat,maxlon,maxlat` or `area=<preset>` — the window. One of the two is
  required.
- `style` — `density`, `spectrum` or `strands`. Default `density`.
- `format` — `png` or `svg`. Default `png`.
- `width` — 64 to 12000 pixels. Default 1600.
- `height` — optional and in the same range; the window's aspect ratio sets it otherwise.
- `scale` — 0.1 to 4.0. Ignored for SVG, which is resolution independent.
- `hue` — 0 to 1. `line_scale` and `alpha_scale` — 0.05 to 8.
- `caption` — up to 120 characters. `background` — `#rrggbb`.
- `credit` — a valueless flag that draws the data credit into the corner. Every render
  carries it in its file metadata regardless.
- `coalesce` — a valueless flag that joins edges meeting end to end into one stroke.
- `download` — a valueless flag that switches `Content-Disposition` to `attachment`.

The data, which is the query spec:

- `weight` — what an edge's value is: `trips`, `services`, `operators`, `patterns`,
  `busiest` or `density`.
- `group` — what one ribbon is: `service`, `operator`, `road_class`, `way` or
  `road_name`.
- `order` — which group is drawn first, and so ends up underneath: `widest`,
  `narrowest`, `busiest`, `quietest` or `name`.
- `operator`, `service`, `class` — comma-separated filters, repeatable, up to 64 distinct
  values each. `class` is `road_class` in the spec, shortened for hand-written URLs.
- `min_trips` — a floor on weekly trips, up to 1,000,000.
- `sample` — draws one edge in n, 1 to 16. A render's cost is per edge and hardly moves
  with the canvas, so this is what makes a preview cheap; the studio asks for 8 while a
  slider is moving.

`GET /art/meta` reports the styles, the presets, the defaults, the limits and the
operators and road classes in the database as JSON, the same way `GET /archives.json`
reports the archives. Neither list is compiled into a page.

The endpoint is bounded, and each bound has a reason.

- **One render at a time.** A render is CPU-bound cairo over a full scan of `edges`, and
  the same box is usually also matching, so two at once finish neither sooner. A request
  waits up to 90 seconds for the slot, and past 4 waiting requests the answer is `503`.
- **The cap is 64 megapixels**, on width × derived height × `scale`², not on width. The
  window's aspect ratio sets the height and `scale` multiplies both, so `width=4000&scale=4`
  over a tall window is 200 megapixels while looking modest.
- **The database opens read-only for one render and closes again.** DuckDB gives a writer
  an exclusive lock, so a handle held open for a viewer nobody is looking at would stop the
  next `match` or `aggregate` from starting. A lock already held by a pipeline stage comes
  back as `503` with the reason, not as a traceback.
- **Errors are always JSON, never HTML**, because every caller of this endpoint is a
  program. `send_error` writes an HTML page, which an `<img>` shows as a broken-image icon
  with the reason nowhere anyone can see it.
- **A lat,lon-swapped window cannot be an error.** A UK latitude is a valid longitude, so
  the swap is a legal window that simply draws nothing. It answers `200` with an
  `X-Wayfare-Warning` header, which is the command line's log warning put where a browser
  can read it.
- **Recent renders are cached**, up to 96 MB of them, and every response carries an `ETag`
  keyed on the size and modification time of the database file. Nudging a slider back is
  then a `304` rather than a redraw.
- **There is still no spatial index on `edges`.** A national window reads the whole table
  however it is asked for. The pixel cap bounds only the drawing.

`WAYFARE_ART=off`, or `wayfare serve --no-art`, switches the endpoint off and answers
`501`. That is worth doing wherever the port is open to strangers, because a render is the
one request here that costs real CPU.

## The render studio

[`art.html`](art.html) is the page for iterating on a design, at
http://localhost:8099/art.html. It builds its controls from `/art/meta`, so a style added in
[`art.py`](../wayfare/art.py) appears in the interface with no change to the HTML. A cheap
preview width is a separate control from the export width, requests are debounced as a
slider moves, and a newer one supersedes whatever is in flight. The whole parameter set
lives in the URL hash, so a design is a shareable link, and [`index.html`](index.html)
carries one link to the studio.

Rendering where the data already is removes the step that made iterating on a design
expensive. What it does not remove is the full scan underneath every render.
