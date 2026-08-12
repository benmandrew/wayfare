#!/usr/bin/env bash
# One monthly refresh of a deployed region, start to finish.
#
# The incremental path, not a rebuild: `match` selects work by the absence of a
# `match_status` row, so a feed whose patterns are mostly unchanged costs only the
# patterns that are new. What that leaves to do is the churn, and nothing else.
#
# Run it from the repository root, or from anywhere -- it finds its own compose
# file. One region per invocation, because one data volume holds one feed version.
#
#   ./deploy/refresh.sh
#   REFRESH_NO_FETCH=1 ./deploy/refresh.sh    # retry without re-downloading
#
# Exits non-zero if the drain did not finish, which is the signal for OnFailure=
# to report and for a later run to pick the rest up.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# -T because there is no terminal behind a timer, and `docker compose run` without
# it fails outright rather than degrading. Logging goes to stderr and `status`
# prints its JSON on stdout, so the two never mix in a capture.
wayfare() { docker compose run --rm -T wayfare "$@"; }

# Valhalla has a 90 minute start_period on its healthcheck for a first graph
# build; once the graph exists this returns immediately. `wayfare` depends on it
# being healthy, so a matcher never opens against a half-built graph.
docker compose up -d valhalla

if [ "${REFRESH_NO_FETCH:-0}" != "1" ]; then
  # --force because acquire skips a file it already has, and the whole point of a
  # refresh is the feed that replaced it. BODS answers an outage with HTTP 200 and
  # an HTML error page, so the size floor in acquire is what turns that into a
  # non-zero exit -- and `set -e` is what stops `patterns` then recounting the
  # feed already on disk and reporting a churn of zero.
  wayfare acquire --region "${WAYFARE_REGION:-all}" --force
fi

wayfare patterns

# --retry transient clears the previous run's transport_error rows so a Valhalla
# outage does not permanently strand the patterns it interrupted. It is the only
# status safe to clear unattended: every other failure means "impossible", and a
# matcher that retries the impossible never finishes.
wayfare match --retry transient

# Two counts, not one. `patterns_pending` is patterns with no match_status row at
# all, so it goes to zero the moment every pattern has an outcome -- including the
# ones whose outcome was a connection fault. transport_error has to be checked
# separately or a Valhalla outage publishes a tileset missing every road it
# interrupted, which reads as a region that lost its buses.
status=$(wayfare status)
pending=$(jq -r '.patterns_pending' <<<"$status")
faults=$(jq -r '.by_status.transport_error // 0' <<<"$status")

if [ "$pending" -ne 0 ] || [ "$faults" -ne 0 ]; then
  echo "refresh: drain incomplete -- $pending pending, $faults transport faults;" \
       "not publishing" >&2
  exit 1
fi

# The modes with no road under them and no operator trace -- the Underground, the
# DLR, London Trams. Deliberately after the publish gate rather than before it, and
# deliberately allowed to fail: Overpass is a third party's metered service, and a
# refresh that dropped a whole region's buses because a public API was busy would be
# the wrong trade entirely. What it does not draw keeps no status row, so the next
# run picks it up unchanged.
#
# `--retry transient` for the same reason `match` gets it: a request that never
# arrived taught us nothing, and it is the only status safe to clear unattended.
wayfare trace --retry transient || \
  echo "refresh: trace did not finish; the relations it missed stay pending" >&2

# The modes with no timetable at all: Great Britain's National Rail, which BODS
# does not carry. Same slot as `trace` and for the same reasons -- after the gate,
# and allowed to fail, because it asks the same metered public API and a busy
# Overpass must not cost a region its buses.
#
# It must run *after* `patterns`, not before. `patterns` sets the feed version these
# rows are stamped with, and a relation written against the previous one is departed
# the moment the new feed lands -- which would draw the country's rail for exactly
# one run and then silently stop.
#
# No `--cif`, so `trips` stays null and the track draws under ODbL alone. That is
# the point of building the geometry from OpenStreetMap first: nothing here waits
# on a Network Rail login, and adding one later fills a column rather than changing
# what is drawn.
wayfare routes || \
  echo "refresh: routes did not finish; rail keeps whatever it last drew" >&2

wayfare aggregate
# Before publish, not after: clustering goes stale rather than off, and the rows
# this run matched land unsorted on the end where no zonemap can help.
wayfare cluster
wayfare publish
