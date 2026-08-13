# Deploy

A deployed region is one Docker Compose project: `valhalla` holding the routing
graph, `web` serving the viewer and the tile archives, and one `data` volume that
both of them share. A refresh replaces the timetable in that volume and republishes
the tileset, on a schedule, with nobody watching. `deploy/refresh.sh` is the stage
sequence, and it is the whole of what this repository owns. The schedule around it
is cron under Ansible, described below. `wayfare-refresh.service` and
`wayfare-refresh.timer` sit beside the script as the reference for what any
scheduler has to provide, and neither has ever been installed.

## What a refresh is

**The stage that dominates a refresh costs the churn and nothing else.** `match`
selects work by the *absence* of a `match_status` row, and `match_status` is keyed on
pattern identity rather than on a run, so a feed whose *patterns* — the ordered stop
sequences that are the unit of work — are mostly unchanged costs only the ones that
are new. Everything carried over from the previous feed is already matched and is
skipped for free. *Churn*, the count of patterns that appear and depart between two
feeds, is therefore the number that sets what the day-or-two stage costs.

`match` and `trace` are the two incremental stages. `acquire`, `patterns`,
`aggregate`, `prune`, `cluster` and `publish` are full passes every run, so what a
refresh costs is that fixed floor plus the churn. The floor is what decides whether a
shorter interval is affordable, and the Cadence section at the end prices it.

**Measured on Wales, two feeds two days apart: 3,584 patterns became 3,541** — 30
new, 73 departed — and matching the new ones took ~8 s against 16m23s for the
original full run. Two days is not a month, and one region is not the nation; the
caveats sit with the measurement in docs/results.md rather than being repeated here.

## One unit, not several

**The *drain* — one run of `match` to an empty queue — is unbounded on the deploy
side.** It is bounded by the container's compute and by how much Valhalla will
answer, not by `--max-seconds`, which nothing in `refresh.sh` passes. So a drain
always runs the queue to empty, and there is no partial run for a later invocation
to pick up.

That collapses what could have been three schedules into one chained unit. Separate
acquire, drain and publish timers would have bought re-entry: a drain stopped after
an hour, resumed the following night, publishing only once the queue finally
emptied. Re-entry is worth having when the drain is bounded. This one is not, so the
script runs `acquire`, `patterns`, `match`, `trace`, `routes`, `aggregate`, `prune`,
`cluster` and `publish` in order under `set -euo pipefail`, and a stage that fails
stops the run where it stands — except the two that ask Overpass, which are
allowed to fail and are covered below.

`cluster` runs before `publish`, not after. Clustering goes stale rather than off,
and the rows this run matched land unsorted on the end of the table where no zonemap
can help them.

**`prune` sits between `aggregate` and `cluster`, and both neighbours decide that
slot.** `aggregate` rebuilds `segments` out of `patterns` and `shapes`, so pruning
before it would take away the source of a non-road pattern's geometry. `cluster` is
the only thing that gives space back — a DELETE leaves DuckDB's high-water mark
where it is, and `cluster` copies the database into a fresh file — so pruning after
it would reclaim nothing until the following run. Wales measured 160 MB going to 114
MB compacted, and the shapes are reloaded from the feed by the next `patterns`
anyway. The command refuses while any matchable pattern is unmatched, which the gate
above has already established by the time it runs.

## The publish gate is two counts

**`patterns_pending` counts patterns with no `match_status` row at all, and a
`transport_error` is a row.** A Valhalla outage part-way through a run leaves those
patterns unmatched but not pending. The run then reaches the end reporting
`patterns_pending: 0`, and a gate reading that one number would publish a tileset
missing every road that failed. Missing road does not read as an incomplete run — it
reads as a region that lost its buses. Both counts have to be zero.

`refresh.sh` reads `.patterns_pending` and `.by_status.transport_error` out of
`wayfare status` with `jq`, and exits 1 without publishing if either is non-zero.
The exit code is what `OnFailure=` reports on and what tells a person to look.

