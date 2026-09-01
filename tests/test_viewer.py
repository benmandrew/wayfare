"""Source-level invariants of the two viewer pages, `web/index.html` and `web/art.html`.

There is no JavaScript test harness in this repository, so nothing here runs a line
of the 3,674 lines of viewer script: no DOM, no MapLibre, no pmtiles.js, no browser.
These tests read the pages as text. That bounds what they can say. They cannot show
that the map draws, that a hover answers, or that a style expression evaluates -- only
that the page still says the thing that made it work. Every one of them guards a
failure that arrived as a page rather than as a stack trace: a blank map with one
console line, a panel off the side of the screen, a credit naming only the basemap.

Being the only coverage those lines have, they are written to fail for the reason
they name and for nothing else. Whitespace is not the reason: a page reformatted by
hand or by a tool must not break a test that is about which expression is nested
inside which. So the assertions match against `_tight` -- the source with every run
of whitespace removed -- or against a regex that spells out where whitespace may
vary. Where a space is part of a string literal the page emits, it is matched
exactly, because there the space is the meaning.

A page is more than its own file. Both pages link `base.css` for the palette and
the panel chrome, and `util.js` beside `credits.js` for what the map and the studio
both do, so an assertion about the cascade reads `_cascade` -- the shared sheet
followed by the page, which is the order the browser applies them in -- and one
about a shared helper reads the file that holds it. What each page must *not* say
is still read off the page alone.

One test here starts `wayfare.server` over the real `web/` directory, because a
file the pages link and the server does not reach is a page that loads without its
palette, and nothing in the source of either page can show that. Everything else
about the serving is `tests/test_server.py`.
"""

from __future__ import annotations

import functools
import re
import socketserver
import threading
import urllib.request
from pathlib import Path

from wayfare import server

WEB = Path(__file__).resolve().parents[1] / "web"
PAGES = ("index.html", "art.html")
# Linked by both pages, in this order, ahead of the page's own rules and script.
SHARED = ("base.css", "util.js", "credits.js")


@functools.cache
def _source(page: str) -> str:
    return (WEB / page).read_text()


@functools.cache
def _cascade(page: str) -> str:
    """The shared stylesheet and then the page, as the browser stacks them.

    Offsets into this are cascade order, which is what a fallback-before-override
    assertion is about. A rule that moved into `base.css` still has to come before
    the page rule that overrides it, and here it does.
    """
    return (WEB / "base.css").read_text() + _source(page)


def _tight(text: str) -> str:
    """`text` with every run of whitespace removed.

    Both sides of a comparison go through this, so an assertion about the shape of
    an expression survives the page being re-indented or re-wrapped.
    """
    return re.sub(r"\s+", "", text)


def _holds(page: str, snippet: str) -> bool:
    return _tight(snippet) in _tight(_source(page))


def _const(page: str, name: str) -> str:
    """The source of one `const <name> = [...]`, tightened.

    The array's close is found by pattern rather than by an exact `\\n];`, so a
    reformatted page still yields the declaration instead of raising here.
    """
    text = _source(page)
    start = text.index(f"const {name} = ")
    end = re.compile(r"\n\s*\];").search(text, start)
    assert end is not None, f"{name} in {page} is not an array declaration"
    return _tight(text[start : end.start()])


def _between(text: str, start: str, end: str) -> str:
    """The source from `start` up to the first `end` after it."""
    at = text.index(start)
    return text[at : text.index(end, at)]


def _declared(page: str, prop: str, value: str) -> int:
    """Where a `prop: value;` declaration first appears, or -1.

    An offset rather than a boolean, because two declarations of one property are
    a fallback and an override, and which comes first is the whole point of them.
    Read out of the whole cascade, so a declaration in `base.css` is found where it
    actually applies rather than reported missing.
    """
    found = re.search(rf"{re.escape(prop)}\s*:\s*{re.escape(value)}\s*;", _cascade(page))
    return found.start() if found else -1


# --- The pages' own credits --------------------------------------------------
#
# The data credit rides in the archive, so nothing here needs to know it. The
# basemap credit cannot: it belongs to the page, and both pages draw the same
# backdrop, so it lives in credits.js alone. These guard the drift, not the wording.


def test_both_pages_take_the_basemap_credit_from_one_place():
    assert "protomaps/basemaps" in (WEB / "credits.js").read_text()
    for page in PAGES:
        text = _source(page)
        assert 'src="credits.js"' in text
        assert "BASEMAP_CREDIT" in text
        # The literal URL belongs in no page: a second spelling of the credit is a
        # spelling that will drift away from BASEMAP_CREDIT.
        assert "protomaps/basemaps" not in text


def test_both_pages_fold_the_credit_away_on_load():
    """`compact: true` buys the (i) button and not the closed state -- MapLibre
    opens the panel itself as soon as a source reports a credit. Every map has to
    start it closed, so a third one added later is the thing this catches."""
    assert re.search(r"function\s+collapseCredit\b", (WEB / "credits.js").read_text())
    for page in PAGES:
        assert re.search(r"collapseCredit\s*\(", _source(page))


def test_neither_page_hardcodes_the_data_credit():
    """Which is the whole design: `wayfare publish` puts it in the archive, and a
    page that also stated it would be wrong for whichever region is not showing."""
    for page in (*PAGES, "credits.js"):
        assert "Open Government Licence" not in (WEB / page).read_text()


