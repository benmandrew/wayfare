# wayfare

Dataset of public transport routes across these islands: Great Britain from the Department for Transport's Bus Open Data Service (BODS), the Republic of Ireland from the National Transport Authority, and Northern Ireland from Translink through OpenDataNI. Bus and coach are snapped to the road network; tram, metro, rail and ferry are drawn from the operator's own shape, or from an OpenStreetMap (OSM) *route relation* where the operator publishes none. Two consumers: an interactive web map (hover a road, see which services use it) and artistic renderings of areas.

## Where the detail lives

This file holds only what changes how code gets written. The reasoning behind each decision is in `docs/`, and is worth reading before touching the area it covers.

- [`docs/pipeline.md`](docs/pipeline.md) — the stages, storage, clustering, tiles.
- [`docs/data.md`](docs/data.md) — the feeds, their traps, mode filtering, coverage gaps, attribution.
- [`docs/deploy.md`](docs/deploy.md) — the scheduled refresh, and why the publish gate is two counts.

## Architecture

Nine stages, each reading what the last wrote, each independently re-runnable:

    acquire   -> raw downloads
    patterns  -> trips collapse to distinct ordered stop sequences
    match     -> Valhalla; the stage that runs for a day or two
    trace     -> cuts a timetabled pattern's geometry out of an OSM route relation
    snap      -> gives an operator's rail shape the OSM way ids it does not carry
    routes    -> builds services from OSM route relations, for modes with no timetable
    aggregate -> invert pattern->edges into edge->services
    publish   -> GeoJSONL -> tippecanoe -> PMTiles
    art       -> draws a window of the road network to PNG or SVG

The `routes` stage lives in [`wayfare/osmroutes.py`](wayfare/osmroutes.py), because `routes` is already the General Transit Feed Specification (GTFS) table name. Prose and the command line both call the stage `routes`; only the module is `osmroutes`.

**A pattern is the unit of work.** Grouping trips by `(route_id, direction, ordered stop sequence)` is what makes national scale affordable, since most trips are the same physical journey repeated through the day.

**Map matching is the primary path.** Only 48.3% of trips carry a `shape_id`, and the split is per-operator and all-or-nothing. Valhalla is the only engine that returns OSM way ids without a custom graph build, and only in its native response — `format=osrm` silently drops them.

**Storage is one DuckDB file** (`work/wayfare.duckdb`), because the central operation is a group-by over a 5 GB CSV, done out of core. Geometry is integer micro-degrees, not WKT.

**A render is a style and a query spec, and they know nothing about each other.** The style decides how an edge is painted; `art.QuerySpec` decides which edges exist, what their weight means, and what a group is. The spec is a closed vocabulary of SQL fragments, never a query language: `/art` runs on the server, and DuckDB's `read_only` does not stop `read_csv` reading arbitrary files.

**[`wayfare/map.toml`](wayfare/map.toml) holds every value that has to be identical on both sides of the Python/JavaScript boundary** — the three tile layer names, every colour, the archive name a page falls back to on a static host, the detail-band pair, and the box the viewer will not pan outside of, which `art.ISLES` reads too. What belongs there is a value that crosses the language boundary rather than configuration in general. The browser cannot read TOML, so `scripts/palette_js.py` generates `web/palette.js` from the same file and CI checks it with `--check` on every push. The *OKLab* derivation that turns a mode's seed colour into a six-step ramp is in `wayfare/palette.py` alone, and the page reads finished arrays and computes no colour.

## Rules

Break one of these and the failure is usually silent.

**`edge_id` is a Valhalla GraphId, stable only within one graph build.** `way_id` is the durable identity; keep it. A graph rebuild is a full re-match, guarded by `match.pin_graph`.

**`pattern_id` is an identity hash, not a rank.** Nothing that varies between feeds may enter it — not trip counts, not shape ids, not operator, or the `match_status` cache misses.

**`match_status` is a permanent cache, and failures are recorded rather than retried,** because a matcher that retries the impossible never finishes. That needs "failed" to mean "impossible": a refused connection is `transport_error`, the one retryable status, and Valhalla's numeric `error_code` rather than its English is what tells a no-path apart from a fault.

**A batch is both the unit of concurrency and the unit of checkpointing.** Work is selected by the *absence* of a `match_status` row, so a batch still in flight is still selectable. Do not pipeline across batch boundaries without an in-flight exclusion.

