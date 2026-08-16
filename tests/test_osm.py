"""Matching a station name written by one publisher against another's.

`osm.normalise` and `osm.spellings` are the join between a timetable's stops and a
route relation's nodes, and three stages now call them: `trace` cuts a pattern out
of a line with them, `osmroutes` names a relation's stops with them and `railtrips`
indexes its lines by them -- while `naptan` leaves the qualifiers on its station
names because it relies on these stripping them. They were tested inside
`test_trace` while it was the only caller; a change here moves what all of them see.
"""

from __future__ import annotations

import math

import builders
import pytest
import requests

from wayfare import osm


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        # The four the Victoria line experiment actually turned on: a suffix on one
        # side and not the other, a full stop inside an abbreviation, an ampersand,
        # and a bare name that must survive untouched.
        ("Blackhorse Road Station", "blackhorse road"),
        ("Blackhorse Road station", "blackhorse road"),
        ("King's Cross St. Pancras Underground Station", "kings cross st pancras"),
        ("King's Cross St Pancras", "kings cross st pancras"),
        ("Highbury & Islington", "highbury and islington"),
        ("Highbury and Islington Station", "highbury and islington"),
        ("Vauxhall", "vauxhall"),
        ("Edgware Road Station Station", "edgware road"),
        ("Pier Head Ferry Terminal", "pier head"),
        # The pair that cost the whole DLR on the first national run: OSM names a
        # PTv2 stop member for its platform, BODS qualifies the same station by its
        # mode, and neither qualifier appears on the other side.
        ("Lewisham Platform 6", "lewisham"),
        ("Lewisham DLR Station", "lewisham"),
        ("Canary Wharf Platforms 5 & 6", "canary wharf"),
        ("Canary Wharf DLR Station", "canary wharf"),
        ("Shadwell DLR", "shadwell"),
        ("Shadwell Platform 2", "shadwell"),
        (
            "Cutty Sark (for Maritime Greenwich) DLR Station",
            "cutty sark for maritime greenwich",
        ),
        (
            "Cutty Sark for Maritime Greenwich Platform 2",
            "cutty sark for maritime greenwich",
        ),
        # A station whose whole name is a qualifier has to survive being stripped.
        ("Bank", "bank"),
        ("Bank Station", "bank"),
        (None, ""),
        ("", ""),
    ],
)
def test_normalise_agrees_across_publishers(raw: str | None, want: str) -> None:
    assert osm.normalise(raw) == want


def test_spellings_offer_the_bracketed_disambiguator_both_ways() -> None:
    """BODS names the line in brackets where OSM lets the relation say which it is."""
    assert osm.spellings("Edgware Road (Bakerloo)") == {
        "edgware road bakerloo",
        "edgware road",
    }
    # And where the brackets hold part of the name, the full form survives.
    assert "cutty sark for maritime greenwich" in osm.spellings(
        "Cutty Sark (for Maritime Greenwich) DLR Station"
    )


# --- Distance ----------------------------------------------------------------


def test_planar_is_the_plane_to_metres_measures_in():
    """Anything compared against a projected chain has to use the same plane, or the
    comparison is against a distance the projection cannot produce."""
    a, b = (51.5, -0.1), (51.52, -0.08)
    (ax, ay), (bx, by) = osm.to_metres([a, b], a[0])
    assert osm.planar_m(a, b) == pytest.approx(math.hypot(bx - ax, by - ay))


def test_haversine_is_the_exact_one():
    """London to Manchester, against the great-circle distance the two cities are
    published at. `db.HAVERSINE_SQL` measures the same span in SQL."""
    assert osm.haversine_m((51.5074, -0.1278), (53.4808, -2.2426)) == pytest.approx(
        261_983, abs=50
    )
    assert osm.haversine_m((51.5, -0.1), (51.5, -0.1)) == 0.0


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # A way endpoint against the next way's, and a stop against its station node:
        # the two comparisons `chain` and `trace` actually make.
        ((51.5, -0.1), (51.5000010, -0.1000010)),
        ((51.5, -0.1), (51.5018, -0.1)),
        ((51.5, -0.1), (51.527, -0.1)),
    ],
)
def test_planar_agrees_with_haversine_over_a_local_distance(a, b):
    """0.11% of 400 m is 45 cm, well inside `TRACE_STOP_MAX_M` and far inside the
    precision the geometry carries, so the approximation costs nothing here."""
    exact = osm.haversine_m(a, b)
    assert osm.planar_m(a, b) == pytest.approx(exact, rel=0.0015)


def test_planar_error_is_pinned_where_it_stops_being_negligible():
    """It is an approximation and the error grows with the separation. Nothing may
    measure a national span with it -- 2 km of the 262 km to Manchester."""
    london, manchester = (51.5074, -0.1278), (53.4808, -2.2426)
    exact = osm.haversine_m(london, manchester)
    drift = osm.planar_m(london, manchester) - exact
    assert 2_000 < drift < 2_200
    assert drift / exact == pytest.approx(0.0079, abs=0.0005)


# --- Preparing a relation ----------------------------------------------------

# A line running due north up the prime-ish meridian, in two ways that join: 0.2
# degrees of latitude, which is 22.26 km in the plane this measures in.
_SOUTH, _MID, _NORTH = (51.0, -1.0), (51.1, -1.0), (51.2, -1.0)
_LINE_M = 0.2 * osm.M_PER_DEG_LAT


