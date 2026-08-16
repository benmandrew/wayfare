"""The Republic of Ireland, the first source in this project that is not BODS.

Three things about it are different in kind rather than in degree: the National
Transport Authority publishes it, the licence carries an attribution condition,
and its ``feed_version`` is a GUID where every other feed here stamps a timestamp.
The last of those is the one the incremental machinery cares about.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from wayfare import acquire, config, db, gtfs, licences, publish

# The real header and row, GUID and all, taken from the 2026-08-08 publication.
NTA_FEED_INFO = (
    "feed_publisher_name,feed_publisher_url,feed_lang,feed_start_date,feed_end_date,"
    "feed_version,feed_contact_email\n"
    "National Transport Authority,https://www.nationaltransport.ie/,en,20260808,"
    "20270808,{guid},apisupport@nationaltransport.ie\n"
)
GUID = "B375DFAC-C156-4A9E-A642-8DF76AAA2A51"


def _feed_info(gtfs_dir: Path, body: str) -> Path:
    (gtfs_dir / "feed_info.txt").write_text(body)
    return gtfs_dir


# --- The source --------------------------------------------------------------


def test_the_feed_comes_from_the_nta_and_not_from_bods():
    feed = config.feed("ireland")
    assert feed.url == config.NTA_GTFS_URL
    assert feed.filename == "nta_gtfs_ireland.zip"


def test_an_unrecognised_slug_is_still_a_bods_region():
    """FEEDS holds the exceptions. Everything else is a BODS slug built on demand,
    so adding a region there must not need an entry here."""
    feed = config.feed("north_east")
    assert feed.url == config.BODS_GTFS_URL.format(region="north_east")
    assert feed.filename == "bods_gtfs_north_east.zip"
    assert feed.licence == licences.OGL


def test_the_licence_is_the_one_thing_that_differs_with_an_obligation():
    """CC BY 4.0 rather than OGL, so crediting the NTA is a condition of using the
    data and not a courtesy. Nothing else in this project is licensed that way."""
    assert config.feed("ireland").licence == licences.CC_BY_4
    assert config.feed("ireland").attribution == "National Transport Authority"
    assert config.feed("wales").licence == licences.OGL


def test_the_licence_is_reported_on_every_run(tmp_path, monkeypatch, caplog):
    """Cache hit or not. The run that fetches the data is the last moment at which
    nobody has yet forgotten where it came from."""
    import logging

    monkeypatch.setattr(config, "RAW", tmp_path)
    monkeypatch.setattr(config, "WORK", tmp_path)
    monkeypatch.setattr(config, "OUT", tmp_path)
    monkeypatch.setattr(acquire, "download", lambda src, **k: tmp_path / src.filename)
    monkeypatch.setattr(acquire, "unpack_gtfs", lambda z, **k: tmp_path)
    with caplog.at_level(logging.INFO, logger="wayfare.acquire"):
        acquire.acquire_all(region="ireland")
    assert any("National Transport Authority" in r.getMessage() for r in caplog.records)


def test_naptan_is_not_fetched_for_a_region_it_does_not_cover():
    """NaPTAN is the GB stop register: 102 MB of stops no Irish service calls at."""
    assert [s.name for s in acquire.sources("ireland")] == ["gtfs"]
    assert "naptan" in [s.name for s in acquire.sources("wales")]


def test_the_nta_host_resumes_where_bods_does_not():
    """Measured against both: the NTA answers a Range request with a 206 and sends
    a Content-Length, BODS does neither."""
    assert acquire.sources("ireland")[0].resumable
    assert not acquire.sources("wales")[0].resumable


def test_both_halves_of_the_island_read_one_osm_extract(monkeypatch):
    """Geofabrik splits Ireland at the sea, not at the border. One extract is one
    graph build and therefore one GraphId space, which is what would let the
    Republic and Northern Ireland share a data root later."""
    monkeypatch.delenv("WAYFARE_OSM_URL", raising=False)
    assert config.osm_url("ireland") == config.osm_url("northern_ireland")
    assert config.osm_url("ireland") != config.osm_url("all")


# --- A feed version that describes nothing -----------------------------------


def test_a_guid_version_is_dated_so_two_feeds_can_be_compared(gtfs_dir: Path):
    _feed_info(gtfs_dir, NTA_FEED_INFO.format(guid=GUID))
    assert acquire.feed_version(gtfs_dir) == "20260808_b375dfac"


def test_two_publications_in_one_validity_window_stay_distinct(gtfs_dir: Path):
    """feed_start_date alone would collide: the NTA declares a year-long window and
    republishes inside it. The GUID digits are what keep the two apart."""
    _feed_info(gtfs_dir, NTA_FEED_INFO.format(guid=GUID))
    first = acquire.feed_version(gtfs_dir)
    _feed_info(gtfs_dir, NTA_FEED_INFO.format(guid="0FA1CE00-" + GUID[9:]))
    assert acquire.feed_version(gtfs_dir) != first


def test_a_bods_timestamp_is_left_exactly_as_it_is(gtfs_dir: Path):
    """Only an opaque version is rewritten. A timestamp already sorts, and changing
    it would orphan every pattern in an existing database."""
    assert acquire.feed_version(gtfs_dir) == "20260806_022608"


def test_a_guid_with_no_start_date_is_still_a_version(gtfs_dir: Path):
    _feed_info(
        gtfs_dir,
        f"feed_publisher_name,feed_lang,feed_version\nNTA,en,{GUID}\n",
    )
    assert acquire.feed_version(gtfs_dir) == GUID.lower()


def test_a_publisher_name_holding_a_comma_does_not_shift_the_columns(gtfs_dir: Path):
    """feed_info.txt is CSV, so a quoted publisher name holding a comma is one field.
    Splitting the line on commas reads it as two and shifts every column after it."""
    _feed_info(
        gtfs_dir,
        "feed_publisher_name,feed_lang,feed_start_date,feed_version\n"
        f'"Authority, National",en,20260808,{GUID}\n',
    )
    assert acquire.feed_version(gtfs_dir) == "20260808_b375dfac"
    assert acquire.feed_info(gtfs_dir)["feed_publisher_name"] == "Authority, National"


def test_a_feed_with_no_feed_info_has_no_version(gtfs_dir: Path):
    (gtfs_dir / "feed_info.txt").unlink()
    assert acquire.feed_version(gtfs_dir) == "unknown"
    assert acquire.feed_info(gtfs_dir) == {}


# --- What the version is for -------------------------------------------------


def test_patterns_are_labelled_with_the_derived_version(gtfs_dir: Path, con):
    _feed_info(gtfs_dir, NTA_FEED_INFO.format(guid=GUID))
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
    assert db.get_meta(con, "feed_version") == "20260808_b375dfac"
    assert con.execute("SELECT DISTINCT last_seen FROM patterns").fetchall() == [
        ("20260808_b375dfac",)
    ]


def test_a_republication_moves_the_version_so_a_departure_is_seen(gtfs_dir: Path, con):
    """The whole incremental design keys on the feed version, and the GUID is the
    only field that moves when the NTA republishes inside its validity window. A
    version that failed to move would leave a withdrawn service looking live for
    ever, because every consumer of `patterns` filters on last_seen."""
    _feed_info(gtfs_dir, NTA_FEED_INFO.format(guid=GUID))
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
    assert con.execute("SELECT count(*) FROM patterns").fetchone()[0] == 2

    # The short working T3 leaves the timetable, and the feed is republished.
    (gtfs_dir / "trips.txt").write_text(
        "route_id,service_id,trip_id,direction_id,shape_id\n"
        "R1,WK,T1,0,SH1\nR1,WK,T2,0,SH1\n"
    )
    (gtfs_dir / "stop_times.txt").write_text(
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T1,09:00:00,09:00:00,S1,1\n"
        "T1,09:05:00,09:05:00,S2,2\n"
        "T1,09:10:00,09:10:00,S3,3\n"
        "T1,09:15:00,09:15:00,S4,4\n"
        "T2,10:00:00,10:00:00,S1,1\n"
        "T2,10:05:00,10:05:00,S2,2\n"
        "T2,10:10:00,10:10:00,S3,3\n"
        "T2,10:15:00,10:15:00,S4,4\n"
    )
    _feed_info(gtfs_dir, NTA_FEED_INFO.format(guid="0FA1CE00-" + GUID[9:]))
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")

    live, departed = con.execute(
        f"SELECT count(*) FILTER (WHERE {db.current_feed()}), "
        f"count(*) FILTER (WHERE NOT ({db.current_feed()})) FROM patterns p"
    ).fetchone()
    assert (live, departed) == (1, 1)


# --- A second region in one data root ----------------------------------------
#
# One data root serves one region. Nothing refuses a second one, and the way it goes
# wrong is quiet on both sides: the pipeline reports a healthy run and the map loses
# a country.

DUBLIN = {
    "stops.txt": (
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "D1,O'Connell Street,53.3500,-6.2600\n"
        "D2,Parnell Square,53.3530,-6.2630\n"
    ),
    "routes.txt": (
        "route_id,agency_id,route_short_name,route_long_name,route_type\n"
        "D1R,OP1,16,Dublin city,3\n"
    ),
    "trips.txt": "route_id,service_id,trip_id,direction_id,shape_id\nD1R,WK,TD1,0,\n",
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "TD1,08:00:00,08:00:00,D1,1\n"
        "TD1,08:06:00,08:06:00,D2,2\n"
    ),
}


def _irish_feed(gtfs_dir: Path, into: Path) -> Path:
    """The NTA's feed beside the BODS one: its own stops, its own GUID version."""
    shutil.copytree(gtfs_dir, into)
    for name, body in DUBLIN.items():
        (into / name).write_text(body)
    return _feed_info(into, NTA_FEED_INFO.format(guid=GUID))


