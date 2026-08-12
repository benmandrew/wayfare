"""Read a Network Rail CIF schedule extract.

The GB rail timetable arrives in no other form. BODS carries no heavy rail at all --
its three ``route_type=2`` routes are the DLR -- so National Rail comes from Network
Rail's SCHEDULE feed, which is fixed-width CIF and not GTFS.

The format is one 80-character record per line, typed by its first two characters,
with meaning carried entirely by column position. Nothing self-describes, so an
offset that is one out reads a valid-looking wrong answer rather than failing: a
train UID becomes a different train's, a date shifts by a decade. Every field here
is therefore taken from the published layout and asserted against the record length
rather than found by splitting.

Three things about this format cost more than they look:

* **A location is not a calling point.** ``LI`` is every place the schedule names,
  and most of them are junctions and passing points the train runs through without
  stopping -- 2,322,531 of Great Britain's 5,033,536. What separates the two is a
  *public* time, and a publisher may record its absence as blanks or as ``0000``:
  Great Britain writes zeros and never blanks, Northern Ireland the reverse. A
  reader that tests for a blank therefore passes every junction in the country off
  as a station stop, and produces patterns no passenger could travel. `_public` is
  where that is decided and is the single most load-bearing line here.
* **Short Term Plan overlays rewrite the base timetable in place.** A schedule is
  identified by ``(train_uid, date range, stp_indicator)``, and a ``C`` record
  cancels a permanent one over a date range rather than deleting it. Reading only
  the ``P`` records draws services that are not running; reading all of them without
  precedence draws them twice.
* **TIPLOC is the only location key.** It is not a station code and not a name, and
  the ``TI`` records carry no coordinates. `wayfare.naptan` is what turns a TIPLOC
  into a place -- see that module for why the join works at all.

This reads the end-user extract (``CIF_ALL_FULL_DAILY``), which is the whole
timetable rather than a daily update, so no record here amends a record from a
previous file.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import logs

log = logs.get("cif")

# The record types this reader knows. Everything the layout defines is listed, even
# where it is consumed and discarded, because an unrecognised type is an error: a
# format that changed under us must not be read as though it had not. `mapinfo`
# refuses unknown objects for the same reason and it is the same class of bug.
HEADER = "HD"
TIPLOC_TYPES = frozenset({"TI", "TA", "TD"})
SCHEDULE_TYPES = frozenset({"BS"})
LOCATION_TYPES = frozenset({"LO", "LI", "LT"})
IGNORED_TYPES = frozenset({"BX", "CR", "AA", "LN", "ZZ"})
KNOWN_TYPES = {HEADER} | TIPLOC_TYPES | SCHEDULE_TYPES | LOCATION_TYPES | IGNORED_TYPES

# Short Term Plan indicators, most authoritative first. A schedule overlaid by a
# higher-precedence record for the same train and dates is not running as written.
#   C  cancellation of a permanent schedule
#   N  short-term new schedule
#   O  overlay, varying a permanent schedule
#   P  permanent, the base timetable
STP_PRECEDENCE = ("C", "N", "O", "P")

_DIGITS = re.compile(r"^\d+$")


class Malformed(Exception):
    """The extract cannot be read without guessing where the fields are."""


@dataclass(frozen=True)
class Call:
    """One place a schedule names, and whether a passenger may board there.

    ``public`` is the whole distinction between a station stop and a junction the
    train runs through, and it is the *public* timetable time rather than the
    working one -- an operational stop the public timetable does not show is not a
    call. See `_public` for why "has a time" is not the test.
    """

    tiploc: str
    public: bool
    platform: str | None


@dataclass(frozen=True)
class Schedule:
    train_uid: str
    runs_from: date
    runs_to: date
    # Monday first, seven entries, as the record writes them.
    days: tuple[bool, ...]
    stp: str
    status: str
    category: str
    calls: tuple[Call, ...]

    @property
    def days_per_week(self) -> int:
        return sum(self.days)

    @property
    def calling_points(self) -> tuple[str, ...]:
        """The TIPLOCs a passenger may board or alight at, in order."""
        return tuple(c.tiploc for c in self.calls if c.public)


@dataclass
class Extract:
    """Everything one CIF file holds, with the record counts to prove it."""

    schedules: list[Schedule] = field(default_factory=list)
    # TIPLOC -> the description the file gives it. Kept for diagnostics only: the
    # coordinates and the name a station is actually known by come from NaPTAN, and
    # the CIF description is an operational label ("LONDON KINGS CROSS").
    tiplocs: dict[str, str] = field(default_factory=dict)
    counts: Counter[str] = field(default_factory=Counter)


# -- field access -------------------------------------------------------------


def _field(line: str, start: int, end: int) -> str:
    """One field by column, stripped. Positions are the published layout's."""
    return line[start:end].strip()


