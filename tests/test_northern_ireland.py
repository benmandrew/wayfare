"""Northern Ireland: Translink's four OpenDataNI datasets, assembled into GTFS.

Nothing else in this project builds its own feed, so the checks here are mostly
about the two joins that hold it together -- MIF objects against MID rows, and
road geometry against the timetable through the NaPTAN ATCO code -- and about the
ids that reach `pattern_id`, which is a permanent match-cache key.

No test needs the real download. The MapInfo fixtures are hand-written and small
enough to read, which is the point: they are the only genuinely new parsing code
in the region.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from wayfare import acquire, config, db, gtfs, mapinfo, translink

# --- MapInfo fixtures --------------------------------------------------------

# Two stopping points, each with the ATCO code that joins them to the timetable.
STOPPING_MIF = """Version 600
Charset "WindowsLatin1"
Delimiter ","
CoordSys Earth Projection 1, 104
Columns 5
  SubNetwork Char(4)
  StopID Integer
  StopAreaID Integer
  StoppingPointID Char(6)
  GlobalId Char(50)
Data

POINT -5.9300 54.5900
POINT -5.9200 54.5910
POINT -5.9100 54.5920
"""
STOPPING_MID = (
    '"nir", 1, 0, "11", "700000000001"\n'
    '"nir", 2, 0, "22", "700000000002"\n'
    '"nir", 3, 0, "33", "700000000003"\n'
)

# Four objects for four attribute rows. The third is a `None`: an attribute row
# with no geometry, which is the trap this format sets. The second is a longer
# alternative for the same pair as the first, so the shortest-wins rule is
# exercised, and the fourth links a pair no journey uses.
LINKS_MIF = """Version 600
Charset "WindowsLatin1"
Delimiter ","
CoordSys Earth Projection 1, 104
Columns 7
  SubNetwork Char(4)
  FromStopID Integer
  FromStopAreaID Integer
  FromStoppingPointID Char(6)
  ToStopID Integer
  ToStopAreaID Integer
  ToStoppingPointID Char(6)
Data

