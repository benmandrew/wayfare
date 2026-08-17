# Vendored viewer dependencies

These are unmodified `dist` builds, committed rather than fetched from a CDN at
runtime.

| File | Version | Licence |
|------|---------|---------|
| [`maplibre-gl.js`](maplibre-gl.js) | 4.7.1 | 3-Clause BSD |
| [`maplibre-gl.css`](maplibre-gl.css) | 4.7.1 | 3-Clause BSD |
| [`pmtiles.js`](pmtiles.js) | 4.4.1 | 3-Clause BSD |

The page is normally served on a private network. Loading these from `unpkg.com`
made a public CDN a hard runtime dependency of a deployment that otherwise has
none, and the one component that could take the map down while every byte it
actually renders was already on local disk. There was no Subresource Integrity
attribute either, so the page executed whatever that host returned.

The basemap is a separate matter and is **not** vendored: it is raster tiles
fetched per view from `basemaps.cartocdn.com`, so it cannot be bundled, and it
still means panning the map is visible to a third party. Point `BASEMAP` at a
local tile source if that matters for your deployment.

`wayfare serve` sends everything in this directory as `immutable` for a year, which is the one place it caches outright rather than revalidating. What makes that safe is the query on the URL: both pages ask for `vendor/maplibre-gl.js?v=4.7.1` and not for the bare name, so the request a browser makes changes when the bytes behind it do. Drop the query and a returning visitor holds the old library until the year is out, with nothing on screen to say so.

## Updating

Fetch the same two packages at the new version, update the table above, and update the `?v=` on the six `vendor/` URLs in [`index.html`](../index.html) and [`art.html`](../art.html) to match. `tests/test_viewer.py` reads the table above and fails on either half of that being forgotten.

```sh
V=4.7.1  # maplibre-gl
curl -sLo maplibre-gl.js  "https://unpkg.com/maplibre-gl@$V/dist/maplibre-gl.js"
curl -sLo maplibre-gl.css "https://unpkg.com/maplibre-gl@$V/dist/maplibre-gl.css"

P=4.4.1  # pmtiles
curl -sLo pmtiles.js "https://unpkg.com/pmtiles@$P/dist/pmtiles.js"
```
