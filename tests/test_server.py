from __future__ import annotations

import dataclasses
import functools
import gzip
import http.client
import io
import json
import os
import socket
import socketserver
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
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
        # The spec's own vocabularies, and each mistake reports the table it belongs
        # to rather than all three.
        # The field is named as well as the alternatives listed: `busiest` is both a
        # valid weight and a valid order, so "unknown 'busiest'" would be ambiguous.
        ("area=cardiff&weight=popularity", "unknown weight=.*known weights: busiest"),
        ("area=cardiff&group=depot", "unknown group=.*known groups: operator"),
        ("area=cardiff&order=alphabetical", "unknown order=.*known orders: busiest"),
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
        ("area=cardiff&sample=99", "sample=99 is out of range; it runs 1 to 16"),
        ("area=cardiff&sample=0", "sample=0 is out of range"),
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


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "TRUE"])
def test_coalesce_takes_the_spellings_a_caller_would_reach_for(raw):
    assert server.parse_art(f"area=cardiff&coalesce={raw}").opts.coalesce is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
def test_coalesce_off_is_off(raw):
    assert server.parse_art(f"area=cardiff&coalesce={raw}").opts.coalesce is False


def test_coalesce_absent_is_off_and_a_typo_is_not():
    """Reading anything unrecognised as false is how `coalesce=nope` silently
    renders the picture the caller was trying to change."""
    assert server.parse_art("area=cardiff").opts.coalesce is False
    with pytest.raises(server.BadRequest, match="yes or a no"):
        server.parse_art("area=cardiff&coalesce=nope")


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on"])
def test_the_credit_caption_can_be_asked_for(raw):
    assert server.parse_art(f"area=cardiff&credit={raw}").opts.credit is True


def test_the_credit_caption_is_off_unless_asked_for():
    """The metadata credit is unconditional and invisible; this one is drawn into
    the picture, so it stays a decision."""
    assert server.parse_art("area=cardiff").opts.credit is False
    assert server.parse_art("area=cardiff&credit=off").opts.credit is False


def test_the_credit_caption_splits_the_cache():
    """A credited render and a plain one are different pictures, so an ETag that
    covered both would serve one for the other."""
    assert (
        server.parse_art("area=cardiff&credit=1").key
        != server.parse_art("area=cardiff").key
    )


def test_coalesce_splits_the_cache():
    """It is a different picture, so it must not be served from the same ETag."""
    assert (
        server.parse_art("area=cardiff&coalesce=1").key
        != server.parse_art("area=cardiff").key
    )


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


def test_a_bare_request_asks_for_the_original_query():
    """Everything about the spec is optional, and asking for none of it must draw what
    the endpoint drew before the spec existed."""
    assert server.parse_art(BASE).query == art.DEFAULT_SPEC


def test_the_spec_reaches_the_request():
    request = server.parse_art(
        f"{BASE}&weight=density&group=operator&order=name&min_trips=20"
    )
    assert request.query.weight == "density"
    assert request.query.group == "operator"
    assert request.query.order == "name"
    assert request.query.min_trips == 20


def test_a_filter_takes_repeats_and_commas_alike():
    """A multi-select sends one comma-joined value; a shell loop appends a parameter
    per value. Both spellings mean the same filter."""
    commas = server.parse_art(f"{BASE}&operator=FIRST,STAGE").query
    repeats = server.parse_art(f"{BASE}&operator=FIRST&operator=STAGE").query
    assert commas.operator == ("FIRST", "STAGE") == repeats.operator
    both = server.parse_art(f"{BASE}&operator=STAGE&operator=FIRST,+STAGE+").query
    assert both.operator == ("FIRST", "STAGE")


def test_class_is_the_url_spelling_of_road_class():
    query = server.parse_art(f"{BASE}&class=motorway,trunk&service=X1").query
    assert query.road_class == ("motorway", "trunk")
    assert query.service == ("X1",)


def test_a_reordered_filter_is_the_same_render():
    """Order cannot matter to an IN list, so two spellings of one filter must not each
    pay for a draw."""
    one = server.parse_art(f"{BASE}&operator=STAGE,FIRST&class=trunk,motorway")
    other = server.parse_art(f"{BASE}&operator=FIRST,STAGE&class=motorway,trunk")
    assert one.key == other.key