**`--retry transient` at the top of the run is what gives those patterns another
attempt.** It clears the previous run's `transport_error` rows and puts them back in
the queue. It is the only status safe to clear unattended: every other failure means
impossible, and a matcher that retries the impossible never finishes.

**Both counts are mode-aware, because a tram never gets a `match_status` row.**
`db.matchable` keeps non-road patterns away from the matcher, and their geometry
comes from the operator's own General Transit Feed Specification (GTFS) shapes
instead. Counting them as unmatched put a floor under `patterns_pending` equal to
the non-road pattern count, so the drain reported work still owed after the queue
was empty. Measured on a synthetic four-pattern database (two bus patterns matched,
one metro, one tram), `match.pending_count` returned 0 while
`status.patterns_pending` returned 2 and `patterns_pct` read 50.0 against a true
100.0. `refresh.sh` exits 1 on a non-zero pending count, so a multi-modal region
would have stopped publishing for ever, reporting a drain that never finished.

Every number in that funnel now counts *matchable* patterns only, and what the other
modes are doing is a separate field. `patterns_by_mode` counts live patterns per
mode, matchable or not, beside a `modes` field echoing the selection the database
was built with. The gate then reads one thing, work still owed to the matcher, and
the mode census sits where a person reads it. It is also the only place a mode going
missing is visible.

## Tracing runs after the gate, and may fail

**`wayfare trace --retry transient` is one line in `refresh.sh`, placed after the
publish gate and allowed to fail.** It draws the modes with no road under them and no
operator trace — the Underground, the Docklands Light Railway (DLR), London Trams —
from OpenStreetMap route relations, and it depends on Overpass, which is a third
party's metered public service.
A refresh that dropped a whole region's buses because a public API was busy would be
the wrong trade, so the command is followed by `|| echo` and the run carries on.
Nothing is lost by that: a pattern it does not draw keeps no `trace_status` row, so
the next refresh selects it again unchanged.

**Its failures stay out of the gate deliberately.** `patterns_pending` counts
matchable patterns, and a metro never gets a `match_status` row. Folding an
unresolvable relation into that number would put a permanent floor under the count
the gate reads, which is the mistake the mode filter already made once, described in
the section above. `wayfare status` reports a separate `traced` block instead —
patterns owed, patterns pending, and a count per status — and a database written
before the stage existed reports nothing there rather than raising.

## `routes` runs in the same slot, for the same reasons

**`wayfare routes` sits immediately after `trace`, also past the gate and also
allowed to fail.** It draws the modes with no timetable *at all* rather than merely
no geometry — Great Britain's National Rail, which BODS does not carry. A route
relation becomes a service in its own right, so the track arrives under ODbL, which
every archive already carries and credits for its matched roads.

It asks the same metered public service `trace` does, so it gets the same treatment:
`|| echo` and the run carries on. What it does not draw this run it draws the next
one, because the stage rebuilds its patterns from the relation set every run rather
than carrying them forward.

**Neither stage re-queries Overpass on a schedule, and the geometry ages because of
it.** `osm.fetch` reads `raw/osm_relations.json` or `raw/osm_routes.json` where one
exists and only `--refresh` overrides that, which `refresh.sh` never passes. That is
what keeps a shortened interval off a public service: the national query cost 131 MB
and 27 seconds once, and every run since has read the file. Left alone, the track
stays as OpenStreetMap had it on the day of that query, so "next run draws it" covers
a fit that failed and not a relation the map has gained since.

**Deleting the two files is what re-queries, and the schedule does it every 30 days.**
`wayfare-refresh.sh` in `roles/wayfare` keeps a second stamp beside the first and
removes `raw/osm_relations.json` and `raw/osm_routes.json` when that stamp is older
than `wayfare_refresh_osm_days`. The stages then find nothing cached and ask Overpass
once, which is one national query a month against a weekly run. That stamp is written
before the run for the same reason the other one is: a re-query that fails must not
turn into a re-query every night.

