"""Matching a station name written by one publisher against another's.

`osm.normalise` and `osm.spellings` are the join between a timetable's stops and a
route relation's nodes, and three stages now call them: `trace` cuts a pattern out
of a line with them, `osmroutes` names a relation's stops with them and `railtrips`
indexes its lines by them -- while `naptan` leaves the qualifiers on its station
names because it relies on these stripping them. They were tested inside
`test_trace` while it was the only caller; a change here moves what all of them see.
"""

from __future__ import annotations

import pytest

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
