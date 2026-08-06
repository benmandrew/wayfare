# wayfare

wayfare builds a Great Britain-wide dataset of bus routes snapped to the real road
network, from Department for Transport (DfT) Bus Open Data Service (BODS) open data.
A timetable feed says which stops a service calls at. It rarely says which roads the
bus drives down, so wayfare resolves each route onto OpenStreetMap (OSM) way
identifiers and inverts the result: every road segment carries the list of services
that use it. Two artefacts come out. The first is a *PMTiles* archive — a single-file
archive of map tiles — behind an interactive map, where zooming anywhere in the
country shows the bus network drawn on the roads and hovering a segment lists the
service numbers on it. The second is print-resolution art of a chosen area, such as
Greater Manchester, with its routes overlaid.

## How it works

The pipeline is five stages plus a renderer. Each stage reads what the last one
wrote, and each can be re-run alone, which matters on a run this long.

**acquire.** Fetches the national BODS General Transit Feed Specification (GTFS)
bundle, the Geofabrik OSM extract for Great Britain, and the National Public
Transport Access Nodes (NaPTAN) stop register. Downloads stage to a `.part` file and
are renamed only when complete, and nothing is re-fetched if a good copy exists.

**patterns.** Collapses the timetable to distinct *patterns* — unique ordered stop
sequences. The national feed holds about 1.55M trips, but most are the same physical
journey repeated through the day. A pattern, not a trip, is the unit of map matching,
and that reduction is what makes national coverage affordable. The group-by runs in
DuckDB rather than Python because `stop_times.txt` is 5.09 GB.

**match.** Turns each pattern into an ordered list of road edges via Valhalla. This is
the long stage; see below.

**aggregate.** Inverts pattern-to-edges into edge-to-services, grouped by public
service number rather than GTFS `route_id`, so a road hovered on the map shows `43`
once instead of once per operator registration.

**publish.** Exports one GeoJSON feature per line and runs tippecanoe to build
`bus.pmtiles` for zoom 5 to 14. Tile attributes are stored per feature per zoom, so
each edge carries at most 12 service numbers plus a true count; the few edges that
overflow are read from an `overflow.json` sidecar.

**art.** Renders a bounding box or a named preset to PNG or SVG in one of three
styles — `density`, `spectrum` or `strands`.

### Two matching strategies

The strategy is chosen per pattern, from whether the operator supplied geometry.

**shape.** The feed carries road geometry for the trip. The trace is already dense and
road-following, so Valhalla's `map_snap` has an easy job.

**stops.** No geometry, so the stop coordinates are the only input. Bus stops sit tens
to hundreds of metres apart, which is far too sparse for `map_snap` to reconstruct
turns reliably; it invents plausible-but-wrong roads. Instead the stop sequence is
routed through with *bus costing* to synthesise dense road geometry, and that result
is then edge-walked to recover the exact edges traversed. Valhalla caps locations per
route request, so patterns longer than 40 stops are routed in overlapping chunks and
stitched.

Both strategies use Valhalla, because it is the only engine that returns OSM way ids
from map matching without a custom graph build: `/trace_attributes` exposes
`edge.way_id` directly. Open Source Routing Machine (OSRM) discards way ids at extract
time and can only give back node ids, and GraphHopper needs `osm_way_id` added as an
encoded value and the graph reimported. One trap is worth knowing — way ids appear
only in Valhalla's native response, and asking for `format=osrm` silently drops them.
Valhalla's own edge ids are stable only within a single graph build, so the OSM
extract is pinned for the duration of a run and the OSM way id is treated as the
durable identity.

## Why map matching is the primary path

Only 48.3% of trips in the national BODS bundle carry a `shape_id` (measured
2026-08-06). The split is strictly per-operator and all-or-nothing: it tracks whose
scheduling software emits TransXChange TrackPoints, not anything about the routes
themselves. In the North East, Stagecoach North East is at 100%, while Go North East
and Arriva North East are both at 0%. Whole cities are therefore missing geometry
entirely.

That single number sets the architecture. Map matching is not a fallback for awkward
feeds — it is the main path, and the supplied-geometry case is the shortcut. Anything
that treated matching as an exception would leave more than half the country blank.

## Quick start

### Local development

Use a small region rather than the national bundle. Wales is 41 MB zipped and
exercises every stage.

```
uv venv && source .venv/bin/activate
pip install -e '.[dev]'

wayfare acquire --region wales
wayfare patterns
wayfare match
wayfare aggregate
wayfare publish
wayfare status
```

A Valhalla server must be reachable; point at it with `WAYFARE_VALHALLA`, which
defaults to `http://localhost:8002`. `publish` needs felt/tippecanoe 2.17 or newer on
`PATH` for native PMTiles output. Serving the map is covered in `web/README.md`.

### Server

```
docker compose up -d valhalla     # first run builds the GB graph: 30-90 min
docker compose run --rm wayfare acquire
docker compose run --rm wayfare patterns
docker compose up -d matcher      # the long one
docker compose logs -f matcher
docker compose run --rm wayfare aggregate
docker compose run --rm wayfare publish
```

The matcher runs as its own service so it survives a disconnected shell and comes back
after a reboot. It is designed to be interrupted: work is selected by the absence of a
`match_status` row, and each batch of 200 is committed before the next is loaded, so
an unclean restart costs at most one batch. Failures are recorded rather than retried
forever, because a pattern whose stops cannot be connected by road will never succeed.

## Requirements

Python 3.12 or newer, and roughly 40 GB of free disk. The BODS bundle is 1.28 GB
zipped and 7.84 GB unpacked, of which `stop_times.txt` is 5.09 GB and `shapes.txt` is
2.53 GB; the Valhalla graph, the DuckDB file and the tile intermediates account for
the rest. All pipeline state lives in a single DuckDB file under `WAYFARE_DATA`.

Valhalla's first start builds the graph from the Great Britain extract, which takes 30
to 90 minutes. After that succeeds, leave `force_rebuild=False` set for the whole run,
because a rebuild invalidates every edge id already stored. Match throughput for the
full Great Britain feed has not been measured yet; the stage is expected to run for a
day or two, and that expectation is not a measurement.

## Layout

```
wayfare/
  cli.py         subcommands: acquire, patterns, match, aggregate, publish, status, art, all
  config.py      paths, tunables, environment
  acquire.py     downloads, staged and resumable
  gtfs.py        timetable -> patterns, in DuckDB
  valhalla.py    map-matching client; the shape and stops strategies
  match.py       the long stage: batching, checkpointing, concurrency
  aggregate.py   patterns -> edge-to-services
  publish.py     GeoJSON export and tippecanoe -> bus.pmtiles
  art.py         bounding box -> PNG/SVG, three styles
  db.py          schema and connections
  polyline.py    Valhalla's encoded polyline codec
web/index.html   MapLibre GL JS viewer, one self-contained page
tests/           pytest; markers `slow` and `valhalla` gate the ones needing real data
docker-compose.yml
```

## Known gaps

Northern Ireland is not covered. BODS is Great Britain only, and Translink NI
publishes ATCO.CIF through OpenDataNI rather than GTFS, a format that carries no
geometry at all. Closing that gap means a second ingest path and a second OSM extract.
Remaining work is tracked in `PLAN.md`.

The interesting part of this project turned out not to be the geometry but the
metadata: which operators bother to publish TrackPoints, and how cleanly that divides
the map. Everything downstream of the 48.3% figure is a consequence of it.
