"use strict";

// The one definition of the basemap credit, because two pages draw the same
// backdrop and the string was copied between them character for character.
//
// The *data* credit is deliberately not here. `wayfare publish` stamps it into
// each PMTiles archive with tippecanoe's --attribution, and MapLibre reads a
// source's own attribution into the control on its own -- so the credit follows
// the archive to whatever host it is copied to, and a page showing two regions
// shows the right one for each. A page-level constant would be a fourth copy and
// would say Great Britain over an Irish map.
//
// A plain script rather than a module: both pages are classic scripts served off
// disk, and a module would make this the only thing on the page that needs an
// origin.
const BASEMAP_CREDIT =
  '<a href="https://carto.com/attributions">CARTO</a> · ' +
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

// Fold the credit down to its (i), which `compact: true` alone does not do.
//
// MapLibre's own _updateCompact adds `maplibregl-compact-show` alongside the
// compact class the first time it runs against a non-empty credit, so the panel
// loads *open* and folds away only on the first drag -- and a map nobody drags
// never gets there. Removing the one class is the library's own collapse, the
// body of the _updateCompactMinimize it binds to `drag`; the `open` attribute is
// deliberately left alone, because the container is a <details> whose button
// lives in the summary and stays open whichever way the panel is folded.
//
// On `idle` rather than straight after the control is added: until a source
// reports a credit the container is `maplibregl-attrib-empty`, which
// _updateCompact skips, and it then adds both classes when that source's
// metadata lands. Idle is the point every source has reported in, so nothing
// arrives afterwards to re-open what this closed. The user is not fought for it
// -- the panel starts open, so the only click available before idle is the one
// that agrees with us.
function collapseCredit(map) {
  map.once("idle", () => {
    for (const el of map.getContainer().querySelectorAll(".maplibregl-ctrl-attrib")) {
      el.classList.remove("maplibregl-compact-show");
    }
  });
}
