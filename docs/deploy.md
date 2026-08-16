# Deploy

A deployment is one Docker Compose project holding a routing graph and the services that
use it. `valhalla` builds and serves the graph from its own named volume,
`valhalla:/custom_files`; `wayfare`, `pipeline`, `matcher` and `web` share the `data`
volume at `/data`, which the routing container does not mount. A refresh replaces the
timetable in that volume and republishes the tileset, on a schedule, with nobody watching.

[`deploy/refresh.sh`](../deploy/refresh.sh) is the stage sequence and the whole of what this
repository owns. The scheduler around it is a cron entry in a separate Ansible repository,
described below. [`deploy/wayfare-refresh.service`](../deploy/wayfare-refresh.service) and
[`deploy/wayfare-refresh.timer`](../deploy/wayfare-refresh.timer) sit beside the script as a
reference for what any scheduler has to provide, and neither has ever been installed.

## What the Compose project contains

`valhalla` is `ghcr.io/valhalla/valhalla-scripted`, listening on 8002, with
`force_rebuild: "False"` so an existing graph is never rebuilt under a database that
stores its edge ids. Its healthcheck carries a 90 minute `start_period`, the window a
first graph build gets, and the three stage services wait on `condition: service_healthy`,
so nothing opens against a half-built graph. `web` opts out with `depends_on: []`, because
serving an archive that is already built needs no routing engine, and gating it on the
graph would take the viewer down for the 90 minutes of a rebuild.

`pipeline`, `wayfare` and `matcher` sit behind the `manual` profile and never start with
`docker compose up`. `pipeline` runs `wayfare all`, which is `acquire`, `patterns`,
`match`, the publish gate, `trace`, `routes`, `aggregate`, `prune`, `cluster` and `publish`
— the sequence in [`refresh.sh`](../deploy/refresh.sh), on the same two counts, with the two
Overpass stages tolerated failures. The one difference is `acquire`: the script forces it
and `all` does not, because an attended first run should not re-fetch a feed it already has.

`web` is the only service `up` starts. It runs
`serve --dir web --out /data/out --port 8099` and answers the HTTP range requests PMTiles
needs and `python -m http.server` does not.

## What a refresh is

The stage that dominates a refresh costs the churn and nothing else. `match` selects
work by the *absence* of a `match_status` row, and `match_status` is keyed on pattern
identity rather than on a run. A feed whose *patterns* — the ordered stop sequences
that are the unit of work — are mostly unchanged therefore costs only the ones that are
new. *Churn*, the count of patterns that appear and depart between two feeds, sets what
the day-or-two stage costs. `match` and `trace` are the two incremental stages; every
other stage is a full pass every run, so a refresh costs that fixed floor plus the
churn.

Measured on Wales, two feeds two days apart, 3,584 patterns became 3,541 — 30 new, 73
departed — and matching the new ones took ~8 s against 16m23s for the original full
run. Two days is not a month and one region is not the nation; the caveats sit with the
measurement in [docs/results.md](results.md).

## One chained unit, not several

The *drain* — one run of `match` to an empty queue — is unbounded on the deploy side. It is
bounded by the container's compute and by how much Valhalla will answer, not by
`--max-seconds`, which nothing in [`refresh.sh`](../deploy/refresh.sh) passes. A drain
always runs the queue to empty, so there is no partial run for a later invocation to pick
up.

So the whole refresh is one chained unit rather than three timers, since separate
acquire, drain and publish schedules would buy a re-entry the drain cannot use. The
sequence is `acquire --force`, `patterns`, `match --retry transient`, the publish gate,
`trace --retry transient`, `routes`, `aggregate`, `prune`, `cluster` and
`publish --name-by-region`, all under `set -euo pipefail`. A stage that fails stops the
run where it stands, except the two that ask Overpass. `acquire` is forced because it
otherwise skips a file it already has, and `patterns` takes no flags because the mode
selection comes out of the database.

`prune` sits between `aggregate` and `cluster` because both neighbours decide the slot.
`aggregate` rebuilds `segments` out of `patterns` and `shapes`, so pruning first would
take away the source of a non-road pattern's geometry. `cluster` is the only thing that
gives space back — a DELETE leaves DuckDB's high-water mark where it is, and `cluster`
copies the database into a fresh file — so pruning after it would reclaim nothing until
the following run. Wales measured 160 MB going to 114 MB compacted, and `patterns`
reloads the shapes from the feed next run. `cluster` in turn runs before `publish`,
because clustering goes stale rather than off, and the rows this run matched otherwise
land unsorted on the end of the table where no zonemap can help them.

