"""What each subcommand does with the flags it was given.

One test per subcommand, with the stage stubbed out: the value here is not that a
stage works -- every stage has its own tests -- but that the flag a person types
still reaches it. A parser this size is rearranged often, and an argument that
quietly stops being passed changes nothing a stage test can see.

The stubs record rather than assert, so a test says which keyword it cares about
and stays silent about the rest.
"""

from __future__ import annotations

import json
import re
import types
from pathlib import Path
from typing import Any

import pytest

from wayfare import (
    acquire,
    aggregate,
    cli,
    config,
    coverage,
    db,
    gtfs,
    maintenance,
    match,
    osmroutes,
    publish,
    railtrips,
    trace,
)


class Spy:
    """A stand-in for a stage: remembers how it was called, answers with `result`."""

    def __init__(self, result: Any = None) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.result = result

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return self.result

    @property
    def called(self) -> bool:
        return bool(self.calls)

    @property
    def args(self) -> tuple[Any, ...]:
        return self.calls[-1][0]

    @property
    def kwargs(self) -> dict[str, Any]:
        return self.calls[-1][1]


def spy(monkeypatch, module: Any, name: str, result: Any = None) -> Spy:
    s = Spy(result)
    monkeypatch.setattr(module, name, s)
    return s


@pytest.fixture
def root(monkeypatch, tmp_path: Path) -> Path:
    """A data root with a database file in it, so `_require_db` is satisfied.

    An empty file rather than a database: every test here stubs `db.connect`, and
    the check the CLI makes is that the path exists.
    """
    for name in ("DATA", "RAW", "WORK", "OUT"):
        monkeypatch.setattr(config, name, tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "wayfare.duckdb")
    (tmp_path / "wayfare.duckdb").write_bytes(b"")
    return tmp_path


@pytest.fixture
def con(monkeypatch) -> types.SimpleNamespace:
    """The connection every stage is handed, and a record of it being closed.

    Closing matters enough to assert on: DuckDB takes a single writer, so a
    subcommand that leaves one open is a subcommand the next one cannot follow.
    """
    fake = types.SimpleNamespace(opened=[], closed=0)

    def connect(*_: Any, read_only: bool = False, **__: Any) -> Any:
        fake.opened.append(read_only)
        return fake

    def close() -> None:
        fake.closed += 1

    fake.close = close
    monkeypatch.setattr(cli.db, "connect", connect)
    return fake


@pytest.fixture
def feed(root: Path) -> Path:
    """An unpacked feed, which `patterns` refuses to run without."""
    d = root / "gtfs"
    d.mkdir(exist_ok=True)
    (d / "stop_times.txt").write_text("trip_id,stop_id\n")
    return d


# --- acquire -------------------------------------------------------------------


def test_acquire_passes_the_region_and_both_switches(monkeypatch, root):
    got = spy(monkeypatch, acquire, "acquire_all", {})
    assert cli.main(["acquire", "--region", "ireland", "--force", "--with-osm"]) == 0
    assert got.kwargs == {"region": "ireland", "force": True, "with_osm": True}


def test_acquire_defaults_to_the_ambient_region(monkeypatch, root):
    """None rather than a slug: `config.feed` reads WAYFARE_REGION, and a default
    spelled here would override the data root's own answer."""
    got = spy(monkeypatch, acquire, "acquire_all", {})
    assert cli.main(["acquire"]) == 0
    assert got.kwargs == {"region": None, "force": False, "with_osm": False}


# --- patterns ------------------------------------------------------------------


def test_patterns_reads_the_unpacked_feed_and_forwards_its_flags(monkeypatch, feed, con):
    got = spy(monkeypatch, gtfs, "build_patterns")
    code = cli.main(["patterns", "--memory", "8GB", "--upgrade-shapes"])
    assert code == 0
    assert got.args[0] == feed
    assert got.kwargs["memory_limit"] == "8GB"
    assert got.kwargs["upgrade_shapes"] is True
    assert got.kwargs["modes"] is None
    assert con.closed == 1


def test_patterns_selects_modes_as_a_set_of_names(monkeypatch, feed, con):
    """The selection is written to `meta.modes` from here, so what the flag parses
    to is what the database is rebuilt against."""
    got = spy(monkeypatch, gtfs, "build_patterns")
    assert cli.main(["patterns", "--modes", "bus, tram"]) == 0
    assert got.kwargs["modes"] == frozenset({"bus", "tram"})


