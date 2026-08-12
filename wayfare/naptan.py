"""NaPTAN, read for the one thing it holds that nothing else does: TIPLOC to place.

A CIF schedule keys every location on a TIPLOC, which is an operational code and
carries neither a name a passenger would recognise nor a coordinate. `trace` joins a
pattern to an OSM route relation on the *normalised station name* with a distance
guard, so a rail pattern is undrawable until its TIPLOCs become names and points.
That crosswalk was the one dependency national rail was blocked on.

It has been in the pipeline the whole time. NaPTAN is already acquired for Great
Britain (`acquire.sources`), and **a rail station's ATCO code is ``9100`` followed by
its TIPLOC**. That prefix is the join, and it is the only place the two identifier
schemes are written down together in an open dataset.

Two traps, both of which give a plausible wrong answer rather than an error:

* **``RSE`` is an entrance, not a station.** There are 4,308 of them against 2,715
  ``RLY`` rows, several per station, and their coordinates sit on the street. Joining
  on them puts a station's point at a doorway -- Highbury & Islington's National Rail
  entrance is 216 m from the platform, which is half of `TRACE_STOP_MAX_M` spent
  before any real error. ``RLY`` is the station.
* **Closed stations keep their rows.** 44 of the 2,715 are not ``active``, and a
  closed station still has a name that will match an OSM relation drawn before it
  shut. The status field is what keeps a service off track it no longer reaches.

The names arrive with an operational suffix -- "Aberdare Rail Station" against OSM's
"Aberdare" -- which `osm.normalise` already strips, so nothing is done to them here.
Measured: 2,179 of 2,660 stations (81.9%) match a stop node on a GB ``route=train``
relation that chains, and 2,172 of those are within 400 m of it.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from . import logs

log = logs.get("naptan")

# A rail station, as against an entrance (RSE), a platform (RPL) or a metro stop.
RAIL_STOP_TYPE = "RLY"
# Rail ATCO codes are `9100` + TIPLOC. The prefix is fixed and is asserted rather
# than assumed: a row that does not carry it is not a rail station keyed this way,
# and slicing its code anyway would mint a TIPLOC that resolves to the wrong place.
RAIL_ATCO_PREFIX = "9100"
ACTIVE = "active"

# The columns actually read. Named so that a NaPTAN schema change is a KeyError on
# the first row rather than a silently empty result 400,000 rows later.
COLUMNS = (
    "ATCOCode",
    "CommonName",
    "Latitude",
    "Longitude",
    "StopType",
    "Status",
)


class Malformed(Exception):
    """The register is not shaped the way this reader needs."""


@dataclass(frozen=True)
class Station:
    """One rail station: its TIPLOC, the name to join on, and where it is."""

    tiploc: str
    name: str
    lat: float
    lon: float
    atco: str


def read(path: Path) -> dict[str, Station]:
    """TIPLOC to station, from the NaPTAN CSV.

    Streamed and filtered as it goes. The national file is 101.8 MB over roughly
    430,000 rows, of which 2,660 are wanted, so nothing here holds the register --
    only the result. Where two rows claim one TIPLOC the first active one wins and
    the collision is logged; NaPTAN is a register rather than a key-value store and
    has no constraint that would stop it.
    """
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        return dict(stations(csv.DictReader(fh)))


def stations(rows: Iterable[dict[str, str]]) -> Iterator[tuple[str, Station]]:
    """(TIPLOC, Station) for every active rail station with a coordinate."""
    checked = False
    seen: set[str] = set()
    kept = skipped_type = skipped_status = skipped_coord = 0

    for row in rows:
        if not checked:
            missing = [c for c in COLUMNS if c not in row]
            if missing:
                raise Malformed(f"NaPTAN is missing columns {missing}")
            checked = True

        if row["StopType"] != RAIL_STOP_TYPE:
            skipped_type += 1
            continue
        if (row.get("Status") or "").strip().lower() != ACTIVE:
            skipped_status += 1
            continue

        atco = (row["ATCOCode"] or "").strip()
        if not atco.startswith(RAIL_ATCO_PREFIX):
            raise Malformed(
                f"rail station {atco!r} does not carry the {RAIL_ATCO_PREFIX} prefix "
                "that makes its ATCO code a TIPLOC"
            )
        tiploc = atco[len(RAIL_ATCO_PREFIX) :].strip()

        lat, lon = (row["Latitude"] or "").strip(), (row["Longitude"] or "").strip()
        if not lat or not lon:
            skipped_coord += 1
            continue

        if tiploc in seen:
            log.warning(
                "NaPTAN carries TIPLOC %s more than once; keeping the first", tiploc
            )
            continue
        seen.add(tiploc)
        kept += 1
        yield (
            tiploc,
            Station(
                tiploc=tiploc,
                name=(row["CommonName"] or "").strip(),
                lat=float(lat),
                lon=float(lon),
                atco=atco,
            ),
        )

    if not checked:
        raise Malformed("NaPTAN register is empty")
    log.info(
        "%d rail stations kept (%d not rail, %d not active, %d without a coordinate)",
        kept,
        skipped_type,
        skipped_status,
        skipped_coord,
    )


def resolve(
    tiplocs: Iterable[str], register: dict[str, Station]
) -> tuple[dict[str, Station], set[str]]:
    """Split a schedule's TIPLOCs into the ones that place and the ones that do not.

    Returned as a pair rather than raising, because an unresolved TIPLOC is a normal
    outcome and not a fault: a CIF calls at freight terminals, depots and stations
    that opened after the register's last revision. What matters is that the caller
    can count them -- a pattern with an unresolved calling point cannot be traced,
    and silently dropping the call would shorten the pattern instead, which draws a
    service running past a station it stops at.
    """
    found = {t: register[t] for t in tiplocs if t in register}
    missing = {t for t in tiplocs if t not in register}
    return found, missing