@pytest.mark.parametrize(
    ("query", "match"),
    [
        (f"operator={','.join(str(i) for i in range(65))}", "over the 64 limit"),
        ("min_trips=-1", "min_trips=-1 is out of range; it runs 0 to"),
        ("min_trips=9999999999", "is out of range"),
        ("min_trips=1.5", "is not a whole number"),
    ],
)
def test_the_filters_are_bounded(query, match):
    with pytest.raises(server.BadRequest, match=match):
        server.parse_art(f"{BASE}&{query}")


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
        # The query spec draws as different a picture as the style does, so every part
        # of it has to reach the key as well.
        "area=cardiff&width=200&weight=services",
        "area=cardiff&width=200&group=operator",
        "area=cardiff&width=200&order=quietest",
        "area=cardiff&width=200&operator=OP1",
        "area=cardiff&width=200&service=42",
        "area=cardiff&width=200&class=secondary",
        "area=cardiff&width=200&min_trips=50",
        # A sampled preview and the full render are different pictures, so serving
        # one from the other's cache entry would show an approximation as the export.
        "area=cardiff&width=200&sample=8",
    ],
)
def test_every_drawn_parameter_reaches_the_cache_key(query):
    assert server.parse_art(query).key != server.parse_art(BASE).key


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


def test_two_specs_draw_two_pictures(art_db):
    """The point of the whole parameter set. `min_trips=250` drops two of the three
    fixture edges, so a shared cache entry would be visible as the same image."""
    renderer = server.Renderer()
    everything, etag = renderer.render(server.parse_art(BASE))
    filtered, filtered_etag = renderer.render(server.parse_art(f"{BASE}&min_trips=250"))
    assert filtered != everything
    assert filtered_etag != etag


def test_a_filter_the_data_cannot_satisfy_still_draws(art_db):
    """An operator this database has never heard of is an empty picture, not an error:
    there is nothing for the caller to fix, and the ground colour says as much."""
    png, _ = server.Renderer().render(server.parse_art(f"{BASE}&operator=NOSUCHCO"))
    assert png.startswith(b"\x89PNG")


def test_too_many_groups_is_the_callers_fault_not_a_server_error(art_db, monkeypatch):
    """`group=way` over a city is one ribbon per OSM way. art.Window.groups refuses
    rather than drawing it, and that refusal has to reach the caller as a 400 with the
    reason -- a 500 would read as a bug in the server."""
    monkeypatch.setattr(art, "MAX_GROUPS", 1)
    with pytest.raises(ValueError, match="over the 1 limit") as exc:
        server.Renderer().render(server.parse_art(f"{BASE}&style=strands&group=service"))
    assert not isinstance(exc.value, server.BadRequest)  # raised by art, not by parsing


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


def test_meta_reports_the_feed_version(art_db):
    database = server.art_meta(True)["database"]
    assert database["present"] is True
    assert database["feed_version"] == "20260806_022608"
    assert database["edges"] == 3
    assert "error" not in database


def test_meta_serves_the_credit_the_renders_carry(art_db):
    """The studio states it at the download, which is where the obligation lands.
    Served rather than written into the page: it follows the region this server's
    database holds, not the markup."""
    assert server.art_meta(True)["credit"] == config.credit_html()


def test_meta_publishes_the_query_vocabularies_in_a_stable_order(art_db):
    """The page builds three dropdowns from these, so adding a weight in art.py has to
    show up in the UI without touching any HTML. Declaration order, not sorted: the
    default belongs at the top of the menu."""
    meta = server.art_meta(True)
    assert meta["query"]["weights"] == list(art.WEIGHTS)
    assert meta["query"]["groups"] == list(art.GROUPS)
    assert meta["query"]["orders"] == list(art.ORDERS)
    assert meta["query"]["weights"][0] == meta["defaults"]["weight"] == "trips"
    assert meta["limits"]["max_groups"] == art.MAX_GROUPS
    assert meta["limits"]["max_filter_values"] == server.MAX_FILTER_VALUES
    assert meta["limits"]["max_min_trips"] == server.MAX_MIN_TRIPS


def test_meta_reports_the_operators_and_road_classes_the_database_holds(art_db):
    """A dropdown of the values that exist beats a free-text box whose typos answer
    with an empty picture."""
    database = server.art_meta(True)["database"]
    assert database["operators"] == ["OP1"]
    assert database["road_classes"] == ["secondary"]


def test_the_facet_lists_are_bounded(art_db, monkeypatch):
    monkeypatch.setattr(server, "MAX_FACET_VALUES", 1)
    con = db.connect(art_db)
    con.execute("INSERT INTO edge_services VALUES (1, '7', 'OP2', 1, 5)")
    con.close()
    assert len(server.art_meta(True)["database"]["operators"]) == 1


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


# --- Compression ------------------------------------------------------------