**It must run after `patterns`, and the ordering is not cosmetic.** `patterns` sets
the feed version these rows are stamped with, and a relation written against the
previous one is departed the moment the new feed lands. Placed before `patterns` the
stage would draw the country's rail for exactly one refresh and then silently stop.

**No `--cif`, so `trips` stays null.** That is the point of taking the geometry from
OpenStreetMap first: nothing in the scheduled run waits on a Network Rail login, and
adding credentials later fills a column rather than changing what is drawn.

## The mode selection lives in the database

**`refresh.sh` runs `wayfare patterns` with no flags, so the selection cannot live
in the invocation.** `--modes` was persisted nowhere, and `build_patterns` fell back
to `config.DEFAULT_MODES`, which is bus and coach. The first refresh after a
multi-modal build would have rebuilt the table as road only and dropped every tram,
and every other number the run reports would have stayed healthy while it did.
`build_patterns` now writes the selection to `meta.modes` beside `feed_version` and
defaults to `gtfs.stored_modes` rather than to a constant. An unrecognised stored
name is left to raise rather than being dropped, on the same reading as
`--force-graph`: renaming a mode should break the schedule and be dealt with by a
person.

**Deselecting a mode has to retire its patterns, because nothing else will.** A
pattern normally leaves by not being seen in a *newer* feed. Narrowing `--modes` and
rebuilding against the feed already on disk leaves `last_seen` at a version that is
still current, so the deselected patterns stay live, keep their geometry and are
published again, and turning a mode off appears to do nothing.
`gtfs._retire_unselected_modes` deletes them after the merge. A NULL mode is never
retired — it means a database written before modes existed, where everything stored
is road-going by construction, and matching those against a name would delete a
national match run.

`refresh.sh` and the cron entry need no change for any of this. The database carries
the selection, so a refresh inherits whatever the region was last built with, and a
multi-modal region is scheduled exactly as a road-only one is.

## How it is scheduled: cron, under Ansible

**The schedule lives in `roles/wayfare` of the Ansible repository, and this script is
vendored into it.** emel carries no checkout of this repository, so the role copies
`deploy/refresh.sh` in unmodified and installs it at `<build home>/deploy/refresh.sh`
— which is the path that makes the script's own `cd "$(dirname …)/.."` land on the
Ansible-rendered build Compose file. The stage sequence stays upstream because which
stages run, behind which gate, with which flags is exactly what changed on 2026-08-12
when `--name-by-region` turned out to be mandatory. The cost is that a change here
reaches the host when somebody re-vendors the file, and the role's header carries the
`diff` recipe that says whether it is current.

Cron rather than a systemd *timer*, because every scheduled job in that repository is
a crontab entry. Four things the unit provided are provided again:

- `Type=oneshot` becomes `flock -n` on the cron line, so a tick landing on a running
  drain is a no-op rather than a second writer.
- `Persistent=true` becomes a stamp file at `/var/lib/wayfare/last-attempt`. The line
  fires nightly at 03:17 and the stamp's age decides whether the tick does anything,
  so a host that was down delays a refresh by a night.
- `OnFailure=` becomes a line in `/var/log/services.log`, which Alloy ships to Loki.
- `RandomizedDelaySec=1h` is dropped. It keeps a fleet off the Bus Open Data Service
  (BODS) at one instant, and this is one host.

**The stamp is written before the run, not after.** A failure then costs a full
interval instead of retrying nightly against a feed that is still broken, which
bounds the rate rather than merely reducing it.

**Tearing the stack down is the wrapper's job, and it is worth real memory.**
`refresh.sh` leaves the containers up and the unit never took them down either.
Measured 19 hours after a finished run, `valhalla` held 4.24 GiB of anonymous memory
at 14.4 GB resident and emel was 2.3 GB into swap. The wrapper runs `docker compose
down` from an `EXIT` trap, so the failure and interrupt paths release it too, and the
graph survives as a bind mount.

