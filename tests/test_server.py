from __future__ import annotations

import dataclasses
import functools
import json
import os
import socketserver
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from wayfare import art, config, db, server

# A window that holds the edges the fixtures insert, and the preset that covers it.
WINDOW = "-3.30,51.40,-3.10,51.60"
BASE = "area=cardiff&width=200"


def _edge(con, edge_id, lon, trips, services=("42",)):
    con.execute(
        "INSERT INTO edges VALUES (?, 1, 'R', 'secondary', 100.0, [?, ?], "
        "[51480000, 51480000], ?, 51480000, ?, 51480000)",
        [edge_id, lon, lon + 1000, lon, lon + 1000],
    )
    for s in services:
        con.execute(
            "INSERT INTO edge_services VALUES (?, ?, 'OP1', 1, ?)", [edge_id, s, trips]
        )


@pytest.fixture
def art_db(tmp_path: Path, monkeypatch) -> Path:
    """A populated database at `config.DB_PATH`, closed so it can be reopened.

    Closed rather than handed over open: DuckDB gives a writer an exclusive lock, and
    everything under test here opens the file read-only for the length of one render.
    """
    path = tmp_path / "work" / "wayfare.duckdb"
    con = db.connect(path)
    for i, lon in enumerate([-3200000, -3190000, -3180000]):
        _edge(con, i + 1, lon, trips=100 * (i + 1), services=("42", "9A")[: i % 2 + 1])
    db.set_meta(con, "feed_version", "20260806_022608")
    con.close()
    monkeypatch.setattr(config, "DB_PATH", path)
    return path


@pytest.fixture
def no_db(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "work" / "absent.duckdb"
    monkeypatch.setattr(config, "DB_PATH", path)
    return path


# --- Parsing ----------------------------------------------------------------


def test_a_preset_and_a_raw_window_both_resolve():
    assert server.parse_art("area=cardiff").area == "cardiff"
    assert server.parse_art(f"bbox={WINDOW}").area == WINDOW


def test_no_area_names_the_presets():
    with pytest.raises(server.BadRequest, match="cardiff"):
        server.parse_art("style=density")


@pytest.mark.parametrize(
    ("query", "match"),
    [
        ("area=cardiff&style=nonesuch", "density, spectrum, strands"),
        ("area=cardiff&format=tiff", "use png or svg"),
        ("area=swansea", "cardiff"),
        ("bbox=-3.3,51.4,-3.0", "minlon,minlat,maxlon,maxlat"),
    ],
)
def test_bad_choices_list_the_alternatives(query, match):
    with pytest.raises(server.BadRequest, match=match):
        server.parse_art(query)


@pytest.mark.parametrize(
    ("query", "match"),
    [
        ("area=cardiff&width=20", "width=20 is out of range; it runs 64"),
        ("area=cardiff&width=99999", "width=99999 is out of range"),
        ("area=cardiff&hue=1.5", "hue=1.5 is out of range; it runs 0 to 1"),
        ("area=cardiff&scale=9", "scale=9 is out of range; it runs 0.1 to 4"),
        ("area=cardiff&line_scale=0", "line_scale=0 is out of range"),
        ("area=cardiff&alpha_scale=99", "alpha_scale=99 is out of range"),
        ("area=cardiff&width=wide", "width='wide' is not a number"),
    ],
)
def test_out_of_range_numbers_report_the_range(query, match):
    with pytest.raises(server.BadRequest, match=match):
        server.parse_art(query)


def test_hue_zero_survives():
    """Zero is red, and it is the one hue a falsy-default would silently discard.

    `_number` used to fall back with `or default`, which turned hue=0 into 0.56.
    """
    assert server.parse_art("area=cardiff&hue=0").opts.hue == 0.0
    assert server.parse_art("area=cardiff").opts.hue == server.DEFAULTS.hue


def test_the_pixel_budget_catches_what_the_width_cap_cannot():
    """Width alone is not the size of a render: the window's aspect ratio picks the
    height and `scale` multiplies both, so a legal width over a tall window is
    hundreds of megapixels."""
    bounds = art.resolve("-3.30,51.40,-3.29,53.40")
    height = art.Projection.canvas_height(bounds, 1000)
    with pytest.raises(server.BadRequest) as exc:
        server.parse_art("bbox=-3.30,51.40,-3.29,53.40&width=1000")
    assert f"{1000 * height / 1e6:.0f} megapixels" in str(exc.value)
    # The same window is fine once it is small enough to draw.
    assert server.parse_art("bbox=-3.30,51.40,-3.29,53.40&width=64").opts.width_px == 64


def test_scale_is_ignored_rather_than_accepted_for_svg():
    """SVG is resolution independent, so a scale that did nothing would still split
    the cache and the ETag."""
    svg = server.parse_art("area=cardiff&format=svg&scale=3")
    assert svg.opts.scale == 1.0
    assert svg.key == server.parse_art("area=cardiff&format=svg").key
    assert server.parse_art("area=cardiff&scale=3").opts.scale == 3.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("%23ff8000", (1.0, 0.5019607843137255, 0.0)),
        ("ff8000", (1.0, 0.5019607843137255, 0.0)),
        ("0.1,0.2,0.3", (0.1, 0.2, 0.3)),
        ("0,0,0", (0.0, 0.0, 0.0)),
    ],
)
def test_background_takes_hex_or_floats(raw, expected):
    got = server.parse_art(f"area=cardiff&background={raw}").opts.background
    assert got == pytest.approx(expected)


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ("zzzzzz", "not a hex colour"),
        ("0.1,0.2", "three 0-1 floats"),
        ("0.1,0.2,0.3,0.4", "three 0-1 floats"),
        ("0.1,0.2,1.4", "outside 0 to 1"),
        ("0.1,0.2,dark", "not a number"),
    ],
)
def test_bad_backgrounds_say_what_is_accepted(raw, match):
    with pytest.raises(server.BadRequest, match=match):
        server.parse_art(f"area=cardiff&background={raw}")