**Every consumer of `patterns` filters on `db.current_feed()`**, so departed patterns are never matched, aggregated or rendered.

**A stop's coordinates being valid does not make it British.** BODS carries international coach, so live stops of the national feed stand between Calais and Warsaw at coordinates that are entirely correct. `config.british_isles_sql` is the boundary, and `patterns` drops the whole pattern rather than the stop. Anything that sizes a window off stop coordinates clips to it as well, because the way this fails is an Overpass query for every railway between Ireland and Poland, which dies with no traceback. The boundary is a box on three sides and a capped line through the Channel on the fourth, since Calais is east of Dover and Brittany is west of Cornwall, so no single line separates both.

**The mode selection lives in `meta.modes`, not in the invocation.** `patterns` rebuilds the table from whatever selection it is handed, and [`deploy/refresh.sh`](deploy/refresh.sh) hands it none. Narrowing the selection retires the deselected patterns, because rebuilding against the feed already on disk leaves them live. Every number in the coverage funnel counts *matchable* patterns only: a tram never gets a `match_status` row, so counting it as unmatched puts a permanent floor under `patterns_pending`, which is half of what the scheduled refresh gates a publish on. `patterns_by_mode` is where the other modes are counted.

**Migrations rewrite in place; they never re-run the pipeline.** A national match run costs a day or two, so a schema change it cannot survive is one nobody applies.

**GTFS ids stay strings.** Route "07" must not become 7. Hence `all_varchar=true` on every GTFS `read_csv` ([`gtfs.py`](wayfare/gtfs.py)). Reads of our own files, such as [`match.py`](wayfare/match.py)'s, pass an explicit column list instead.

**DuckDB takes a single writer**, and a connection holds one result at a time: a second query abandons the first silently, mid-stream, and the truncated result looks complete.

**Never add a row-at-a-time insert loop on a table that grows with the network.** Staging to a file and reading it back is orders of magnitude faster than `executemany`.

**Bad geometry is worse than missing geometry.** A wrong match draws a confident-looking line down a road no bus uses. `low_confidence` rows are kept so they are never retried, but their edges are dropped.

**Nothing holds a whole window or a whole table.** `art` streams its window and `publish.export_edges_geojsonl` streams by `way_id`. Anything statistical — weight percentiles, group stats — must read the *unsampled*, *unbanded* window, and only drawn geometry may be thinned.

**Every ORDER BY needs a unique tiebreak, including the ones whose order looks irrelevant.** DuckDB's parallel hash join returns rows in a varying order, and a commutative compositing operator (ADD, SCREEN) hides a missing tiebreak from every check that looks at pixels. Test that the order is *defined*, not that two runs agree.

**What a low zoom holds is left to tippecanoe, and five attempts to take that decision away all made the map worse.** `OVERVIEW_CAP_FAR` and `OVERVIEW_CAP_MID` are both `None`; the quota machinery under them is switched off. Read the block in `config` before reviving any of it.

**Judge a low zoom by lit pixels, never by feature counts.** A cap keeps many short features spread over many cells; no cap keeps fewer, longer ones. Every count-based statistic rewards the first, and only the second is visible, so every round shipped on numbers that went up while the map got worse. `wayfare coverage` counts the same way and inherits the same blind spot. Draw the geometry and look at it. Three silent failures guard the rest: tippecanoe applies `-x` before `-j`, so a filter naming an excluded attribute matches nothing and writes an empty band; `--extend-zooms-if-still-dropping` treats `-z` as a ceiling it may raise, which overlaps the next band and has `tile-join` merge both copies of every road; and a longitude near the prime meridian is written `-1.1e-05`, which a number pattern without an exponent skips without a word.

**The detail band's feature id is the OSM way id, and the overview bands' is the Valhalla edge id.** So `way` is an attribute of no band and neither is `id`, and the viewer tells the two ranges apart by reading `refs`, which `_DETAIL_ONLY` strips from exactly the bands that carry the edge id. Put `way` back as an attribute, or strip `refs` from a band, and the viewer hovers in the wrong id space with no error to show for it. Both halves are one entry in `map.toml` — `bands.detail_only` is what `publish` strips by and `bands.sentinel` is what the viewer tests for — and loading it raises when the sentinel is not in the list, because a sentinel every band carries marks every feature as detail-band.