def test_both_pages_ask_the_pmtiles_plugin_for_the_archive_metadata():
    """`metadata: true` is off by default, and without it MapLibre never sees a
    tileset's metadata at all -- which is where `publish` stamps the credit the
    licence obliges. What that looks like is not an error but a viewer crediting
    its basemap and nothing else, on an archive that carries the credit correctly.

    Every construction is checked rather than the first, because a second protocol
    added for a second source would be the one that drops it."""
    for page in PAGES:
        made = re.findall(r"new\s+pmtiles\.Protocol\s*\(([^)]*)\)", _source(page))
        assert made, f"{page} builds no pmtiles.Protocol"
        for args in made:
            assert re.search(r"metadata\s*:\s*true", args), f"{page}: {args}"


# --- What the two pages share -------------------------------------------------
#
# They are still two self-contained pages, and the shared files are the parts that
# had already been copied between them. A copy is not wrong until it drifts, and
# every one of these had: the studio's basemap lost the slow-link check, its
# roaming box gained a guard the viewer's never got, and the same box was written
# nested on one page and flat on the other.


def test_the_pages_take_what_they_share_from_one_place():
    """Neither page may hold its own copy of anything in `util.js`, because a copy
    is what the drift above was made of. `escapeHtml` is the one that matters most
    and reads least: it is what stands between a service number out of a feed and
    the innerHTML of the card, and two of them is one that can be fixed alone."""
    shared = (WEB / "util.js").read_text()
    for name in ("escapeHtml", "roamingBounds", "basemapLayers", "debounce", "bootTheme"):
        assert re.search(rf"(function|const)\s+{name}\b", shared), name
        for page in PAGES:
            assert not re.search(rf"(function|const)\s+{name}\b", _source(page)), (
                page,
                name,
            )
    # And the linking that makes the sharing real, in both pages.
    for page in PAGES:
        for name in SHARED:
            assert name in _source(page), (page, name)


def test_the_shared_files_are_reachable_and_typed():
    """A stylesheet the server does not answer for is a page drawn with no palette,
    and a script it answers as `text/plain` is a page that runs none of it --
    neither of which the source of a page can show. So they are fetched.

    `wayfare.server` over the real `web/` directory rather than a fixture, since
    what is in question is whether these particular files are reachable through the
    server the deployment runs, at a type a browser will act on."""
    handler = functools.partial(server.Handler, directory=str(WEB))
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
        httpd.daemon_threads = True
        thread = threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            wanted = {
                "base.css": "text/css",
                "util.js": "javascript",
                "credits.js": "javascript",
                # Generated rather than written, and the viewer's every colour and
                # layer name is in it. A 404 here is a page that throws on the
                # first `PALETTE` it reads and draws no map at all.
                "palette.js": "javascript",
            }
            for name, kind in wanted.items():
                with urllib.request.urlopen(f"{base}/{name}", timeout=30) as res:
                    assert res.status == 200, name
                    assert kind in res.headers["Content-Type"], (name, res.headers)
                    assert res.read() == (WEB / name).read_bytes(), name
        finally:
            httpd.shutdown()
            thread.join(timeout=5)


def test_the_theme_is_on_the_document_before_anything_is_painted():
    """Both pages hardcoded `data-theme="light"` and applied the stored theme from
    the script at the foot of the body -- behind an 803 KB vendored library -- so a
    reader who had chosen dark got a full screen of white until it parsed.

    The read belongs in <head>, where it runs before the first paint. What this
    pins is the ordering: the call is in the head, and the vendored library is not
    what it waits on."""
    for page in PAGES:
        text = _source(page)
        head = text[: text.index("</head>")]
        assert re.search(r"<script>\s*bootTheme\(\);?\s*</script>", head), page
        # The library the theme used to queue behind is loaded past the head.
        assert "vendor/maplibre-gl.js" not in head, page
    # One definition, and it is the same one both pages call.
    assert re.search(r"function\s+bootTheme\b", (WEB / "util.js").read_text()), (
        "util.js defines no bootTheme"
    )


# --- The viewer's own style ---------------------------------------------------


def test_every_width_keeps_zoom_at_the_top_of_its_interpolate():
    """A `["zoom"]` expression is legal only as the input to the outermost
    interpolate, and MapLibre rejects the whole style for one paint property that
    breaks the rule -- basemap, roads, every region. `trackWidth` wrapped its zoom
    interpolate in a `["+"]` to add the hover bump, so the viewer drew a blank page
    with one line in the console for every archive, from the day the track layer
    landed. The bump belongs on each stop.

    All three widths now come out of one model, so the rule is checked where it is
    kept -- `widthOf` -- and each width is checked to go through it. The zoom term
    is arithmetic done in JavaScript, which is what keeps `["zoom"]` out of the
    stops: a model that reached for `["^", gain, ["-", ["zoom"], 6]]` instead would
    read perfectly well and blank the page."""
    model = _const("index.html", "widthOf")
    assert model.startswith(
        _tight('const widthOf = (f, bump) => ["interpolate", ["linear"], ["zoom"],')
    )
    for name in ("width", "segWidth", "trackWidth"):
        assert _holds("index.html", f"const {name} = (bump) => widthOf(WIDTH.")
    # One `["zoom"]` in the whole model, and it is that interpolate's input.
    assert model.count(_tight('["zoom"]')) == 1


