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