def _line() -> osm.Relation:
    return builders.relation(
        ways=[builders.way(10, [_SOUTH, _MID]), builders.way(11, [_MID, _NORTH])],
        stops=[
            builders.stop(100, "Alpha Rail Station", *_SOUTH),
            builders.stop(101, "Beta Rail Station", *_NORTH),
        ],
    )


def test_prepare_chains_and_measures_one_relation():
    m = osm.prepare(_line())
    assert m.chains and m.breaks == 0
    assert m.points == [_SOUTH, _MID, _NORTH]
    assert m.way_ids == [10, 11]
    assert m.way_at == [10, 10, 11]
    assert m.cum[-1] == pytest.approx(_LINE_M, rel=1e-6)
    assert m.ref_lat == _SOUTH[0]


def test_prepare_normalises_one_name_per_stop():
    """One per stop, including an unnamed one, so a caller matching a stop sequence
    can index `relation.stops` with the position it matched at."""
    rel = builders.relation(
        stops=[
            builders.stop(100, "Alpha Rail Station", *_SOUTH),
            builders.stop(101, None, *_MID),
            builders.stop(102, "Beta Underground Station", *_NORTH),
        ]
    )
    assert osm.prepare(rel).names == ["alpha", "", "beta"]


def test_prepare_reports_a_break_rather_than_refusing_it():
    """Every caller gates differently -- one counts breaks apart from its other
    refusals -- so nothing is dropped here and the chain is handed back as it came."""
    m = osm.prepare(builders.broken_relation())
    assert m.breaks == 1
    assert not m.chains


def test_prepare_survives_a_relation_with_nothing_to_measure():
    """A relation whose ways Overpass could not resolve has no first point to hang a
    plane on, and asking it for one must not raise on the failure path."""
    m = osm.prepare(builders.relation(ways=[]))
    assert not m.chains
    assert m.metres == [] and m.cum == [0.0]


def test_prepare_places_a_stop_along_the_chain():
    """`trace` cuts a pattern out of a line with the first number and refuses a stop
    that is not on the line with the second."""
    m = osm.prepare(_line())
    along, off = m.place(*_MID)
    assert along == pytest.approx(_LINE_M / 2, rel=1e-6)
    assert off == pytest.approx(0.0, abs=1e-6)

    # A stop 300 m east of the track is on some other line that shares the name.
    # In the plane the chain was projected into, which passes through its first
    # point rather than through this stop.
    east = _MID[1] + 300 / (osm.M_PER_DEG_LAT * math.cos(math.radians(m.ref_lat)))
    _, off = m.place(_MID[0], east)
    assert off == pytest.approx(300.0, rel=1e-3)


# --- Fetching ----------------------------------------------------------------


class _Answers:
    """A session that answers each request with the next canned response, raising
    one where it is an exception. `builders.FakeSession` gives one answer for ever,
    which cannot show a retry recovering."""

    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.calls = 0

    def post(self, url: str, **_: object) -> object:
        answer = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def waits(monkeypatch) -> list[float]:
    """Every backoff the fetch asks for, and none of them actually taken."""
    out: list[float] = []
    monkeypatch.setattr(osm.time, "sleep", out.append)
    return out


def test_fetch_retries_a_refused_slot_and_carries_on(waits, tmp_path):
    """A national query is the only request a `trace` run makes, so one 429 arriving
    after a minutes-long wait used to end the stage having learned nothing."""
    body = builders.overpass([builders.member_way(10, [(51.0, -1.0), (51.1, -1.0)])], [])
    sess = _Answers(builders.FakeResponse({}, 429), builders.FakeResponse(body, 200))
    got = osm.fetch((51.0, -1.0, 52.0, 1.0), tmp_path / "r.json", session=sess)

    assert [r.relation_id for r in got] == [900]
    assert sess.calls == 2
    assert waits == [osm.OVERPASS_BACKOFF]
    # The body that came back is still cached, so the retry is not paid for twice.
    assert (tmp_path / "r.json").exists()


def test_fetch_gives_up_and_still_raises_the_retryable_error(waits, tmp_path):
    """The caller's handling of a spent transport error is unchanged: `trace` writes
    nothing down, because nothing was learned about any pattern."""
    sess = _Answers(builders.FakeResponse({}, 429))
    with pytest.raises(osm.TransportError):
        osm.fetch((51.0, -1.0, 52.0, 1.0), tmp_path / "r.json", session=sess)

    assert sess.calls == osm.OVERPASS_RETRIES
    assert waits == [osm.OVERPASS_BACKOFF * n for n in range(1, osm.OVERPASS_RETRIES)]
    assert not (tmp_path / "r.json").exists()


def test_fetch_retries_a_connection_that_never_arrived(waits, tmp_path):
    sess = _Answers(requests.ConnectionError("refused"))
    with pytest.raises(osm.TransportError):
        osm.fetch((51.0, -1.0, 52.0, 1.0), tmp_path / "r.json", session=sess)
    assert sess.calls == osm.OVERPASS_RETRIES


def test_fetch_does_not_retry_a_query_the_server_refused(waits, tmp_path):
    """A malformed query will be just as malformed in thirty seconds, and Overpass
    is a metered public service."""
    sess = _Answers(builders.FakeResponse("line 3: parse error", 400))
    with pytest.raises(osm.OverpassError):
        osm.fetch((51.0, -1.0, 52.0, 1.0), tmp_path / "r.json", session=sess)

    assert sess.calls == 1
    assert waits == []
