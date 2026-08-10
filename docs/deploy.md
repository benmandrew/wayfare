# Deploy

A deployed region is one Docker Compose project: `valhalla` holding the routing
graph, `web` serving the viewer and the tile archives, and one `data` volume that
both of them share. A refresh replaces the timetable in that volume and republishes
the tileset, on a schedule, with nobody watching. Three files under `deploy/` are
the whole of it — `refresh.sh`, `wayfare-refresh.service` and
`wayfare-refresh.timer`.

## What a refresh is

**A refresh is the churn and nothing else.** `match` selects work by the *absence*
of a `match_status` row, and `match_status` is keyed on pattern identity rather than
on a run, so a feed whose *patterns* — the ordered stop sequences that are the unit
of work — are mostly unchanged costs only the ones that are new. Everything carried
over from the previous feed is already matched and is skipped for free. *Churn*, the
count of patterns that appear and depart between two feeds, is therefore the number
that sets what a refresh costs.

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
script runs `acquire`, `patterns`, `match`, `aggregate`, `cluster` and `publish` in
order under `set -euo pipefail`, and a stage that fails stops the run where it
stands.

`cluster` runs before `publish`, not after. Clustering goes stale rather than off,
and the rows this run matched land unsorted on the end of the table where no zonemap
can help them.

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

## Installing it

`refresh.sh` stays in the checkout and finds its own compose file, so it can be run
from anywhere. The unit and the timer go to `/etc/systemd/system/`. Edit
`WorkingDirectory=` in the unit to wherever the checkout lives, then:

    systemctl daemon-reload
    systemctl enable --now wayfare-refresh.timer
    systemctl start wayfare-refresh.service

The third line runs one refresh immediately, without waiting for the timer.
`systemctl status wayfare-refresh` and `journalctl -u wayfare-refresh` are where its
output goes: logging is on stderr and `wayfare status` prints its JSON on stdout, so
the two never mix in a capture.

**`REFRESH_NO_FETCH=1` skips the download.** It is for a retry after a failure past
`acquire`, because `acquire --force` otherwise re-fetches 1.28 GB for Great Britain
to get a feed already sitting in the volume.

## Traps

**`--force-graph` must never appear in a scheduled run.** It overrides
`match.pin_graph`, the check that stops edge ids from one Valhalla graph build
mixing with edge ids from another. Attended, that override is a decision; unattended,
the failure is silent, renders fine and is wrong, and costs a full re-match to undo.
A graph rebuild should break the timer and be dealt with by a person.

**`Type=oneshot` is load-bearing, not ceremony.** DuckDB takes a single writer, and
oneshot is what stops a timer firing mid-drain from starting a second one. The later
fire becomes a no-op rather than a corruption.

**One region per deployment.** `meta.feed_version` is single-valued, so a second
region acquired into the first's volume becomes the current feed, and the next
`publish` overwrites the first region's archive. A second region means a second
compose project and a second data volume, not a second entry in the same one.

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

## Cadence

**Monthly is the largest sensible gap rather than the natural one.** BODS
republishes far more often than that, and the pair churn was first measured against
were two days apart. A shorter period means less churn per run and a shorter drain,
so the timer's `OnCalendar=monthly` is a ceiling to move in from, not a target.
`Persistent=true` runs a refresh missed while the box was down, and
`RandomizedDelaySec=1h` keeps a fleet of these off BODS at the same instant.

The honest way to pick a period is to watch what `patterns` logs — new, carried over
still unmatched, departed — across a few runs, and set the interval against the
churn actually observed rather than against a calendar word. What makes that worth
doing is that the cost of a refresh is not the timetable's size but its
instability, and nothing in the pipeline knows that number until a schedule has been
running long enough to reveal it.
