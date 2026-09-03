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
//
// The mask over the margin is not among them and cannot be: it is a custom
// layer, and MapLibre takes those through `addLayer` alone. `addRoamMask` is
// what puts it above these 55 and below whatever a page adds of its own.
function basemapLayers(t) {
  return BASEMAP_LAYERS.map((layer) => {
    const paint = BASEMAP_PAINT[t][layer.id];
    return paint ? { ...layer, paint } : layer;
  });
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
  // The mask draws none of the paint above -- it is a custom layer holding one
  // colour of its own -- so it is repainted here rather than found by the loop.
  repaintRoamMask(map, t);
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
// It has to be the widening the *widest* view needs and not the one the current
// zoom needs, which is the whole reason the pan is held separately below. A box
// sized to the zoom the camera is at is a box the camera can never leave: the
// viewport already fills it, so the next zoom out is refused, and one straight
// jump to a country-wide view -- which is what `fitBounds` does on load -- lands
// a level and a half short with no error anywhere.
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

// That widening is slack at every zoom but the widest, and slack is a corridor of
// masked margin to pan into -- 9.5 degrees of it on a 1400 pixel window, at z14 as
// much as at z5. maxBounds cannot take it back for the reason above, so the pan is
// held here instead, against the window's own width at the zoom being drawn.
//
// Above the zoom where the window is narrower than the box -- z6.5 on that same
// window -- nothing outside the box can be reached at all. Below it the centre is
// pinned to the middle of the box, which is the least margin that view can show:
// the country has to fit, and what is left over is split evenly either side.
//
// Written onto the transform rather than through `setCenter`, and that is the
// whole difference between a wall and a jolt. `setCenter` stops whatever the
// camera is doing, so it could not be used while a zoom was easing, which left the
// correction to land at the end of the gesture instead -- a wait and then a shunt
// sideways, with nothing on the way to say what was happening. Writing the centre
// where MapLibre's own maxBounds writes it corrects the frame that is about to be
// drawn and stops nothing, so a drag holds against the edge and a zoom out slides
// straight to where it belongs.
//
// `map.transform` is the one piece of MapLibre's insides either page reads. It is
// what maxBounds is implemented against, and the version it belongs to is pinned
// in `vendor/`.
const WORLD_TILE_PX = 512; // MapLibre's `Transform.tileSize`, not the archive's

function holdInRoamBox(map, el) {
  const [w, , e] = ISLES;
  const hold = () => {
    const tr = map.transform;
    const view = (360 * el.clientWidth) / (WORLD_TILE_PX * 2 ** tr.zoom);
    const half = view / 2;
    const { lng, lat } = tr.center;
    const held = view >= e - w ? (w + e) / 2 : clamp(lng, w + half, e - half);
    // A pixel of longitude, and not equality. The centre goes through maxBounds on
    // the way in and comes back a rounding apart from what it was handed, so the
    // difference never reaches zero and a test for zero never stops asking.
    if (Math.abs(held - lng) > view / el.clientWidth) {
      tr.center = new maplibregl.LngLat(held, lat);
    }
  };
  // A resize changes the aspect, and with it how wide the box has to be for the
  // country to still fit top to bottom.
  const box = () => map.setMaxBounds(roamingBounds(el));
  map.on("move", hold);
  map.on("resize", () => {
    box();
    hold();
  });
  box();
  hold();
}


/* ---------- the ground beyond the box ---------- */

// `pmtiles extract --bbox` keeps every tile that intersects the box and does not
// clip what is inside one, so the backdrop reaches past the box by however wide a
// tile is at that zoom. Measured against this box: 19.90 degrees of longitude and
// 5.21 of latitude at z4, 8.65 and 0.31 at z5, 3.02 at z6, and 0.21 from z7 down.
// So France, Denmark and southern Norway drew at a country-wide view and then
// left a step at a time on the way in, which reads as tiles failing rather than
// as an edge.
//
// The page paints over the margin instead, and the backdrop then ends in the same
// place at every zoom. What it paints is the flavour's own `background` colour,
// which is already what MapLibre draws wherever no tile reaches, so the margin
// reads as the end of the map. It was painted the water colour, on the argument
// that the sea inside the box and the sea outside it had to agree -- and they
// did, which was the fault: a reader cannot tell a sea that is empty from a sea
// nobody drew France into.
//
// The cut is a straight line and the eastern edge of the box runs inland through
// the Pas-de-Calais rather than out at sea. That is what a rectangle costs, and
// it is spent on the one corner of the map that carries no services.
const ROAM_MASK = "roam-mask";

// What MapLibre paints where a tile does not reach, per flavour, read out of the
// vendored paint rather than written down a second time.
const roamMaskColour = (t) => BASEMAP_PAINT[t].background["background-color"];

// `#rrggbb` to the four floats a uniform wants. Both flavours give `background`
// a plain hex and `test_the_mask_can_read_a_colour_out_of_both_flavours` is what
// holds them to it, because a colour this could not parse would paint the margin
// transparent and put the overhang back with nothing to say so.
function glColour(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255, 1];
}

