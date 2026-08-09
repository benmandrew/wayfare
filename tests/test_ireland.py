"""The Republic of Ireland, the first source in this project that is not BODS.

Three things about it are different in kind rather than in degree: the National
Transport Authority publishes it, the licence carries an attribution condition,
and its ``feed_version`` is a GUID where every other feed here stamps a timestamp.
The last of those is the one the incremental machinery cares about.
"""

from __future__ import annotations

from pathlib import Path

from wayfare import acquire, config, db, gtfs

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
    assert feed.licence == config.OGL


def test_the_licence_is_the_one_thing_that_differs_with_an_obligation():
    """CC BY 4.0 rather than OGL, so crediting the NTA is a condition of using the
    data and not a courtesy. Nothing else in this project is licensed that way."""
    assert config.feed("ireland").licence == config.CC_BY_4
    assert config.feed("ireland").attribution == "National Transport Authority"
    assert config.feed("wales").licence == config.OGL


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


def test_the_same_publication_gives_the_same_version_twice(gtfs_dir: Path):
    """It is a cache key, so an unstable one would re-match a whole country."""
    _feed_info(gtfs_dir, NTA_FEED_INFO.format(guid=GUID))
    assert acquire.feed_version(gtfs_dir) == acquire.feed_version(gtfs_dir)


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
    """The version used to be read by splitting the line on commas, which reads a
    quoted field as two and hands back whatever ends up under the header."""
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
