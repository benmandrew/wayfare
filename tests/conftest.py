from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import pytest

from wayfare import db

# A four-stop line running east along a single street, with two trips sharing one
# pattern and a third trip that turns short. Enough to exercise the collapse from
# trips to patterns, the shape_id carry-through, and the span calculation.
#
# It also carries one ferry and one rail route, which must never reach a pattern.
# They are in the shared fixture rather than in a test of their own so that every
# test using this feed is a check on the mode filter: the counts elsewhere in the
# suite are the counts for the bus route alone, and stay that way.
MINI = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nOP1,Example,http://x,Europe/London\n",
    "stops.txt": (
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "S1,Alpha,53.4800,-2.2450\n"
        "S2,Bravo,53.4800,-2.2400\n"
        "S3,Charlie,53.4800,-2.2350\n"
        "S4,Delta,53.4800,-2.2300\n"
        "P1,Pier Head,53.4050,-2.9960\n"
        "P2,Island Quay,53.3200,-3.1800\n"
    ),
    "routes.txt": (
        "route_id,agency_id,route_short_name,route_long_name,route_type\n"
        "R1,OP1,42,Alpha to Delta,3\n"
        "F1,OP1,FERRY,Pier Head to Island Quay,4\n"
        "X1,OP1,EXPRESS,Alpha to Island Quay,2\n"
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
        "F1,WK,TF1,0,\n"
        "X1,WK,TX1,0,\n"
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
        # A sea crossing and a train, neither of which has any road under it.
        "TF1,12:00:00,12:00:00,P1,1\n"
        "TF1,12:30:00,12:30:00,P2,2\n"
        "TX1,13:00:00,13:00:00,S1,1\n"
        "TX1,13:40:00,13:40:00,P2,2\n"
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
def con(tmp_path: Path):
    c = db.connect(tmp_path / "test.duckdb")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def staging(monkeypatch, tmp_path: Path) -> None:
    """`db.insert_via_file` stages under WORK, which a test must not be writing to.

    Autouse rather than opt-in: a test reaching the real working directory is a
    fault wherever it happens, and the staging path is not visible from the call.
    """
    monkeypatch.setattr(db.config, "WORK", tmp_path / "work")


@pytest.fixture
def legacy_db(tmp_path: Path) -> Callable[..., Path]:
    """Build a database at an older schema, then hand back its path for `db.connect`
    to migrate. A national match run costs a day or two, so every migration is
    checked against the layout it has to rewrite rather than against a fresh one."""

    def build(
        *ddl: str, rows: dict[str, list[tuple]] | None = None, name: str = "old"
    ) -> Path:
        path = tmp_path / f"{name}.duckdb"
        old = duckdb.connect(str(path))
        try:
            for statement in ddl:
                old.execute(statement)
            for table, values in (rows or {}).items():
                if not values:
                    continue
                marks = ", ".join("?" * len(values[0]))
                old.executemany(f"INSERT INTO {table} VALUES ({marks})", values)
        finally:
            old.close()
        return path

    return build


@pytest.fixture
def tippecanoe_calls(monkeypatch) -> list[list[str]]:
    """Stub tippecanoe and tile-join, collecting the argv of every invocation.

    Each writes an empty file at its `-o`, because `build_tiles` checks the output
    exists before joining. Thirteen copies of this lived inline in test_publish.
    """
    import subprocess

    from wayfare import publish

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess:
        calls.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(publish.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(publish.subprocess, "run", fake_run)
    return calls
