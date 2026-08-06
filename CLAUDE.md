# wayfare

UK-wide dataset of bus routes snapped to the road network, from DfT BODS open
data. Two consumers: an interactive web map (hover a road, see which buses use it)
and artistic renderings of areas.

The directory is still called `coda`; that was a placeholder. The package, the CLI
and the docs are all `wayfare`. Rename the directory when convenient.

## Hard-won facts — do not rediscover these

Measured against the live feeds on 2026-08-06, feed version `20260806_022608`.

**Only 48.3% of trips carry a `shape_id`.** 748,087 of 1,549,590. This is the
single most important fact about the project. The split is strictly per-operator
and all-or-nothing, tracking whose scheduling software emits TransXChange
`TrackPoint`s — Stagecoach North East 100%, Go North East 0%, Arriva North East
0%. So **map matching is the primary path, not a fallback**. Where shapes do
exist they are genuine road geometry (median 849 points, p90 2,109, max 3,705),
not stop-to-stop lines.

**Valhalla is the only engine that returns OSM way ids from map matching** without
a custom graph build. `/trace_attributes` exposes `edge.way_id` directly. OSRM
discards way ids at extract time and can only return node ids; GraphHopper needs
`osm_way_id` added as an encoded value and the graph reimported.

**Way ids appear only in Valhalla's native response.** Asking for `format=osrm`
silently drops them. This is the kind of failure that looks like empty data rather
than an error.

**Valhalla `edge.id` is a GraphId, stable only within one graph build.** It is the
join key for the whole pipeline, so the OSM extract is pinned for the duration of a
run (`force_rebuild: "False"` in docker-compose.yml). Rebuild the graph and every
`edge_id` in the database becomes meaningless. `way_id` is the durable identity;
keep it. Geofabrik rebuilds daily, so this is not hypothetical.

**BODS sends no `Content-Length`.** A truncated download looks exactly like a
complete one. Hence `MIN_GTFS_BYTES` and the `.part` staging in `acquire.py`.

**BODS blocks requests that look like generic scrapers.** A real User-Agent is
required; see `config.USER_AGENT`.

**Sizes.** National GTFS: 1.28 GB zipped, 7.84 GB unpacked, `stop_times.txt` 5.09
GB, `shapes.txt` 2.53 GB, 1.55M trips. OSM Great Britain: 2.16 GB. NaPTAN CSV:
102 MB, 435k records. Budget ~40 GB of disk including the Valhalla graph.

**Regional slugs** (`config.BODS_GTFS_URL`): `all`, `england`, `scotland`,
`wales`, `north_east`, `north_west`, `yorkshire`, `east_midlands`,
`west_midlands`, `east_anglia`, `london`, `south_east`, `south_west`. Use
`wales` (41 MB) for development.

**OSM `route=bus` relations are not viable as a source.** 12,968 nationally, only
818 `route_master` relations, and Greater London alone is 13% of the total. BODS
is the authority for what services exist; OSM is only the geometry substrate.

**Northern Ireland has no GTFS.** BODS and NaPTAN are both GB-only. Translink
publishes ATCO.CIF via OpenDataNI, which carries no geometry at all. Not yet
covered — see PLAN.md.

## Architecture

Five stages, each reading what the last wrote, each independently re-runnable:

    acquire  -> raw downloads
    patterns -> 1.55M trips collapse to distinct ordered stop sequences
    match    -> Valhalla; the stage that runs for a day or two
    aggregate-> invert pattern->edges into edge->services
    publish  -> GeoJSONL -> tippecanoe -> PMTiles

**A pattern is the unit of work.** Grouping trips by `(route_id, direction,
ordered stop sequence)` is what makes national scale affordable — most trips are
the same physical journey repeated through the day.

**Two matching strategies**, chosen per pattern in `valhalla.Client`:
- `shape`: operator geometry exists. Dense trace, `map_snap`, one call.
- `stops`: no geometry. Route the stops with `bus` costing and `break_through`
  locations to synthesise road geometry, then `edge_walk` that result to recover
  edges exactly. Two calls. Falls back to `map_snap` if `edge_walk` refuses on a
  chunk-stitch discontinuity.

Confidence from the `stops` path is deliberately reported as 0.0, not 1.0: it is a
guess about which roads the bus takes, not an observation of it, and `edge_walk`
returns 1.0 by construction.

**Storage is one DuckDB file** (`work/wayfare.duckdb`). DuckDB rather than SQLite
because the central operation is a group-by over a 5 GB CSV, done out of core. The
minimal-dependency discipline that governs `ontime` does not apply here — this is
an offline batch pipeline, not a 62 MB container.

## Constraints

**The match stage must survive interruption.** It runs for days on a server that
may reboot. Work is selected by the *absence* of a `match_status` row. This means
one batch is both the unit of concurrency and the unit of checkpointing, and they
cannot be separated: a batch still in flight is still selectable, so loading the
next batch before committing the last hands the same patterns out twice. This was
a real bug, caught in testing. Do not reintroduce pipelining across batch
boundaries without adding an in-flight exclusion.

**Failures are recorded, not retried.** A pattern whose stops cannot be connected
by road will never succeed. A matcher that retries it on every restart never
finishes. Every outcome gets a row, including `no_route`, `error` and `skipped`.

**Bad geometry is worse than missing geometry.** A wrong match produces a
confident-looking line down a road no bus uses. `low_confidence` rows are kept (so
they are never retried) but their edges are dropped.

**The detour check needs both a ratio and an absolute slack.** On a short pattern
a ratio alone is meaningless — a one-way system that sends the bus around one
block triples a 300 m span. Both `MAX_DETOUR_RATIO` and `DETOUR_SLACK_M` must be
exceeded.

**GTFS ids stay strings.** Route "07" must not become 7, or the join silently
loses every service with a leading zero. Hence `all_varchar=true` on every
`read_csv`.

**DuckDB takes a single writer.** Match workers do HTTP only; the main thread
writes.

**Tile attributes are per-feature per-zoom.** A full service list on every central
London edge would dominate tile size. Edges carry a capped `refs` list plus the
true count `n`; the overflow goes to a sidecar the viewer fetches on demand.

## Standards

- Python 3.12, ruff at line-length 92, mypy strict on `wayfare`.
- `.venv` via uv. `.venv/bin/python -m pytest -q`, `-m ruff check .`, `-m mypy`.
- Tests must not need the real datasets. `tests/conftest.py` holds a mini GTFS
  feed; mark anything needing real data `slow` or `valhalla`.
- Comment the non-obvious and leave the obvious alone.
- Use `wayfare.db.row` / `db.scalar` rather than `.fetchone()[0]`.

## Current state

Scaffold complete and green: 39 tests pass, ruff and mypy clean. Nothing has been
downloaded and no real data has been through the pipeline yet. See PLAN.md.
