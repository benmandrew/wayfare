#!/usr/bin/env python3
"""Write `web/vendor/basemap-style.js` from the Protomaps basemap style package.

    python scripts/basemap_style.py             # regenerate the vendored style
    python scripts/basemap_style.py --version 5.7.2

Run by hand when the style version in `web/vendor/README.md` moves, and never by
CI: it fetches over the network, and CI has to pass on a machine that cannot
reach one.

The backdrop is a vector basemap now, which means the page carries the
cartography rather than the tile server. `@protomaps/basemaps` publishes that
cartography as an array of MapLibre layers per flavour, and this turns the two
flavours the page uses into one script it can load beside the library.

Three transformations happen on the way, and each of them is a fact about the
archive this is drawn against rather than a preference. `web/README.md` says
what that archive holds and who builds it:

  * The archive carries no `buildings`, `pois` or `landuse`, so the layers
    drawing them are dropped. MapLibre would ignore a layer naming a source-layer
    that is not in the tile, so this is for the reader of the file rather than
    for the renderer -- 17 layers that could never draw anything.
  * `roads_oneway` and `roads_shields` are dropped, and the `icon-*` keys come
    off `places_locality`. All three want a sprite sheet, which is the one
    remaining third-party asset this exercise was about removing. What is lost is
    a direction arrow, a motorway shield around a number the road label already
    carries, and the dot beside a town name. The name itself still draws.
  * `source` is rewritten from `protomaps` to `basemap`, which is what the two
    pages have always called the backdrop's source.
  * The dark flavour's colours are replaced with CARTO Dark Matter's, the raster
    backdrop this one replaced and the ground the network on top was drawn
    against. Protomaps paints a `#1f1f1f` earth under a `#34373d` background with
    landcover tinted green, where Dark Matter puts background, landcover, land use
    and parks alike at `#0e0e0e`. Ground is most of any frame, so that is most of
    the difference in brightness. The light flavour is untouched.

Paint is split from structure because the two flavours differ in nothing else --
asserted below, not assumed. Their layer lists are identical and so is every
filter, layout and zoom range, so storing both in full would be 43 KB of
duplicated structure to carry a 6.5 KB colour change. The split is also the shape
the theme toggle wants: `wireTheme` walks `BASEMAP_PAINT[theme]` and reassigns,
exactly as it already does for wayfare's own layers.
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GENERATED = ROOT / "web" / "vendor" / "basemap-style.js"

# The generator `maps.protomaps.com` itself calls. Undocumented, so it is read
# once into a committed file rather than at runtime -- the documented route is
# the package's own `generate_style` CLI, which would put npm on the path of
# anyone regenerating this.
ENDPOINT = "https://npm-style.protomaps.dev/layers.json"

# What the archive holds. Anything else is a layer that cannot draw.
ARCHIVE_LAYERS = frozenset({"earth", "landcover", "water", "roads", "places", "boundaries"})

# Sprite-only decoration, dropped whole. `roads_shields` also carries a
# `text-field`, and that is the reason it goes rather than losing its icon: a
# shield number floating unboxed over a road reads as a defect.
SPRITE_LAYERS = frozenset({"roads_oneway", "roads_shields"})

SOURCE = "basemap"
UPSTREAM_SOURCE = "protomaps"

# CARTO Dark Matter's palette, mapped onto the Protomaps layer names. Values are
# read from `mapboxgl/dark-matter.json` in CartoDB/basemap-styles, which is the
# vector style the raster tiles were rendered from.
#
# Only the dark flavour takes it, and only the properties named here: the merge is
# per property, so a line keeps the zoom expression that sets its width.
#
# Labels keep Protomaps' greys. Those are dimmer than Dark Matter's, and dropping
# the ground from `#1f1f1f` to `#0e0e0e` raises their contrast rather than lowering
# it, so the only label property below is the halo -- which is a ground colour
# wherever it appears, and would otherwise ring every name in a lighter patch.
DARK_MATTER: dict[str, dict[str, object]] = {
    # Ground. Dark Matter draws landcover, land use and parks in the background
    # colour, so nothing but water and roads breaks the black.
    "background": {"background-color": "#0e0e0e"},
    "earth": {"fill-color": "#0e0e0e"},
    "landcover": {"fill-color": "#0e0e0e"},
    # Water. The fill is Dark Matter's `water`, the lines its `waterway`, which is
    # the brighter of the two.
    "water": {"fill-color": "#2c353c"},
    "water_stream": {"line-color": "#3f5a6d"},
    "water_river": {"line-color": "#3f5a6d"},
    "roads_runway": {"line-color": "#111111"},
    "roads_taxiway": {"line-color": "#111111"},
    # Surface roads. `#414758` is the slate Dark Matter gives everything from a
    # minor road up to a trunk road, `#494949` the grey it reserves for a
    # motorway, and `#0b0b0b` what it leaves a service road.
    "roads_highway": {"line-color": "#494949"},
    "roads_major": {"line-color": "#414758"},
    "roads_link": {"line-color": "#414758"},
    "roads_minor": {"line-color": "#414758"},
    "roads_minor_service": {"line-color": "#0b0b0b"},
    "roads_other": {"line-color": "#262626"},
    "roads_pier": {"line-color": "#1c1c1c"},
    # Casings depart from Dark Matter at the minor road, which it casings in the
    # same `#414758` as the fill. Its casing is drawn under the fill and this one
    # is drawn outside it through `line-gap-width`, so matching the colour would
    # widen every minor road in the country rather than outline it.
    "roads_highway_casing_early": {"line-color": "#232323"},
    "roads_highway_casing_late": {"line-color": "#232323"},
    "roads_major_casing_early": {"line-color": "#232323"},
    "roads_major_casing_late": {"line-color": "#232323"},
    "roads_link_casing": {"line-color": "#232323"},
    "roads_minor_casing": {"line-color": "#1a1a1a"},
    "roads_minor_service_casing": {"line-color": "#1c1c1c"},
    # Bridges take their surface colour and tunnels a dimmer one. Dark Matter
    # splits these by road class in a way that does not map -- its `tunnel_pri`
    # is `#414758` against a `#161616` `tunnel_trunk` -- so the rule is stated
    # here rather than copied.
    "roads_tunnels_highway": {"line-color": "#414758"},
    "roads_tunnels_major": {"line-color": "#161616"},
    "roads_tunnels_link": {"line-color": "#161616"},
    "roads_tunnels_minor": {"line-color": "#161616"},
    "roads_tunnels_other": {"line-color": "#262626"},
    "roads_tunnels_highway_casing": {"line-color": "#232323"},
    "roads_tunnels_major_casing": {"line-color": "#232323"},
    "roads_tunnels_link_casing": {"line-color": "#1a1a1a"},
    "roads_tunnels_minor_casing": {"line-color": "#1a1a1a"},
    "roads_tunnels_other_casing": {"line-color": "#1a1a1a"},
    "roads_bridges_highway": {"line-color": "#494949"},
    "roads_bridges_major": {"line-color": "#414758"},
    "roads_bridges_link": {"line-color": "#414758"},
    "roads_bridges_minor": {"line-color": "#414758"},
    "roads_bridges_other": {"line-color": "#262626"},
    "roads_bridges_highway_casing": {"line-color": "#232323"},
    "roads_bridges_major_casing": {"line-color": "#232323"},
    "roads_bridges_link_casing": {"line-color": "#232323"},
    "roads_bridges_minor_casing": {"line-color": "#1a1a1a"},
    "roads_bridges_other_casing": {"line-color": "#1a1a1a"},
    "roads_rail": {"line-color": "#1a1a1a"},
    "boundaries_country": {"line-color": "#606060"},
    "boundaries": {"line-color": "#2c353c"},
    "roads_labels_minor": {"text-halo-color": "#0e0e0e"},
    "roads_labels_major": {"text-halo-color": "#0e0e0e"},
    "earth_label_islands": {"text-halo-color": "#0e0e0e"},
    "places_subplace": {"text-halo-color": "#0e0e0e"},
    "places_region": {"text-halo-color": "#0e0e0e"},
    "places_locality": {"text-halo-color": "#0e0e0e"},
    "places_country": {"text-halo-color": "#0e0e0e"},
    "water_waterway_label": {"text-halo-color": "#2c353c"},
    "water_label_ocean": {"text-halo-color": "#2c353c"},
    "water_label_lakes": {"text-halo-color": "#2c353c"},
}


FLAVOURS = ("light", "dark")

BANNER = "Generated by scripts/basemap_style.py. Do not edit."


def fetch(version: str, flavour: str) -> list[dict[str, object]]:
    url = f"{ENDPOINT}?version={version}&theme={flavour}&lang=en"
    # `lang` is not optional: without it the package emits no label layers at all,
    # and the failure is a map that renders perfectly with nothing named on it.
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310
        layers = json.load(response)
    if not isinstance(layers, list) or not layers:
        raise SystemExit(f"{url} returned no layers")
    return layers


def wanted(layer: dict[str, object]) -> bool:
    if layer["id"] in SPRITE_LAYERS:
        return False
    source_layer = layer.get("source-layer")
    # The background layer names no source and is what paints the ground.
    return source_layer is None or source_layer in ARCHIVE_LAYERS


def strip_icons(layer: dict[str, object]) -> dict[str, object]:
    layout = layer.get("layout")
    if not isinstance(layout, dict):
        return layer
    kept = {k: v for k, v in layout.items() if not k.startswith("icon-")}
    if kept == layout:
        return layer
    return (
        {**layer, "layout": kept}
        if kept
        else {k: v for k, v in layer.items() if k != "layout"}
    )


def structure(layer: dict[str, object]) -> dict[str, object]:
    out = {k: v for k, v in layer.items() if k != "paint"}
    if out.get("source") == UPSTREAM_SOURCE:
        out["source"] = SOURCE
    return out


def recolour(
    layers: list[dict[str, object]], overrides: dict[str, dict[str, object]]
) -> list[dict[str, object]]:
    """Merge `overrides` onto each named layer's paint, property by property.

    Every guard here catches a change that would otherwise be silent. A layer id
    the style no longer has is an override that applies to nothing. A layer with no
    paint of its own would gain one, and the two flavours have to colour the same
    layer list for `repaintBasemap` to be a repaint rather than a rebuild. A
    property the layer does not already set is a colour written into a key that
    draws nothing, which is what a renamed paint property looks like from here.
    """
    by_id = {layer["id"]: layer for layer in layers}
    missing = sorted(set(overrides) - set(by_id))
    if missing:
        raise SystemExit(f"overrides name layers the style does not have: {missing}")

    out = []
    for layer in layers:
        override = overrides.get(str(layer["id"]))
        if not override:
            out.append(layer)
            continue
        paint = layer.get("paint")
        if not isinstance(paint, dict) or not paint:
            raise SystemExit(f"{layer['id']} has no paint to override")
        unknown = sorted(set(override) - set(paint))
        if unknown:
            raise SystemExit(f"{layer['id']} does not set {unknown}")
        out.append({**layer, "paint": {**paint, **override}})
    return out


def render(version: str, layers: list[dict[str, object]], paint: dict[str, dict]) -> str:
    lines = [
        '"use strict";',
        "",
        f"// {BANNER}",
        "//",
        f"// @protomaps/basemaps {version}, flavours light and dark, lang=en.",
        "// See web/vendor/README.md for the version table and how to update this.",
        "//",
        "// A plain script rather than a module or a fetched document, matching the rest",
        "// of web/vendor/: both pages are classic scripts served off disk, and a style",
        "// arriving a round trip after the page would build the map without a backdrop",
        "// and then rebuild it.",
        "const BASEMAP_LAYERS = [",
    ]
    lines += [f"  {json.dumps(layer, separators=(',', ':'))}," for layer in layers]
    lines += [
        "];",
        "",
        "// Every colour, by flavour and then by layer id. The structure above carries",
        "// none, so a theme change is this object and nothing else.",
        "const BASEMAP_PAINT = {",
    ]
    for flavour in FLAVOURS:
        lines.append(f'  "{flavour}": {{')
        for layer_id, props in paint[flavour].items():
            lines.append(f'    "{layer_id}": {json.dumps(props, separators=(",", ":"))},')
        lines.append("  },")
    lines += ["};", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="5.7.2", help="@protomaps/basemaps version")
    args = parser.parse_args()

    flavours = {f: fetch(args.version, f) for f in FLAVOURS}

    ids = {f: [layer["id"] for layer in layers] for f, layers in flavours.items()}
    if len({tuple(v) for v in ids.values()}) != 1:
        raise SystemExit("flavours do not share a layer list; the split below is invalid")

    kept = {
        f: [strip_icons(layer) for layer in layers if wanted(layer)]
        for f, layers in flavours.items()
    }

    kept["dark"] = recolour(kept["dark"], DARK_MATTER)

    shapes = {f: [structure(layer) for layer in layers] for f, layers in kept.items()}
    if len({json.dumps(v, sort_keys=True) for v in shapes.values()}) != 1:
        raise SystemExit("flavours differ outside paint; the split below would lose that")

    paint = {
        f: {
            layer["id"]: layer["paint"]
            for layer in layers
            if isinstance(layer.get("paint"), dict) and layer["paint"]
        }
        for f, layers in kept.items()
    }

    GENERATED.write_text(render(args.version, shapes[FLAVOURS[0]], paint))
    kept_count = len(shapes[FLAVOURS[0]])
    dropped = len(flavours[FLAVOURS[0]]) - kept_count
    print(
        f"{GENERATED.relative_to(ROOT)}: {kept_count} layers, {dropped} dropped, "
        f"{len(DARK_MATTER)} recoloured in dark"
    )


if __name__ == "__main__":
    main()