def test_a_segment_with_no_timetable_keeps_its_mode():
    """`trips` is absent on every segment `routes` builds, because a relation is the
    service there and Britain publishes no rail timetable to count against it: the
    tile over Crewe holds 50 rail segments carrying `mode` and `ref` and nothing
    else. Drawn through `guarded` -- the road network's rule, where a missing weight
    means "no answer here" -- that painted the whole national railway in the neutral
    grey, next to the Republic's rail in full colour.

    So a segment falls back to the middle of its own mode's ramp, flat, which is what
    the track band does with the same absence. Grey goes back to meaning the one
    thing it should: a mode this palette does not know.

    The width falls the same way. `to-number`'s default was `quiet`, the bottom of
    the range, so those lines were drawn at the width of a service running twice a
    day as well as in the colour of one nobody could identify."""
    body = _const("index.html", "segColourExpr")
    assert _tight("shadedOrMid") in body and _tight("guarded") not in body
    assert _tight('const shadedOrMid = (t, m) => ["case", ["has", RAMP.needs],') in _const(
        "index.html", "shadedOrMid"
    )
    # The road network keeps the grey: a road has no mode colour to fall back to.
    assert _holds(
        "index.html", "const colourExpr = (t) => guarded(t, rampOver(RAMP_COLOURS[t]));"
    )
    assert _holds("index.html", '["to-number", ["get", f.weight], midOf(f)]')


def test_a_shadow_takes_every_filter_the_line_above_it_takes():
    """The shadows under the non-road modes are drawn and nothing else -- no hover
    query, no legend count -- but they carry the same features, so every filter and
    every dimming that reaches a line has to reach its shadow. A shadow the mode
    switch misses is the outline of a mode the legend says is off, and a shadow the
    search misses is the outline of a service the search said no to, drawn at full
    strength under a line dimmed to a tenth.

    They stay out of the hover query on purpose: a shadow is offset from its line,
    so a cursor over one is a cursor beside the thing it would report."""
    tight = _tight(_source("index.html"))
    for line, shadow in (("seg", "segShadow"), ("track", "trackShadow")):
        assert _holds("index.html", f"for (const id of [...{line}, ...{shadow}])")
        assert _holds("index.html", f"for (const id of {shadow})")
    assert _holds("index.html", "for (const id of [...segShadow, ...trackShadow])")
    # Above every road and under the line each belongs to.
    order = [
        tight.index(_tight(f"layers.push({name}(r))"))
        for name in (
            "busLayer",
            "trackShadowLayer",
            "trackLayer",
            "segShadowLayer",
            "segLayer",
        )
    ]
    assert order == sorted(order)
    # The cursor asks the lines, never the shadows.
    assert _holds("index.html", "at(seg)") and not _holds("index.html", "at(segShadow)")


def test_every_legend_row_is_a_switch():
    """The key is also the control: a checkbox per row, on unless it has been
    cleared. `hiddenRows` holds that off the DOM because `paintLegend` rewrites the
    rows on a theme change and on the first sighting of a mode -- a box drawn from
    the markup alone comes back ticked over a mode that is still off the map."""
    text = _source("index.html")
    assert re.search(r'type="checkbox"\s+data-row=', text)
    assert _holds("index.html", "const off = hiddenRows.has(key);")
    # The space inside `" checked"` is matched exactly: it is what separates the
    # attribute from the one before it, so losing it emits `data-row="bus"checked`.
    assert re.search(r'\$\{\s*off\s*\?\s*""\s*:\s*" checked"\s*\}', text)
    # The road network and the relation track are a layer each; the modes share one
    # layer per region, so a mode goes off by filter and not by visibility.
    assert "withoutModes" in text
    assert _holds("index.html", "const filter = off.length ? withoutModes(off) : null;")


def test_a_selection_that_has_not_moved_is_not_reapplied():
    """`setFilter` marks the layer's source for reload, and MapLibre's guard against
    a redundant call cannot fire on these layers: they are built with no `filter`, so
    the stored value is `undefined` and the guard's `undefined === null` is false.
    Every keystroke therefore re-parsed every tile of every archive, indefinitely.

    Both keys are asserted, because the guard has to be on the mode rows and the
    track rows alike -- one of the two left open is still a reload of one source per
    archive per keystroke. Sorting is asserted with them: unsorted, the key names the
    order the rows were clicked in rather than the selection, and two rows switched
    off in the other order read as a change that is not one."""
    text = _source("index.html")
    assert _holds("index.html", "if (nextMode !== modeKey) {")
    assert _holds("index.html", "if (nextTrack !== trackKey) {")
    assert len(re.findall(r"\[\.\.\.off\w*\]\.sort\(\)\.join\(\",\"\)", text)) == 2


def test_the_viewer_tells_the_two_id_spaces_apart_by_refs():
    """The detail band's feature id is the OSM way id and the overview bands' is the
    Valhalla edge id, so a hover has to know which range it is holding before it can
    report anything. `refs` is the sentinel: `_DETAIL_ONLY` strips it from exactly
    the bands that carry the edge id, and it is stripped from no other.

    `way` is an attribute of no band -- it is the feature id itself -- so a page that
    read one would hover in the wrong id space with nothing to show for it. Both
    halves are asserted, because either one alone leaves the discrimination able to
    move to an attribute that is never written.

    The three readers go through one helper now, and the attribute it tests comes
    out of `map.toml` alongside the `_DETAIL_ONLY` list `publish` strips by. What is
    asserted here is therefore the arrangement rather than the name: that the page
    holds exactly one definition of the test, that all three callers use it, and
    that the name it reads is the one the pipeline strips."""
    from wayfare import palette, publish

    ink = palette.load()
    assert ink.detail_sentinel in publish._DETAIL_ONLY
    assert _holds(
        "index.html",
        "const fromDetailBand = (props) => props[PALETTE.detailSentinel] !== undefined;",
    )
    assert _holds("index.html", "const detailed = fromDetailBand(p);")
    assert _holds(
        "index.html",
        "fromDetailBand(f.properties) ? `w${f.id}` : `${f.source}:${f.id}`",
    )
    # Merging two archives over one road unions their service lists, and the same
    # sentinel says whether there is anything to union.
    assert _holds("index.html", "if (!fromDetailBand(top)) return top;")
    # One definition, three callers: a second test written out inline is the copy
    # that survives the next change to `_DETAIL_ONLY`.
    assert _source("index.html").count("PALETTE.detailSentinel") == 1
    # The way id reaching the card comes from the feature id, never from a property.
    assert _holds("index.html", "card.feature(mergeRegions(hits), f.id)")
    for page in PAGES:
        text = _source(page)
        assert not re.search(r'\[\s*"get"\s*,\s*"way"\s*\]', text), page
        assert not re.search(r"\.properties\.way\b", text), page