def test_a_second_region_retires_the_first_rather_than_joining_it(
    gtfs_dir: Path, con, tmp_path: Path
):
    """`meta.feed_version` holds one value, so two regions cannot both be current.

    Acquiring Ireland into a data root that already holds a British region makes the
    Irish feed the current one, and every consumer of `patterns` filters on exactly
    that. The British patterns are still in the table, still matched, and no longer
    reachable -- they read as services withdrawn from a timetable they were never in.
    """
    gtfs.build_patterns(gtfs_dir, con, memory_limit="1GB")
    assert db.get_meta(con, "feed_version") == "20260806_022608"
    british = {r[0] for r in con.execute("SELECT pattern_id FROM patterns").fetchall()}
    assert len(british) == 2

    gtfs.build_patterns(_irish_feed(gtfs_dir, tmp_path / "nta"), con, memory_limit="1GB")

    assert db.get_meta(con, "feed_version") == "20260808_b375dfac"
    live = {
        r[0]
        for r in con.execute(
            f"SELECT pattern_id FROM patterns p WHERE {db.current_feed()}"
        ).fetchall()
    }
    # Nothing is deleted, so the loss is invisible to any count of the table itself.
    assert db.scalar(con, "SELECT count(*) FROM patterns") == len(british) + 1
    assert not (live & british)


def test_the_second_regions_publish_writes_over_the_first_ones_archive(
    tmp_path: Path, monkeypatch
):
    """The other half of the same fact, and the half that reaches a served map.

    A publish given no `--out` writes `bus.pmtiles` whatever region it just built, so
    the second region's archive lands on the first's. The refusal in `default_out`
    only fires once an archive named after the region is there to be made stale, and
    a data root that has only ever published the default has nothing of the kind.
    """
    monkeypatch.setattr(config, "OUT", tmp_path)
    assert publish.default_out("wales") == publish.default_out("ireland")
    assert publish.default_out("ireland").name == publish.DEFAULT_ARCHIVE