// The box in the space a custom layer's matrix is in: x and y each run 0 to 1
// over the whole world, y downwards from the north.
function roamMercBox() {
  const [w, s, e, n] = ISLES;
  const x = (lon) => (lon + 180) / 360;
  const y = (lat) => 0.5 - mercY(lat) / (2 * Math.PI);
  return { left: x(w), right: x(e), top: y(n), bottom: y(s) };
}

// The world with the box taken out of it, as four rectangles: the two sides run
// the full height, and the strips above and below cover what is left between
// them. Two triangles each, so 24 vertices and no index buffer.
function roamMaskVertices() {
  const { left, right, top, bottom } = roamMercBox();
  const quad = (x0, y0, x1, y1) =>
    [x0, y0, x1, y0, x0, y1, x1, y0, x1, y1, x0, y1];
  return new Float32Array([
    ...quad(0, 0, left, 1),
    ...quad(right, 0, 1, 1),
    ...quad(left, 0, right, top),
    ...quad(left, bottom, right, 1),
  ]);
}

// A custom layer rather than a fill over a GeoJSON source, which is what this
// was and what made it flash. A GeoJSON source is tiled like every other source
// and its tiles are built in a worker, so a fast zoom asks for a set that is not
// built yet -- while the backdrop's tiles for the same view are often already in
// hand. For those frames the overhang draws with nothing over it, which is a
// reader watching France arrive for two frames and leave again.
//
// A custom layer holds no tiles and asks no worker for anything. It is handed the
// same matrix as every other layer and draws in the same frame, so there is no
// state in which the backdrop is on screen and this is not.
function roamMaskLayer(t) {
  let program = null;
  let buffer = null;
  let uMatrix = null;
  let uColour = null;
  let aPos = 0;
  let colour = glColour(roamMaskColour(t));

  const compile = (gl, type, source) => {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    return shader;
  };

  return {
    id: ROAM_MASK,
    type: "custom",
    // Flat colour on a north-up map: nothing here wants a depth buffer, and
    // "3d" would have MapLibre keep one for it.
    renderingMode: "2d",

    setColour(hex) {
      colour = glColour(hex);
    },

    onAdd(_map, gl) {
      program = gl.createProgram();
      gl.attachShader(
        program,
        compile(
          gl,
          gl.VERTEX_SHADER,
          `uniform mat4 u_matrix;
           attribute vec2 a_pos;
           void main() { gl_Position = u_matrix * vec4(a_pos, 0.0, 1.0); }`,
        ),
      );
      gl.attachShader(
        program,
        compile(
          gl,
          gl.FRAGMENT_SHADER,
          `precision mediump float;
           uniform vec4 u_colour;
           void main() { gl_FragColor = u_colour; }`,
        ),
      );
      gl.linkProgram(program);
      aPos = gl.getAttribLocation(program, "a_pos");
      uMatrix = gl.getUniformLocation(program, "u_matrix");
      uColour = gl.getUniformLocation(program, "u_colour");
      buffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(gl.ARRAY_BUFFER, roamMaskVertices(), gl.STATIC_DRAW);
    },

    onRemove(_map, gl) {
      gl.deleteProgram(program);
      gl.deleteBuffer(buffer);
      program = buffer = null;
    },

    render(gl, matrix) {
      if (!program) return;
      gl.useProgram(program);
      gl.uniformMatrix4fv(uMatrix, false, matrix);
      gl.uniform4fv(uColour, colour);
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.enableVertexAttribArray(aPos);
      gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);
      gl.drawArrays(gl.TRIANGLES, 0, 24);
    },
  };
}

// One mask per map, held against the map rather than in a module constant,
// because `art.html` builds a second map on the same script.
const roamMasks = new WeakMap();

// On top of everything, wayfare's own layers included. The box is where this map
// claims to draw, and the claim has to hold for the network as well as for the
// backdrop: a rail relation runs on to Lille and a ferry to Amsterdam, and drawn
// past the edge they are two lines over a ground nobody mapped.
//
// On `style.load` rather than on `load`, which is the whole of what a reader saw
// as the overhang flashing up on the way in: `load` waits for the first complete
// frame, so the backdrop had already been drawn -- overhang and all -- by the
// time the mask went on. `style.load` is before any tile has been asked for.
function addRoamMask(map) {
  const layer = roamMaskLayer(theme);
  roamMasks.set(map, layer);
  map.addLayer(layer);
}

// Whether a point is somewhere this map draws. `roamingBounds` widens the box a
// landscape window is framed against and this does not: the widening is exactly
// the part the mask paints over.
//
// A hover needs it because `queryRenderedFeatures` reads geometry and not pixels,
// so a line the mask has covered is still under the cursor -- and a card naming a
// service over blank ground is worse than no card at all.
function insideRoamBox({ lng, lat }) {
  const [w, s, e, n] = ISLES;
  return lng >= w && lng <= e && lat >= s && lat <= n;
}

function repaintRoamMask(map, t) {
  const layer = roamMasks.get(map);
  if (!layer) return;
  layer.setColour(roamMaskColour(t));
  map.triggerRepaint();
}
