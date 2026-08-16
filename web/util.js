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

/* ---------- the basemap ---------- */

const BASEMAP = {
  light: "light_all",
  dark:  "dark_all",
};

// What the browser will say about the connection, where it says anything.
// Save-Data is a header the user asked for and is honoured wherever it appears;
// effectiveType is the browser's own estimate. Both are Chromium-only, so this is
// an improvement where it exists and never a requirement -- Safari and Firefox
// take the full basemap, exactly as every browser did before.
function thriftyConnection() {
  const c = navigator.connection;
  if (!c) return false;
  return Boolean(c.saveData) || ["slow-2g", "2g", "3g"].includes(c.effectiveType);
}

// Cold profiling put the basemap at 447,036 bytes over 30 requests, 40.9% of
// everything transferred, and blocking it took a throttled load from 19.4 s to
// 12.1 s. It competes with the archive for the same pipe while being the context
// rather than the subject, so on a connection that says it is slow it is drawn at
// half resolution: `tileSize: 512` against 256-pixel tiles makes MapLibre ask for
// one zoom level lower and scale it up, which is a quarter of the tiles for a
// blurrier backdrop. The roads on top are unaffected -- they are vector.
const basemapTileSize = () => (thriftyConnection() ? 512 : 256);

function basemapTiles(t) {
  // MapLibre has no {s} or {r} placeholder: expand the subdomains here and pick
  // the retina variant up front. A thrifty connection takes the plain tile
  // whatever the screen is: a retina backdrop is the first thing to give up when
  // the network underneath it is still arriving.
  const r = devicePixelRatio > 1.4 && !thriftyConnection() ? "@2x" : "";
  return ["a", "b", "c", "d"].map(
    (s) => `https://${s}.basemaps.cartocdn.com/${BASEMAP[t]}/{z}/{x}/{y}${r}.png`
  );
}

/* ---------- how far a map may roam ---------- */

// West of the Blaskets to east of Lowestoft, Scilly up to Unst, with enough
// margin that coastal places do not sit jammed against the edge. The data is
// British Isles only, so there is nothing to find outside this. `art.ISLES` holds
// the same box for the renderer, in the same minlon,minlat,maxlon,maxlat order.
const ISLES = [-11.5, 49.4, 2.6, 61.3];

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