def _public(raw: str) -> bool:
    """Whether a public-time field holds a time, as against a filled-in absence.

    Not ``bool(raw)``, and the difference is the whole reader. Two publishers write
    "no public time" two different ways: Translink leaves the columns blank, and
    Network Rail writes ``0000``. Measured on the April 2024 national extract, *no*
    ``LI`` record leaves them blank -- all 5,033,536 carry either ``0000``
    (2,322,531) or a real time (2,711,005) -- so a blank test passes every junction
    in the country off as a station stop, which is not a failure anything downstream
    can see. It draws a service calling at Bletchley Junction.

    Zeros rather than a midnight time: of the 2,322,531 all-zero records only 553
    carry a ``T`` activity, and every genuine passenger call carries one.
    """
    return bool(raw) and set(raw) != {"0"}


def _yymmdd(raw: str, *, what: str) -> date:
    """A CIF date. Six digits, and the century is not written down.

    CIF has no century field, so the window has to be assumed. Timetables run
    forward: 1960 is not a date any live schedule carries, and 999999 is the
    format's own "no end date", which the caller sees as `date.max`.
    """
    if raw == "999999":
        return date.max
    if len(raw) != 6 or not _DIGITS.match(raw):
        raise Malformed(f"{what}: expected YYMMDD, got {raw!r}")
    yy, mm, dd = int(raw[0:2]), int(raw[2:4]), int(raw[4:6])
    try:
        return date(2000 + yy if yy < 60 else 1900 + yy, mm, dd)
    except ValueError as exc:
        raise Malformed(f"{what}: {raw!r} is not a date: {exc}") from exc


# -- reading ------------------------------------------------------------------


def read(path: Path) -> Extract:
    """Read one extract. Streamed -- a national CIF is hundreds of MB of text."""
    with path.open("r", encoding="latin-1", errors="strict") as fh:
        return parse(fh)


def parse(lines: Iterable[str]) -> Extract:
    """Records to schedules, applying nothing and dropping nothing.

    STP precedence is deliberately *not* applied here. This returns what the file
    says; `live` decides what is running. Keeping them apart is what makes the
    overlay logic testable without a 400 MB fixture.
    """
    out = Extract()
    current: dict[str, object] | None = None
    calls: list[Call] = []
    seen_header = False

    for n, raw in enumerate(lines, start=1):
        line = raw.rstrip("\n").rstrip("\r")
        if not line.strip():
            continue
        kind = line[:2]
        out.counts[kind] += 1

        if kind not in KNOWN_TYPES:
            raise Malformed(f"line {n}: unknown record type {kind!r}")
        if kind == HEADER:
            seen_header = True
            continue
        if not seen_header:
            raise Malformed(f"line {n}: {kind} record before the HD header")

        if kind in TIPLOC_TYPES:
            if kind != "TD":  # a delete carries no description to keep
                out.tiplocs[_field(line, 2, 9)] = _field(line, 18, 44)
            continue

        if kind == "BS":
            if current is not None:
                out.schedules.append(_finish(current, calls))
            current, calls = _basic_schedule(line, n), []
            continue

        if kind in LOCATION_TYPES:
            if current is None:
                raise Malformed(f"line {n}: {kind} location outside any schedule")
            calls.append(_location(kind, line))
            continue

    if current is not None:
        out.schedules.append(_finish(current, calls))
    if not seen_header:
        raise Malformed("no HD header record; this is not a CIF extract")

    log.info(
        "%d schedules, %d TIPLOCs, from %d records",
        len(out.schedules),
        len(out.tiplocs),
        sum(out.counts.values()),
    )
    return out


def _basic_schedule(line: str, n: int) -> dict[str, object]:
    """A ``BS`` record's header fields.

    The transaction type at column 2 is ``N``/``D``/``R``. A full extract holds only
    ``N``, so anything else means this is an update file being read as a full one --
    which would silently drop the amendments it exists to carry.
    """
    transaction = line[2:3]
    if transaction != "N":
        raise Malformed(
            f"line {n}: BS transaction {transaction!r}; this reader takes a full "
            "extract, where every schedule is new"
        )
    days_raw = _field(line, 21, 28)
    if len(days_raw) != 7 or set(days_raw) - {"0", "1"}:
        raise Malformed(f"line {n}: days-run {days_raw!r} is not seven 0/1 flags")
    return {
        "train_uid": _field(line, 3, 9),
        "runs_from": _yymmdd(_field(line, 9, 15), what=f"line {n} runs-from"),
        "runs_to": _yymmdd(_field(line, 15, 21), what=f"line {n} runs-to"),
        "days": tuple(c == "1" for c in days_raw),
        "status": _field(line, 29, 30),
        "category": _field(line, 30, 32),
        # The STP indicator is the last column of the record, and a file written
        # with trailing spaces stripped loses it -- hence the default rather than
        # an index that would raise.
        "stp": (line[79:80].strip() or "P"),
    }


