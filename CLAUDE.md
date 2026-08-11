# wayfare

Dataset of public transport routes across these islands: Great Britain from
DfT BODS, the Republic of Ireland from the National Transport Authority, and
Northern Ireland from Translink through OpenDataNI. Bus and coach are snapped
to the road network; tram, metro, rail and ferry are drawn from the operator's
own shape, or from an OSM route relation where the operator publishes none. Two
consumers: an interactive web map (hover a road, see which services use it) and
artistic renderings of areas.

## Where the detail lives

This file holds only what changes how code gets written. The measurements and the
reasoning behind each decision are in `docs/`, and are worth reading before touching
the area they cover — most of them record something that was a bug first.

- `docs/data.md` — the feeds, their sizes and traps, mode filtering, coverage gaps,
  attribution.
- `docs/pipeline.md` — the stages, storage, DuckDB lessons, clustering, tiles.
- `docs/rendering.md` — `art`: the style/spec split, streaming, banding, coalescing,
  where a render's time actually goes, the `/art` endpoint and the studio page.
- `docs/results.md` — measured runs: Wales, Greater London, Great Britain, feed churn.
- `docs/deploy.md` — the scheduled refresh: one systemd unit over Compose, why the
  publish gate is two counts, and what must never run unattended.
- `PLAN.md` — what is done, what is next, known gaps.

## Architecture

Six stages, each reading what the last wrote, each independently re-runnable:

    acquire  -> raw downloads
    patterns -> 1.55M trips collapse to distinct ordered stop sequences
    match    -> Valhalla; the stage that runs for a day or two
    trace    -> OSM route relations for the modes with no road and no shape
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

**What a low zoom holds is left to tippecanoe, and four attempts to take that
decision away all made the map worse.** `--drop-densest-as-needed` picks by density
rather than service level, which is a real fault — but it thins only the tiles that
will not fit, 18 of them at z5–z7, where every cap tried thinned the whole country to
spare those. `OVERVIEW_CAP_FAR` and `OVERVIEW_CAP_MID` are both `None`; the quota
machinery under them is switched off and kept only because z5 is still thinner than
Ireland. Read the block in `config` before reviving any of it.

**Judge a low zoom by lit pixels, never by feature counts.** This is the mistake
under all four attempts, and it is easy to make again. A cap keeps many short features
spread over many cells; no cap keeps fewer, longer ones. Features per zoom, populated
cells, features per cell and bins-holding-anything all reward the first, and only the
second is visible — so every round shipped on numbers that went up while the map got
worse. `wayfare coverage` counts the same way and inherits the same blind spot. Draw
the geometry and look at it. Three silent failures guard the rest: tippecanoe applies
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

**`trace` refuses a relation that does not chain, or stops that do not run in order
along it.** It cuts a pattern's geometry out of an OSM route relation's own chain, so
it snaps nothing and has no confidence score to fall back on. A break in the chain and
a sequence that turns round partway both draw confident track no service runs — a loop
takes the wrong branch — so both end as an outcome in `trace_status` and write nothing
to `traces`. That table is a permanent cache like `match_status`, `transport_error` is
its one retryable status, and trace failures never gate a publish.

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

**Great Britain was republished on 2026-08-11 with the overview uncapped**, 130.4 MB,
credit intact, carrying its non-road modes for the first time. It lights 6.3% of the
country at z6 against the capped build's 4.8%, and around London at z8 8.1% against
5.0%. Ireland was republished the same day at 18.1 MB with 371 rail and tram patterns;
Northern Ireland has no non-bus routes and was not touched. Nothing was re-matched. The
archive the capped build replaced is at `work/previous-great_britain.pmtiles`, and the
pre-2026-08-10 set at `/home/ben/archive-backup-20260810/`.

**Both regions' non-road modes are live**, drawn from operator geometry: 629 patterns
for Great Britain, 371 for Ireland. Great Britain's had been map-matched onto roads
before the mode filter existed — 1,726,822 `pattern_edges` for the Underground alone,
and 16,833 edges reachable from no bus at all — because `aggregate` filtered on the
live feed and never on `matchable`. Those are gone, which is why the edge count fell to
2,729,428.

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