@pytest.fixture
def gzip_cache(monkeypatch) -> OrderedDict:
    """An empty cache per test, since it is process-wide and outlives a server."""
    cache: OrderedDict[tuple[str, int, int], bytes] = OrderedDict()
    monkeypatch.setattr(server, "_gzip_cache", cache)
    return cache


def _count_compressions(monkeypatch) -> list[int]:
    """[calls to gzip.compress], so a cache hit is visible as work not done."""
    calls = [0]
    real = gzip.compress

    def counted(*a, **kw):
        calls[0] += 1
        return real(*a, **kw)

    monkeypatch.setattr(server.gzip, "compress", counted)
    return calls


def test_a_file_is_compressed_once_and_again_when_it_changes(
    tmp_path, monkeypatch, gzip_cache
):
    """784 KB of vendored maplibre cost 15.8 ms of CPU to gzip and 0.04 ms to read,
    and the page asks for the same handful of files every load -- so recompressing
    per request was nearly all of the cost of serving them. Keyed on the mtime and
    size the ETag already uses, so an edited file recompresses rather than being
    served stale under a validator that has moved."""
    path = tmp_path / "app.js"
    path.write_text("var x = 1;\n" * 500)
    calls = _count_compressions(monkeypatch)

    first = server._gzipped(str(path))
    assert first is not None
    assert gzip.decompress(first) == path.read_bytes()
    assert server._gzipped(str(path)) == first
    assert calls == [1]

    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    again = server._gzipped(str(path))
    assert calls == [2]
    # Byte-identical, because the content did not change and `mtime=0` keeps gzip
    # from stamping the clock into its header. Without it two compressions of one
    # file differ, under an ETag that says they do not.
    assert again == first

    path.write_text("var x = 2;\n" * 500)
    changed = server._gzipped(str(path))
    assert calls == [3]
    assert changed != first


def test_the_compression_cache_is_bounded(tmp_path, monkeypatch, gzip_cache):
    """COMPRESS_MAX caps an entry at 1 MiB, so a count is enough to bound the
    total. The web directory holds six compressible files; the cap is for a
    directory nobody has served yet."""
    monkeypatch.setattr(server, "GZIP_CACHE_ENTRIES", 2)
    for i in range(3):
        path = tmp_path / f"f{i}.js"
        path.write_text(f"var x{i};")
        server._gzipped(str(path))
    assert len(gzip_cache) == 2
    assert not any(key[0].endswith("f0.js") for key in gzip_cache)  # oldest went first


def test_an_unreadable_file_compresses_to_nothing_rather_than_raising(tmp_path):
    assert server._gzipped(str(tmp_path / "absent.js")) is None


def test_an_aborted_body_closes_the_connection():
    """The prerequisite for keep-alive. A body that stopped short of its
    Content-Length has desynchronised the stream, so the next response would be
    parsed as the tail of this one -- a truncated tile becoming a corrupt one on a
    connection that still looks healthy. Under HTTP/1.0 the connection died with
    the response either way, which is why swallowing the abort was free."""

    class Hangup:
        def write(self, data: bytes) -> int:
            raise BrokenPipeError(32, "Broken pipe")

    handler = server.Handler.__new__(server.Handler)
    handler.close_connection = False
    handler.copyfile(io.BytesIO(b"x" * 4096), Hangup())
    assert handler.close_connection is True

    handler.close_connection = False
    handler.copyfile(io.BytesIO(b"x" * 4096), io.BytesIO())
    assert handler.close_connection is False


# --- Over HTTP --------------------------------------------------------------


@pytest.fixture
def serve_at(tmp_path: Path, monkeypatch):
    """Starts one server on an ephemeral port and shuts it down again."""
    running: list[tuple[socketserver.TCPServer, threading.Thread]] = []

    def start(
        *, art_enabled: bool = True, handler_cls: type[server.Handler] = server.Handler
    ) -> str:
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
        handler = functools.partial(handler_cls, directory=str(web))
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


def test_a_spec_over_http_answers_with_its_own_image_and_etag(art_db, serve_at):
    base = serve_at()
    seen = {}
    for query in (BASE, f"{BASE}&weight=services&group=operator&min_trips=150"):
        with _get(f"{base}/art?{query}") as response:
            assert response.status == 200
            seen[response.headers["ETag"]] = response.read()
    assert len(seen) == 2  # two ETags
    assert len(set(seen.values())) == 2  # and two pictures


