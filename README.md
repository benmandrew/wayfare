# wayfare

wayfare builds a Great Britain-wide dataset of bus routes snapped to the real road
network, from Department for Transport (DfT) Bus Open Data Service (BODS) open data. A
timetable says which stops a service calls at, not which roads the bus drives down. So
wayfare resolves each route onto OpenStreetMap (OSM) way identifiers and inverts the
result: every road segment carries the list of services that use it.

Two artefacts come out. The first is a *PMTiles* archive — a single-file archive of map
tiles — behind an interactive map, where hovering a road lists the service numbers on
it. The second is print-resolution art of a chosen area with its routes overlaid.

## Why matching is the primary path

Only 48.3% of trips in the national bundle carry a `shape_id` (measured 2026-08-06).
The split is per-operator and all-or-nothing, tracking whose scheduling software emits
TransXChange TrackPoints, so whole cities arrive with no geometry at all. Map matching
is therefore the main path and supplied geometry is the shortcut. Matching runs through
Valhalla, the only engine that returns OSM way ids without a custom graph build.

## Stages

Each stage reads what the last one wrote, and each re-runs on its own.

- **acquire.** The BODS General Transit Feed Specification (GTFS) bundle, the Geofabrik
  OSM extract, and the National Public Transport Access Nodes (NaPTAN) stop register.
- **patterns.** The timetable collapses to distinct ordered stop sequences, in DuckDB.
- **match.** Each pattern becomes an ordered list of road edges. This is the long stage;
  it is interruption-safe and resumes from its last committed batch.
- **aggregate.** Pattern-to-edges inverted to edge-to-services, keyed on the public
  service number rather than the GTFS `route_id`.
- **publish.** One GeoJSON feature per line, then tippecanoe to `bus.pmtiles`.
- **art.** A bounding box or named preset to PNG or SVG, in one of three styles:
  `density`, `spectrum` or `strands`.

## Quick start

Use a small region rather than the national bundle. Wales is 41 MB zipped and exercises
every stage.

The development environment is a *Nix flake*. `direnv allow` on first entry is the whole
of the setup, or `nix develop` for those without direnv. The hook creates `.venv` with
uv, installs `-e '.[dev,art]'` and re-syncs it as `pyproject.toml` or the nixpkgs Python
moves. `.venv/bin` is on `PATH`, so `pytest -q`, `ruff check .` and `mypy` run bare.
`.envrc` also loads `.env` if present, the file Compose reads, so a local run and a
containerised one share `WAYFARE_REGION` and `WAYFARE_OSM_URL`.

```
direnv allow                      # or: nix develop

wayfare acquire --region wales
wayfare patterns && wayfare match && wayfare aggregate && wayfare publish
wayfare status
```

On a server, through Docker Compose:

```
docker compose up -d valhalla     # first run builds the graph
docker compose run --rm pipeline  # acquire -> patterns -> match -> aggregate -> publish
docker compose up -d web          # viewer on :8099
```

That pulls the published `benmandrew/wayfare:latest` rather than building, so a server
never compiles tippecanoe itself. `.github/workflows/image.yml` publishes it from `main`
and from the `v*` tags cut from `main`; its header comments cover the two repository
secrets a fork needs. The compose file's own header covers running single stages and
pointing `WAYFARE_IMAGE` at a local build.

`WAYFARE_REGION` and `WAYFARE_OSM_URL` must describe the same area. Nothing checks this,
and a mismatch fails every pattern rather than erroring.

## Requirements

The flake supplies the toolchain: Python 3.12, uv, cairo and pkg-config (pycairo ships
no wheel and compiles), felt/tippecanoe 2.79.0 — the fork and version the Dockerfile
builds from source — and the duckdb command-line interface (CLI) for reading the
database by hand. Budget roughly 40 GB of free disk. A Valhalla server must be reachable
at `WAYFARE_VALHALLA`, which defaults to `http://localhost:8002`. All pipeline state
lives in a single DuckDB file under `WAYFARE_DATA`.

## Layout

```
wayfare/
  cli.py         subcommands, including `all`, `status` and `prune`
  config.py      paths, tunables, environment
  acquire.py     downloads, staged and resumable
  gtfs.py        timetable -> patterns
  valhalla.py    map-matching client
  match.py       batching, checkpointing, concurrency
  aggregate.py   patterns -> edge-to-services
  publish.py     GeoJSON export and tippecanoe
  art.py         bounding box -> PNG/SVG
  db.py          schema and connections
  polyline.py    Valhalla's encoded polyline codec
web/index.html   MapLibre GL JS viewer, one self-contained page
tests/           pytest; markers `slow` and `valhalla` gate real-data tests
docker-compose.yml
.github/workflows/image.yml  builds and pushes benmandrew/wayfare to Docker Hub
```

## Further reading

- `CLAUDE.md` — architecture, measured results, and the facts worth not rediscovering.
- `PLAN.md` — roadmap, regions in progress, known gaps.
- `web/README.md` — serving the viewer and hosting the archive.
