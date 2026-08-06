from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from wayfare import db

# A four-stop line running east along a single street, with two trips sharing one
# pattern and a third trip that turns short. Enough to exercise the collapse from
# trips to patterns, the shape_id carry-through, and the span calculation.
MINI = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nOP1,Example,http://x,Europe/London\n",
    "stops.txt": (
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "S1,Alpha,53.4800,-2.2450\n"
        "S2,Bravo,53.4800,-2.2400\n"
        "S3,Charlie,53.4800,-2.2350\n"
        "S4,Delta,53.4800,-2.2300\n"
    ),
    "routes.txt": (
        "route_id,agency_id,route_short_name,route_long_name,route_type\n"
        "R1,OP1,42,Alpha to Delta,3\n"
    ),
    "calendar.txt": (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date\n"
        "WK,1,1,1,1,1,0,0,20260101,20261231\n"
    ),
    "trips.txt": (
        "route_id,service_id,trip_id,direction_id,shape_id\n"
        "R1,WK,T1,0,SH1\n"
        "R1,WK,T2,0,SH1\n"
        "R1,WK,T3,0,\n"
    ),
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T1,09:00:00,09:00:00,S1,1\n"
        "T1,09:05:00,09:05:00,S2,2\n"
        "T1,09:10:00,09:10:00,S3,3\n"
        "T1,09:15:00,09:15:00,S4,4\n"
        "T2,10:00:00,10:00:00,S1,1\n"
        "T2,10:05:00,10:05:00,S2,2\n"
        "T2,10:10:00,10:10:00,S3,3\n"
        "T2,10:15:00,10:15:00,S4,4\n"
        "T3,11:00:00,11:00:00,S1,1\n"
        "T3,11:05:00,11:05:00,S2,2\n"
    ),
    "shapes.txt": (
        "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
        "SH1,53.4800,-2.2450,1\n"
        "SH1,53.4800,-2.2400,2\n"
        "SH1,53.4800,-2.2350,3\n"
        "SH1,53.4800,-2.2300,4\n"
        "SH2,53.0,-2.0,1\n"  # orphaned: no pattern refers to it
    ),
    "feed_info.txt": (
        "feed_publisher_name,feed_publisher_url,feed_lang,feed_version\n"
        "BODS,http://x,en,20260806_022608\n"
    ),
}


@pytest.fixture
def gtfs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "gtfs"
    d.mkdir()
    for name, body in MINI.items():
        (d / name).write_text(body)
    return d


@pytest.fixture
def gtfs_zip(gtfs_dir: Path, tmp_path: Path) -> Path:
    z = tmp_path / "feed.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for f in sorted(gtfs_dir.iterdir()):
            zf.write(f, f.name)
    return z


@pytest.fixture
def con(tmp_path: Path):
    c = db.connect(tmp_path / "test.duckdb")
    yield c
    c.close()