def test_too_many_groups_reads_as_a_400_over_http(art_db, serve_at, monkeypatch):
    """The one error a caller will actually hit. It has to arrive as the message
    art.Window.groups wrote, so the studio page can show it verbatim."""
    monkeypatch.setattr(art, "MAX_GROUPS", 1)
    base = serve_at()
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(f"{base}/art?{BASE}&style=strands&group=service")
    assert exc.value.code == 400
    detail = json.loads(exc.value.read())["error"]
    assert "group='service' gives 2 groups" in detail
    assert "group by something coarser" in detail


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


# --- The pages' own credits --------------------------------------------------
#
# The data credit rides in the archive, so nothing here needs to know it. The
# basemap credit cannot: it belongs to the page, and it used to be the same string
# typed into two files. These guard the drift, not the wording.

WEB = Path(__file__).resolve().parents[1] / "web"
PAGES = ("index.html", "art.html")


def test_both_pages_take_the_basemap_credit_from_one_place():
    assert "carto.com/attributions" in (WEB / "credits.js").read_text()
    for page in PAGES:
        text = (WEB / page).read_text()
        assert 'src="credits.js"' in text
        assert "BASEMAP_CREDIT" in text
        # The copy each page used to carry. Both draw the same backdrop, so a
        # second spelling of it is a spelling that will drift.
        assert "carto.com/attributions" not in text


def test_both_pages_fold_the_credit_away_on_load():
    """`compact: true` buys the (i) button and not the closed state -- MapLibre
    opens the panel itself as soon as a source reports a credit. Every map has to
    start it closed, so a third one added later is the thing this catches."""
    assert "function collapseCredit" in (WEB / "credits.js").read_text()
    for page in PAGES:
        assert "collapseCredit(" in (WEB / page).read_text()


def test_neither_page_hardcodes_the_data_credit():
    """Which is the whole design: `wayfare publish` puts it in the archive, and a
    page that also stated it would be wrong for whichever region is not showing."""
    for page in (*PAGES, "credits.js"):
        assert "Open Government Licence" not in (WEB / page).read_text()


# --- The viewer's own style ---------------------------------------------------
#
# Two rules the browser enforces and nothing else does. There is no JavaScript
# test harness here, so these read the source: narrow, and each one guards a
# failure that showed up as a page rather than as a stack trace.


def _const(page: str, name: str) -> str:
    """One `const <name> = [...]` from a page, whitespace collapsed."""
    text = (WEB / page).read_text()
    start = text.index(f"const {name} = ")
    return " ".join(text[start : text.index("\n];", start)].split())


def test_every_width_keeps_zoom_at_the_top_of_its_interpolate():
    """A `["zoom"]` expression is legal only as the input to the outermost
    interpolate, and MapLibre rejects the whole style for one paint property that
    breaks the rule -- basemap, roads, every region. `trackWidth` wrapped its zoom
    interpolate in a `["+"]` to add the hover bump, so the viewer drew a blank page
    with one line in the console for every archive, from the day the track layer
    landed. The bump belongs on each stop."""
    for name in ("width", "segWidth", "trackWidth"):
        body = _const("index.html", name)
        assert body.startswith(
            f'const {name} = (bump) => [ "interpolate", ["linear"], ["zoom"],'
        )


def test_every_legend_row_is_a_switch():
    """The key is also the control: a checkbox per row, on unless it has been
    cleared. `hiddenRows` holds that off the DOM because `paintLegend` rewrites the
    rows on a theme change and on the first sighting of a mode -- a box drawn from
    the markup alone comes back ticked over a mode that is still off the map."""
    text = (WEB / "index.html").read_text()
    assert 'type="checkbox" data-row=' in text
    assert "const off = hiddenRows.has(key);" in text
    assert '${off ? "" : " checked"}' in text
    # The road network and the relation track are a layer each; the modes share one
    # layer per region, so a mode goes off by filter and not by visibility.
    assert "withoutModes" in text
    assert "map.setFilter(id, off.length ? withoutModes(off) : null)" in text


def _connect(base: str) -> http.client.HTTPConnection:
    """A raw connection, because urllib sends `Connection: close` on every request
    and so can never show one being reused."""
    url = urllib.parse.urlparse(base)
    assert url.hostname and url.port
    return http.client.HTTPConnection(url.hostname, url.port, timeout=30)