def test_the_archive_head_is_prefetched_under_the_key_that_reads_it():
    """The prefetch in <head> and the lookup in `openArchive` have to agree on the
    URL, because the map is keyed on it. They cannot share a helper -- one runs
    before the page's own script exists -- so the expression is written twice, and
    a divergence is silent in the worst way: every lookup misses, every archive
    reads the network exactly as it did before, and the only evidence is a
    prefetch nobody uses. So each site is found and the expression read out of it,
    rather than counted over the file, where a third caller would be a failure with
    nothing wrong.

    16 KB because that is what `getHeaderAndRoot` reads for itself. Larger was
    measured and lost: over a 60 KB/s pipe a 32 KB head cost more than the round
    trip it saved."""
    text = _source("index.html")
    prefetch = _between(text, "window.__heads = ", "</script>")
    opener = _between(text, "async function openArchive", "\n}\n")
    built = _tight("const url = new URL(name, location.href).href;")
    assert built in _tight(prefetch)
    assert built in _tight(opener)
    assert re.search(r'Range:\s*"bytes=0-16383"', prefetch)
    assert _holds(
        "index.html",
        "new pmtiles.PMTiles(new HeadSource(url, heads.get(url) || null), DIR_CACHE)",
    )


def test_the_head_source_answers_only_what_it_wholly_holds():
    """A range straddling the end of the buffer must go to the network entire. Half
    an answer stitched to a second request is the round trip this exists to remove,
    and a slice past the end of an ArrayBuffer is short rather than an error -- so
    getting this wrong hands pmtiles.js a truncated directory, which it reads as a
    corrupt archive rather than as a bug here."""
    assert _holds("index.html", "offset + length <= head.buf.byteLength")
    # And a null head is "ask the network", so a host that refuses ranges or
    # answers 404 to the prefetch behaves exactly as it did before it existed.
    assert _holds("index.html", "const head = await this.head;")
    assert _holds("index.html", "heads.get(url) || null")


def test_the_backdrop_is_served_from_this_origin():
    """The backdrop used to be raster tiles off a public CDN, which now paints "API
    KEY REQUIRED" across a keyless tile. Nothing about that failure was visible to
    this repository: the URL kept working, the tiles kept arriving, and only the
    pixels changed.

    So what is asserted is the property that made it possible -- that a page draws
    its backdrop from somewhere this deployment does not control. No page, and
    nothing in `util.js`, may name a tile host or a raster tile template."""
    sources = [_source(page) for page in PAGES] + [
        (WEB / name).read_text() for name in ("util.js", "credits.js")
    ]
    for text in sources:
        assert "{z}/{x}/{y}" not in text
        assert "cartocdn" not in text
        assert "://tile" not in text
    # The archive is named once, in map.toml, and reaches the pages through
    # PALETTE. A literal here would be a second name to rename.
    util = (WEB / "util.js").read_text()
    assert "PALETTE.basemapArchive" in util
    assert re.search(r"const\s+basemapUrl\s*=", util)


def test_the_style_names_a_glyph_endpoint():
    """A vector style resolves every `text-field` through `glyphs`, and a style with
    symbol layers and no glyph endpoint draws the whole map correctly with nothing
    named on it -- no error, no warning, and a country of unlabelled roads.

    The template is checked rather than the files, because MapLibre substitutes
    `{fontstack}` and `{range}` itself and a wrong template is the failure that
    reaches production."""
    util = (WEB / "util.js").read_text()
    assert "{fontstack}/{range}.pbf" in util
    assert "vendor/fonts/" in util
    for page in PAGES:
        assert "glyphs: BASEMAP_GLYPHS" in _source(page), page


def test_the_vendored_glyphs_cover_the_stacks_the_style_asks_for():
    """A missing range is not an error either: MapLibre asks for
    `vendor/fonts/<stack>/<range>.pbf`, takes a 404 as an empty range, and draws
    the labels in it as nothing. The stacks come out of the style, so a style
    version that adds a fourth font is what this catches."""
    style = (WEB / "vendor" / "basemap-style.js").read_text()
    stacks = set(re.findall(r'"text-font":\["([^"]+)"\]', style))
    assert stacks, "no text-font in the vendored style"
    for stack in stacks:
        directory = WEB / "vendor" / "fonts" / stack
        assert directory.is_dir(), stack
        # The Latin block alone would leave a Welsh circumflex blank.
        for start in ("0-255", "256-511", "768-1023", "7680-7935"):
            assert (directory / f"{start}.pbf").is_file(), (stack, start)


