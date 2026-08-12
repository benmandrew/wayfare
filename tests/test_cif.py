"""Network Rail CIF: fixed-width records, and the two things that are not obvious.

The format carries meaning entirely in column position, so the fixtures here are
built by *placing* fields at their documented offsets rather than by writing literal
strings. A literal would encode the same assumption the reader makes, and the pair
would agree with each other while both being wrong.

The two behaviours worth testing are the ones a naive reader gets wrong and still
produces output for: a passing point counted as a station stop, and a Short Term
Plan cancellation ignored so a service draws on track it is not running on.
"""

from __future__ import annotations

from datetime import date

import pytest

from wayfare import cif

# --- record building ---------------------------------------------------------

RECORD_LEN = 80


def line(*placed: tuple[int, str]) -> str:
    """An 80-character record with each field at its documented column."""
    buf = [" "] * RECORD_LEN
    for start, text in placed:
        buf[start : start + len(text)] = list(text)
    return "".join(buf)


def header() -> str:
    return line((0, "HDTPS.UTEST .PD2608121200260812"))


def schedule(
    uid: str = "C12345",
    runs_from: str = "260810",
    runs_to: str = "261212",
    days: str = "1111100",
    stp: str = "P",
    transaction: str = "N",
) -> str:
    return line(
        (0, "BS"),
        (2, transaction),
        (3, uid),
        (9, runs_from),
        (15, runs_to),
        (21, days),
        (29, "P"),  # train status
        (30, "OO"),  # category: ordinary passenger
        (79, stp),
    )


def origin(tiploc: str, public: str = "0800") -> str:
    return line((0, "LO"), (2, tiploc), (10, "0800"), (15, public), (19, "1"))


def intermediate(tiploc: str, public_arr: str = "", public_dep: str = "") -> str:
    """A calling point has a public time; a passing point has none."""
    return line(
        (0, "LI"),
        (2, tiploc),
        (10, "0810"),
        (15, "0811"),
        (25, public_arr),
        (29, public_dep),
    )


def passing(tiploc: str) -> str:
    """A junction as Great Britain writes it: a pass time, public columns ``0000``.

    Zeros rather than blanks, because that is what the national extract contains --
    all 5,033,536 of its LI records fill these columns, so a fixture using blanks
    tests a case the real data never produces.
    """
    return line((0, "LI"), (2, tiploc), (20, "0815"), (25, "0000"), (29, "0000"))


def passing_blank(tiploc: str) -> str:
    """A junction as Northern Ireland writes it, with the columns left empty."""
    return line((0, "LI"), (2, tiploc), (20, "0815"))


def terminus(tiploc: str, public: str = "0900") -> str:
    return line((0, "LT"), (2, tiploc), (10, "0900"), (15, public), (19, "2"))


def trailer() -> str:
    return line((0, "ZZ"))


BASIC = [
    header(),
    schedule(),
    origin("EUSTON "),
    intermediate("WATFDJ ", "0815", "0816"),
    passing("BLTCHLY"),
    terminus("RUGBY  "),
    trailer(),
]


# --- parsing -----------------------------------------------------------------


def test_a_schedule_reads_its_header_fields():
    ex = cif.parse(BASIC)
    assert len(ex.schedules) == 1
    s = ex.schedules[0]
    assert s.train_uid == "C12345"
    assert s.runs_from == date(2026, 8, 10)
    assert s.runs_to == date(2026, 12, 12)
    assert s.days == (True, True, True, True, True, False, False)
    assert s.days_per_week == 5
    assert s.stp == "P"


def test_a_passing_point_is_not_a_calling_point():
    """The whole reason `public` exists. BLTCHLY is run through, not stopped at."""
    (s,) = cif.parse(BASIC).schedules
    assert len(s.calls) == 4
    assert s.calling_points == ("EUSTON", "WATFDJ", "RUGBY")


def test_a_zero_public_time_is_an_absence_not_midnight():
    """The defect the national extract exposed and the NI file could not.

    Great Britain never leaves these columns blank, so a reader testing for a blank
    counts every junction in the country as a station stop.
    """
    records = [
        header(),
        schedule(),
        origin("A"),
        passing("BLTCHLY"),
        passing_blank("WATFDJ"),
        terminus("B"),
        trailer(),
    ]
    (s,) = cif.parse(records).schedules
    assert len(s.calls) == 4
    assert s.calling_points == ("A", "B")


def test_a_zero_public_time_on_an_origin_is_also_an_absence():
    records = [header(), schedule(), origin("A", public="0000"), terminus("B"), trailer()]
    (s,) = cif.parse(records).schedules
    assert s.calling_points == ("B",)


def test_an_intermediate_with_only_a_departure_still_calls():
    """A first-stop-after-origin publishes a departure and no arrival."""
    records = [
        header(),
        schedule(),
        origin("A"),
        intermediate("B", "", "0830"),
        terminus("C"),
        trailer(),
    ]
    (s,) = cif.parse(records).schedules
    assert s.calling_points == ("A", "B", "C")