**The overview bands are built from a second export, and it can build nothing else.** `coalesce` keeps `way_id` in its key because the detail band spends the way id on its feature id, so every way boundary is a feature break the bands below `DETAIL_ZOOM` pay for and cannot show; `publish.merge_overview` joins across it wherever the attributes match, which moves no point and averages nothing. What fails silently is the other direction: that file has none of the info card's attributes, so handing it to the detail band publishes a region with no road names, no service lists and no feature ids at all. `build_tiles` writes the merged file into the publish scratch directory, never beside the export, so no later `--from-export` can pick it up by mistake.

**`trace` refuses a relation that does not chain, or stops that do not run in order along it.** It cuts a pattern's geometry out of a relation's own chain, so it snaps nothing and has no confidence score to fall back on. What it records against the pattern is the ways under that cut and not the ways of the line, because `aggregate` inverts that list per way, and the whole chain would say a short working runs the length of the line it is a part of. A break in the chain and a sequence that turns round partway both draw confident track no service runs, so both end as an outcome in `trace_status` and write nothing to `traces`. That table is a permanent cache like `match_status`, `transport_error` is its one retryable status, and trace failures never gate a publish.

**`routes` draws whatever rail its window admits, and a window is a box while a border is not.** Northern Ireland's stops reach Dublin, and the viewer draws every archive it is offered onto one map. `config.Feed.bounds` narrows the window per region, and `config.Feed.operators` refuses a relation whose `operator` names only another region's rail while leaving one that names nobody to the window, which is what keeps every BODS slug drawing what it always drew. A tag naming two regions goes to whichever name comes first inside it, so a jointly run service lands in exactly one archive. Bounds that never meet the region's live stops raise, because an empty box reports that nothing was discovered.

**`routes` is for a mode with no timetable behind it, and a region that publishes one draws every line twice.** `config.Feed.route_relations` is the gate — `None` takes `osmroutes.ROUTE_MODES`, `()` draws none. Read it with `is None` at every hop, because a selection that refuses everything is falsy and `or` hands it back the default it just refused. A region that draws nothing keeps its `operators` anyway, since that claim is what makes *another* region refuse the same relations. `osmroutes.write` retires on an empty run for the same reason it exists at all: a region that has stopped drawing relations is exactly the case whose last run has to be retired.

**`segments` and `track_services` partition on `traces.ways_cut`, and a pattern in both is drawn twice.** A trace cut to its own pattern is inverted per way; a trace holding the whole line's chain keeps its polyline, because inverting it would attribute a short working to track it never reaches. The column exists because nothing recoverable tells the two apart once the polyline is stored. A pattern sitting in both arms looks like a hover on a National Rail way answering with one relation's card rather than the way's service list. `mode` is in the track key, so a way carrying two networks is two features and is drawn twice on purpose.

**A `shape_id` no longer says which of the two draws a pattern.** `config.TRACE_OVER_SHAPE_MODES` names the modes fitted against OpenStreetMap even where the operator published a shape, and it holds `rail` alone: a shape carries no way ids, and way ids are the whole of what makes track shared. Tram is out and is the reason this is a set rather than a rule about shapes — street running and depot moves are in a tram's shape and in no route relation, so trading one for the other loses geometry that is correct. So `build_segments` tests for a cut trace rather than for a shape, which is also what makes the shape a fallback: a pattern neither stage resolves costs the sharing and never the line.

**`trace` and `snap` answer the same question from opposite ends, and `traces` holds one row per pattern.** `trace` fits a pattern to a route relation by station sequence, which needs the relation to list its calling points in order; `snap` takes the operator's shape as the evidence and OpenStreetMap as the identity alone, snapping each vertex to the track under it. The relation fit wins where both could answer, and a snapped trace is told from a fitted one by its NULL `relation_id`. `snap` asks Overpass for bare `railway=*` ways and not for relations, because relation members cover only the track somebody drew a route over, and it excludes `service=*` or a shape snaps onto a siding and reports a service running through a depot. Three guards keep it honest: `SNAP_MAX_M` is a margin and not a knob; a cover under `SNAP_MIN_COVER` is refused whole rather than trimmed, because attributing the half of a shape that found track reports a short working over a line the service runs the length of; and `SNAP_HOLD_M` bounds the anti-flapping hold against the *nearest* way rather than against the tolerance, since held to the tolerance a way that has already diverged keeps the shape for another 25 m. Its three writes are one transaction, since work is selected by the absence of a `snap_status` row and a status committed without its geometry is a pattern marked resolved that nothing will ever ask about again.