def test_the_vendored_style_needs_no_sprite_sheet():
    """A sprite is a second asset to host and version, for a direction arrow, a
    motorway shield around a number the road label already carries, and the dot
    beside a town name. `scripts/basemap_style.py` drops the two layers that are
    only an icon and strips `icon-*` off the one that is not, so the town names
    still draw.

    MapLibre resolves `icon-image` through `sprite`, and a style with neither
    draws nothing for those layers and logs nothing either -- which is why the
    absence is asserted rather than left to be noticed."""
    style = (WEB / "vendor" / "basemap-style.js").read_text()
    assert "icon-" not in style
    for page in PAGES:
        assert "sprite:" not in _source(page), page


def test_every_painted_layer_is_a_layer_the_style_declares():
    """`basemapLayers` merges paint onto structure by id and `repaintBasemap` walks
    the same object at a theme change, so the two halves of the vendored style have
    to name the same layers. A paint entry for an id that does not exist is a colour
    that silently never applies, and `map.setPaintProperty` on a missing layer
    throws."""
    style = (WEB / "vendor" / "basemap-style.js").read_text()
    ids = set(re.findall(r'\{"id":"([^"]+)"', style))
    assert ids, "no layers in the vendored style"
    flavours = re.findall(
        r'^  "(light|dark)": \{$(.*?)^  \},$', style, re.MULTILINE | re.DOTALL
    )
    assert {f for f, _ in flavours} == {"light", "dark"}
    for flavour, block in flavours:
        painted = set(re.findall(r'^    "([^"]+)":', block, re.MULTILINE))
        assert painted <= ids, (flavour, painted - ids)
    # And both flavours colour the same layers, which is what makes a theme change a
    # repaint rather than a rebuild: a layer painted in one and not the other would
    # keep the previous flavour's colour after a toggle.
    assert set(re.findall(r'^    "([^"]+)":', flavours[0][1], re.MULTILINE)) == set(
        re.findall(r'^    "([^"]+)":', flavours[1][1], re.MULTILINE)
    )


def test_the_backdrop_is_not_opened_as_a_region():
    """It holds the Protomaps schema rather than wayfare's, so a page opening it as
    a region draws a `bus` layer against tiles that have no such thing, takes the
    extract's bounds as the country's, and puts "Basemap" in the strapline.

    `server.archives` keeps it out of the index it writes. Both pages filter it out
    of whatever index they are handed, because a deployment can have its manifest
    written by something else -- the one serving this in production globs a
    directory."""
    for page in PAGES:
        assert "PALETTE.basemapArchive" in _source(page), page
        assert re.search(r"filter\(\s*\(?name\)?\s*=>\s*!name\.endsWith", _source(page)), (
            page
        )


# --- The pages on a small screen ----------------------------------------------
#
# Four rules a phone enforces and a desktop browser never will, so nothing else
# here would report any of them. Each one was a page that drew and could not be
# used, rather than a page that failed.


def test_both_pages_measure_the_viewport_that_is_actually_showing():
    """A phone's layout viewport is the *large* viewport -- the one with the
    browser's toolbars hidden -- so a height of `100%` is taller than the screen
    for as long as a toolbar is on it. What that hides is the bottom of the page,
    which on the viewer is the credit and on the studio is the status line. The
    `%` declaration stays as the fallback for a browser that does not know the
    unit, so both spellings have to be there and in that order -- a fallback
    written second overrides the thing it is a fallback for."""
    for page in PAGES:
        percent = _declared(page, "height", "100%")
        dynamic = _declared(page, "height", "100dvh")
        assert percent >= 0 and dynamic >= 0, page
        assert percent < dynamic, page
    # The studio's stacked layout scrolls, and a scroll is what slides the toolbar
    # away -- `dvh` there would resize the stage mid-scroll, which `refit` reads as
    # a new preview width and answers with a whole new render. `svh` is the one
    # that does not move.
    for older, newer in (("100%", "100svh"), ("52vh", "52svh")):
        fallback = _declared("art.html", "min-height", older)
        small = _declared("art.html", "min-height", newer)
        assert fallback >= 0 and small >= 0, newer
        assert fallback < small, newer


def test_every_safe_area_inset_carries_a_fallback():
    """`env(safe-area-inset-left)` is undefined on a browser that does not do
    notches, and an undefined `env()` with no fallback makes the whole declaration
    invalid rather than zero. So `right: calc(10px + env(...))` becomes `right:
    auto`, and an absolutely positioned panel with no right edge is one that sizes
    itself to its content and runs off the side of the screen. Which is what it
    did.

    A close paren straight after the inset name is the fault, wherever it is
    written, so the whole page is searched rather than each line: a declaration
    wrapped over two lines is the same bug."""
    bare = re.compile(r"env\(\s*safe-area-inset-(?:top|right|bottom|left)\s*\)")
    for page in PAGES:
        text = _source(page)
        assert "safe-area-inset-" in text, page  # not vacuous
        assert not bare.search(text), page


def test_the_viewer_answers_a_tap_and_gives_it_room_to_land():
    """A touchscreen sends no `mousemove`, so the map's one interaction -- ask a
    line what runs on it -- was unreachable on a phone: every road drawn, and no
    way to select any of them. A tap arrives as a click, and it has to be met with
    a box rather than a point, because a road is two pixels wide and a fingertip
    covers forty. A mouse click keeps the point: a slop box under an arrow answers
    for the road beside the one being pointed at."""
    assert _holds("index.html", "if (point) selectAt(point, 0);")
    assert _holds("index.html", 'map.on("click", (e) => {')
    assert _holds("index.html", 'e.originalEvent.pointerType === "mouse"')
    assert _holds("index.html", "selectAt(e.point, mouse ? 0 : SLOP)")
    # The box is built from the slop, and a slop of zero stays the point itself.
    assert _holds("index.html", "const box = slop")