def test_a_lat_lon_window_warns_rather_than_failing():
    """A UK latitude is a valid longitude, so the swap renders an empty picture
    instead of raising. Over HTTP the warning header is the only way a caller finds
    out why the image is blank."""
    request = server.parse_art("bbox=51.4,-3.3,51.6,-3.1")
    assert request.warning is not None
    assert "lon first" in request.warning
    assert server.parse_art(f"bbox={WINDOW}").warning is None


def test_an_overlong_caption_is_rejected():
    with pytest.raises(server.BadRequest, match="longer than 120"):
        server.parse_art(f"area=cardiff&caption={'x' * (server.MAX_CAPTION + 1)}")
    assert server.parse_art("area=cardiff&caption=cardiff").opts.caption == "cardiff"


@pytest.mark.parametrize(
    "query",
    [
        "area=london&width=200",
        "area=cardiff&width=201",
        "area=cardiff&width=200&style=spectrum",
        "area=cardiff&width=200&format=svg",
        "area=cardiff&width=200&height=300",
        "area=cardiff&width=200&scale=2",
        "area=cardiff&width=200&hue=0.2",
        "area=cardiff&width=200&line_scale=2",
        "area=cardiff&width=200&alpha_scale=2",
        "area=cardiff&width=200&caption=hello",
        "area=cardiff&width=200&background=ff0000",
    ],
)
def test_every_drawn_parameter_reaches_the_cache_key(query):
    assert server.parse_art(query).key != server.parse_art(BASE).key


def test_identical_requests_share_a_key():
    assert server.parse_art(BASE).key == server.parse_art(BASE).key


def test_the_warning_is_not_part_of_the_key():
    """It changes the response header, not the pixels, so it must not split the
    cache."""
    request = server.parse_art(BASE)
    assert dataclasses.replace(request, warning="swapped").key == request.key


def test_filename_names_the_preset_but_not_a_raw_window():
    assert server.parse_art("area=cardiff&format=svg").filename() == "cardiff-density.svg"
    assert server.parse_art(f"bbox={WINDOW}").filename() == "window-density.png"


# --- The renderer -----------------------------------------------------------


def test_a_render_returns_an_image_of_the_format_asked_for(art_db):
    renderer = server.Renderer()
    png, etag = renderer.render(server.parse_art(BASE))
    assert png.startswith(b"\x89PNG")
    assert etag == renderer.etag(server.parse_art(BASE))
    svg, _ = renderer.render(server.parse_art(f"{BASE}&format=svg"))
    assert b"<svg" in svg


def test_the_second_identical_request_is_not_redrawn(art_db, monkeypatch):
    renderer = server.Renderer()
    draws = _count_draws(monkeypatch)
    first = renderer.render(server.parse_art(BASE))
    second = renderer.render(server.parse_art(BASE))
    assert second is first
    assert draws == [1]


