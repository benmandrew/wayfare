"""NaPTAN, read for the TIPLOC crosswalk that unblocks national rail.

The join is one convention: a rail station's ATCO code is ``9100`` followed by its
TIPLOC. Everything here is about not taking the wrong row -- an entrance instead of
a station, a closed station instead of none -- because both give a coordinate that
looks fine and puts a service in the wrong place.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from wayfare import naptan

COLUMNS = list(naptan.COLUMNS)


def row(
    atco: str = "9100EUSTON",
    name: str = "London Euston Rail Station",
    lat: str = "51.52800",
    lon: str = "-0.13350",
    stop_type: str = "RLY",
    status: str = "active",
) -> dict[str, str]:
    return {
        "ATCOCode": atco,
        "CommonName": name,
        "Latitude": lat,
        "Longitude": lon,
        "StopType": stop_type,
        "Status": status,
    }


def read(rows: list[dict[str, str]]) -> dict[str, naptan.Station]:
    return dict(naptan.stations(rows))


# --- the crosswalk -----------------------------------------------------------


def test_the_atco_code_yields_the_tiploc():
    (tiploc, station) = next(iter(read([row()]).items()))
    assert tiploc == "EUSTON"
    assert station.name == "London Euston Rail Station"
    assert (station.lat, station.lon) == (51.528, -0.1335)
    assert station.atco == "9100EUSTON"


def test_the_operational_suffix_is_left_on_the_name():
    """`osm.normalise` strips it at the join; doing it twice is how a name is lost."""
    assert read([row()])["EUSTON"].name.endswith("Rail Station")


# --- rows that must not be taken ---------------------------------------------


def test_a_station_entrance_is_not_a_station():
    """RSE sits on the street. There are 4,308 of them against 2,715 stations."""
    assert read([row(stop_type="RSE")]) == {}


def test_a_closed_station_is_not_taken():
    """Its name still matches a relation drawn before it shut."""
    assert read([row(status="inactive")]) == {}


def test_a_row_without_a_coordinate_is_not_taken():
    assert read([row(lat="", lon="")]) == {}


def test_a_metro_stop_is_not_a_rail_station():
    assert read([row(atco="9400ZZMAABM", stop_type="MET")]) == {}


def test_the_first_of_a_duplicated_tiploc_wins():
    first = row(name="London Euston Rail Station")
    second = row(name="Euston (something else)", lat="52.0")
    assert read([first, second])["EUSTON"].name == "London Euston Rail Station"


# --- refusals ----------------------------------------------------------------


def test_a_rail_row_without_the_9100_prefix_is_refused():
    """Slicing it anyway would mint a TIPLOC pointing at a different place."""
    with pytest.raises(naptan.Malformed, match="9100"):
        read([row(atco="0100EUSTON")])


def test_a_missing_column_is_refused_on_the_first_row():
    broken = row()
    del broken["Latitude"]
    with pytest.raises(naptan.Malformed, match="missing columns"):
        read([broken])


def test_an_empty_register_is_refused():
    with pytest.raises(naptan.Malformed, match="empty"):
        read([])


# --- resolving a schedule's TIPLOCs ------------------------------------------


def test_resolve_separates_the_placed_from_the_unplaced():
    register = read([row(), row(atco="9100RUGBY", name="Rugby Rail Station")])
    found, missing = naptan.resolve(["EUSTON", "RUGBY", "WATFDJ"], register)
    assert set(found) == {"EUSTON", "RUGBY"}
    assert missing == {"WATFDJ"}


def test_resolve_reports_rather_than_raises():
    """An unresolved TIPLOC is normal -- a depot, a freight terminal, a new station."""
    found, missing = naptan.resolve(["NOWHERE"], {})
    assert found == {}
    assert missing == {"NOWHERE"}


# --- reading the file ---------------------------------------------------------


def test_read_takes_the_csv_from_disk(tmp_path: Path):
    path = tmp_path / "naptan.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerow(row())
        writer.writerow(row(stop_type="BCT", atco="0100BRP90310"))
    register = naptan.read(path)
    assert list(register) == ["EUSTON"]
