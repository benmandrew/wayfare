# wayfare

Dataset of public transport routes across these islands: Great Britain from
DfT BODS, the Republic of Ireland from the National Transport Authority, and
Northern Ireland from Translink through OpenDataNI. Bus and coach are snapped
to the road network; tram, metro, rail and ferry are drawn from the operator's
own shape. Two consumers: an interactive web map (hover a road, see which
services use it) and artistic renderings of areas.

## Where the detail lives

This file holds only what changes how code gets written. The measurements and the
reasoning behind each decision are in `docs/`, and are worth reading before touching
the area they cover — most of them record something that was a bug first.

- `docs/data.md` — the feeds, their sizes and traps, mode filtering, coverage gaps,
  attribution.
- `docs/pipeline.md` — the five stages, storage, DuckDB lessons, clustering, tiles.
- `docs/rendering.md` — `art`: the style/spec split, streaming, banding, coalescing,
  where a render's time actually goes, the `/art` endpoint and the studio page.
- `docs/results.md` — measured runs: Wales, Greater London, Great Britain, feed churn.
- `docs/deploy.md` — the scheduled refresh: one systemd unit over Compose, why the
  publish gate is two counts, and what must never run unattended.
- `PLAN.md` — what is done, what is next, known gaps.

## Architecture

Five stages, each reading what the last wrote, each independently re-runnable:

    acquire  -> raw downloads
    patterns -> 1.55M trips collapse to distinct ordered stop sequences
    match    -> Valhalla; the stage that runs for a day or two
    aggregate-> invert pattern->edges into edge->services
    publish  -> GeoJSONL -> tippecanoe -> PMTiles

Plus `art`, which draws a window of the road network to PNG or SVG, from the command
line or over HTTP through `wayfare serve`.

**Storage is one DuckDB file** (`work/wayfare.duckdb`), because the central operation
is a group-by over a 5 GB CSV, done out of core. Geometry is integer micro-degrees,
not WKT.

**A pattern is the unit of work.** Grouping trips by `(route_id, direction, ordered
stop sequence)` is what makes national scale affordable — most trips are the same
physical journey repeated through the day.

**Map matching is the primary path, not a fallback.** Only 48.3% of trips carry a
`shape_id`, and the split is per-operator and all-or-nothing. Valhalla is the only
engine that returns OSM way ids without a custom graph build, and only in its native
response — `format=osrm` silently drops them.

**A render is a style and a query spec, and they know nothing about each other.** The
style decides how an edge is painted; `art.QuerySpec` decides which edges exist, what
their weight means, and what a group is. The spec is a closed vocabulary of SQL
fragments, never a query language: `/art` runs on the server, and DuckDB's `read_only`
does not stop `read_csv` reading arbitrary files.

## Rules

Break one of these and the failure is usually silent.

**`edge_id` is a Valhalla GraphId, stable only within one graph build.** `way_id` is
the durable identity; keep it. A graph rebuild is a full re-match, guarded by
`match.pin_graph`.

**`pattern_id` is an identity hash, not a rank.** Nothing that varies between feeds
may enter it — not trip counts, not shape ids, not operator, or the `match_status`
cache misses.

**`match_status` is a permanent cache, and failures are recorded rather than
retried.** Every outcome gets a row, including `no_route`, `error` and `skipped`. A
matcher that retries the impossible never finishes — which needs "failed" to mean
"impossible": a refused connection is `transport_error`, the one retryable status,
and Valhalla's `error_code` rather than its English is what tells a no-path apart
from a fault.

**A batch is both the unit of concurrency and the unit of checkpointing.** Work is
selected by the *absence* of a `match_status` row, so a batch still in flight is still
selectable. Do not pipeline across batch boundaries without an in-flight exclusion.

**Every consumer of `patterns` filters on `db.current_feed()`**, so departed patterns
are never matched, aggregated or rendered.

**The mode selection lives in `meta.modes`, not in the invocation.** `patterns` rebuilds
the table from whatever selection it is handed and `deploy/refresh.sh` hands it none, so
it defaults to `gtfs.stored_modes`, and narrowing the selection retires the deselected
patterns because rebuilding against the feed already on disk leaves them live. Every
number in the coverage funnel counts *matchable* patterns only: a tram never gets a
`match_status` row, so counting it as unmatched puts a permanent floor under
`patterns_pending`, which is half of what the scheduled refresh gates a publish on.
`patterns_by_mode` is where the other modes are counted.