## The publish gate is two counts

The gate is `patterns_pending` and `by_status.transport_error`, both read out of `wayfare
status` with `jq`. [`refresh.sh`](../deploy/refresh.sh) exits 1 without publishing if either
is non-zero, and that exit code is what a scheduler reports on and what tells a person to
look.

One count is not enough. `patterns_pending` counts patterns with no `match_status` row
at all, and a `transport_error` is a row. A Valhalla outage part-way through a run
leaves those patterns unmatched but not pending, so the run reaches the end reporting
`patterns_pending: 0`. A gate reading that one number would publish a tileset missing
every road that failed, and missing road does not read as an incomplete run — it reads
as a region that lost its buses.

`--retry transient` at the top of the run is what gives those patterns another attempt.
It clears the previous run's `transport_error` rows and puts them back in the queue.
Every other failure status means impossible, and a matcher that retries the impossible
never finishes.

Both counts are mode-aware, because a tram never gets a `match_status` row. `db.matchable`
keeps non-road patterns away from the matcher, and their geometry comes from the operator's
own General Transit Feed Specification (GTFS) shapes instead. Counting them as unmatched
put a floor under `patterns_pending` equal to the non-road pattern count, and on a
four-pattern test database `status.patterns_pending` returned 2 where
`match.pending_count` returned 0. A multi-modal region would have stopped publishing for
ever while reporting a drain that never finished.

Every number in that funnel counts *matchable* patterns only. `patterns_by_mode` counts
live patterns per mode, matchable or not, beside a `modes` field echoing the selection the
database was built with. The gate reads work still owed to the matcher, the mode census
sits beside it, and that census is the only place a mode going missing is visible.

## The three Overpass stages run after the gate

`wayfare trace --retry transient` draws the modes with no road under them and no operator
geometry: the Underground, the Docklands Light Railway (DLR), London Trams. `wayfare snap`
follows and gives the rail an operator *does* publish a shape for the way ids that shape
does not carry, which is what lets two services over one stretch share it. `wayfare routes`
runs last and draws the modes with no timetable at all — Great Britain's National Rail,
which the Bus Open Data Service (BODS) does not carry, and Northern Ireland's, which
Translink's four datasets do not. All three depend on Overpass, a third party's metered
public service, so each is followed by `|| echo` and the run carries on. A pattern none of
them draws keeps no status row, so the next refresh selects it again unchanged.

The three are separate lines rather than a chain, because they issue different queries and
one being refused says nothing about the others. `snap` gets no `--retry`: a request that
never arrived writes no row at all, and `partial_cover` means the track is not mapped, which
clearing weekly would re-ask a question OpenStreetMap has not changed its answer to.

`routes` reads `WAYFARE_REGION` to decide both the window it asks Overpass for and which
operators' relations it keeps, so a run against the wrong data root draws another region's
rail into this one's archive. The stage names the region it thinks it is in its own log
line.

Track arrives under the Open Database License (ODbL), and `config.credit_parts` adds the
OpenStreetMap credit when an archive holds matched road *or* track. An archive with
neither credits only the timetable's publisher.

Their failures stay out of the gate deliberately. Folding an unresolvable relation into
`patterns_pending` would put a permanent floor under the count the gate reads, which is
the mistake the mode filter already made once. `wayfare status` reports a separate
`traced` block instead: patterns owed, patterns pending, and a count per status.

No stage re-queries Overpass on a schedule, and the geometry ages because of it.
`osm.fetch` reads `raw/osm_relations.json` or `raw/osm_routes.json` where one exists, and
`osm.fetch_ways` reads `raw/osm_track.json`; only `--refresh` overrides that, which
[`refresh.sh`](../deploy/refresh.sh) never passes.
The national query cost 131 MB and 27 seconds once, and every run since has read the file,
so "next run draws it" covers a fit that failed rather than a relation the map has gained
since. Deleting the two files is what re-queries; the external Ansible role does that on a
second stamp, `wayfare_refresh_osm_days` old.

All three must run after `patterns`, which sets the feed version their rows are stamped
with. A relation written against the previous version is departed the moment the new feed
lands, so placed first the stage would draw the country's rail for exactly one refresh
and then silently stop. There is no `--cif`, so `trips` stays null and nothing in the
scheduled run waits on a Network Rail login.

## The mode selection lives in the database