PLINE 3
-5.9300 54.5900
-5.9250 54.5905
-5.9200 54.5910
PLINE 4
-5.9300 54.5900
-5.9250 54.5800
-5.9250 54.5905
-5.9200 54.5910
None
PLINE 2
-5.9200 54.5910
-5.9100 54.5920
"""
LINKS_MID = (
    '"nir", 1, 0, "11", 2, 0, "22"\n'
    '"nir", 1, 0, "11", 2, 0, "22"\n'
    '"nir", 3, 0, "33", 1, 0, "11"\n'
    '"nir", 2, 0, "22", 3, 0, "33"\n'
)

# --- TransXChange fixture ----------------------------------------------------

# One Ulsterbus service, two journey patterns and three journeys. The element
# order is the order TransXChange guarantees, which is what lets the reader work
# in one forward pass.
TXC = """<?xml version="1.0" encoding="Windows-1252"?>
<TransXChange CreationDateTime="2026-08-06T14:07:51.1447055+01:00"
  SchemaVersion="2.4" xmlns="http://www.transxchange.org.uk/">
  <StopPoints>
    <StopPoint><AtcoCode>700000000001</AtcoCode>
      <Descriptor><CommonName>Alpha</CommonName></Descriptor>
      <Place><Location><Longitude>-5.9300</Longitude><Latitude>54.5900</Latitude>
      </Location></Place></StopPoint>
    <StopPoint><AtcoCode>700000000002</AtcoCode>
      <Descriptor><CommonName>Bravo</CommonName></Descriptor>
      <Place><Location><Longitude>-5.9200</Longitude><Latitude>54.5910</Latitude>
      </Location></Place></StopPoint>
    <StopPoint><AtcoCode>700000000003</AtcoCode>
      <Descriptor><CommonName>Charlie</CommonName></Descriptor>
      <Place><Location><Longitude>-5.9100</Longitude><Latitude>54.5920</Latitude>
      </Location></Place></StopPoint>
    <StopPoint><AtcoCode>700000000009</AtcoCode>
      <Descriptor><CommonName>Null Island</CommonName></Descriptor>
      <Place><Location><Longitude>0.0</Longitude><Latitude>0.0</Latitude>
      </Location></Place></StopPoint>
  </StopPoints>
  <JourneyPatternSections>
    <JourneyPatternSection id="JPS1">
      <JourneyPatternTimingLink id="L1">
        <From SequenceNumber="1"><StopPointRef>700000000001</StopPointRef></From>
        <To SequenceNumber="2"><StopPointRef>700000000002</StopPointRef></To>
        <RunTime>PT2M30S</RunTime></JourneyPatternTimingLink>
      <JourneyPatternTimingLink id="L2">
        <From SequenceNumber="2"><StopPointRef>700000000002</StopPointRef></From>
        <To SequenceNumber="3"><StopPointRef>700000000003</StopPointRef></To>
        <RunTime>PT90S</RunTime></JourneyPatternTimingLink>
    </JourneyPatternSection>
    <JourneyPatternSection id="JPS2">
      <JourneyPatternTimingLink id="L3">
        <From SequenceNumber="1"><StopPointRef>700000000003</StopPointRef></From>
        <To SequenceNumber="2"><StopPointRef>700000000009</StopPointRef></To>
        <RunTime>PT1M</RunTime></JourneyPatternTimingLink>
    </JourneyPatternSection>
  </JourneyPatternSections>
  <Operators>
    <Operator id="OId_ULB"><OperatorCode>ULB</OperatorCode>
      <OperatorShortName>Ulsterbus</OperatorShortName></Operator>
  </Operators>
  <Services>
    <Service>
      <ServiceCode>2-40-_-y18-1</ServiceCode>
      <Lines><Line id="2-40-_-y18-1"><LineName>40</LineName></Line></Lines>
      <OperatingPeriod><StartDate>2026-08-06</StartDate>
        <EndDate>2026-08-31</EndDate></OperatingPeriod>
      <OperatingProfile><RegularDayType><DaysOfWeek><MondayToFriday/>
        </DaysOfWeek></RegularDayType></OperatingProfile>
      <RegisteredOperatorRef>OId_ULB</RegisteredOperatorRef>
      <Mode>bus</Mode>
      <Description>Alpha - Charlie</Description>
      <StandardService><Origin>Alpha</Origin><Destination>Charlie</Destination>
        <JourneyPattern id="JP1"><Direction>outbound</Direction>
          <JourneyPatternSectionRefs>JPS1</JourneyPatternSectionRefs></JourneyPattern>
        <JourneyPattern id="JP2"><Direction>inbound</Direction>
          <JourneyPatternSectionRefs>JPS2</JourneyPatternSectionRefs></JourneyPattern>
      </StandardService>
    </Service>
  </Services>
  <VehicleJourneys>
    <VehicleJourney><VehicleJourneyCode>VJ1</VehicleJourneyCode>
      <ServiceRef>2-40-_-y18-1</ServiceRef><LineRef>2-40-_-y18-1</LineRef>
      <JourneyPatternRef>JP1</JourneyPatternRef>
      <DepartureTime>09:00:00</DepartureTime></VehicleJourney>
    <VehicleJourney>
      <OperatingProfile><RegularDayType><DaysOfWeek><Saturday/>
        </DaysOfWeek></RegularDayType></OperatingProfile>
      <VehicleJourneyCode>VJ2</VehicleJourneyCode>
      <ServiceRef>2-40-_-y18-1</ServiceRef><LineRef>2-40-_-y18-1</LineRef>
      <JourneyPatternRef>JP1</JourneyPatternRef>
      <DepartureTime>10:00:00</DepartureTime></VehicleJourney>
    <VehicleJourney><VehicleJourneyCode>VJ3</VehicleJourneyCode>
      <ServiceRef>2-40-_-y18-1</ServiceRef><LineRef>2-40-_-y18-1</LineRef>
      <JourneyPatternRef>JP2</JourneyPatternRef>
      <DepartureTime>11:00:00</DepartureTime></VehicleJourney>
  </VehicleJourneys>
