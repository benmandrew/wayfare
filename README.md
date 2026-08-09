# wayfare

wayfare builds a dataset of bus routes snapped to the real road network across these
islands: Great Britain from Department for Transport (DfT) Bus Open Data Service (BODS)
open data, the Republic of Ireland from the National Transport Authority, and Northern
Ireland from Translink through OpenDataNI. A timetable says which stops a service calls
at, not which roads the bus drives down. So
wayfare resolves each route onto OpenStreetMap (OSM) way identifiers and inverts the
result: every road segment carries the list of services that use it.

Two artefacts come out. The first is a *PMTiles* archive — a single-file archive of map
tiles — behind an interactive map, where hovering a road lists the service numbers on
it. The second is print-resolution art of a chosen area with its routes overlaid, drawn
to a file from the command line or served on demand over HTTP.

## Why matching is the primary path

Only 48.3% of trips in the national bundle carry a `shape_id` (measured 2026-08-06).
The split is per-operator and all-or-nothing, tracking whose scheduling software emits
TransXChange TrackPoints, so whole cities arrive with no geometry at all. Map matching
is therefore the main path and supplied geometry is the shortcut. Matching runs through
Valhalla, the only engine that returns OSM way ids without a custom graph build.

The Republic of Ireland is the opposite case and the sharpest available contrast: all
129,405 trips in the National Transport Authority's feed carry a `shape_id`, across all
108 agencies, with none of the per-operator split. That region is entirely the shortcut.

## Stages

Each stage reads what the last one wrote, and each re-runs on its own.

- **acquire** (`acquire.py`). The General Transit Feed Specification (GTFS) bundle for
  the chosen region, the Geofabrik OSM extract, and the National Public Transport Access
  Nodes (NaPTAN) stop register. The bundle comes from BODS for every region except
  `ireland`, which comes from the NTA and skips NaPTAN, since NaPTAN covers only GB.
- **patterns** (`gtfs.py`). The timetable collapses to distinct ordered stop sequences,
  in DuckDB.
- **match** (`match.py`, `valhalla.py`). Each pattern becomes an ordered list of road
  edges. This is the long stage; it is interruption-safe and resumes from its last
  committed batch.
- **aggregate** (`aggregate.py`). Pattern-to-edges inverted to edge-to-services, keyed on
  the public service number rather than the GTFS `route_id`.
- **publish** (`publish.py`). One GeoJSON feature per line, then tippecanoe to
  `bus.pmtiles`.
- **art** (`art.py`). A bounding box or named preset to PNG or SVG, in one of three
  styles: `density`, `spectrum` or `strands`. A PNG is drawn in horizontal bands, one
  process per core, which is about three times faster over a national window and
  byte-identical to drawing it on one. `--workers` sets the count;
  `WAYFARE_RENDER_WORKERS` sets it for a deployment, and the default follows the
  container's CPU quota.

`cli.py` fronts all six, plus `serve`, `status`, `prune`, `cluster` and `all`. `serve`
(`server.py`) answers the viewer, the archives and `GET /art`, which renders a window on
demand instead of only to a file. Two self-contained pages sit in `web/`: the viewer
`index.html`, and `art.html`, a studio for iterating on a render's design.

## Quick start

Use a small region rather than the national bundle. Wales is 41 MB zipped and exercises
every stage.

The development environment is a *Nix flake*. `direnv allow` on first entry is the whole
of the setup, or `nix develop` for those without direnv. The hook creates `.venv` with
uv, installs `-e '.[dev,art]'` and re-syncs it as `pyproject.toml` or the nixpkgs Python
moves. `.venv/bin` is on `PATH`, so `pytest -q`, `ruff check .` and `mypy` run bare.
`.envrc` also loads `.env` if present, the file Compose reads, so a local run and a
containerised one share `WAYFARE_REGION` and `WAYFARE_OSM_URL`.

```console
$ direnv allow                      # or: nix develop

$ wayfare acquire --region wales
$ wayfare patterns && wayfare match && wayfare aggregate && wayfare publish
$ wayfare status
```

