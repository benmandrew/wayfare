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

wayfare aggregate
# Before publish, not after: clustering goes stale rather than off, and the rows
# this run matched land unsorted on the end where no zonemap can help.
wayfare cluster
wayfare publish
