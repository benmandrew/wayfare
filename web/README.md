# Map viewer

`index.html` is one self-contained page. It loads MapLibre GL JS and the *PMTiles* protocol plugin (PMTiles is a single-file archive of map tiles) from the unpkg content delivery network (CDN), so there is no build step and nothing to install.

## Serve locally

Copy the `bus.pmtiles` archive that `wayfare` emits into `web/`, next to `index.html`, then run:

```
cd web
python3 -m http.server 8000
```

Open http://localhost:8000. PMTiles reads slices of one large archive with HTTP `Range` requests, which `http.server` has honoured since Python 3.7. A plain `file://` open does not work, because the browser blocks `fetch` against file URLs.

## Point at a remote archive

The viewer reads the tiles URL from the `?tiles=` query parameter and defaults to `./bus.pmtiles`.

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