def test_patterns_refuses_an_unknown_mode_without_touching_the_feed(monkeypatch, feed, con):
    got = spy(monkeypatch, gtfs, "build_patterns")
    assert cli.main(["patterns", "--modes", "hovercraft"]) == 1
    assert not got.called


def test_patterns_refuses_an_empty_selection(monkeypatch, feed, con):
    """`--modes ''` would otherwise build a database with no patterns and report
    success."""
    got = spy(monkeypatch, gtfs, "build_patterns")
    assert cli.main(["patterns", "--modes", " , "]) == 1
    assert not got.called


def test_patterns_names_the_missing_feed_rather_than_failing_in_duckdb(
    monkeypatch, root, con
):
    got = spy(monkeypatch, gtfs, "build_patterns")
    assert cli.main(["patterns"]) == 1
    assert not got.called


# --- match ---------------------------------------------------------------------


@pytest.fixture
def valhalla_client(monkeypatch) -> Spy:
    from wayfare import valhalla

    return spy(monkeypatch, valhalla, "Client", object())


def test_match_forwards_every_bound_on_the_run(monkeypatch, root, con, valhalla_client):
    run = spy(monkeypatch, match, "run", {})
    spy(monkeypatch, match, "summary", [])
    code = cli.main(
        [
            "match",
            "--workers",
            "3",
            "--limit",
            "50",
            "--max-seconds",
            "600",
            "--valhalla",
            "http://v:8002",
            "--force-graph",
        ]
    )
    assert code == 0
    assert run.kwargs["workers"] == 3
    assert run.kwargs["limit"] == 50
    assert run.kwargs["max_seconds"] == 600.0
    assert run.kwargs["force_graph"] is True
    assert valhalla_client.args == ("http://v:8002",)
    assert con.closed == 1


def test_match_clears_the_retried_statuses_before_the_first_batch(
    monkeypatch, root, con, valhalla_client
):
    """Work is selected by the absence of a status row, so a row deleted while its
    pattern is in flight is handed out twice."""
    order: list[str] = []

    def note(name: str, spied: Spy) -> Any:
        def fn(*a: Any, **k: Any) -> Any:
            order.append(name)
            return spied(*a, **k)

        return fn

    retry, run = Spy(0), Spy({})
    monkeypatch.setattr(match, "reclassify_transport_faults", note("reclassify", Spy(0)))
    monkeypatch.setattr(match, "retry", note("retry", retry))
    monkeypatch.setattr(match, "run", note("run", run))
    spy(monkeypatch, match, "summary", [])

    argv = ["match", "--reclassify-transport", "--retry", "transient, error"]
    assert cli.main(argv) == 0
    assert order == ["reclassify", "retry", "run"]
    assert retry.args[1] == ["transient", "error"]


def test_match_without_retry_clears_nothing(monkeypatch, root, con, valhalla_client):
    retry = spy(monkeypatch, match, "retry", 0)
    spy(monkeypatch, match, "run", {})
    spy(monkeypatch, match, "summary", [])
    assert cli.main(["match"]) == 0
    assert not retry.called


# --- trace ---------------------------------------------------------------------


def test_trace_forwards_the_relation_cache_and_the_limit(monkeypatch, root, con, tmp_path):
    run = spy(monkeypatch, trace, "run", {})
    spy(monkeypatch, trace, "summary", [])
    cache = tmp_path / "relations.json"
    code = cli.main(["trace", "--relations", str(cache), "--refresh", "--limit", "9"])
    assert code == 0
    assert run.kwargs == {"cache": cache, "refresh": True, "limit": 9}
    assert con.closed == 1


def test_trace_retries_the_statuses_it_was_given_before_running(monkeypatch, root, con):
    order: list[str] = []
    got = Spy(0)

    def retry(con: Any, statuses: list[str]) -> int:
        order.append("retry")
        got(statuses)
        return 0

    monkeypatch.setattr(trace, "retry", retry)
    monkeypatch.setattr(trace, "run", lambda *a, **k: order.append("run") or {})
    spy(monkeypatch, trace, "summary", [])
    assert cli.main(["trace", "--retry", "transient,ok"]) == 0
    assert order == ["retry", "run"]
    assert got.args == (["transient", "ok"],)