def test_the_cursor_asks_once_a_frame_and_never_mid_drag():
    """`mousemove` arrives at the pointer's rate rather than the map's, and each one
    ran up to three `queryRenderedFeatures` whose hits carry the road's whole
    coordinate array. A pointer has one position per frame, so the rest were built
    and discarded.

    The drag guard and the `moveend` flush are asserted together, because either
    one alone is a bug rather than half a fix: without the guard the hot path runs
    over a viewport whose tiles the pan is still re-parsing, and without the flush
    the highlight is left on whatever the pointer was over before the pan moved the
    map out from under it."""
    assert _holds("index.html", "frame = requestAnimationFrame(ask);")
    assert _holds("index.html", "if (e.originalEvent.buttons || frame) return;")
    assert _holds("index.html", 'map.on("moveend", () => {')


def test_every_vendored_url_carries_the_version_the_readme_names():
    """`wayfare serve` sends `web/vendor/*` as `immutable` for a year, and what makes
    that safe is the query: the URL a page asks for changes when the bytes behind it
    do. Bump a library without bumping the pages and every returning visitor holds
    the old one until the year is out, with nothing to see and no way to tell.

    The versions are read out of `web/vendor/README.md`, which is the table the
    update instructions there tell you to edit, so this fails on either half of the
    bump being forgotten rather than on a number written twice."""
    table = (WEB / "vendor" / "README.md").read_text()
    versions = dict(re.findall(r"\|\s*\[`([\w.-]+)`\][^|]*\|\s*([\d.]+)\s*\|", table))
    assert set(versions) == {
        "maplibre-gl.js",
        "maplibre-gl.css",
        "pmtiles.js",
        "basemap-style.js",
        "fonts",
    }
    for page in PAGES:
        text = _source(page)
        asked = dict(re.findall(r'"vendor/([\w.-]+)\?v=([\d.]+)"', text))
        assert asked, page
        for name, version in asked.items():
            assert versions[name] == version, (page, name)
        # And no unversioned one alongside them, which would be the copy that goes
        # on being served out of a year-old cache.
        assert not re.search(r'"vendor/[\w.-]+"', text), page
    # The glyph template is the one vendored URL written in `util.js` rather than in
    # a page, because MapLibre builds the request from it rather than the page. It
    # is served `immutable` like the rest, so it needs the query like the rest.
    util = (WEB / "util.js").read_text()
    glyphs = re.search(r'"vendor/(fonts)/\{fontstack\}/\{range\}\.pbf\?v=([\d.]+)"', util)
    assert glyphs, "no versioned glyph template in util.js"
    assert versions[glyphs.group(1)] == glyphs.group(2)


def test_neither_page_lets_the_map_stylesheet_block_the_theme():
    """A classic script cannot run until every stylesheet above it has loaded, so
    65 KB of MapLibre CSS in `<head>` gated the `bootTheme()` call whose whole
    purpose is running before the first paint -- and the first paint with it. Every
    `.maplibregl-*` selector in that file matches nothing until a map is built.

    Both halves are asserted per page. The `media="print"` alone leaves the sheet
    never applying, and the flip alone leaves it blocking. Its position is asserted
    too: it stays above each page's own `<style>`, because the flip does not move it
    in the cascade and the `.maplibregl-ctrl-*` overrides below win on document
    order rather than on specificity."""
    for page in PAGES:
        text = _source(page)
        assert _holds(page, 'id="mapcss" rel="stylesheet"'), page
        assert re.search(r'href="vendor/maplibre-gl\.css\?v=[\d.]+" media="print"', text), (
            page
        )
        assert _holds(page, 'document.getElementById("mapcss").media = "all";'), page
        # The opening tag at the start of its own line, because the prose above the
        # link names `<style>` and would otherwise be what this found.
        own = re.search(r"^<style>$", text, re.MULTILINE)
        assert own, page
        assert text.index('id="mapcss"') < own.start(), page
        assert own.start() < text.index('getElementById("mapcss")'), page


def test_the_map_is_constructed_with_the_settings_a_weak_device_needs():
    """Every one of these is a MapLibre default that was never chosen. They are
    asserted here because a default is invisible: nothing reports that the map is
    rasterising nine times the fragments it needs, holding four uncapped tile
    caches, or re-fetching tiles it already has.

    `maxPitch: 0` and `disableRotation` are asserted together. Bearing is not pitch
    and the pitch cap does not reach it, so with only one of the two a pinch on a
    phone still carries a rotation nobody asked for."""
    text = _source("index.html")
    assert _holds(
        "index.html", "pixelRatio: weakDevice() ? Math.min(devicePixelRatio, 1.5)"
    )
    assert _holds("index.html", "maxTileCacheSize: 15,")
    assert _holds("index.html", "maxPitch: 0,")
    assert _holds("index.html", "map.touchZoomRotate.disableRotation();")
    for off in ("dragRotate", "touchPitch", "refreshExpiredTiles", "renderWorldCopies"):
        assert re.search(rf"{off}:\s*false", text), off


def test_the_legend_scan_waits_for_a_tile_to_arrive():
    """Both `noteRendered` calls are a `queryRenderedFeatures` over the whole
    viewport with no bounding box, and every hit is materialised as a LineString
    carrying its full geometry so that one property can be read off it. `seenModes`
    and `seenTrackModes` only ever grow, so once they have stopped growing the whole
    allocation is discarded -- and `idle` fires on every camera settle for the life
    of the page.

    A mode can only be seen for the first time in a tile drawn for the first time,
    so the gate is a tile having loaded. It has to start true, or the opening view
    is scanned only if a tile happens to finish after the first idle."""
    assert _holds("index.html", "let tileArrived = true;")
    assert _holds(
        "index.html", 'if (e.dataType === "source" && e.tile) tileArrived = true;'
    )
    assert _holds("index.html", "if (!tileArrived) return;")


