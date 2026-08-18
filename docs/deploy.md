# Deploy

A deployment is one Docker Compose project holding a routing graph and the services that use it. `valhalla` builds and serves the graph from its own named volume; `wayfare`, `pipeline`, `matcher` and `web` share the `data` volume, which the routing container does not mount. A refresh replaces the timetable in that volume and republishes the tileset, on a schedule, with nobody watching.

[`deploy/refresh.sh`](../deploy/refresh.sh) is the stage sequence and the whole of what this repository owns. The scheduler around it is a cron entry in a separate Ansible repository, described below.

## What the Compose project contains

`valhalla` runs with `force_rebuild` off, so an existing graph is never rebuilt under a database that stores its edge ids. Its healthcheck's `start_period` is the window a first graph build gets, and the three stage services wait on `condition: service_healthy`, so nothing opens against a half-built graph.

`web` opts out of that dependency, because serving an archive that is already built needs no routing engine, and gating it on the graph would take the viewer down for the whole of a rebuild. `pipeline`, `wayfare` and `matcher` sit behind the `manual` profile and never start with `docker compose up`.

## What a refresh is

The stage that dominates a refresh costs the churn and nothing else. `match` selects work by the *absence* of a `match_status` row, and that table is keyed on pattern identity rather than on a run. A feed whose *patterns* — the ordered stop sequences that are the unit of work — are mostly unchanged therefore costs only the ones that are new.

*Churn*, the count of patterns that appear and depart between two feeds, sets what the day-or-two stage costs. `match` and `trace` are the two incremental stages and every other stage is a full pass every run, so a refresh costs that fixed floor plus the churn.

## One chained unit, not several

A *drain*, one run of `match` to an empty queue, always runs the queue to empty, so there is no partial run for a later invocation to pick up. The whole refresh is therefore one chained unit rather than three timers, since separate acquire, drain and publish schedules would buy a re-entry the drain cannot use.

`acquire` is forced because it otherwise skips a file it already has, and `patterns` takes no flags because the mode selection comes out of the database.

`prune` sits between `aggregate` and `cluster` because both neighbours decide the slot. `aggregate` rebuilds `segments` out of `patterns` and `shapes`, so pruning first would take away the source of a non-road pattern's geometry. `cluster` is the only thing that gives space back, since a DELETE leaves DuckDB's high-water mark where it is and `cluster` copies the database into a fresh file. `cluster` in turn runs before `publish`, because clustering goes stale rather than off.

## The publish gate is two counts

The gate is `patterns_pending` and `by_status.transport_error`, both read out of `wayfare status`. [`refresh.sh`](../deploy/refresh.sh) exits non-zero without publishing if either is, and that exit code is what tells a person to look.

One count is not enough. `patterns_pending` counts patterns with no `match_status` row at all, and a `transport_error` is a row. A Valhalla outage part-way through a run leaves those patterns unmatched but not pending, so the run reaches the end reporting zero pending. A gate reading that one number would publish a tileset missing every road that failed, and missing road does not read as an incomplete run — it reads as a region that lost its buses.

`--retry transient` at the top of the run is what gives those patterns another attempt, clearing the previous run's `transport_error` rows and putting them back in the queue. Every other failure status means impossible, and a matcher that retries the impossible never finishes.

Both counts are mode-aware, because a tram never gets a `match_status` row and its geometry comes from the operator's own shapes instead. Counting non-road patterns as unmatched put a permanent floor under `patterns_pending`, which would have stopped a multi-modal region publishing for ever while reporting a drain that never finished.

Every number in that funnel counts *matchable* patterns only. `patterns_by_mode` counts live patterns per mode beside it, and that census is the only place a mode going missing is visible.

## The three Overpass stages run after the gate

`trace` draws the modes with no road under them and no operator geometry, `snap` gives the rail an operator does publish a shape for the way ids that shape does not carry, and `routes` draws the modes with no timetable at all. All three depend on Overpass, a third party's metered public service, so each is followed by `|| echo` and the run carries on. A pattern none of them draws keeps no status row, so the next refresh selects it again unchanged.

The three are separate lines rather than a chain, because they issue different queries and one being refused says nothing about the others. `snap` gets no `--retry`: a request that never arrived writes no row at all, and `partial_cover` means the track is not mapped, which clearing weekly would re-ask a question OpenStreetMap has not changed its answer to.

All three read `WAYFARE_REGION` for the window they ask Overpass for, and `routes` reads it again to decide which operators' relations it keeps. A run against the wrong data root therefore draws another region's rail into this one's archive.

Their failures stay out of the gate deliberately. Folding an unresolvable relation into `patterns_pending` would put a permanent floor under the count the gate reads, which is the mistake the mode filter already made once. `wayfare status` reports a separate block instead.

All three must run after `patterns`, which sets the feed version their rows are stamped with. A relation written against the previous version is departed the moment the new feed lands, so placed first the stage would draw the country's rail for exactly one refresh and then silently stop.

No stage re-queries Overpass on a schedule, and the geometry ages because of it. Each reads the cached response body where one exists, so deleting those files is what re-queries; the external Ansible role does that on a second stamp.

