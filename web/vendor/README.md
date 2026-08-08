# Vendored viewer dependencies

These are unmodified `dist` builds, committed rather than fetched from a CDN at
runtime.

| File | Version | Licence |
|------|---------|---------|
| `maplibre-gl.js` | 4.7.1 | 3-Clause BSD |
| `maplibre-gl.css` | 4.7.1 | 3-Clause BSD |
| `pmtiles.js` | 4.4.1 | 3-Clause BSD |

The page is normally served on a private network. Loading these from `unpkg.com`
made a public CDN a hard runtime dependency of a deployment that otherwise has
none, and the one component that could take the map down while every byte it
actually renders was already on local disk. There was no Subresource Integrity
attribute either, so the page executed whatever that host returned.

The basemap is a separate matter and is **not** vendored: it is raster tiles
fetched per view from `basemaps.cartocdn.com`, so it cannot be bundled, and it
still means panning the map is visible to a third party. Point `BASEMAP` at a
local tile source if that matters for your deployment.

## Updating

Fetch the same two packages at the new version and update the table above:

```sh
V=4.7.1  # maplibre-gl
curl -sLo maplibre-gl.js  "https://unpkg.com/maplibre-gl@$V/dist/maplibre-gl.js"
curl -sLo maplibre-gl.css "https://unpkg.com/maplibre-gl@$V/dist/maplibre-gl.css"

P=4.4.1  # pmtiles
curl -sLo pmtiles.js "https://unpkg.com/pmtiles@$P/dist/pmtiles.js"
```