def test_a_rewritten_database_is_not_served_from_the_cache(art_db, monkeypatch):
    """Every stage rewrites the file in place, so the cache key and the ETag carry the
    file's size and mtime. A stale render is worse than a slow one."""
    renderer = server.Renderer()
    draws = _count_draws(monkeypatch)
    before = renderer.stamp()
    etag_before = renderer.etag(server.parse_art(BASE))
    renderer.render(server.parse_art(BASE))

    st = art_db.stat()
    os.utime(art_db, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert renderer.stamp() != before
    assert renderer.etag(server.parse_art(BASE)) != etag_before
    renderer.render(server.parse_art(BASE))
    assert draws == [2]


def test_a_missing_database_stamps_as_absent(no_db):
    assert server.Renderer().stamp() == "absent"


def test_a_missing_database_says_where_it_looked(no_db):
    with pytest.raises(server.Unavailable, match="no database at"):
        server.Renderer().render(server.parse_art(BASE))
    assert str(no_db) in _message(server.Renderer(), server.parse_art(BASE))


def test_the_cache_evicts_to_stay_under_its_cap():
    renderer = server.Renderer(cache_bytes=100)
    for i in range(4):
        renderer._store(f"k{i}", (b"x" * 40, f"e{i}"))
    assert renderer._held <= 100
    assert renderer._cache.get("k0") is None  # oldest went first
    assert renderer._cache.get("k3") is not None


def test_an_entry_larger_than_the_cache_is_not_stored():
    """Keeping it would evict everything else to hold one render nobody asked for
    twice."""
    renderer = server.Renderer(cache_bytes=100)
    renderer._store("small", (b"x" * 10, "e"))
    renderer._store("huge", (b"x" * 200, "e"))
    assert list(renderer._cache) == ["small"]


def test_a_full_queue_is_turned_away_rather_than_made_to_wait(monkeypatch):
    """A studio page re-rendering on every slider move would otherwise build a
    backlog of renders nobody is looking at any more."""
    monkeypatch.setattr(server, "QUEUE_LIMIT", 2)
    # Long enough that the waiters cannot time out mid-test, but the slot is released
    # immediately afterwards, so nothing waits it out.
    monkeypatch.setattr(server, "RENDER_WAIT_S", 30.0)
    renderer = server.Renderer()
    monkeypatch.setattr(renderer, "_draw", lambda request: b"drawn")
    request = server.parse_art(BASE)

    waiters = [threading.Thread(target=renderer.render, args=(request,)) for _ in range(2)]
    assert renderer._slot.acquire(timeout=5)
    try:
        for t in waiters:
            t.daemon = True
            t.start()
        _until(lambda: renderer._waiting == 2)
        with pytest.raises(server.Unavailable, match="2 renders already queued"):
            renderer.render(request)
    finally:
        renderer._slot.release()
    for t in waiters:
        t.join(timeout=5)
        assert not t.is_alive()


def _count_draws(monkeypatch) -> list[int]:
    """[calls to art.render_bytes], so a cache hit is visible as a call that
    did not happen."""
    calls = [0]
    real = art.render_bytes

    def counted(*a, **kw):
        calls[0] += 1
        return real(*a, **kw)

    monkeypatch.setattr(art, "render_bytes", counted)
    return calls


def _message(renderer: server.Renderer, request: server.ArtRequest) -> str:
    try:
        renderer.render(request)
    except server.Unavailable as exc:
        return str(exc)
    return ""


def _until(ready, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready():
            return
        time.sleep(0.005)
    raise AssertionError("condition never became true")


# --- Metadata ---------------------------------------------------------------


def test_meta_reports_every_style_and_preset(art_db):
    meta = server.art_meta(True)
    assert [s["name"] for s in meta["styles"]] == list(art.STYLES)
    assert all(s["blurb"] for s in meta["styles"])
    assert meta["presets"]["cardiff"] == [-3.32, 51.42, -3.08, 51.57]
    assert set(meta["presets"]) == set(art.PRESETS)


def test_meta_reflects_whether_rendering_is_on(art_db):
    assert server.art_meta(True)["enabled"] is True
    assert server.art_meta(False)["enabled"] is False


def test_meta_reports_the_feed_version(art_db):
    database = server.art_meta(True)["database"]
    assert database["present"] is True
    assert database["feed_version"] == "20260806_022608"
    assert database["edges"] == 3
    assert "error" not in database


def test_meta_survives_a_database_with_no_feed_version(tmp_path, monkeypatch):
    """A bare select raises on an empty result, and a viewer that cannot name the feed
    is still a working viewer."""
    path = tmp_path / "empty.duckdb"
    db.connect(path).close()
    monkeypatch.setattr(config, "DB_PATH", path)
    database = server.art_meta(True)["database"]
    assert database["feed_version"] is None
    assert "error" not in database


def test_meta_survives_no_database_at_all(no_db):
    meta = server.art_meta(True)
    assert meta["database"] == {"present": False}
    assert meta["styles"]  # the controls still build


# --- render_bytes -----------------------------------------------------------


HELD = [
    art.Edge(1, "secondary", 100.0, [(-3.20, 51.480), (-3.19, 51.485)], 1, 100, ("42",)),
    art.Edge(2, "secondary", 100.0, [(-3.19, 51.485), (-3.18, 51.490)], 1, 900, ("9A",)),
]


@pytest.mark.parametrize(("fmt", "magic"), [(".png", b"\x89PNG"), (".svg", b"<svg")])
def test_render_bytes_writes_nothing_to_disk(fmt, magic, tmp_path, monkeypatch):
    """The reason the function exists: an image on its way to a socket has no business
    landing in the output directory first."""
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(config, "OUT", out)
    body = art.render_bytes(
        "cardiff", "density", fmt=fmt, opts=art.RenderOpts(width_px=200), edges=HELD
    )
    assert magic in body[:64]
    assert list(out.iterdir()) == []


def test_render_bytes_rejects_a_format_before_touching_the_database(no_db):
    with pytest.raises(ValueError, match="unsupported output format"):
        art.render_bytes("cardiff", "density", fmt=".tiff")


@pytest.mark.parametrize("fmt", [".png", ".svg"])
def test_render_bytes_is_deterministic(fmt):
    opts = art.RenderOpts(width_px=200)
    once = art.render_bytes("cardiff", "density", fmt=fmt, opts=opts, edges=HELD)
    twice = art.render_bytes("cardiff", "density", fmt=fmt, opts=opts, edges=HELD)
    assert once == twice


# --- Over HTTP --------------------------------------------------------------


@pytest.fixture
def serve_at(tmp_path: Path, monkeypatch):
    """Starts one server on an ephemeral port and shuts it down again."""
    running: list[tuple[socketserver.TCPServer, threading.Thread]] = []

    def start(*, art_enabled: bool = True) -> str:
        web = tmp_path / "web"
        web.mkdir(exist_ok=True)
        out = tmp_path / "out"
        out.mkdir(exist_ok=True)
        (out / "wales.pmtiles").write_bytes(b"pmtiles")
        (out / "notes.txt").write_text("not an archive")
        monkeypatch.setattr(server.Handler, "out_dir", out)
        monkeypatch.setattr(
            server.Handler, "renderer", server.Renderer() if art_enabled else None
        )
        handler = functools.partial(server.Handler, directory=str(web))
        httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        httpd.daemon_threads = True
        # A short poll interval only so shutdown() returns promptly in teardown.
        thread = threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True)
        thread.start()
        running.append((httpd, thread))
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield start
    for httpd, thread in running:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def _get(url: str, **headers: str):
    request = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(request, timeout=30)  # noqa: S310 - a local test server


def test_art_over_http_answers_with_a_png_an_etag_and_a_timing(art_db, serve_at):
    base = serve_at()
    with _get(f"{base}/art?{BASE}") as response:
        assert response.status == 200
        assert response.headers["Content-Type"] == "image/png"
        assert response.headers["ETag"]
        assert response.headers["Server-Timing"].startswith("render;dur=")
        assert response.read().startswith(b"\x89PNG")


def test_an_unchanged_request_costs_a_304(art_db, serve_at):
    base = serve_at()
    with _get(f"{base}/art?{BASE}") as response:
        etag = response.headers["ETag"]
        response.read()
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(f"{base}/art?{BASE}", **{"If-None-Match": etag})
    assert exc.value.code == 304
    assert exc.value.read() == b""


def test_a_bad_parameter_is_json_not_an_html_page(art_db, serve_at):
    """Every caller of this endpoint is a program. send_error's HTML page shows up in
    an <img> as a broken image with the reason nowhere anyone can read it."""
    base = serve_at()
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(f"{base}/art?area=cardiff&hue=7")
    assert exc.value.code == 400
    assert exc.value.headers["Content-Type"] == "application/json"
    assert "hue=7 is out of range" in json.loads(exc.value.read())["error"]


def test_meta_over_http(art_db, serve_at):
    base = serve_at()
    with _get(f"{base}/art/meta") as response:
        assert response.status == 200
        meta = json.loads(response.read())
    assert meta["enabled"] is True
    assert meta["presets"]["cardiff"]


def test_a_server_without_art_says_so_rather_than_failing(art_db, serve_at):
    base = serve_at(art_enabled=False)
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(f"{base}/art?{BASE}")
    assert exc.value.code == 501
    assert "--no-art" in json.loads(exc.value.read())["error"]
    with _get(f"{base}/art/meta") as response:
        assert json.loads(response.read())["enabled"] is False


def test_archives_lists_only_the_tile_archives(serve_at):
    base = serve_at()
    with _get(f"{base}/archives.json") as response:
        assert json.loads(response.read()) == ["wales.pmtiles"]
