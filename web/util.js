"use strict";

// What the map and the studio both need, beside credits.js and loaded the same
// way: a plain script, no bundler, no module. Both pages are classic scripts
// served off disk, and a module would make this the only thing on either page
// that needs an origin.
//
// It is linked from <head> rather than beside the other scripts at the end of
// the body, because the theme has to be on the document before the first paint.
// The rest of it is small enough that blocking the parser on it costs nothing
// measurable against the 803 KB vendored library that follows.

const $ = (id) => document.getElementById(id);

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
  );
}

const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);

// Collapse a burst of calls into one. Every caller keeps its own interval,
// because they are timing different things: a keystroke, a slider, a drag of the
// window edge. `cancel` is for the caller that also does the work directly --
// the studio's Refresh button, which must not then be followed by the render it
// superseded.
function debounce(fn, ms) {
  let timer = null;
  const run = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
  run.cancel = () => clearTimeout(timer);
  return run;
}

// A name a machine wrote, drawn as a person would write it: an archive's file
// name in the viewer, a preset or a vocabulary entry in the studio. A ?tiles=
// URL is a name we cannot label -- it may be somebody's bucket -- so the viewer
// only passes the ones it recognises.
const label = (n) =>
  n.replace(/\.pmtiles$/, "").replace(/[-_]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

/* ---------- theme ---------- */

// Both pages read and write the one key, so a dark map and a dark studio are one
// choice rather than two.
const THEME_KEY = "wayfare-theme";

// Module scope and mutable, because it is what every colour on either page is
// built from and both pages read it by this name. `bootTheme` sets it.
let theme = "light";

// Called from an inline <head> script on both pages, which is the whole point of
// it being there: the theme used to be applied by the page's own script, after
// the vendored library had parsed, so a dark-mode reader got a full screen of
// white first. An attribute set in <head> is set before anything is painted.
function bootTheme() {
  theme =
    localStorage.getItem(THEME_KEY) ||
    (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  document.documentElement.dataset.theme = theme;
  return theme;
}

// What the button says is the theme it switches *to*, so it is written from the
// theme rather than toggled alongside it.
const themeLabel = () => (theme === "dark" ? "Light" : "Dark");

function toggleTheme(btn) {
  theme = theme === "dark" ? "light" : "dark";
  localStorage.setItem(THEME_KEY, theme);
  document.documentElement.dataset.theme = theme;
  if (btn) btn.textContent = themeLabel();
  return theme;
}

/* ---------- what the machine will admit to ---------- */

// Whether the device is one of the ones this page has to be careful with. Both
// readings are Chromium-only, so like `thriftyConnection` below this is an
// improvement where it exists and never a requirement -- absent, it reads false
// and every browser gets exactly what it got before.
//
// `deviceMemory` is the half worth trusting: it is reported in whole gigabytes,
// capped at 8, and a desktop reports the cap. `hardwareConcurrency` on its own is
// a poor signal, because a four-core laptop is not a weak device in any sense
// this page cares about -- so it is only read where there is no memory figure at
// all, which is Safari and Firefox, and only at a count low enough that nothing
// current reaches it.
function weakDevice() {
  const mem = navigator.deviceMemory;
  if (mem) return mem <= 4;
  return navigator.hardwareConcurrency ? navigator.hardwareConcurrency <= 4 : false;
}

/* ---------- the basemap ---------- */

// The backdrop is a PMTiles archive on this origin, drawn with cartography
// vendored into `vendor/basemap-style.js`. Serving it here is what makes panning
// the map nobody else's business, and what stops a tile server's terms from
// deciding whether this page has a backdrop at all: the one it used to draw
// began painting "API KEY REQUIRED" diagonally across every keyless tile in
// August 2026, with the requests still answering 200.
//
// Nothing here picks a resolution, and there is nothing to pick. A vector tile
// is drawn at whatever the screen is from the same bytes, so one archive serves
// a retina desktop and a phone on a slow radio alike -- the choice exists only
// for a raster backdrop, where a PNG is drawn at the size it was rasterised at.

// The one thing a vector style needs that a raster one did not. A style with
// symbol layers and no glyph endpoint renders every label as nothing, silently,
// and a map missing only its place names looks like a map of an empty country
// rather than like a fault.
//
// Seven ranges of each of the three Noto stacks the style asks for, from this
// origin like everything else -- `vendor/README.md` says which seven and why not
// all 256. The version is that table's, and it is what makes a year of
// `immutable` safe on a path whose bytes could otherwise change under it.
const BASEMAP_GLYPHS = "vendor/fonts/{fontstack}/{range}.pbf?v=2025.10.31";

// Resolved against the page, so a viewer opened out of a sub-directory or off a
// file:// path asks for the archive beside it. Deliberately not read from
// `archives.json`: that index lists regions, and the backdrop is not one --
// `map.toml` says why the name is reserved rather than discovered.
const basemapUrl = () => new URL(PALETTE.basemapArchive, location.href).href;

// The vendored layers with the flavour's colours put back on them. The two
// flavours differ in nothing but paint, which is why the file stores the
// structure once, and which is also what makes a theme change a repaint.
// The mask goes last, so it covers all 55 and nothing else: both pages push
// their own layers after this, and every one of them belongs above it. A caller
// taking these layers has to declare `roamMaskSource()` beside the basemap
// source, and `tests/test_viewer.py` is what holds the two pages to that.
function basemapLayers(t) {
  const layers = BASEMAP_LAYERS.map((layer) => {
    const paint = BASEMAP_PAINT[t][layer.id];
    return paint ? { ...layer, paint } : layer;
  });
  layers.push(roamMaskLayer(t));
  return layers;
}

// What `getSource("basemap").setTiles()` used to do in one call. A raster source
// carried the flavour in its URL; a vector source carries it in the paint of 55
// layers, so this walks them the way `wireTheme` already walks wayfare's own.
//
// Guarded on `getLayer`, because the studio's picker builds a subset of this
// style and a missing layer there is not an error here.
function repaintBasemap(map, t) {
  for (const [id, paint] of Object.entries(BASEMAP_PAINT[t])) {
    if (!map.getLayer(id)) continue;
    for (const [prop, value] of Object.entries(paint)) {
      map.setPaintProperty(id, prop, value);
    }
  }
  // The mask is not in the vendored paint, taking its colour from the water in
  // it, so it is repainted here rather than found by the loop above.
  if (map.getLayer(ROAM_MASK)) {
    map.setPaintProperty(ROAM_MASK, "fill-color", roamMaskColour(t));
  }
}

/* ---------- how far a map may roam ---------- */

// West of the Blaskets to east of Lowestoft, Scilly up to Unst, with enough
// margin that coastal places do not sit jammed against the edge. The data is
// British Isles only, so there is nothing to find outside this.
//
// Out of `wayfare/map.toml`, which `art.ISLES` also reads. The two used to be
// written out separately in the same minlon,minlat,maxlon,maxlat order, each
// carrying a comment naming the other as its twin.
const ISLES = PALETTE.roam;

const MERC_MAX_LAT = 85.05112878;

function mercY(lat) {
  const phi = (clamp(lat, -MERC_MAX_LAT, MERC_MAX_LAT) * Math.PI) / 180;
  return Math.log(Math.tan(Math.PI / 4 + phi / 2));
}

// maxBounds constrains the whole viewport, not just the centre: MapLibre zooms
// *in* until the visible area fits inside the box, taking whichever of width and
// height binds harder. In Web Mercator the isles are taller than they are wide --
// 0.059 of the world against 0.039 -- so on any landscape window it is the width
// that binds, and full zoom out shows a slice of the middle of the country rather
// than the whole of it.
//
// So widen the box, and only the box, until it is at least as wide as the
// viewport. Height then governs, and zooming out fits the country end to end. A
// portrait phone already has room and gets no widening at all.
//
// The floor of 1 on the width is for the studio's picker, whose container is
// display:none until someone opens it: a zero width makes the aspect zero, which
// is a box no wider than the isles under a frame that is, and the map then
// refuses to zoom out at all.
function roamingBounds(el) {
  const merc = (lat) => 0.5 - mercY(lat) / (2 * Math.PI);
  const [w, s, e, n] = ISLES;
  const tall = merc(s) - merc(n); // north is the smaller y
  const wide = (e - w) / 360;
  const aspect = Math.max(el.clientWidth, 1) / Math.max(el.clientHeight, 1);
  const grow = (Math.max(0, aspect * tall - wide) * 360) / 2;
  return [[w - grow, s], [e + grow, n]];
}

/* ---------- the sea outside the box ---------- */

// `pmtiles extract --bbox` keeps every tile that intersects the box and does not
// clip what is inside one, so the backdrop reaches past the box by however wide a
// tile is at that zoom. Measured against this box: 19.90 degrees of longitude and
// 5.21 of latitude at z4, 8.65 and 0.31 at z5, 3.02 at z6, and 0.21 from z7 down.
// So France, Denmark and southern Norway drew at a country-wide view and then
// left a step at a time on the way in, which reads as tiles failing rather than
// as an edge.
//
// The page draws sea over everything outside the box instead, and the backdrop
// then ends in the same place at every zoom. `roamingBounds` widens the box a
// landscape window is framed against, so there is always a margin to cover, and
// this is what fills it.
//
// The cut is a straight line and the eastern edge of the box runs inland through
// the Pas-de-Calais rather than out at sea. That is what a rectangle costs, and
// it is spent on the one corner of the map that carries no services.
const ROAM_MASK = "roam-mask";

const WORLD_RING = [
  [-180, -MERC_MAX_LAT],
  [180, -MERC_MAX_LAT],
  [180, MERC_MAX_LAT],
  [-180, MERC_MAX_LAT],
  [-180, -MERC_MAX_LAT],
];

// The basemap's own water, read out of the flavour rather than written down a
// second time -- the sea drawn inside the box and the sea drawn outside it have
// to be one colour or the box has a visible edge.
const roamMaskColour = (t) => BASEMAP_PAINT[t].water["fill-color"];

// The world with the roam box cut out of it. Every ring after the first is a
// hole whichever way it winds: MapLibre triangulates with earcut, which reads
// the order rather than the direction.
function roamMaskSource() {
  const [w, s, e, n] = ISLES;
  return {
    type: "geojson",
    data: {
      type: "Feature",
      properties: {},
      geometry: {
        type: "Polygon",
        coordinates: [
          WORLD_RING,
          [[w, s], [e, s], [e, n], [w, n], [w, s]],
        ],
      },
    },
  };
}

function roamMaskLayer(t) {
  return {
    id: ROAM_MASK,
    type: "fill",
    source: ROAM_MASK,
    paint: { "fill-color": roamMaskColour(t) },
  };
}