</TransXChange>
"""


@pytest.fixture
def geometry_zip(tmp_path: Path) -> Path:
    z = tmp_path / "routes.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("StoppingPoints.stp.MIF", STOPPING_MIF)
        zf.writestr("StoppingPoints.stp.MID", STOPPING_MID)
        zf.writestr("PtLinks_t.ptl.MIF", LINKS_MIF)
        zf.writestr("PtLinks_t.ptl.MID", LINKS_MID)
    return z


@pytest.fixture
def timetable_zip(tmp_path: Path) -> Path:
    z = tmp_path / "timetable.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("TXC_24_2026-08-06T14-07-45.xml", TXC)
    return z


@pytest.fixture
def ni_gtfs(tmp_path: Path, timetable_zip: Path, geometry_zip: Path) -> Path:
    out = tmp_path / "ni.zip"
    translink.build_gtfs([timetable_zip], [geometry_zip], out)
    dest = tmp_path / "unpacked"
    return acquire.unpack_gtfs(out, dest=dest)


def _mif_mid(tmp_path: Path, mif: str, mid: str) -> tuple[Path, Path]:
    a, b = tmp_path / "t.MIF", tmp_path / "t.MID"
    a.write_text(mif)
    b.write_text(mid)
    return a, b


# --- The MIF/MID reader ------------------------------------------------------


def test_the_header_names_the_columns_and_the_delimiter(tmp_path: Path):
    lines = iter(STOPPING_MIF.splitlines())
    head = mapinfo.header(lines)
    assert head.columns[:4] == ("SubNetwork", "StopID", "StopAreaID", "StoppingPointID")
    assert head.delimiter == ","
    assert head.encoding == "cp1252"
    # The iterator is left on the first object, not on the header's last line.
    assert next(lines).strip() in ("", "POINT -5.9300 54.5900")


def test_attributes_join_to_geometry_by_position_alone(tmp_path: Path):
    mif, mid = _mif_mid(tmp_path, LINKS_MIF, LINKS_MID)
    features = list(mapinfo.read(mif, mid))
    assert len(features) == 4
    assert [len(f.points) for f in features] == [3, 4, 0, 2]
    assert [f.values["ToStoppingPointID"] for f in features] == ["22", "22", "11", "33"]


def test_a_none_object_carries_a_row_and_no_geometry(tmp_path: Path):
    """This is the whole reason the reader knows the object types it knows. A
    `None` line does not look like a keyword, and dropping it would shift every
    attribute row after it onto the wrong road."""
    mif, mid = _mif_mid(tmp_path, LINKS_MIF, LINKS_MID)
    third = list(mapinfo.read(mif, mid))[2]
    assert third.points == ()
    assert third.values["FromStoppingPointID"] == "33"


def test_a_point_object_is_one_coordinate(tmp_path: Path):
    mif, mid = _mif_mid(tmp_path, STOPPING_MIF, STOPPING_MID)
    features = list(mapinfo.read(mif, mid))
    assert features[0].points == ((-5.93, 54.59),)
    assert features[0].values["GlobalId"] == "700000000001"


def test_an_object_type_the_reader_does_not_know_is_an_error(tmp_path: Path):
    mif, mid = _mif_mid(
        tmp_path, STOPPING_MIF.replace("POINT -5.9300 54.5900", "ARC 1 2 3 4"), STOPPING_MID
    )
    with pytest.raises(mapinfo.Malformed, match="ARC"):
        list(mapinfo.read(mif, mid))


def test_more_attribute_rows_than_objects_is_an_error(tmp_path: Path):
    mif, mid = _mif_mid(tmp_path, STOPPING_MIF, STOPPING_MID + '"nir", 4, 0, "44", "7"\n')
    with pytest.raises(mapinfo.Malformed, match="more rows"):
        list(mapinfo.read(mif, mid))


def test_fewer_attribute_rows_than_objects_is_an_error(tmp_path: Path):
    mif, mid = _mif_mid(tmp_path, STOPPING_MIF, '"nir", 1, 0, "11", "700000000001"\n')
    with pytest.raises(mapinfo.Malformed, match="ran out of rows"):
        list(mapinfo.read(mif, mid))


def test_a_projected_coordinate_system_is_refused(tmp_path: Path):
    """Irish Grid eastings read as degrees are still numbers, so this would put
    Belfast in the Atlantic and raise nothing."""
    mif, mid = _mif_mid(
        tmp_path,
        STOPPING_MIF.replace(
            "CoordSys Earth Projection 1, 104", "CoordSys Earth Projection 8, 79"
        ),
        STOPPING_MID,
    )
    with pytest.raises(mapinfo.Malformed, match="CoordSys"):
        list(mapinfo.read(mif, mid))


# --- Joining geometry to the timetable ---------------------------------------


def test_the_shortest_variant_of_a_hop_is_the_one_kept(tmp_path: Path, geometry_zip):
    """A pair usually has one polyline per line and branch that runs it. The pick
    has to be a property of the geometry, not of file order, or a re-export
    changes every shape and therefore every match."""
    mifs = translink._extract([geometry_zip], tmp_path, translink.LINKS_MEMBER)
    stops = translink._extract([geometry_zip], tmp_path, translink.STOPS_MEMBER)
    hops = translink._hop_geometry(mifs, translink._atco_by_point(stops))
    assert len(hops[("700000000001", "700000000002")]) == 3


def test_a_journey_missing_one_hop_carries_no_geometry_at_all():
    """Stitching around a gap hands the matcher a straight line across a town,
    which map_snap lays down the wrong roads with confidence. No shape sends the
    pattern to the `stops` path instead, which is what that path is for."""
    hops = {("A", "B"): ((0.0, 0.0), (1.0, 1.0))}
    assert translink._stitch(("A", "B"), hops) is not None
    assert translink._stitch(("A", "B", "C"), hops) is None


def test_the_joint_between_two_hops_is_not_repeated():
    hops = {
        ("A", "B"): ((0.0, 0.0), (1.0, 1.0)),
        ("B", "C"): ((1.0, 1.0), (2.0, 2.0)),
    }
    assert translink._stitch(("A", "B", "C"), hops) == (
        (0.0, 0.0),
        (1.0, 1.0),
        (2.0, 2.0),
    )


# --- What reaches the pattern identity ---------------------------------------


def test_a_route_id_is_the_operator_and_the_line_not_the_service_code(ni_gtfs: Path):
    """`pattern_id` hashes route_id and is a permanent match-cache key. The
    ServiceCode `2-40-_-y18-1` carries an operating branch, a schedule dataset tag
    and a registration revision, all three of which move without the bus
    changing."""
    routes = (ni_gtfs / "routes.txt").read_text().splitlines()
    assert routes[1].startswith("ULB-40,ULB,40,")
    assert "y18" not in (ni_gtfs / "routes.txt").read_text()


def test_stop_ids_are_the_naptan_atco_codes_verbatim(ni_gtfs: Path):
    """The exact list, because it is also what proves 700000000009 was dropped:
    Translink ships stops at exactly 0.0, which is in the Gulf of Guinea and passes
    every IS NOT NULL test there is."""
    ids = [ln.split(",")[0] for ln in (ni_gtfs / "stops.txt").read_text().splitlines()[1:]]
    assert ids == ["700000000001", "700000000002", "700000000003"]


def test_a_stop_at_zero_latitude_is_dropped_on_load(gtfs_dir: Path, con):
    (gtfs_dir / "stops.txt").write_text(
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "S1,Alpha,53.4800,-2.2450\nS2,Bravo,53.4800,-2.2400\n"
        "S3,Charlie,53.4800,-2.2350\nS4,Delta,53.4800,-2.2300\n"
        "P1,Pier,53.4050,-2.9960\nP2,Quay,53.3200,-3.1800\n"
        "Z1,Null Island,0.0,0.0\n"
    )
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
    assert db.scalar(con, "SELECT count(*) FROM stops WHERE stop_id = 'Z1'") == 0


def test_a_shape_id_is_a_function_of_the_stop_sequence(ni_gtfs: Path):
    """`gtfs.py` collapses several journey patterns with `mode(shape_id)`, which
    has no tiebreak. Two patterns over the same stops therefore have to agree on
    one id rather than be picked between."""
    assert translink._shape_id(("A", "B")) == translink._shape_id(("A", "B"))
    assert translink._shape_id(("A", "B")) != translink._shape_id(("B", "A"))


def test_direction_is_transxchanges_own_and_totally_mapped():
    assert translink._direction("outbound") == "0"
    assert translink._direction("inbound") == "1"
    assert translink._direction("circular") == "0"


def test_a_calendar_id_is_prefixed_so_the_two_irelands_can_share_a_database(
    ni_gtfs: Path,
):
    """Translink's day patterns and the Republic's service ids are both numeric
    and they collide. One OSM extract covers the island, so the two feeds are
    meant to be able to sit in one DuckDB file."""
    ids = [
        ln.split(",")[0] for ln in (ni_gtfs / "calendar.txt").read_text().splitlines()[1:]
    ]
    assert all(i.startswith("NI-") for i in ids)
    assert "NI-1111100-20260806-20260831" in ids


# --- The assembled bundle ----------------------------------------------------


def test_the_bundle_holds_everything_the_pipeline_reads(ni_gtfs: Path):
    for name in acquire.REQUIRED_GTFS:
        assert (ni_gtfs / name).exists()
    assert (ni_gtfs / "shapes.txt").exists()


def test_run_times_accumulate_into_stop_times(ni_gtfs: Path):
    rows = [
        ln.split(",")
        for ln in (ni_gtfs / "stop_times.txt").read_text().splitlines()[1:]
        if ln.startswith("VJ1,")
    ]
    assert [r[1] for r in rows] == ["09:00:00", "09:02:30", "09:04:00"]
    assert [r[3] for r in rows] == ["700000000001", "700000000002", "700000000003"]


def test_a_journey_takes_its_own_days_over_its_services(ni_gtfs: Path):
    trips = {
        ln.split(",")[2]: ln.split(",")[1]
        for ln in (ni_gtfs / "trips.txt").read_text().splitlines()[1:]
    }
    assert trips["VJ1"].startswith("NI-1111100")
    assert trips["VJ2"].startswith("NI-0000010")


def test_two_builds_of_one_publication_are_byte_identical(
    tmp_path, timetable_zip, geometry_zip
):
    """This is the one feed in the project whose bytes are ours to make
    comparable, so every row is written in a defined order and the archive
    carries a fixed timestamp rather than the moment it was staged."""
    a = translink.build_gtfs([timetable_zip], [geometry_zip], tmp_path / "a.zip")
    b = translink.build_gtfs([timetable_zip], [geometry_zip], tmp_path / "b.zip")
    assert a.read_bytes() == b.read_bytes()


def test_the_feed_version_is_the_timetables_build_stamp(ni_gtfs: Path):
    """A sortable timestamp, the same shape BODS stamps, so `acquire.feed_version`
    passes it through and the incremental machinery keeps working."""
    assert acquire.feed_version(ni_gtfs) == "20260806_140751"


def test_patterns_build_from_the_assembled_feed(ni_gtfs: Path, con):
    gtfs.build_patterns(ni_gtfs, con, memory_limit="1GB")
    rows = con.execute(
        "SELECT route_id, direction, n_stops, shape_id IS NOT NULL "
        "FROM patterns ORDER BY direction"
    ).fetchall()
    # The outbound journeys share one pattern and have geometry for every hop.
    # The inbound one calls at a stop that was dropped for its coordinates, so it
    # is a two-stop pattern with no shape.
    assert rows[0] == ("ULB-40", 0, 3, True)
    assert rows[1][3] is False


# --- The source --------------------------------------------------------------


class FakeCkan:
    """CKAN's package_show, with the shape the real one has: several formats in
    one dataset and several ZIPs of different vintages."""

    def __init__(self, body):
        self.body = body
        self.seen = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.seen.append((url, params))
        return self

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


PACKAGE = {
    "success": True,
    "result": {
        "resources": [
            {"format": "PDF", "url": "https://x/spec.pdf", "created": "2030-01-01"},
            {
                "format": "ZIP",
                "url": "https://x/metro-glider-routes_20220916.zip",
                "last_modified": "2022-09-16T11:25:21",
                "size": "10",
            },
            {
                "format": "ZIP",
                "url": "https://x/metro-glider-routes-updated-23092025.zip",
                "last_modified": "2025-09-23T14:19:15",
                "size": "2691616",
            },
        ]
    },
}


def test_the_newest_zip_is_the_one_taken():
    """Resource ids and filenames move on every publication, and a dataset keeps
    its old bundles beside the current one -- the 2022 Glider routes sit in the
    same dataset as the 2025 Metro ones."""
    fake = FakeCkan(PACKAGE)
    res = translink.resource("translink-metro-bus-routes", session=fake)
    assert res.filename == "metro-glider-routes-updated-23092025.zip"
    assert res.size == 2691616
    assert fake.seen[0] == (config.OPENDATANI_API, {"id": "translink-metro-bus-routes"})


def test_a_dataset_with_no_zip_is_an_error():
    body = {"success": True, "result": {"resources": [PACKAGE["result"]["resources"][0]]}}
    with pytest.raises(RuntimeError, match="no ZIP"):
        translink.resource("x", session=FakeCkan(body))


def test_a_refused_package_show_is_an_error():
    with pytest.raises(RuntimeError, match="package_show"):
        translink.resource("x", session=FakeCkan({"success": False}))


def test_the_province_is_four_datasets_and_no_naptan(monkeypatch):
    feed = config.feed("northern_ireland")
    assert feed.url == ""
    assert [p.kind for p in feed.parts] == [
        "timetable",
        "timetable",
        "geometry",
        "geometry",
    ]
    assert not feed.stop_register
    assert feed.licence == config.OGL


def test_an_assembled_bundle_is_not_rebuilt_while_its_parts_stand(
    tmp_path, monkeypatch, timetable_zip, geometry_zip
):
    """Assembling is a minute of XML. It is keyed on the parts' sizes and
    timestamps rather than their contents, because re-reading 130 MB to decide
    whether to re-read it saves nothing."""
    monkeypatch.setattr(config, "WORK", tmp_path)
    feed = config.feed("northern_ireland")
    parts = {
        "timetable_ulsterbus": timetable_zip,
        "timetable_metro": timetable_zip,
        "routes_ulsterbus": geometry_zip,
        "routes_metro": geometry_zip,
    }
    calls = []
    real = translink.build_gtfs
    monkeypatch.setattr(
        translink,
        "build_gtfs",
        lambda t, g, o: (calls.append(1), real(t, g, o))[1],
    )
    first = acquire.assemble(feed, parts)
    acquire.assemble(feed, parts)
    assert len(calls) == 1
    assert first.exists()