**A licence condition travels with the data, not with the page.** `publish` stamps the credit into the archive's own tileset metadata, derived from `config.Feed`, so a copied archive keeps it, and the viewer shows the credits of every archive it loads together in the one control. Every archive owes the timetable's publisher, and owes OpenStreetMap under the Open Database License (ODbL) only where a route was matched onto its ways or drawn as track, which `publish.contents` reads off the database. `pmtiles.Protocol` needs `{ metadata: true }` or MapLibre never sees any of it, which looks like a viewer crediting only its basemap rather than an error. Every render stamps the same credit into its own PNG or SVG metadata, and nothing that varies between two renders of one request may join it there — no timestamp, no path, no version, or the byte-identical tests are a fiction.

## Standards

- Python 3.12, ruff at line-length 92, mypy strict on `wayfare`. `ruff format` owns the layout, `nixfmt` owns [`flake.nix`](flake.nix) and `taplo` owns the TOML; all three are enforced, so do not hand-tune spacing back. [`.github/workflows/check.yml`](.github/workflows/check.yml) runs format, lint, types and tests in this same devShell on every push and pull request.
- `taplo lint` checks [`wayfare/map.toml`](wayfare/map.toml) against [`wayfare/map.schema.json`](wayfare/map.schema.json), wired up in [`.taplo.toml`](.taplo.toml), which also drives the editor extension. What JSON Schema cannot say — a relation between one value and another — is refused by `palette.load` instead.
- **`biome` is the linter for `web/`, and most of what it reads is inside `<script>` tags.** The two viewer pages hold around 3,000 lines of JavaScript that ruff and mypy never see. Biome parses every `.js` as a module, which is wrong for `util.js`, `credits.js` and `palette.js` — those are classic `<script src>` files whose top-level names *are* the pages' globals, so a shared function reads as dead and an explicit `"use strict"` reads as redundant. Both rules are off for those three and must stay off. CI passes `--error-on-warnings`, because Biome exits zero on warnings and a warning nothing fails on is a warning nobody fixes. Configured in [`biome.jsonc`](biome.jsonc).
- `actionlint` reads the workflows as workflows rather than as YAML, which matters because [`image.yml`](.github/workflows/image.yml) calls [`check.yml`](.github/workflows/check.yml): a `needs` naming a job that moved and an `if:` that parses as a truthy string both leave a green check on a step that never ran. `hadolint` covers the Dockerfile, and [`.hadolint.yaml`](.hadolint.yaml) turns off the version-pinning advisories and says why.
- `lychee` checks the markdown's links, and every one of them is a relative path into this repo, so it runs `--offline` and a green check never depends on a third party being up. What it is for is prose that points into source: a stale relative link reports nothing until somebody clicks it.
- The dev environment is the nix flake and nothing else. direnv enters it; `nix develop` is the same shell without direnv. It supplies Python 3.12, uv, cairo, pkg-config, felt/tippecanoe and the duckdb CLI, and its hook builds `.venv` with uv. Dependencies stay in [`pyproject.toml`](pyproject.toml), never in the flake.
- `.venv/bin` is on `PATH` in the shell, so the commands are bare: `pytest -q`, `ruff check .`, `mypy`. Outside the shell they are not on `PATH` at all, so get into it rather than reaching for a system Python or a hand-made venv.
- `LD_LIBRARY_PATH` in the flake is load-bearing: duckdb's manylinux wheel wants a distro `libstdc++`, and the nix interpreter has none. It fails at `import duckdb`, not install.
- `pycairo`, `numpy` and `pyarrow` are the `art` extra and only that — the pipeline and the tile server do not import them.
- Tests must not need the real datasets. [`tests/conftest.py`](tests/conftest.py) holds a mini GTFS feed; mark anything needing real data `slow` or `valhalla`.
- Comment the non-obvious and leave the obvious alone. A comment says why the code is as it is, never what it used to be or what changed.
- Use `wayfare.db.row` / `db.scalar` rather than `.fetchone()[0]`.

## Current state

Great Britain, the Republic of Ireland and Northern Ireland are each complete end to end and served.

The two Irish regions share one Valhalla graph built from the whole-island extract, so they share an edge id space, and a rebuild re-matches both.

One data root serves one region. `meta.feed_version` is single-valued, so a second region acquired into the first's database becomes the current feed, and the next `publish` overwrites the first region's archive.