def test_two_requests_share_one_connection(serve_at):
    """MapLibre issues dozens of PMTiles range requests per pan, and under HTTP/1.0
    each was a fresh TCP connection and a fresh thread. On loopback the setup is a
    fraction of a millisecond; on the deployed tailnet path it is a full round
    trip, which is most of what a 16 KB range costs."""
    conn = _connect(serve_at())
    try:
        conn.request("GET", "/archives.json")
        first = conn.getresponse()
        assert first.version == 11  # HTTP/1.1, not the stdlib's 1.0 default
        first.read()
        socket = conn.sock
        assert socket is not None  # http.client drops it when the server says close

        conn.request("GET", "/wales.pmtiles")
        second = conn.getresponse()
        assert second.status == 200
        assert second.read() == b"pmtiles"
        assert conn.sock is socket  # the same connection, not a reconnect
    finally:
        conn.close()


def test_a_kept_alive_connection_sends_without_waiting_for_nagle(serve_at):
    """The other half of keep-alive, and a cost it introduced rather than found.

    `BaseHTTPRequestHandler` flushes its headers and then writes the body as a
    second, smaller write. Nagle holds that write until the peer acknowledges the
    first, and Linux delays that acknowledgement by 40 ms. Closing after each
    response flushed it, so HTTP/1.0 could not show this; keeping the connection
    open put the timer on every request after the first. Measured on loopback in
    the deployed container: 41 ms a request, against 0.3 ms with TCP_NODELAY set.

    The assertion is on the socket rather than on a duration, because a timing
    threshold either has to be loose enough to pass while the stall is back or
    tight enough to fail on a loaded runner.
    """
    seen: list[int] = []

    class Recording(server.Handler):
        def do_GET(self) -> None:
            seen.append(self.connection.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY))
            super().do_GET()

    conn = _connect(serve_at(handler_cls=Recording))
    try:
        for _ in range(2):
            conn.request("GET", "/wales.pmtiles")
            conn.getresponse().read()
    finally:
        conn.close()

    # Both requests, so this cannot pass on the first while the reused connection --
    # the only one that ever stalled -- goes back to waiting for the timer. The
    # option is a flag and not a 1: Linux reports it set as 1 and macOS as 4.
    assert len(seen) == 2
    assert all(seen)


def test_a_range_naming_neither_end_is_a_400(serve_at):
    """`bytes=-` matches the Range pattern -- both halves are `\\d*` -- and asks for
    nothing. It used to reach `int("")` and raise, which on a connection the client
    means to reuse costs more than the one aborted request it used to."""
    base = serve_at()
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(f"{base}/wales.pmtiles", Range="bytes=-")
    assert exc.value.code == 400
    assert "malformed Range" in exc.value.reason


def test_an_unsatisfiable_range_frames_its_empty_body(serve_at):
    """416 sends no body, and HTTP/1.1 has no read-until-EOF framing to fall back
    on -- so without an explicit length the client waits for a body that is never
    coming, on a connection it is entitled to keep."""
    conn = _connect(serve_at())
    try:
        conn.request("GET", "/wales.pmtiles", headers={"Range": "bytes=99-"})
        response = conn.getresponse()
        assert response.status == 416
        assert response.headers["Content-Length"] == "0"
        assert response.headers["Content-Range"] == "bytes */7"
        assert response.read() == b""
        # Framed, so the connection survives it and the next request is answered
        # rather than being read as the tail of this one.
        conn.request("GET", "/archives.json")
        assert json.loads(conn.getresponse().read()) == ["wales.pmtiles"]
    finally:
        conn.close()


def test_a_satisfiable_suffix_range_still_works(serve_at):
    """The branch `bytes=-` used to fall into. PMTiles reads its footer this way,
    without knowing the file length up front."""
    base = serve_at()
    with _get(f"{base}/wales.pmtiles", Range="bytes=-5") as response:
        assert response.status == 206
        assert response.headers["Content-Range"] == "bytes 2-6/7"
        assert response.headers["Content-Length"] == "5"
        assert response.read() == b"tiles"


def test_a_compressible_file_goes_out_gzipped_and_stays_identical(
    tmp_path, serve_at, gzip_cache
):
    base = serve_at()
    (tmp_path / "web" / "app.js").write_text("var x = 1;\n" * 500)
    bodies = set()
    for _ in range(2):
        with _get(f"{base}/app.js", **{"Accept-Encoding": "gzip"}) as response:
            assert response.headers["Content-Encoding"] == "gzip"
            assert response.headers["Vary"] == "Accept-Encoding"
            assert response.headers["ETag"].endswith('-gzip"')
            body = response.read()
            assert response.headers["Content-Length"] == str(len(body))
            bodies.add(body)
    assert len(bodies) == 1
    assert gzip.decompress(bodies.pop()) == (tmp_path / "web" / "app.js").read_bytes()
    assert len(gzip_cache) == 1