def test_trace_reports_an_overpass_failure_as_an_exit_code(monkeypatch, root, con):
    def boom(*a: Any, **k: Any) -> dict[str, int]:
        raise RuntimeError("Overpass said no")

    monkeypatch.setattr(trace, "run", boom)
    assert cli.main(["trace"]) == 1
    assert con.closed == 1


# --- routes --------------------------------------------------------------------


BUILT = types.SimpleNamespace(
    considered=3,
    chained=2,
    patterns=2,
    ways=40,
    skipped_not_ours=1,
    skipped_broken=0,
    skipped_no_stops=0,
)


def test_routes_forwards_its_own_relation_cache(monkeypatch, root, con, tmp_path):
    """A different file from `trace`'s on purpose: the two ask for different
    windows, and one shared body lets whichever ran first decide the other's
    coverage."""
    run = spy(monkeypatch, osmroutes, "run", BUILT)
    cache = tmp_path / "osm_routes.json"
    assert cli.main(["routes", "--relations", str(cache), "--refresh"]) == 0
    assert run.kwargs == {"cache": cache, "refresh": True}
    assert con.closed == 1


def test_routes_attributes_trips_from_a_cif_on_the_date_it_was_given(
    monkeypatch, root, con, tmp_path
):
    import datetime

    spy(monkeypatch, osmroutes, "run", BUILT)
    got = spy(
        monkeypatch,
        railtrips,
        "run_cached",
        types.SimpleNamespace(legs_placed=1, legs=2, trip_coverage=50.0, ways=3),
    )
    cif = tmp_path / "schedule.cif"
    stops = tmp_path / "naptan.csv"
    code = cli.main(
        [
            "routes",
            "--cif",
            str(cif),
            "--stops",
            str(stops),
            "--on",
            "2026-03-01",
        ]
    )
    assert code == 0
    assert got.args[1:] == (cif, stops)
    assert got.kwargs["on"] == datetime.date(2026, 3, 1)


def test_routes_without_a_cif_never_opens_one(monkeypatch, root, con):
    spy(monkeypatch, osmroutes, "run", BUILT)
    got = spy(monkeypatch, railtrips, "run_cached")
    assert cli.main(["routes"]) == 0
    assert not got.called


def test_routes_reports_a_refused_relation_set_as_an_exit_code(monkeypatch, root, con):
    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("no bounds for this region")

    monkeypatch.setattr(osmroutes, "run", boom)
    assert cli.main(["routes"]) == 1
    assert con.closed == 1


# --- aggregate -----------------------------------------------------------------


def test_aggregate_builds_and_closes(monkeypatch, root, con):
    got = spy(monkeypatch, aggregate, "build")
    assert cli.main(["aggregate"]) == 0
    assert got.called
    assert con.closed == 1


# --- publish -------------------------------------------------------------------


def test_publish_hands_the_region_and_the_archive_path_over(monkeypatch, root, con):
    got = spy(monkeypatch, publish, "build", Path("out.pmtiles"))
    code = cli.main(["publish", "--region", "ireland", "--name-by-region"])
    assert code == 0
    assert got.kwargs["region"] == "ireland"
    assert got.kwargs["out"] == root / config.archive_name("ireland")
    assert got.kwargs["from_export"] is None
    # Read-only: a publish must never be the writer that blocks a stage.
    assert con.opened == [True]
    assert con.closed == 1


def test_publish_from_an_export_opens_no_database(monkeypatch, root):
    """The one publish reaching for a data root whose database is gone."""
    monkeypatch.setattr(
        cli.db, "connect", lambda **_: pytest.fail("a connection was opened")
    )
    got = spy(monkeypatch, publish, "build", Path("out.pmtiles"))
    assert cli.main(["publish", "--from-export"]) == 0
    assert got.args[0] is None
    assert got.kwargs["from_export"] == root / "edges.geojsonl"


def test_publish_reports_a_refused_archive_name_as_an_exit_code(monkeypatch, root, con):
    def boom(*a: Any, **k: Any) -> Path:
        raise RuntimeError("--name-by-region")

    monkeypatch.setattr(publish, "build", boom)
    assert cli.main(["publish"]) == 1
    assert con.closed == 1


# --- coverage and draw ---------------------------------------------------------