[`refresh.sh`](../deploy/refresh.sh) runs `wayfare patterns` with no flags, so the selection
cannot live in the invocation. `build_patterns` writes it to `meta.modes` beside
`feed_version` and defaults to `gtfs.stored_modes` rather than to a constant, so a refresh
inherits whatever the region was last built with and a multi-modal region is scheduled
exactly as a road-only one is. An unrecognised stored name raises: renaming a mode should
break the schedule and be dealt with by a person.

Narrowing the selection by hand retires the deselected patterns, which
`gtfs._retire_unselected_modes` does after the merge. A pattern otherwise leaves only by
not being seen in a *newer* feed, so rebuilding against the feed already on disk would
leave them live and published again.

## Running the script

`./deploy/refresh.sh` refreshes the default service, and two environment variables change
what it does.

`REFRESH_SERVICE` names the Compose service the stages run as, defaulting to `wayfare`
(`deploy/refresh.sh:13-20, 32`). It exists because a Compose project is a Valhalla graph
rather than a region. Ireland and Northern Ireland match against one island graph, so they
are two services and two data roots inside one project, sharing one `valhalla` container,
and refreshing both means invoking the script twice with the service changed. Great
Britain has a graph to itself and never sets the variable. Whether a given scheduler
invokes the script more than once is a property of that scheduler.

`REFRESH_NO_FETCH=1` skips the download, for a retry after a failure past `acquire` where
`acquire --force` would otherwise re-fetch 1.28 GB for Great Britain to get a feed already
sitting in the volume.

One data root still holds one region. `meta.feed_version` is single-valued, so a second
region acquired into the first's data root becomes the current feed and the next `publish`
overwrites the first region's archive. A second region means a second service and a second
volume, never a second entry in the same one.

## How it is scheduled

The schedule lives in `roles/wayfare` of a separate Ansible repository, which cannot be
checked from this tree; the role is the authority on its own behaviour. The host carries no
checkout of this repository, so the role vendors [`deploy/refresh.sh`](../deploy/refresh.sh)
in unmodified and installs it at `<build home>/deploy/refresh.sh` — the path that makes the
script's own `cd "$(dirname …)/.."` land on the Ansible-rendered build Compose file. A
change here reaches the host when somebody re-vendors the file, and the role's header
carries the `diff` recipe that says whether it is current.

Cron rather than a systemd *timer*, because every scheduled job in that repository is a
crontab entry. Four things the reference unit provides are provided again:

- `Type=oneshot` becomes `flock -n` on the cron line, so a tick landing on a running
  drain is a no-op rather than a second writer.
- `Persistent=true` becomes a stamp file at `/var/lib/wayfare/last-attempt`. The line
  fires nightly at 03:17 and the stamp's age decides whether the tick does anything, so
  a host that was down delays a refresh by a night.
- `OnFailure=` becomes a line in `/var/log/services.log`, which Alloy ships to Loki.
- `RandomizedDelaySec=1h` is dropped. It keeps a fleet off BODS at one instant, and this
  is one host.

The stamp is written before the run, not after. A failure then costs a full interval
instead of retrying nightly against a feed that is still broken. Removing the stamp is
what lets the next nightly tick pick a failed run up early.

Tearing the stack down is the wrapper's job, and it is worth real memory.
[`refresh.sh`](../deploy/refresh.sh) leaves the containers up, and 19 hours after a finished
run on the deployed host `valhalla` still held 4.24 GiB of anonymous memory at 14.4 GB
resident, with the box 2.3 GB into swap. The wrapper runs `docker compose down` from an
`EXIT` trap, so the failure and interrupt paths release it too, and the graph survives in
its own volume.

Concurrency on that host is set below the Compose defaults: 2 match workers and 3
Valhalla threads, `WAYFARE_MEM` at 6 GB against 8 GB. 4 workers and 6 threads put
Valhalla at its 10 GB ceiling for a whole run — 1.66M reclaims, four OOM kills, and
throughput falling from 8.6/s to 0.13/s at 92% as reclaim evicted the memory-mapped graph
tiles. Every rate in [docs/results.md](results.md) is a ceiling on that box.

## Traps

`--force-graph` must never appear in a scheduled run. It overrides `match.pin_graph`,
the check that stops edge ids from one Valhalla graph build mixing with edge ids from
another. Attended, that override is a decision; unattended, the failure is silent,
renders fine and is wrong, and costs a full re-match to undo.

Mutual exclusion is load-bearing, and cron gives none of it for free. DuckDB takes a
single writer, and a drain runs for hours, so a nightly tick will routinely land on one
still running. `flock -n` makes that tick a no-op; without `-n` it would queue and start
a second writer the moment the first finished. Installing the reference timer alongside
the cron entry does the same damage, and neither side checks for it.