**The host's caps are lower than the numbers elsewhere in these docs.** emel runs 2
match workers and 3 Valhalla threads against the 6 the throughput figures were taken
at, and `WAYFARE_MEM` is 6 GB rather than the project's 8 GB. 4 workers and 6 threads
put Valhalla at its 10 GB ceiling for a whole run: 1.66M reclaims, four OOM kills, and
throughput falling from 8.6/s to 0.13/s at 92% as reclaim evicted the mmap'd graph
tiles. Every rate in docs/results.md is a ceiling on that box.

**`REFRESH_NO_FETCH=1` skips the download.** It is for a retry after a failure past
`acquire`, because `acquire --force` otherwise re-fetches 1.28 GB for Great Britain
to get a feed already sitting in the volume. Removing the stamp is what lets the next
nightly tick pick a failed run up early.

## Traps

**`--force-graph` must never appear in a scheduled run.** It overrides
`match.pin_graph`, the check that stops edge ids from one Valhalla graph build
mixing with edge ids from another. Attended, that override is a decision; unattended,
the failure is silent, renders fine and is wrong, and costs a full re-match to undo.
A graph rebuild should break the schedule and be dealt with by a person.

**Mutual exclusion is load-bearing, and cron gives none of it for free.** DuckDB
takes a single writer, and a drain runs for hours, so a nightly tick will routinely
land on one still running. `flock -n` on the cron line is what makes that tick a
no-op, standing in for the unit's `Type=oneshot`. Without `-n` it would queue and
start a second writer the moment the first finished.

**One region per deployment.** `meta.feed_version` is single-valued, so a second
region acquired into the first's volume becomes the current feed, and the next
`publish` overwrites the first region's archive. A second region means a second
compose project and a second data volume, not a second entry in the same one.

**`publish` needs `--name-by-region` here, and refuses without it.** Every deployed
data root holds its region's archive under its own name, and the served directory
holds all three. A bare `publish` writes `bus.pmtiles`, which nothing serves, so
`publish.default_out` stops rather than succeeding quietly — and `set -e` then ends
the run. The flag was missing from this script until 2026-08-13, so no scheduled
refresh could have completed on any of the three deployments, and the republishes up
to that date were done by hand.

**Running the timer alongside the cron entry would put two writers on one database.**
The unit and the timer in `deploy/` are a reference for what a scheduler owes this
script, and Ansible replaces them rather than joining them. Installing both is the
one way to lose the database that neither side checks for.

**Only Great Britain is on the schedule.** The role deploys one build stack at
`WAYFARE_REGION=all`, which publishes `great_britain.pmtiles`. Ireland's and Northern
Ireland's archives are served from the same read-only mount and are refreshed by
nothing, because a second region needs a second data root and the role renders one.

**Failure has to propagate, hence `set -e` and `&&` rather than `;`.** The Bus Open
Data Service (BODS) answers an outage with HTTP 200 and an HTML error page, and the
`MIN_GTFS_BYTES` floor in `acquire` is what turns that body into a non-zero exit
before the structural check on the zip's members ever opens it. Without the exit
propagating, `patterns` runs next against the feed already on disk, recounts it, and
reports a churn of zero — a refresh that looks like a quiet month. This is not
hypothetical: it happened on 2026-08-08 and the guard caught it.

**A republish is atomic, and it has to stay that way.** `build_tiles` builds both
bands and the joined archive in a scratch subdirectory of the output and moves the
finished file into place with `os.replace`. That is not tidiness. `web` serves
`/data/out` from the same volume `publish` writes to, so writing the final
`.pmtiles` directly left minutes in which a client reading it in byte ranges could
span two different archives — and the bands, which carried their own `.pmtiles`
names, were globbed by `server.archives` and offered to the viewer as though an
overview band were another region. Anything that moves this work back beside the
archive brings both back.

## Serving