**Migrations rewrite in place; they never re-run the pipeline.** A national match run
costs a day or two, so a schema change it cannot survive is one nobody applies.

**GTFS ids stay strings.** Route "07" must not become 7. Hence `all_varchar=true` on
every `read_csv`.

**DuckDB takes a single writer**, and a connection holds one result at a time — a
second query abandons the first silently, mid-stream, and the truncated result looks
complete.

**Never add a row-at-a-time insert loop on a table that grows with the network.**
executemany is ~2,700 rows/s; staging to a file and reading it back is 1.6M/s.

**What a low zoom holds is chosen before tippecanoe sees it, and it is chosen
per place.** `--drop-densest-as-needed` picks by density, so it thins cities and
spares a rural road carrying two buses a week; `publish` therefore holds the quietest
roads back from the overview bands itself, and a region under the caps is not
filtered at all. **The `trips` floor is per cell and never national, and a cell's
quota goes as `size ** OVERVIEW_WEIGHT`, never as `size`.** Both of those have been
live and both drew cities in a black field. One national floor emptied 310 of 655
populated cells outright. Giving every cell the same *fraction* empties nothing and
looks the same on the map, because the fraction is the wrong thing to hold constant:
at 24% the countryside drew 15 features a cell at z6 where Ireland, which is under
every cap and so is filtered not at all, drew 53. At `OVERVIEW_WEIGHT = 0.5` the
countryside keeps everything it has and the cities pay 23.7% -> 21.2% for it.
**Ireland is the control, so measure against it**: its retention is flat across the
country, and a weighting that tilts is the bug either way round. **Feature counts and
cell presence cannot see this** — only decoding the tile geometry back to lon/lat and
counting drawn features per cell per zoom can, which is what `wayfare coverage` does.
Run it on the archive a publish just wrote, and read the tilt. Three silent failures
guard the rest: tippecanoe applies
`-x` before `-j`, so a filter naming an excluded attribute matches nothing and writes
an empty band; `--extend-zooms-if-still-dropping` treats `-z` as a ceiling it may
raise, which overlaps the next band and has `tile-join` merge both copies of every
road; and a longitude near the prime meridian is written `-1.1e-05`, which a number
pattern without an exponent skips without a word. Counting features per zoom, and per
cell, in the finished archive is what catches any of them.

**Nothing holds a whole window or a whole table.** `art` streams its window and
`publish.export_geojsonl` streams by `way_id`. Anything statistical — weight
percentiles, group stats — must read the *unsampled*, *unbanded* window, and only
drawn geometry may be thinned.

**Every ORDER BY needs a unique tiebreak, including the ones whose order looks
irrelevant.** DuckDB's parallel hash join returns rows in a varying order, and a
commutative compositing operator (ADD, SCREEN) hides a missing tiebreak from every
check that looks at pixels. Test that the order is *defined*, not that two runs agree.

**Bad geometry is worse than missing geometry.** A wrong match draws a
confident-looking line down a road no bus uses. `low_confidence` rows are kept so they
are never retried, but their edges are dropped.

**A licence condition travels with the data, not with the page.** `publish` stamps the
credit into the archive's own tileset metadata, derived from `config.Feed`, so a copied
archive keeps it and the viewer, which loads every archive it is offered onto one
map, shows all of their credits together in the one control.
Every archive owes the timetable's publisher, and owes OpenStreetMap under ODbL only
where a route was matched onto its ways, which `publish.contents` reads off the
database. `pmtiles.Protocol` needs `{ metadata: true }` or MapLibre never sees
any of it, which looks like a viewer crediting only its basemap rather than an error.
Every render stamps the same credit into its own PNG or SVG metadata, unconditionally,
and nothing that varies between two renders of one request may join it there — no
timestamp, no path, no version, or the byte-identical tests are a fiction.

## Standards

- Python 3.12, ruff at line-length 92, mypy strict on `wayfare`. `ruff format` owns the
  layout and `nixfmt` owns `flake.nix`; both are enforced, so do not hand-tune spacing
  back. `.github/workflows/check.yml` runs format, lint, types and tests in this same
  devShell on every push and pull request.
