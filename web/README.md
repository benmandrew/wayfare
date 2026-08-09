# Serving the viewer, the archives and renders

`wayfare serve` answers three things on one port: the viewer, the tile archives with the byte ranges they are read in, and renders on demand at `/art`.

`index.html` is one self-contained page. It loads MapLibre GL JS and the *PMTiles* protocol plugin (PMTiles is a single-file archive of map tiles) from the unpkg content delivery network (CDN), so there is no build step and nothing to install.

## Serve locally

From the repository root, after `wayfare publish`:

```
wayfare serve
```

Open http://localhost:8099. There is nothing to copy — the server reads `index.html` from `web/` and any `*.pmtiles` file it finds in `data/out/`, so a rebuild is visible on refresh.

Use `wayfare serve` rather than `python3 -m http.server`. PMTiles reads slices of one large archive with HTTP `Range` requests, and Python's built-in server does not implement Range — it answers `200` with the whole file. The viewer then re-fetches all 24 MB for every tile it wants, so the symptom is a map that is unbearably slow rather than one that is obviously broken. `wayfare serve` answers `206` properly, including the suffix ranges PMTiles uses to locate its footer.

The server used to be `scripts/serve.py`, which now prints a notice and forwards, so a Compose file already on a server keeps working. It moved into the package because `pyproject.toml` puts only `wayfare` under mypy and ruff, and copying bytes off disk is fine unchecked where taking parameters off a URL and driving cairo is not.

A plain `file://` open does not work either, because the browser blocks `fetch` against file URLs.

## Hold several regions side by side

The viewer loads every archive the server can see, all at once and into one map: one MapLibre vector source and one pair of layers per archive. The regions barely overlap, so several archives side by side read as a single network. There is nothing to switch between.

```
data/out/wales.pmtiles
data/out/london.pmtiles
```

`wayfare publish` still writes `bus.pmtiles` every time, so this means renaming or copying the archives into one output directory. Wales was published to `data/out/` and then renamed; London's was copied in from its own data root.

`wayfare serve` answers `GET /archives.json` with a JavaScript Object Notation (JSON) list of the archive filenames it can see. The viewer is a static page and cannot list a directory, so without that request the region names would have to be compiled into it.

The header dropdown survives with a different job. It is now "Go to…", and choosing a region calls `fitBounds` on that archive's bounds rather than reloading the page, then resets to the placeholder. It appears once at least two archives carry names the page can label; a `?tiles=` URL is left out of that list.

An archive that fails to open no longer takes the map down with it. The others still load, and the strapline reports the count instead of a name: "3 regions, 1 unavailable — bus services by road segment".

Three things follow from holding a set rather than one archive. `minZoom` is the deepest floor of the set, the maximum of the loaded headers, because at a zoom one archive cannot answer, that region alone goes blank. Feature ids are unique within an archive and not across archives, so hover highlighting and the match count are keyed on the source as well as the id. And where two archives genuinely overlap, the same road is in both, drawn twice and counted twice — Northern Ireland and the Republic were matched against one island graph, so every cross-border road is in each.

Each archive still carries its own attribution in its own tileset metadata, and MapLibre gathers every loaded credit into the one control.

## Opening view

The viewer frames itself from the union of the bounds in every loaded archive's PMTiles header, rather than opening on a fixed UK-wide view. A regional archive covers a small part of the British Isles, and a UK-wide opening view put London on screen as a smudge a few pixels across. A `#hash` in the URL still wins, because that is someone sharing a place.

## Point at a remote archive

The `?tiles=` query parameter names exactly one archive by URL, and the page loads that one alone rather than everything in `/archives.json`. Its purpose is pointing the page at a bucket, not choosing a region.

```
http://localhost:8000/?tiles=https://pub-xxxx.r2.dev/bus.pmtiles
```

The identical file therefore works both locally and in production.

## Hosting on Cloudflare R2 or Amazon S3

Upload `bus.pmtiles` as a single object; there is no tile directory to sync. The bucket must allow cross-origin resource sharing (CORS) from the origin of the viewer. Put `range` in `AllowedHeaders`, expose `ETag` and `Content-Range`, and allow `GET` and `HEAD`:

```json
[{"AllowedOrigins": ["https://your.site"], "AllowedMethods": ["GET","HEAD"], "AllowedHeaders": ["range","if-match"], "ExposeHeaders": ["ETag","Content-Range"], "MaxAgeSeconds": 3600}]
```

The bucket must also serve range requests. R2 and S3 both do this natively, but a CDN or proxy in front of the bucket can strip them, and the two faults look the same from the browser: an empty map, and a console error on the first fetch.

## Render over HTTP

Every expensive stage runs on the server, because that is where the disk is. `wayfare art` draws only against a database on the same machine, so iterating on a design meant copying tens of gigabytes to a laptop, or editing a style and then watching a deploy. `GET /art` renders a window on demand instead, which turns a design into a query string.

```
curl -o cardiff.png 'http://localhost:8099/art?area=cardiff&style=strands&width=2000'
```

The parameters:

- `bbox=minlon,minlat,maxlon,maxlat` or `area=<preset>` — the window. One of the two is required.
- `style` — `density`, `spectrum` or `strands`. Default `density`.
- `format` — `png` or `svg`. Default `png`.
- `width` — 64 to 12000 pixels. Default 1600.
- `height` — optional and in the same range; the window's aspect ratio sets it otherwise.
- `scale` — 0.1 to 4.0. Ignored for SVG, which is resolution independent.
- `hue` — 0 to 1.
- `line_scale` and `alpha_scale` — 0.05 to 8.
- `caption` — up to 120 characters.
- `background` — `#rrggbb`.
- `download` — a valueless flag that switches `Content-Disposition` from `inline` to `attachment`.

`GET /art/meta` reports the styles, the presets, the defaults and the limits as JSON, the same way `GET /archives.json` reports the archives. Neither list is compiled into a page.

The endpoint is bounded, and each bound has a reason.

- **One render at a time.** A render is CPU-bound cairo over a full scan of `edges`, and the same box is usually also matching, so two at once finish neither sooner. A request waits up to 90 seconds for the slot, and past 4 waiting requests the answer is `503`. A page that re-renders on every slider move would otherwise queue pictures nobody will look at.
- **The cap is 64 megapixels**, on width × derived height × `scale`², not on width. The window's aspect ratio sets the height and `scale` multiplies both, so `width=4000&scale=4` over a tall window is 200 megapixels while looking modest.
- **The database opens read-only for one render and closes again.** DuckDB gives a writer an exclusive lock, so a handle held open for a viewer nobody is looking at would stop the next `match` or `aggregate` from starting. A lock already held by a pipeline stage comes back as `503` with the reason, not as a traceback.
- **Errors are always JSON, never HTML**, because every caller of this endpoint is a program. `send_error` writes an HTML page, which an `<img>` shows as a broken-image icon with the reason nowhere anyone can see it.
- **A lat,lon-swapped window cannot be an error.** A UK latitude is a valid longitude, so the swap is a legal window that simply draws nothing. It answers `200` with an `X-Wayfare-Warning` header, which is the command line's log warning put where a browser can read it.
- **Recent renders are cached**, up to 96 MB of them, and every response carries an `ETag` keyed on the size and modification time of the database file. Nudging a slider back is then a `304` rather than a redraw.
- **There is still no spatial index on `edges`.** A national window reads the whole table however it is asked for. The serialisation and the queue limit are the only protection here; the pixel cap bounds the drawing, not the scan.

`WAYFARE_ART=off`, or `wayfare serve --no-art`, switches the endpoint off and answers `501`. That is worth doing wherever the port is open to strangers, because serving tiles is reading bytes off disk and a render is not.

## The render studio

`art.html` is the page for iterating on a design.

```
http://localhost:8099/art.html
```

It builds its controls from `/art/meta`, so a style added in `art.py` appears in the interface with no change to the HTML. A cheap preview width is a separate control from the export width, and requests are debounced as a slider moves, with a newer one superseding whatever is in flight. The whole parameter set lives in the URL hash, so a design is a shareable link, and `index.html` carries one link to the studio.

Rendering where the data already is removes the step that made iterating on a design expensive. What it does not remove is the full scan underneath every render. The scan is the real limit.