def test_coverage_measures_the_banded_overview_by_default(monkeypatch, root, tmp_path):
    sizes = spy(monkeypatch, coverage, "report_sizes", {})
    report = spy(monkeypatch, coverage, "report", {})
    archive = tmp_path / "gb.pmtiles"
    assert cli.main(["coverage", str(archive)]) == 0
    assert sizes.args == (archive,)
    assert report.args == (archive, list(range(config.MIN_ZOOM, config.DETAIL_ZOOM)), None)


def test_coverage_takes_an_explicit_zoom_list_and_cell(monkeypatch, root, tmp_path):
    spy(monkeypatch, coverage, "report_sizes", {})
    report = spy(monkeypatch, coverage, "report", {})
    assert (
        cli.main(
            ["coverage", str(tmp_path / "gb.pmtiles"), "--zooms", "6,8", "--cell", "0.5"]
        )
        == 0
    )
    assert report.args[1] == [6, 8]
    assert report.args[2] == 0.5


def test_coverage_reports_an_unreadable_archive_as_an_exit_code(
    monkeypatch, root, tmp_path
):
    def boom(*a: Any, **k: Any) -> Any:
        raise OSError("no such archive")

    monkeypatch.setattr(coverage, "report_sizes", boom)
    assert cli.main(["coverage", str(tmp_path / "gone.pmtiles")]) == 1


def test_draw_passes_the_window_as_one_box(monkeypatch, root, tmp_path):
    """Four values rather than a comma-separated string: every window over these
    islands opens on a negative longitude, which argparse reads as an option."""
    got = spy(monkeypatch, coverage, "draw")
    archive = tmp_path / "gb.pmtiles"
    out = tmp_path / "z7.png"
    code = cli.main(
        [
            "draw",
            str(archive),
            str(out),
            "--zoom",
            "7",
            "--window",
            "-1.4",
            "51.0",
            "1.0",
            "52.2",
            "--width",
            "800",
        ]
    )
    assert code == 0
    assert got.args == (archive, 7, (-1.4, 51.0, 1.0, 52.2), out, 800)


# --- status, prune, cluster ----------------------------------------------------


def test_status_prints_the_funnel_as_json_on_stdout(monkeypatch, root, con, capsys):
    """stdout, because `deploy/refresh.sh` reads it with `jq` while the logging
    goes to stderr."""
    spy(monkeypatch, aggregate, "funnel", {"patterns_pending": 0, "by_status": {}})
    assert cli.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out) == {"patterns_pending": 0, "by_status": {}}
    assert con.opened == [True]
    assert con.closed == 1


def test_prune_drops_the_shapes_and_closes(monkeypatch, root, con):
    got = spy(monkeypatch, maintenance, "prune_shapes", 12)
    assert cli.main(["prune"]) == 0
    assert got.called
    assert con.closed == 1


def test_prune_reports_a_refusal_as_an_exit_code(monkeypatch, root, con):
    """It refuses while any matchable pattern is unmatched, which is the whole
    reason it sits after the publish gate."""

    def boom(*a: Any, **k: Any) -> int:
        raise RuntimeError("patterns are still unmatched")

    monkeypatch.setattr(maintenance, "prune_shapes", boom)
    assert cli.main(["prune"]) == 1
    assert con.closed == 1


def test_cluster_reorders_the_database_in_place(monkeypatch, root):
    got = spy(monkeypatch, maintenance, "cluster", (100, 2_000_000, 1_000_000))
    assert cli.main(["cluster"]) == 0
    assert got.called


def test_cluster_says_so_when_there_is_nothing_to_do(monkeypatch, root):
    spy(monkeypatch, maintenance, "cluster", (0, 0, 0))
    assert cli.main(["cluster"]) == 0


def test_cluster_reports_a_failure_as_an_exit_code(monkeypatch, root):
    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("no room to copy the database")

    monkeypatch.setattr(maintenance, "cluster", boom)
    assert cli.main(["cluster"]) == 1


# --- art -----------------------------------------------------------------------


def test_art_renders_the_window_it_was_given(monkeypatch, root):
    from wayfare import art

    got = spy(monkeypatch, art, "render", Path("out.png"))
    code = cli.main(
        [
            "art",
            "--bbox=-3.3,51.4,-3.0,51.6",
            "--style",
            "spectrum",
            "--width",
            "2000",
            "--scale",
            "2.0",
            "--caption",
            "Cardiff",
            "--credit",
            "--coalesce",
            "--workers",
            "2",
        ]
    )
    assert code == 0
    assert got.args == ("-3.3,51.4,-3.0,51.6",)
    assert got.kwargs["style"] == "spectrum"
    assert got.kwargs["workers"] == 2
    opts = got.kwargs["opts"]
    assert (opts.width_px, opts.scale, opts.caption) == (2000, 2.0, "Cardiff")
    assert opts.credit is True and opts.coalesce is True