- The dev environment is the nix flake and nothing else. direnv enters it (`.envrc` is
  `use flake` plus `dotenv_if_exists .env`, the same file Compose reads); `nix develop`
  is the same shell without direnv. It supplies Python 3.12, uv, cairo, pkg-config,
  felt/tippecanoe and the duckdb CLI, and its hook builds `.venv` with uv, re-syncing
  when `pyproject.toml` or the nixpkgs Python moves. Dependencies stay in
  `pyproject.toml`, never in the flake.
- `.venv/bin` is on `PATH` in the shell, so the commands are bare: `pytest -q`,
  `ruff check .`, `mypy`. Outside the shell they are not on `PATH` at all — get into it
  rather than reaching for a system Python or a hand-made venv.
- Tools that must be found by hand outside the shell: `tippecanoe` for `publish`, cairo
  for `art`. That is what the flake exists to stop.
- `LD_LIBRARY_PATH` in the flake is load-bearing: duckdb's manylinux wheel wants a distro
  `libstdc++`, and the nix interpreter has none. It fails at `import duckdb`, not install.
- `pycairo`, `numpy` and `pyarrow` are the `art` extra and only that — the pipeline and
  the tile server do not import them.
- Tests must not need the real datasets. `tests/conftest.py` holds a mini GTFS feed;
  mark anything needing real data `slow` or `valhalla`.
- Comment the non-obvious and leave the obvious alone.
- Use `wayfare.db.row` / `db.scalar` rather than `.fetchone()[0]`.

## Current state

**Great Britain is complete end to end**, on the server, feed `20260807_022616`: 52,554
patterns, 95.9% matched, 2,746,261 edges, 129.5 MB PMTiles. Wales and Greater London were
the two rehearsals for it and both stand. 546 tests pass, ruff and mypy clean.

**Both parts of Ireland are complete end to end**, on the server, against one shared
Valhalla graph `3.8.3/1786309727` built from the 409 MB island extract. The Republic on
feed `20260808_b375dfac`: 2,853 patterns, 95.4% matched, 352,945 edges, 16.4 MB PMTiles.
Northern Ireland on `20260806_140751`: 2,071 patterns, 99.5% matched, 121,384 edges, 6.1
MB PMTiles. One data root per region, not one shared: `meta.feed_version` is single-valued,
so a second region acquired into the first's database becomes the current feed and the next
`publish` overwrites the first region's archive.

**All three were republished on 2026-08-10 and every one of them now carries its credit**,
which closes the CC BY 4.0 and OGL v3.0 breach that serving the earlier archives was. The
same republish carried the `far` band, which then took three more goes to get right —
a national `trips` floor emptied 310 of 655 cells, and a per-cell floor sharing the cap out
*in proportion to* cell size emptied none while still drawing 15 features per rural cell
where Ireland drew 53.

**Great Britain was republished on 2026-08-11 at `OVERVIEW_WEIGHT = 0.5` and four zoom
bands** (z5-z7 capped at 190,000, z8-z9 at 450,000, z10 and z11-z14 uncapped), 126.6 MB,
credit intact. No band reports a single tile over tippecanoe's size limit, so nothing in
the archive is chosen by density any more. Measured with `wayfare coverage`: the emptiest
quarter of cells draws 36 features at z5, 41 at z6 and 52 at z8 against the previous
build's 15 at z6, cells drawing under five fell from 28 to 8, and z10 carries its full
943,040 features. Ireland rebuilt feature-for-feature identical at every zoom, so both it
and Northern Ireland were left alone. Nothing was re-matched. The archives from before
2026-08-10 are on the server at `/home/ben/archive-backup-20260810/`.

**Great Britain's database is pre-multi-modal** — no `patterns.mode`, no `segments`, no
`modes` in `meta` — because `connect` migrates only when it is not read-only and nothing
has opened it for writing since. `status` and `publish` both connect read-only, and both
failed against it until `db.matchable` was given a connection and `publish` learnt to read
a missing table as an empty one. Its archive therefore carries no `segments` layer and a
road-only credit, both correct for what the database holds. Re-running `patterns` would
migrate it and give Great Britain its trams, rail and ferries.

Feed churn is measured: two Wales feeds two days apart took 3,584 patterns to 3,541 — 30
new, 73 departed, about 8 seconds of matching to catch up against 16m23s for the full run.
That is a two-day delta on one region, so the linear scaling to ~12.8% a month is an upper
bound and not the national figure. Full figures in `docs/results.md`, roadmap in `PLAN.md`.