## The mode selection lives in the database

`build_patterns` writes the selection to `meta.modes` and defaults to whatever the region was last built with, so a multi-modal region is scheduled exactly as a road-only one is. An unrecognised stored name raises, because renaming a mode should break the schedule and be dealt with by a person.

Narrowing the selection by hand retires the deselected patterns. A pattern otherwise leaves only by not being seen in a *newer* feed, so rebuilding against the feed already on disk would leave them live and published again.

## Running the script

`REFRESH_SERVICE` names the Compose service the stages run as. It exists because a Compose project is a Valhalla graph rather than a region: the Republic and Northern Ireland match against one island graph, so they are two services and two data roots inside one project, and refreshing both means invoking the script twice with the service changed.

One data root still holds one region. `meta.feed_version` is single-valued, so a second region acquired into the first's data root becomes the current feed and the next `publish` overwrites the first region's archive.

## How it is scheduled

The schedule lives in `roles/wayfare` of a separate Ansible repository, which cannot be checked from this tree, and that role is the authority on its own behaviour. The host carries no checkout of this repository, so the role vendors [`deploy/refresh.sh`](../deploy/refresh.sh) in unmodified.

Cron rather than a systemd *timer*, because every scheduled job in that repository is a crontab entry. Four things the reference unit provides are provided again:

- `Type=oneshot` becomes `flock -n` on the cron line, so a tick landing on a running drain is a no-op rather than a second writer.
- `Persistent=true` becomes a stamp file whose age decides whether a tick does anything, so a host that was down delays a refresh by a night.
- `OnFailure=` becomes a line in the host's service log.
- `RandomizedDelaySec` is dropped. It keeps a fleet off the feed's publisher at one instant, and this is one host.

The stamp is written before the run, not after. A failure then costs a full interval instead of retrying nightly against a feed that is still broken.

Tearing the stack down is the wrapper's job, from an `EXIT` trap, so the failure and interrupt paths release the memory too and the graph survives in its own volume. Concurrency on that host is set below the Compose defaults, because more workers put Valhalla at its ceiling and reclaim evicted the memory-mapped graph tiles.

## Traps

`--force-graph` must never appear in a scheduled run. It overrides `match.pin_graph`, the check that stops edge ids from one Valhalla graph build mixing with edge ids from another. Attended, that override is a decision; unattended, the failure is silent, renders fine and is wrong, and costs a full re-match to undo.

Mutual exclusion is load-bearing, and cron gives none of it for free. DuckDB takes a single writer, and a drain runs for hours, so a nightly tick will routinely land on one still running. `flock -n` makes that tick a no-op; without `-n` it would queue and start a second writer the moment the first finished. Installing the reference timer alongside the cron entry does the same damage, and neither side checks for it.

`publish` needs `--name-by-region` on a deployed root, and does not always refuse without it. On a fresh data root a bare `publish` writes an archive under a name nothing here serves, and succeeds.

Failure has to propagate, hence `set -e`. The Bus Open Data Service (BODS) answers an outage with HTTP 200 and an HTML error page, and the byte floor in `acquire` is what turns that body into a non-zero exit. Without the exit propagating, `patterns` runs next against the feed already on disk, recounts it, and reports a churn of zero — a refresh that looks like a quiet month.

A republish is atomic and has to stay that way. `web` serves the output directory from the same volume `publish` writes to, so writing the final archive directly left minutes in which a client reading byte ranges could span two archives.

Northern Ireland is not deployed the way `REFRESH_SERVICE` above describes. That variable is what the script supports; what the server runs is `docker run` by hand against the Ireland project's network, so its data root is host state that no committed file reproduces.

Departed patterns are never evicted. `prune_shapes` drops operator geometry only, and nothing removes a departed pattern's `pattern_edges` rows, so that table grows monotonically however long a database is kept current. Keeping the `match_status` rows is deliberate, so a seasonal service that returns is free; no policy decides when the edges should go.

## Serving

`web` serves the viewer and every archive in one directory, and the viewer draws all of them onto one map. Nothing in this path caches, so every millisecond in it is one the browser pays on every tile it fetches.

[`wayfare/server.py`](../wayfare/server.py) sets `protocol_version = "HTTP/1.1"` for keep-alive and disables *Nagle's algorithm* to make it cheap. Without the second, each response body waited on the kernel's acknowledgement of its headers.

## Cadence

Two schedules exist and they do not agree. [`deploy/wayfare-refresh.timer`](../deploy/wayfare-refresh.timer) carries `OnCalendar=monthly`, which is a ceiling to move in from rather than a recommendation. What runs in production is the Ansible cron: the committed unit is the reference and the role is the schedule.

Shortening the interval buys a smaller drain and pays for the full passes proportionally more often. More runs a month is also more departed rows arriving in a month, so the size of the DuckDB file is the thing to watch on a shortened interval.

The honest way to pick a period is to watch what `patterns` logs across a few runs, and set the interval against the churn observed rather than against a calendar word. The cost of a refresh is its timetable's instability rather than its size, and nothing in the pipeline knows that number until a schedule has been running long enough to reveal it.