def test_the_open_ended_date_is_not_read_as_a_date():
    records = [header(), schedule(runs_to="999999"), origin("A"), terminus("B"), trailer()]
    (s,) = cif.parse(records).schedules
    assert s.runs_to == date.max


def test_tiploc_records_are_kept_for_diagnostics():
    ti = line((0, "TI"), (2, "EUSTON "), (18, "LONDON EUSTON"))
    ex = cif.parse([header(), ti, schedule(), origin("A"), terminus("B"), trailer()])
    assert ex.tiplocs == {"EUSTON": "LONDON EUSTON"}


def test_record_counts_cover_every_line():
    ex = cif.parse(BASIC)
    assert ex.counts["LI"] == 2
    assert sum(ex.counts.values()) == len(BASIC)


# --- refusals ----------------------------------------------------------------


def test_an_unknown_record_type_is_refused():
    """Not skipped. A format that changed must not be read as though it had not."""
    with pytest.raises(cif.Malformed, match="unknown record type"):
        cif.parse([header(), line((0, "QQ")), trailer()])


def test_a_record_before_the_header_is_refused():
    with pytest.raises(cif.Malformed, match="before the HD header"):
        cif.parse([schedule(), origin("A"), terminus("B")])


def test_a_file_with_no_header_is_refused():
    with pytest.raises(cif.Malformed, match="not a CIF extract"):
        cif.parse([])


def test_an_update_file_is_refused_rather_than_read_as_a_full_one():
    """`D` and `R` amend a previous file. Reading them as new drops the amendment."""
    with pytest.raises(cif.Malformed, match="full extract"):
        cif.parse([header(), schedule(transaction="R"), origin("A"), terminus("B")])


def test_a_malformed_days_field_is_refused():
    with pytest.raises(cif.Malformed, match="seven 0/1 flags"):
        cif.parse([header(), schedule(days="11111"), origin("A"), terminus("B")])


def test_a_location_outside_a_schedule_is_refused():
    with pytest.raises(cif.Malformed, match="outside any schedule"):
        cif.parse([header(), origin("A")])


def test_a_bad_date_names_the_line():
    with pytest.raises(cif.Malformed, match="line 2"):
        cif.parse([header(), schedule(runs_from="269999"), origin("A"), terminus("B")])


# --- short term plan ---------------------------------------------------------

WEEKDAY = date(2026, 8, 12)  # a Wednesday inside every fixture's date range


def overlaid(stp: str, uid: str = "C12345", days: str = "1111100") -> list[str]:
    return [schedule(uid=uid, stp=stp, days=days), origin("A"), terminus("B")]


def test_a_cancellation_suppresses_the_permanent_schedule():
    """The failure this prevents: drawing a service through its own engineering work."""
    ex = cif.parse([header(), *overlaid("P"), *overlaid("C"), trailer()])
    assert len(ex.schedules) == 2
    assert cif.live(ex.schedules, WEEKDAY) == []


def test_an_overlay_wins_over_the_permanent_schedule():
    ex = cif.parse([header(), *overlaid("P"), *overlaid("O"), trailer()])
    (running,) = cif.live(ex.schedules, WEEKDAY)
    assert running.stp == "O"


def test_two_trains_are_not_overlays_of_each_other():
    ex = cif.parse(
        [header(), *overlaid("P", uid="C11111"), *overlaid("C", uid="C22222"), trailer()]
    )
    running = cif.live(ex.schedules, WEEKDAY)
    assert [s.train_uid for s in running] == ["C11111"]


def test_a_schedule_not_running_that_weekday_is_not_live():
    ex = cif.parse([header(), *overlaid("P", days="0000011"), trailer()])
    assert cif.live(ex.schedules, WEEKDAY) == []
    assert len(cif.live(ex.schedules, date(2026, 8, 15))) == 1  # a Saturday


def test_a_schedule_outside_its_date_range_is_not_live():
    ex = cif.parse([header(), *overlaid("P"), trailer()])
    assert cif.live(ex.schedules, date(2027, 1, 1)) == []


# --- collapsing to patterns --------------------------------------------------


def test_identical_calling_sequences_collapse():
    ex = cif.parse(
        [header(), *overlaid("P", uid="C11111"), *overlaid("P", uid="C22222"), trailer()]
    )
    assert cif.patterns(ex.schedules) == {("A", "B"): 2}


def test_trips_are_counted_per_week_not_per_schedule():
    """`n_trips` is per week, so a five-day schedule contributes five."""
    ex = cif.parse([header(), *overlaid("P", days="1111100"), trailer()])
    assert cif.weekly_trips(ex.schedules) == {("A", "B"): 5}


def test_a_sequence_of_one_call_is_not_a_journey():
    records = [header(), schedule(), origin("A"), passing("B"), trailer()]
    ex = cif.parse(records)
    assert ex.schedules[0].calling_points == ("A",)
    assert cif.patterns(ex.schedules) == {}


def test_tiplocs_used_covers_only_calling_points():
    ex = cif.parse(BASIC)
    assert cif.tiplocs_used(ex.schedules) == {"EUSTON", "WATFDJ", "RUGBY"}