def test_art_takes_a_preset_by_name(monkeypatch, root):
    from wayfare import art

    got = spy(monkeypatch, art, "render", Path("out.png"))
    assert cli.main(["art", "london"]) == 0
    assert got.args == ("london",)


def test_art_refuses_an_area_and_a_bbox_together(monkeypatch, root):
    from wayfare import art

    got = spy(monkeypatch, art, "render", Path("out.png"))
    assert cli.main(["art", "london", "--bbox=-3.3,51.4,-3.0,51.6"]) == 1
    assert not got.called


def test_art_with_no_area_at_all_lists_the_presets(monkeypatch, root):
    from wayfare import art

    got = spy(monkeypatch, art, "render", Path("out.png"))
    assert cli.main(["art"]) == 1
    assert not got.called


# --- serve ---------------------------------------------------------------------


def test_serve_passes_the_bind_the_bundle_and_the_archives(monkeypatch, root, tmp_path):
    from wayfare import server

    got = spy(monkeypatch, server, "serve")
    code = cli.main(
        [
            "serve",
            "--port",
            "9000",
            "--host",
            "127.0.0.1",
            "--dir",
            str(tmp_path / "web"),
            "--out",
            str(tmp_path / "served"),
            "--max-age",
            "0",
        ]
    )
    assert code == 0
    assert got.kwargs["port"] == 9000
    assert got.kwargs["host"] == "127.0.0.1"
    assert got.kwargs["web_dir"] == tmp_path / "web"
    assert got.kwargs["out_dir"] == tmp_path / "served"
    assert got.kwargs["max_age"] == 0
    assert got.kwargs["art_enabled"] is config.ART_ENABLED


def test_serve_defaults_the_archives_to_the_data_root(monkeypatch, root):
    from wayfare import server

    got = spy(monkeypatch, server, "serve")
    assert cli.main(["serve"]) == 0
    assert got.kwargs["out_dir"] == config.OUT


def test_serve_can_switch_off_the_one_endpoint_that_costs_cpu(monkeypatch, root):
    from wayfare import server

    got = spy(monkeypatch, server, "serve")
    assert cli.main(["serve", "--no-art"]) == 0
    assert got.kwargs["art_enabled"] is False


# --- all -----------------------------------------------------------------------


def _script_stages() -> list[str]:
    """The stages `deploy/refresh.sh` runs, in the order it runs them."""
    stages = []
    for raw in REFRESH.read_text().splitlines():
        line = raw.strip()
        if line.startswith("#") or line.startswith("wayfare()"):
            continue
        found = re.search(r"\bwayfare ([a-z]+)", line)
        if found:
            stages.append(found.group(1))
    return stages


@pytest.fixture
def chain(monkeypatch) -> list[tuple[str, tuple[str, ...]]]:
    """Record the subcommands `all` chains, without running any of them."""
    ran: list[tuple[str, tuple[str, ...]]] = []

    def run(cmd: str, *flags: str) -> int:
        ran.append((cmd, flags))
        return 0

    monkeypatch.setattr(cli, "_run", run)
    return ran


def test_all_runs_the_stages_the_scheduled_script_runs(monkeypatch, root, con, chain):
    """One definition of the pipeline, not two. `all` and `deploy/refresh.sh` had
    already drifted -- `all` omitted `routes`, `prune`, `cluster` and the publish
    gate, so the chained run a deployment offers built a different archive from the
    scheduled one. The script's `status` is the gate, which `all` applies in process
    and the tests below check on its own.
    """
    spy(monkeypatch, aggregate, "funnel", {"patterns_pending": 0, "by_status": {}})
    assert cli.main(["all"]) == 0
    assert [cmd for cmd, _ in chain] == [s for s in _script_stages() if s != "status"]


