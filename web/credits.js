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

// Fold the credit down to its (i) before it is ever drawn open, which
// `compact: true` alone does not do.
//
// MapLibre's _updateCompact adds `maplibregl-compact-show` *alongside* the
// compact class, and only on the run where it first adds compact -- a run it
// skips while the container is still `maplibregl-attrib-empty`, so the panel
// opens itself when the first source's metadata lands. Adding the compact class
// here, straight after the control is added, is the guard that run tests first:
// finding it already present, it never adds the show class at all, and the
// button then owns the panel from the first frame. Waiting for `idle` and
// removing the show class also folds it, but only after a visible flash of the
// open panel.
//
// The container is a <details> and `open` is set to match: MapLibre's own folded
// state is open-without-compact-show, because the button lives in the summary
// and the CSS, not the element, hides the credit body.
function collapseCredit(map) {
  for (const el of map.getContainer().querySelectorAll(".maplibregl-ctrl-attrib")) {
    el.classList.add("maplibregl-compact");
    el.setAttribute("open", "");
  }
}