The viewer is tailnet-only. A Tailscale sidecar terminates HTTPS on :443 and proxies
to `wayfare serve` on loopback, and there is no content delivery network (CDN) and no
reverse proxy behind that sidecar, so nothing between the browser and Python caches a
byte. The container has 4 CPUs and 3 GB on emel, a box with 8 cores and 15 GB. HTTP/2
is negotiated to the browser, and the sidecar-to-Python hop is HTTP/1.1 keep-alive.

**Keep-alive without `disable_nagle_algorithm` costs 40 ms a request.**
`wayfare/server.py` sets `protocol_version = "HTTP/1.1"` for keep-alive, which removed
a round trip per range request and silently added a stall to every request after the
first on a connection. `BaseHTTPRequestHandler` flushes its headers, then writes the
body as a second, smaller write. *Nagle's algorithm* holds that second write until the
peer acknowledges the first, and Linux delays that acknowledgement by 40 ms. Under
HTTP/1.0 the close after each response flushed the body at once, so the stall could
not appear until keep-alive existed.

On loopback inside the deployed container, keep-alive requests took 41 ms each against
0.3 ms with `TCP_NODELAY` set — a controlled A/B, same handler shape, no network in the
path. From a laptop over the tailnet a warm 16 KB range took 62 ms against a 21 ms
round trip, so about three requests in four paid the timer. The fix is
`disable_nagle_algorithm = True` on the Handler.

Two other suspects were measured and cleared. The relay is one: 21.7 ms round trip via
the London Designated Encrypted Relay for Packets (DERP) node against 20.1 ms direct to
the same host, so relaying costs ~1.6 ms and a direct path is not worth chasing.

The disk is the other. The archives sit on a rotational drive and are essentially not
in page cache, `great_britain.pmtiles` being 0.5% resident, and isolated random 16 KB
reads cost p50 7.4 ms and p90 11.7 ms. End to end, 40 parallel ranges took 343 ms cold
against 330 ms warm, a difference of 4%. The network masks the seeks. Moving `out/`
(146 MB) to the SSD is still worth doing for the tail, where worst-case reads under
16-way concurrency reach 155 ms, and it is safe because `publish` creates its scratch
directory inside `out.parent`, keeping the atomic `os.replace` on one filesystem.

Nothing in this path caches, so every millisecond in it is one the browser pays on
every tile it fetches. The 40 ms hid for as long as it did because it arrived inside a
change that made the same requests cheaper by a round trip.

## Cadence

**Monthly is the largest sensible gap, and the deployed interval is 7 days.** BODS
republishes far more often than monthly, and the pair churn was first measured
against were two days apart, so the `OnCalendar=monthly` the unit still carries is a
ceiling to move in from. Ansible sets `wayfare_refresh_interval_days: 7` and moved in
once.

**Shortening the interval buys a smaller drain and pays for the full passes four
times as often.** `match` and `trace` are the two stages whose cost is churn;
`acquire`, `patterns`, `aggregate`, `prune`, `cluster` and `publish` cost the same
whatever the interval, so weekly is four times that floor. In transfer that floor is
the 1.28 GB feed and the 102 MB NaPTAN register, about 1.4 GB a week. The 2.16 GB OSM
extract is not in it, because `acquire` fetches a pbf only under `--with-osm` and the
Valhalla image pulls its own from `tile_urls` when it builds a graph.

**Departed rows accumulate per run rather than per month, and `prune` does not touch
them.** `prune_shapes` drops operator geometry alone. A departed pattern keeps its
`match_status` row deliberately, so a seasonal service that returns is free, and its
`pattern_edges` rows stay because no policy decides when they should go — the gap is
recorded in PLAN.md and nothing in the schedule closes it. Four times the runs is
four times the departed rows arriving in a month, so the size of the DuckDB file
under `wayfare_data_dir` is the thing to watch on a shortened interval.

The honest way to pick a period is to watch what `patterns` logs — new, carried over
still unmatched, departed — across a few runs, and set the interval against the
churn actually observed rather than against a calendar word. What makes that worth
doing is that the cost of a refresh is its timetable's instability rather than its
size, and nothing in the pipeline knows that number until a schedule has been
running long enough to reveal it.
