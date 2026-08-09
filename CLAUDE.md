# wayfare

UK-wide dataset of bus routes snapped to the road network, from DfT BODS open data.
Two consumers: an interactive web map (hover a road, see which buses use it) and
artistic renderings of areas.

## Where the detail lives

This file holds only what changes how code gets written. The measurements and the
reasoning behind each decision are in `docs/`, and are worth reading before touching
the area they cover — most of them record something that was a bug first.

- `docs/data.md` — the feeds, their sizes and traps, mode filtering, coverage gaps.
- `docs/pipeline.md` — the five stages, storage, DuckDB lessons, clustering, tiles.
- `docs/rendering.md` — `art`: the style/spec split, streaming, banding, coalescing,
  where a render's time actually goes, the `/art` endpoint and the studio page.
- `docs/results.md` — measured runs: Wales, Greater London, Great Britain.
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
matcher that retries the impossible never finishes.

**A batch is both the unit of concurrency and the unit of checkpointing.** Work is
selected by the *absence* of a `match_status` row, so a batch still in flight is still
selectable. Do not pipeline across batch boundaries without an in-flight exclusion.

**Every consumer of `patterns` filters on `db.current_feed()`**, so departed patterns
are never matched, aggregated or rendered.

**Migrations rewrite in place; they never re-run the pipeline.** A national match run
costs a day or two, so a schema change it cannot survive is one nobody applies.

**GTFS ids stay strings.** Route "07" must not become 7. Hence `all_varchar=true` on
every `read_csv`.

**DuckDB takes a single writer**, and a connection holds one result at a time — a
second query abandons the first silently, mid-stream, and the truncated result looks
complete.

**Never add a row-at-a-time insert loop on a table that grows with the network.**
executemany is ~2,700 rows/s; staging to a file and reading it back is 1.6M/s.

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
patterns, 95.9% matched, 2,746,261 edges, 130 MB PMTiles. Wales and Greater London were
the two rehearsals for it and both stand. 483 tests pass, ruff and mypy clean.

Feed churn — how many patterns are new month to month — is still the unmeasured number
that decides everything, and it is one `acquire` and one `patterns` away from being
known. Full figures in `docs/results.md`, roadmap in `PLAN.md`.