def test_all_forwards_the_region_the_modes_and_the_workers(monkeypatch, root, con, chain):
    spy(monkeypatch, aggregate, "funnel", {"patterns_pending": 0, "by_status": {}})
    argv = ["all", "--region", "ireland", "--modes", "bus,tram", "--workers", "2"]
    assert cli.main(argv) == 0
    flags = dict(chain)
    assert flags["acquire"] == ("--region", "ireland")
    assert flags["patterns"] == ("--modes", "bus,tram")
    assert flags["match"] == ("--retry", "transient", "--workers", "2")
    # An explicit path rather than --name-by-region, because `all` settles the name
    # before it starts and must publish to the one it settled on.
    assert flags["publish"][:1] == ("--out",)
    assert flags["publish"][2:] == ("--region", "ireland")


def test_all_stops_at_the_publish_gate_a_transport_fault_fails(monkeypatch, root, con):
    """The count `patterns_pending` cannot see. Publishing on that number alone
    ships a tileset missing every road a Valhalla outage interrupted."""
    ran: list[str] = []
    monkeypatch.setattr(cli, "_run", lambda cmd, *f: ran.append(cmd) or 0)
    spy(
        monkeypatch,
        aggregate,
        "funnel",
        {"patterns_pending": 0, "by_status": {"transport_error": 4}},
    )
    assert cli.main(["all"]) == 1
    assert "publish" not in ran


def test_all_stops_at_the_publish_gate_when_patterns_are_still_pending(
    monkeypatch, root, con
):
    ran: list[str] = []
    monkeypatch.setattr(cli, "_run", lambda cmd, *f: ran.append(cmd) or 0)
    spy(monkeypatch, aggregate, "funnel", {"patterns_pending": 12, "by_status": {}})
    assert cli.main(["all"]) == 1
    assert "publish" not in ran


def test_all_publishes_when_overpass_is_down(monkeypatch, root, con):
    """Overpass being unreachable must not throw away a match run that has just cost
    a day or two. What the stage did not draw keeps no status row, so the next run
    picks it up unchanged."""
    ran: list[str] = []

    def run(cmd: str, *flags: str) -> int:
        ran.append(cmd)
        if cmd in ("trace", "routes"):
            raise RuntimeError("Overpass is busy")
        return 0

    monkeypatch.setattr(cli, "_run", run)
    spy(monkeypatch, aggregate, "funnel", {"patterns_pending": 0, "by_status": {}})
    assert cli.main(["all"]) == 0
    assert "publish" in ran


def test_all_stops_at_the_first_stage_that_fails(monkeypatch, root, con):
    ran: list[str] = []

    def run(cmd: str, *flags: str) -> int:
        ran.append(cmd)
        return 1 if cmd == "patterns" else 0

    monkeypatch.setattr(cli, "_run", run)
    assert cli.main(["all"]) == 1
    assert ran == ["acquire", "patterns"]


def test_all_settles_the_archive_name_before_anything_expensive(monkeypatch, root, chain):
    """A name that will be refused is refused just as well now as after a day of
    matching."""

    def boom(region=None):
        raise RuntimeError("this data root publishes by region")

    monkeypatch.setattr(publish, "default_out", boom)
    assert cli.main(["all"]) == 1
    assert not chain


def test_all_hands_every_stage_argv_its_own_parser_accepts(monkeypatch, feed, con):
    """`_run` builds each stage's argv and parses it with that stage's own parser,
    so a flag `all` passes that the stage never declared is a parse error rather
    than a silent no-op. Nothing is stubbed here but the stages themselves."""
    from wayfare import valhalla

    spy(monkeypatch, valhalla, "Client", object())
    for module, name, result in (
        (acquire, "acquire_all", {}),
        (gtfs, "build_patterns", None),
        (match, "retry", 0),
        (match, "run", {}),
        (match, "summary", []),
        (trace, "retry", 0),
        (trace, "run", {}),
        (trace, "summary", []),
        (osmroutes, "run", BUILT),
        (aggregate, "build", None),
        (maintenance, "prune_shapes", 0),
        (maintenance, "cluster", (0, 0, 0)),
        (publish, "build", Path("out.pmtiles")),
    ):
        spy(monkeypatch, module, name, result)
    spy(monkeypatch, aggregate, "funnel", {"patterns_pending": 0, "by_status": {}})

    assert cli.main(["all", "--region", "ireland", "--workers", "2"]) == 0


# --- the shared pieces ---------------------------------------------------------