def _location(kind: str, line: str) -> Call:
    """One ``LO``/``LI``/``LT`` record.

    The public-time columns differ per type and that is the only reason this needs
    the kind: an origin publishes a departure, a terminus an arrival, and an
    intermediate both. A blank in all of them is a passing point.
    """
    tiploc = _field(line, 2, 9)
    if kind == "LI":
        # An intermediate carries scheduled arrival, departure and pass before its
        # public pair, so its public columns sit ten further along than the ends'.
        public = _public(_field(line, 25, 29)) or _public(_field(line, 29, 33))
        platform = _field(line, 33, 36) or None
    else:
        # LO publishes a departure and LT an arrival, in the same columns.
        public = _public(_field(line, 15, 19))
        platform = _field(line, 19, 22) or None
    return Call(tiploc=tiploc, public=public, platform=platform)


def _finish(header: dict[str, object], calls: list[Call]) -> Schedule:
    return Schedule(
        train_uid=str(header["train_uid"]),
        runs_from=header["runs_from"],  # type: ignore[arg-type]
        runs_to=header["runs_to"],  # type: ignore[arg-type]
        days=header["days"],  # type: ignore[arg-type]
        stp=str(header["stp"]),
        status=str(header["status"]),
        category=str(header["category"]),
        calls=tuple(calls),
    )


# -- what is actually running -------------------------------------------------


def live(schedules: Iterable[Schedule], on: date) -> list[Schedule]:
    """The schedules in force on one day, with STP overlays applied.

    A train UID may carry several schedules at once: the permanent one, an overlay
    varying it for a season, and a cancellation covering a week of engineering work.
    Only the most authoritative in force on the day is running, and a cancellation
    means none is.

    One day rather than a range, because a range has no single answer -- a service
    cancelled for one week of a six-month period is running and is not, and picking
    either for the whole range is a number that cannot be defended.
    """
    by_uid: defaultdict[str, list[Schedule]] = defaultdict(list)
    for s in schedules:
        if s.runs_from <= on <= s.runs_to and s.days[on.weekday()]:
            by_uid[s.train_uid].append(s)

    out: list[Schedule] = []
    for candidates in by_uid.values():
        best = min(candidates, key=lambda s: STP_PRECEDENCE.index(s.stp))
        if best.stp != "C":
            out.append(best)
    return out


def patterns(schedules: Iterable[Schedule]) -> dict[tuple[str, ...], int]:
    """Distinct calling sequences and how many schedules run each.

    This is the collapse that makes national scale affordable, and it is the same
    one `gtfs.build_patterns` does: most schedules are the same physical journey
    repeated through the day. A sequence of fewer than two calling points is not a
    journey anybody can make and is dropped rather than counted.
    """
    out: Counter[tuple[str, ...]] = Counter()
    for s in schedules:
        seq = s.calling_points
        if len(seq) >= 2:
            out[seq] += 1
    return dict(out)


def weekly_trips(schedules: Iterable[Schedule]) -> dict[tuple[str, ...], int]:
    """Trips per week per calling sequence, which is what `n_trips` means.

    `edge_services.n_trips` is per week, so a schedule running five days a week
    contributes five and not one. Overlays are the caller's to resolve first: this
    counts what it is handed.
    """
    out: Counter[tuple[str, ...]] = Counter()
    for s in schedules:
        seq = s.calling_points
        if len(seq) >= 2:
            out[seq] += s.days_per_week
    return dict(out)


def tiplocs_used(schedules: Iterable[Schedule]) -> set[str]:
    """Every TIPLOC that is a calling point, which is all NaPTAN has to resolve."""
    return {t for s in schedules for t in s.calling_points}


def iter_records(path: Path) -> Iterator[tuple[str, str]]:
    """(record type, line) for every record, for diagnostics on a file that fails."""
    with path.open("r", encoding="latin-1", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n").rstrip("\r")
            if line.strip():
                yield line[:2], line