On a server, through Docker Compose:

```console
$ docker compose up -d valhalla     # first run builds the graph
$ docker compose run --rm pipeline  # acquire -> patterns -> match -> aggregate -> publish
$ docker compose up -d web          # viewer and renderer on :8099
```

That pulls the published `benmandrew/wayfare:latest` rather than building, so a server
never compiles tippecanoe itself. `.github/workflows/image.yml` publishes it from `main`
and from the `v*` tags cut from `main`; its header comments cover the two repository
secrets a fork needs. The compose file's own header covers running single stages and
pointing `WAYFARE_IMAGE` at a local build.

`WAYFARE_REGION` and `WAYFARE_OSM_URL` must describe the same area. Nothing checks this,
and a mismatch fails every pattern rather than erroring. The Republic of Ireland is
`--region ireland` and Northern Ireland is `--region northern_ireland`; both read the
same extract,
`https://download.geofabrik.de/europe/ireland-and-northern-ireland-latest.osm.pbf` —
409 MB covering the whole island, because Geofabrik splits Ireland at the sea rather
than at the border. One extract is one graph build and therefore one set of edge ids,
so the two regions can share a data root.

Northern Ireland is the one region `acquire` assembles rather than downloads. Translink
publishes no GTFS: `--region northern_ireland` resolves four OpenDataNI datasets through
CKAN, fetches them, and builds a GTFS bundle from the TransXChange timetables and the
MapInfo road geometry before `patterns` ever runs. Two builds of one publication are
byte-identical.

## Data sources and licences

| Source | Covers | Licence |
|---|---|---|
| DfT Bus Open Data Service (BODS) GTFS | Great Britain | Open Government Licence (OGL) v3.0 |
| NaPTAN stop register | Great Britain | OGL v3.0 |
| National Transport Authority (NTA) GTFS, published as Transport for Ireland | Republic of Ireland | Creative Commons Attribution (CC BY) 4.0 |
| Translink TransXChange timetables and MapInfo route geometry, via OpenDataNI | Northern Ireland | OGL v3.0 |
| Geofabrik OpenStreetMap extracts | all three | Open Database Licence (ODbL) |

The NTA feed is the one that carries an obligation rather than a courtesy. CC BY 4.0
makes attribution a condition of use, so anything published from it — a tile archive, a
render, a map on a page — has to credit the National Transport Authority. `wayfare
acquire` prints the licence and the credit on every run, whether it fetches the bundle
or finds it already there.

The credit also reaches the map. `wayfare publish` stamps it into the PMTiles archive's
own tileset metadata, along with OpenStreetMap's ODbL credit for the road geometry,
which every region owes because every road segment is an OSM way. Putting it there
rather than on the page is what makes it survive a copy to someone else's bucket. The
viewer and the art page's window picker both read it out of the archive and show it in
the map's attribution control, so an archive built for one region credits that region's
publisher even though the two pages are shared.

## Requirements

The flake supplies the toolchain: Python 3.12, uv, cairo and pkg-config (pycairo ships
no wheel and compiles), felt/tippecanoe 2.79.0 — the fork and version the Dockerfile
builds from source — and the duckdb command-line interface (CLI) for reading the
database by hand. Budget roughly 40 GB of free disk. A Valhalla server must be reachable
at `WAYFARE_VALHALLA`, which defaults to `http://localhost:8002`. All pipeline state
lives in a single DuckDB file under `WAYFARE_DATA`.

## Further reading

- `CLAUDE.md` — the architecture in brief, and the rules a change has to hold to.
- `docs/data.md` — the feeds, their sizes and traps, mode filtering, coverage gaps.
- `docs/pipeline.md` — the five stages, storage, DuckDB lessons, clustering, tiles.
- `docs/rendering.md` — how `art` draws, and where a render's time actually goes.
- `docs/results.md` — measured runs: Wales, Greater London, Great Britain.
- `PLAN.md` — roadmap, what is next, known gaps.
- `web/README.md` — serving the viewer, hosting the archive, and rendering over HTTP.
