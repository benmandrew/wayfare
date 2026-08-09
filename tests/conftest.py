from __future__ import annotations

from pathlib import Path

import pytest

from wayfare import db, valhalla


class FakeClient:
    """Stands in for Valhalla. Returns two edges for anything it is asked to match,
    which is enough to exercise checkpointing, resumption and aggregation."""

    def __init__(
        self,
        road_m: float = 1000.0,
        fail: Exception | None = None,
        graph: str | None = "valhalla-test/1",
    ):
        self.base = "http://fake-valhalla/"
        self.road_m = road_m
        self.fail = fail
        self.graph = graph
        self.calls: list[str] = []

    def healthy(self) -> bool:
        return True

    def graph_id(self) -> str | None:
        return self.graph

    def _match(self, source: str) -> valhalla.Match:
        self.calls.append(source)
        if self.fail:
            raise self.fail
        edges = [
            valhalla.Edge(
                1001,
                44556677,
                self.road_m / 2,
                "Oxford Road",
                "secondary",
                [(53.48, -2.245), (53.48, -2.240)],
            ),
            valhalla.Edge(
                1002,
                44556678,
                self.road_m / 2,
                "Oxford Road",
                "secondary",
                [(53.48, -2.240), (53.48, -2.235)],
            ),
        ]
        return valhalla.Match(edges, confidence=0.9, road_m=self.road_m, source=source)

    def match_shape(self, shape):
        return self._match("shape")

    def match_stops(self, stops):
        return self._match("stops")


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