def test_both_pages_give_a_coarse_pointer_a_field_it_will_not_zoom_into():
    """Safari zooms the page in on a focused input drawn under 16px, and what it
    zooms to is the panel -- leaving a map, or a column of knobs, that has to be
    pinched back out of before anything else can be read. Both pages set their
    fields to 16px for a finger and leave the mouse's 13px alone."""
    opens = re.compile(r"@media\s*\(\s*pointer\s*:\s*coarse\s*\)\s*\{")
    for page in PAGES:
        text = _source(page)
        at = opens.search(text)
        assert at is not None, page
        block = text[at.end() :].split("\n}", 1)[0]
        assert re.search(r"font-size\s*:\s*16px\s*;", block), page


def test_the_viewer_folds_its_key_away_where_there_is_no_room_for_it():
    """The key is a row of colour per mode, which on a national archive is eight of
    them and most of a phone. Folded by width *and* by height, because a laptop
    turned landscape has the same problem. The toggles are wired in `chrome`, which
    runs before the archives are asked for: a page waiting on a slow archive should
    still open its own help, and one that never gets a usable archive at all is
    where the help is most wanted."""
    text = _source("index.html")
    tight = _tight(text)
    assert tight.index("chrome();") < tight.index("boot().catch(")
    assert re.search(r'fold\(\s*"keyBtn"\s*,\s*"legend"', text)
    # The query itself is a string the browser parses, so it is matched as written.
    assert 'matchMedia("(max-width: 720px), (max-height: 600px)")' in text
    # The panel's own `hidden` is the state, so the button cannot come to disagree
    # with what is on screen.
    assert _holds(
        "index.html", '$(btn).addEventListener("click", () => set($(panel).hidden));'
    )


# --- What the pages do when they cannot start ---------------------------------
#
# Both open on an overlay that the boot path is meant to take down: the viewer's
# "Loading tiles...", the studio's "Loading...". Nothing awaits either boot, so a
# throw anywhere inside one is an unhandled rejection, and an unhandled rejection
# there is a page that keeps saying it is loading. Neither failure is visible to a
# test that only reads what the page draws when it works.


def test_the_viewer_reports_a_boot_it_could_not_finish():
    """`boot()` is called and not awaited, so every throw inside it has to be caught
    at the call or it goes nowhere: `failed` never runs and the overlay reads
    "Loading tiles..." for good, with one line in the console under it.

    `TRIED` is what the handler names, and it is set before anything is awaited, so
    a page that fell over ahead of the index still reports the archive it would have
    read rather than an empty list."""
    text = _source("index.html")
    assert _holds("index.html", "boot().catch((err) => {")
    handler = _between(text, "boot().catch(", "\n});")
    assert "failed(TRIED)" in _tight(handler)
    assert re.search(r"let\s+TRIED\s*=", text)
    # And the reporting path takes the same list the opening path took, so the two
    # cannot come to name different archives.
    assert _holds("index.html", "REGIONS = (await Promise.all(TRIED.map(openArchive)))")
    assert _holds("index.html", "failed(TRIED);")


def test_the_viewer_guards_the_whole_of_opening_an_archive():
    """`?tiles=` is whatever was typed at the page, and `new URL` throws on a name it
    cannot resolve. Outside the try that was not one archive failing to open -- it
    rejected the `Promise.all` over all of them, which is the hang above.

    So the URL is built inside the guard, and what the console names is the name as
    given: the URL is what could not be built."""
    opener = _between(_source("index.html"), "async function openArchive", "\n}\n")
    tight = _tight(opener)
    assert tight.index("try{") < tight.index("consturl=newURL(name,location.href).href;")
    assert _tight("console.error(`could not read ${name}`, err)") in tight
    assert _tight("return null;") in tight


def test_the_studio_reports_a_boot_it_could_not_finish():
    """Only the `/art/meta` fetch is inside the studio's own try. A server that
    answers 200 with a body missing `limits`, `styles` or `presets` throws out of
    `defaults()` or `build()` instead, past the guard, and the stage stays on
    "Loading..." with nothing said.

    It routes to `offline`, which is the page's one way of saying why nothing will be
    drawn, and it carries the error rather than a fixed sentence -- a studio that
    only says "something went wrong" is the console line again."""
    text = _source("art.html")
    assert _holds("art.html", "boot().catch((err) => {")
    handler = _between(text, "boot().catch(", "\n});")
    assert "offline(" in handler
    assert "escapeHtml(" in handler and "err" in handler
    assert re.search(r"function\s+offline\s*\(", text)


def test_the_studio_picker_draws_every_archive_the_server_lists():
    """The picker exists to frame a window against the network it will contain. Opening
    `archives[0]` alone drew one region's network under a window framed over another,
    on exactly the multi-region server where the framing matters most.

    Each archive is a source of its own, because PMTiles is one archive to one tile
    pyramid and there is no union source -- which is also what keeps the credits
    right: no source states an `attribution`, each carries its own in its own
    metadata, and MapLibre merges what every source reports into the one control. A
    source given a credit here would be the page overriding the archive's."""
    text = _source("art.html")
    network = _between(text, "async function addNetwork", "\n}\n")
    tight = _tight(network)
    assert "archives[0]" not in tight, "the picker still opens only the first archive"
    assert _tight("wanted.map(async (name) => {") in tight
    assert _tight("const id = `bus-${i}`;") in tight
    assert _tight("map.addSource(id, {") in tight
    # No credit of the page's own on a source that brings one.
    assert "attribution:" not in tight
    # One archive that will not open costs itself and not the picker.
    assert _tight(").filter(Boolean);") in tight
    # A theme change has to reach every layer drawn, not the one named `bus`.
    assert _holds("art.html", "for (const id of pickLayers) {")
    assert '"bus"' not in _tight(_between(text, "function repaintPicker", "\n}\n"))