`publish` needs `--name-by-region` on a deployed root, and does not always refuse
without it. `publish.default_out` raises only when an archive already named after this
region sits in `OUT`; on a fresh data root a bare `publish` writes `bus.pmtiles` and
succeeds, which is a name nothing here serves. The flag was missing from this script
until 2026-08-13, so the republishes up to that date were done by hand.

Failure has to propagate, hence `set -e`. BODS answers an outage with HTTP 200 and an
HTML error page, and the `MIN_GTFS_BYTES` floor in `acquire` is what turns that body
into a non-zero exit before the structural check on the zip's members ever opens it.
Without the exit propagating, `patterns` runs next against the feed already on disk,
recounts it, and reports a churn of zero — a refresh that looks like a quiet month. It
happened on 2026-08-08 and the guard caught it.

A republish is atomic and has to stay that way. `build_tiles` builds every band and the
joined archive in a scratch subdirectory of the output, then moves the finished file into
place with `os.replace`. `web` serves `/data/out` from the same volume `publish` writes
to, so writing the final `.pmtiles` directly left minutes in which a client reading byte
ranges could span two archives, and the bands were globbed by `server.archives` and
offered to the viewer as though an overview band were another region.

Northern Ireland is not deployed the way `REFRESH_SERVICE` above describes. That variable
is what the script supports; what the server actually runs is `docker run` by hand against
the Ireland project's network, so Northern Ireland has no Compose service of its own and
its data root is host state that no committed file reproduces and Ansible does not know
about. Bringing it under the project is what closes the gap.

Departed patterns are never evicted. `prune_shapes` drops operator geometry only, and
nothing removes a departed pattern's `pattern_edges` rows, so that table grows
monotonically however long a database is kept current. Wales departed 73 of 3,584
patterns across a two-day feed gap, about 2%, and the edges of all 73 stayed. Keeping the
`match_status` rows is deliberate, so a seasonal service that returns is free; no policy
decides when the edges should go, and none has been written.

## Serving

`web` serves the viewer and every archive in `/data/out` from one directory, and the
viewer draws all of them onto one map. Nothing in this path caches, so every millisecond
in it is one the browser pays on every tile it fetches.

[`wayfare/server.py`](../wayfare/server.py) sets `protocol_version = "HTTP/1.1"` for
keep-alive and `disable_nagle_algorithm = True` to make it cheap. Without the second,
*Nagle's algorithm* held each response body until Linux acknowledged the headers 40 ms
later: on loopback inside the deployed container, keep-alive requests took 41 ms each
against 0.3 ms with `TCP_NODELAY` set.

The network and the disk were both measured on the deployment the viewer runs on, which
is reachable over a tailnet with no content delivery network (CDN) and no reverse proxy
between the browser and Python, and both were cleared: a relayed round trip cost 21.7 ms
against 20.1 ms direct, and 40 parallel ranges off the rotational drive took 343 ms cold
against 330 ms warm. Moving `out/` (146 MB) to an SSD is worth doing for the tail, where
worst-case reads under 16-way concurrency reach 155 ms, and it is safe because `publish`
creates its scratch directory inside `out.parent`, keeping `os.replace` on one filesystem.

## Cadence

Two schedules exist and they do not agree.
[`deploy/wayfare-refresh.timer`](../deploy/wayfare-refresh.timer) carries
`OnCalendar=monthly`, which is a ceiling to move in from rather than a recommendation: BODS
republishes far more often than that. What runs in production is the Ansible cron, at
`wayfare_refresh_interval_days: 7`. The committed unit is the reference; the role is the
schedule.

Shortening the interval buys a smaller drain and pays for the full passes four times as
often. In transfer that floor is the 1.28 GB feed and the 102 MB NaPTAN register, about
1.4 GB a week; the 2.16 GB OpenStreetMap extract is not in it, because `acquire` fetches a
pbf only under `--with-osm` and the Valhalla image pulls its own from `tile_urls`. Four
times the runs is also four times the departed rows arriving in a month, so the size of
the DuckDB file is the thing to watch on a shortened interval.

The honest way to pick a period is to watch what `patterns` logs — new, carried over still
unmatched, departed — across a few runs, and set the interval against the churn observed
rather than against a calendar word. The cost of a refresh is its timetable's instability
rather than its size, and nothing in the pipeline knows that number until a schedule has
been running long enough to reveal it.