def test_every_subcommand_the_table_names_reaches_the_parser(capsys):
    """The table is what replaced a 294-line if-chain, and `--help` is the only
    place its keys and argparse's own list of choices meet."""
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    declared = set(re.findall(r"^\s{4}(\w[\w-]*)", capsys.readouterr().out, re.M))
    assert set(cli._SUBCOMMANDS) <= declared
    assert len(cli._SUBCOMMANDS) == 15


def test_data_retargets_every_path_config_computed_at_import(monkeypatch, tmp_path):
    """A sixth path constant added to `config` and forgotten here would leave one
    stage writing to the default data root while every other one wrote to --data,
    and nothing in a run would say so."""
    before = [
        name
        for name, value in vars(config).items()
        if isinstance(value, Path) and value.is_relative_to(config.DATA)
    ]
    assert "DB_PATH" in before  # the walk found something to check
    # Retargeting is a module-level assignment, so it outlives the test without this.
    for name in before:
        monkeypatch.setattr(config, name, getattr(config, name))

    elsewhere = (tmp_path / "other").resolve()
    monkeypatch.setattr(cli.acquire, "acquire_all", Spy({}))
    assert cli.main(["--data", str(elsewhere), "acquire"]) == 0

    for name in before:
        assert getattr(config, name).is_relative_to(elsewhere), name


def test_an_interrupt_is_a_normal_way_to_stop(monkeypatch, root):
    """Every stage checkpoints, so 130 and a line saying so, not a traceback."""

    def interrupted(*a: Any, **k: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(acquire, "acquire_all", interrupted)
    assert cli.main(["acquire"]) == 130


def test_no_subcommand_is_refused_by_the_parser(root):
    with pytest.raises(SystemExit) as e:
        cli.main([])
    assert e.value.code == 2


# --- the publish gate ----------------------------------------------------------


REFRESH = Path(__file__).resolve().parent.parent / "deploy" / "refresh.sh"


def _jq_paths(script: str) -> list[list[tuple[str, bool]]]:
    """Every `jq -r '.a.b'` in the refresh script, as (key, has a default) pairs.

    The default matters: `.by_status.transport_error // 0` reads a key that is
    absent whenever no pattern failed that way, and `.patterns_pending` reads one
    that has to be there every run.
    """
    paths = []
    for expr in re.findall(r"jq -r '([^']+)'", script):
        keys = [k for k in expr.strip().split(".") if k]
        paths.append([(k.partition("//")[0].strip(), "//" in k) for k in keys])
    return paths


def test_the_publish_gate_reads_keys_the_status_json_actually_has(tmp_path):
    """The gate is shell reading JSON with `jq`, so a key renamed in `funnel` is a
    gate that reads null and a region published without its buses. Nothing else in
    the suite connects the two names."""
    paths = _jq_paths(REFRESH.read_text())
    assert paths, "the refresh script no longer reads the funnel with jq"

    con = db.connect(tmp_path / "gate.duckdb")
    try:
        funnel = aggregate.funnel(con)
    finally:
        con.close()

    for path in paths:
        node: Any = funnel
        for key, defaulted in path:
            where = ".".join(k for k, _ in path)
            assert isinstance(node, dict), where
            assert defaulted or key in node, f"{where} is not in the status JSON"
            node = node.get(key)


def test_the_gate_reads_both_counts():
    """One is not enough: `patterns_pending` counts patterns with no status row, and
    a transport fault is a row, so an outage part-way through a run reaches the end
    reporting nothing pending."""
    read = {tuple(k for k, _ in p) for p in _jq_paths(REFRESH.read_text())}
    assert ("patterns_pending",) in read
    assert ("by_status", "transport_error") in read


def test_transport_error_is_still_the_status_the_matcher_writes():
    """The gate names it as a literal in shell, where nothing checks the spelling."""
    assert match.TRANSPORT_ERROR == "transport_error"


def test_the_scheduled_run_never_overrides_the_graph_pin():
    """--force-graph mixes edge ids from two Valhalla graph builds. Attended, that
    is a decision; unattended it is silent, renders fine and costs a full re-match."""
    assert "--force-graph" not in REFRESH.read_text()


def test_the_scheduled_publish_names_the_archive_after_its_region():
    """Without the flag `publish` writes bus.pmtiles, which is a name nothing on a
    deployed host serves, and on a fresh data root it succeeds at it."""
    assert "publish --name-by-region" in REFRESH.read_text()