def test_the_studio_revokes_a_render_it_never_got_to_show():
    """`show` took ownership of an object URL inside `img.onload`. A second `show`
    overwrites `img.onload` and `img.src`, so when the second response arrives
    before the first has decoded the first URL is never revoked and never again
    reachable -- it holds its blob and its decoded bitmap for the life of the page.

    That is the ordinary path rather than a corner: a preview is drawn at
    `sample=8` and then at `sample=1`, and the overtaking happens exactly on the
    slow device this matters on. `onerror` is asserted with it, because a blob that
    will not decode is the other way out of `onload` and leaks the same way."""
    text = _source("art.html")
    assert _holds("art.html", "if (pendingURL) URL.revokeObjectURL(pendingURL);")
    assert _holds("art.html", "pendingURL = next;")
    assert _holds("art.html", "img.onerror = () => {")
    # The revoke of the pending URL comes before the <img> is pointed anywhere else,
    # because after that assignment nothing can reach it.
    assert text.index("URL.revokeObjectURL(pendingURL)") < text.index("img.src = next;")


def test_the_studio_builds_the_picker_only_when_it_is_opened():
    """`initPicker` ran from `boot()` unconditionally and only decided at the end
    whether anyone wanted a picker. A reader who closed it last visit still paid for
    a second WebGL context, the raster basemap, `archives.json`, a `getHeader()` per
    archive and the whole road network drawn into a box roughly 272 by 200 pixels --
    held for the session, and never on screen.

    The order is what this pins. `buildPicker` has to run with the container already
    visible, because a MapLibre map constructed under `display: none` comes up zero
    by zero -- so the build sits after the line that unhides the wrapper, not before
    it."""
    text = _source("art.html")
    build = _between(text, "function showPicker(open) {", "\n}\n")
    assert "else buildPicker();" in build
    assert build.index('$("pickwrap").hidden = !open;') < build.index("buildPicker()")
    # And nothing constructs a map before that.
    init = _between(text, "function initPicker()", "\n}\n")
    assert "new maplibregl.Map" not in init
    assert "showPicker(" in init


def test_the_studio_does_not_resize_a_picker_nobody_is_looking_at():
    """Android fires `resize` whenever the address bar slides, and resizing a hidden
    container reallocates the drawing buffer to nothing and back. `showPicker` does
    the resize on the way open, which is the moment it is needed."""
    assert _holds("art.html", 'if (!$("pickwrap").hidden) quietly(() => pickMap.resize());')


def test_the_studio_debounces_every_side_effect_of_a_knob_moving():
    """`scheduleRender` was the only debounced part of `changed`, so `refit`,
    `writeHash` and `exports` ran per `input` event -- a 300-pixel slider drag being
    about 300 calls to `history.replaceState`, which Safari throttles past roughly a
    hundred in thirty seconds.

    The count is not the worst of it: `exports` writes DOM and the next event's
    `refit` reads `getComputedStyle` and `clientWidth`, so every event forced a
    synchronous style and layout flush of a document holding selects of up to 500
    options. The commit delay is asserted to be under the render's, because the
    width `refit` settles on has to be the width the render then asks for."""
    text = _source("art.html")
    assert _holds("art.html", "const scheduleCommit = debounce(commit, COMMIT_MS);")
    commit_ms = int(re.search(r"const COMMIT_MS = (\d+);", text).group(1))
    debounce_ms = int(re.search(r"const DEBOUNCE_MS = (\d+);", text).group(1))
    assert commit_ms < debounce_ms
    # A render overtaking a pending commit runs it rather than leaving it to land
    # after the picture it was part of.
    assert _holds("art.html", "scheduleCommit.cancel();")
    changed = _between(text, "function changed() {", "\n}\n")
    for direct in ("refit()", "writeHash()", "exports()"):
        assert direct not in changed, direct


def test_the_studio_previews_as_a_raster_whatever_it_will_export():
    """`exports` caps a download against `max_pixels` and nothing caps the preview,
    so choosing SVG handed the browser a full-detail vector of a national window --
    hundreds of thousands of paths to parse and rasterise into a few hundred pixels
    of frame. Both formats come off the same spec through the same renderer.

    The status line has to say so, because the size and the time it reports are then
    a PNG's and they are what somebody sizing an export reads."""
    request = _between(_source("art.html"), "for (const sample of stages)", "const res =")
    assert 'format: "png"' in _tight(request).replace("format:", "format: ")
    assert "S.format" not in request
    assert _holds("art.html", 'S.format === "png" ? "" : " · previewed as PNG"')


def test_the_studio_does_not_refit_the_picker_onto_the_window_it_is_already_on():
    """`apply` calls `showOnMap` on every discrete change -- a style, a weight, a
    group, a format -- and none of those moves the window, while `fitBounds` is a
    full repaint and a tile-coverage check whether or not the camera has anywhere to
    go."""
    assert _holds("art.html", "if (where === shownWindow) return;")
