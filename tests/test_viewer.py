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

Nothing here imports `wayfare.server`; the pages are static files it happens to
serve. `tests/test_server.py` covers the serving.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
PAGES = ("index.html", "art.html")


@functools.cache
def _source(page: str) -> str:
    return (WEB / page).read_text()


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
    """
    found = re.search(rf"{re.escape(prop)}\s*:\s*{re.escape(value)}\s*;", _source(page))
    return found.start() if found else -1


# --- The pages' own credits --------------------------------------------------
#
# The data credit rides in the archive, so nothing here needs to know it. The
# basemap credit cannot: it belongs to the page, and both pages draw the same
# backdrop, so it lives in credits.js alone. These guard the drift, not the wording.


def test_both_pages_take_the_basemap_credit_from_one_place():
    assert "carto.com/attributions" in (WEB / "credits.js").read_text()
    for page in PAGES:
        text = _source(page)
        assert 'src="credits.js"' in text
        assert "BASEMAP_CREDIT" in text
        # The literal URL belongs in no page: a second spelling of the credit is a
        # spelling that will drift away from BASEMAP_CREDIT.
        assert "carto.com/attributions" not in text


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
    for line, shadow in (("SEG", "SEG_SHADOW"), ("TRACK", "TRACK_SHADOW")):
        assert _holds("index.html", f"for (const id of [...{line}, ...{shadow}])")
        assert _holds("index.html", f"for (const id of {shadow})")
    assert _holds("index.html", "for (const id of [...SEG_SHADOW, ...TRACK_SHADOW])")
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
    assert _holds("index.html", "at(SEG)") and not _holds("index.html", "at(SEG_SHADOW)")


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
    assert _holds("index.html", "map.setFilter(id, off.length ? withoutModes(off) : null)")


def test_the_viewer_tells_the_two_id_spaces_apart_by_refs():
    """The detail band's feature id is the OSM way id and the overview bands' is the
    Valhalla edge id, so a hover has to know which range it is holding before it can
    report anything. `refs` is the sentinel: `_DETAIL_ONLY` strips it from exactly
    the bands that carry the edge id, and it is stripped from no other.

    `way` is an attribute of no band -- it is the feature id itself -- so a page that
    read one would hover in the wrong id space with nothing to show for it. Both
    halves are asserted, because either one alone leaves the discrimination able to
    move to an attribute that is never written."""
    assert _holds("index.html", "const detailed = p.refs !== undefined;")
    assert _holds(
        "index.html",
        "f.properties.refs !== undefined ? `w${f.id}` : `${f.source}:${f.id}`",
    )
    # Merging two archives over one road unions their service lists, and the same
    # sentinel says whether there is anything to union.
    assert _holds("index.html", "if (top.refs === undefined) return top;")
    # The way id reaching the card comes from the feature id, never from a property.
    assert _holds("index.html", "showFeature(mergeRegions(hits), f.id)")
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
        "new pmtiles.PMTiles(new HeadSource(url, heads.get(url) || null))",
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


def test_the_basemap_gives_up_resolution_when_the_link_says_it_is_slow():
    """Cold profiling put the basemap at 40.9% of everything transferred, and 72.7%
    on a retina screen. Both halves of the reduction are gated on the same check --
    the retina variant and the tile size -- because either one alone leaves the
    other paying full price."""
    text = _source("index.html")
    assert re.search(r"function\s+thriftyConnection\s*\(\s*\)", text)
    assert _holds("index.html", "devicePixelRatio > 1.4 && !thriftyConnection()")
    assert _holds("index.html", "thriftyConnection() ? 512 : 256")
    # Save-Data is the user asking; effectiveType is the browser guessing. Both.
    assert "c.saveData" in text
    assert "effectiveType" in text


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
    assert _holds("index.html", 'map.on("mousemove", (e) => selectAt(e.point, 0));')
    assert _holds("index.html", 'map.on("click", (e) => {')
    assert _holds("index.html", 'e.originalEvent.pointerType === "mouse"')
    assert _holds("index.html", "selectAt(e.point, mouse ? 0 : SLOP)")
    # The box is built from the slop, and a slop of zero stays the point itself.
    assert _holds("index.html", "const box = slop")


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
