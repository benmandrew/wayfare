"""The map's colours, read from `map.toml` and derived once.

Two consumers draw the same network in the same colours and neither can see the
other: the viewer, which paints tiles through MapLibre, and `coverage.draw`,
which rasterises an archive to PNG. A third, `scripts/palette.py`, writes the
first one's copy out as JavaScript. All three read this module, so the OKLab
derivation below exists once rather than once per language.

The derivation used to live in `web/index.html` and ran on every repaint. It runs
here now, at generation time, and the values it produces are byte-identical to
what that code produced -- `tests/test_palette.py` pins all thirty-six of them.
"""

from __future__ import annotations

import itertools
import math
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

MAP_TOML = Path(__file__).with_name("map.toml")

Theme = Literal["light", "dark"]
THEMES: tuple[Theme, ...] = ("light", "dark")

# The road ramp and every mode ramp are the same length, because the legend draws
# one bar for all of them and the ticks under it have to line up.
RAMP_STEPS = 6

# Track is drawn flat at the middle of its mode's ramp, and six steps have no
# middle one -- steps 2 and 3 straddle it. Three steps put one exactly on the
# midpoint, and the same generator produces both.
_MID_STEPS = 3

# The key a mode with no entry of its own falls back to. It carries a ramp and a
# label like any other mode, and is kept out of `named_modes` because the paint
# expressions enumerate the real modes and use this one as their default branch:
# listing it twice would make it match itself before reaching the default.
FALLBACK_MODE = "other"


# --- OKLab -----------------------------------------------------------------
#
# Bradford-free sRGB <-> OKLab, the coefficients Björn Ottosson published. Kept
# in full rather than rounded: the ramps are compared byte-for-byte against what
# the viewer used to compute, and a trimmed coefficient moves a channel by one.


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def hex_to_oklch(colour: str) -> tuple[float, float, float]:
    """A `#rrggbb` string as lightness, chroma and hue."""
    n = int(colour[1:], 16)
    r = _srgb_to_linear(((n >> 16) & 255) / 255)
    g = _srgb_to_linear(((n >> 8) & 255) / 255)
    b = _srgb_to_linear((n & 255) / 255)
    long = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    med = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    short = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    a = 1.9779984951 * long - 2.4285922050 * med + 0.4505937099 * short
    b2 = 0.0259040371 * long + 0.7827717662 * med - 0.8086757660 * short
    lightness = 0.2104542553 * long + 0.7936177850 * med - 0.0040720468 * short
    return lightness, math.hypot(a, b2), math.atan2(b2, a)


def _oklch_to_rgb(lightness: float, chroma: float, hue: float) -> tuple[float, ...]:
    a = chroma * math.cos(hue)
    b = chroma * math.sin(hue)
    long = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    med = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    short = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (
        _linear_to_srgb(4.0767416621 * long - 3.3077115913 * med + 0.2309699292 * short),
        _linear_to_srgb(-1.2684380046 * long + 2.6097574011 * med - 0.3413193965 * short),
        _linear_to_srgb(-0.0041960863 * long - 0.7034186147 * med + 1.7076147010 * short),
    )


def _in_gamut(lightness: float, chroma: float, hue: float) -> bool:
    return all(-0.0005 <= v <= 1.0005 for v in _oklch_to_rgb(lightness, chroma, hue))


def fit_chroma(lightness: float, chroma: float, hue: float) -> float:
    """The most chroma sRGB holds at this lightness and hue.

    Reducing lightness while keeping chroma asks for colours the gamut has not
    got, and clamping the channels then answers with a differently *hued* colour
    that is in it -- which is the one failure the mode ramps exist to avoid,
    arrived at from the other side. So find the chroma that exists rather than
    clamp one that does not. Sixteen bisections, which is the JavaScript this
    replaced and is what makes the output identical to it.
    """
    if _in_gamut(lightness, chroma, hue):
        return chroma
    lo, hi = 0.0, chroma
    for _ in range(16):
        mid = (lo + hi) / 2
        if _in_gamut(lightness, mid, hue):
            lo = mid
        else:
            hi = mid
    return lo


def hex_to_rgb(colour: str) -> tuple[int, int, int]:
    """A `#rrggbb` string as three 0-255 channels."""
    n = int(colour[1:], 16)
    return (n >> 16) & 255, (n >> 8) & 255, n & 255


def oklch_to_hex(lightness: float, chroma: float, hue: float) -> str:
    channels = _oklch_to_rgb(lightness, chroma, hue)
    return "#" + "".join(f"{round(min(1.0, max(0.0, v)) * 255):02x}" for v in channels)


# --- The file ---------------------------------------------------------------


@dataclass(frozen=True)
class Low:
    """Where a mode ramp starts, relative to the mode's own colour."""

    mul: float
    add: float
    cap: float
    chroma: float

    def lightness(self, top: float) -> float:
        return min(self.cap, top * self.mul + self.add)


@dataclass(frozen=True)
class Palette:
    """Everything in `map.toml`, with the mode ramps already derived."""

    layers: dict[str, str]
    road_ramp: dict[Theme, list[str]]
    ramp_at: list[int]
    trips_per: int
    ramp_needs: str
    mode_labels: dict[str, str]
    mode_seeds: dict[Theme, dict[str, str]]
    mode_ramps: dict[Theme, dict[str, list[str]]]
    mode_mids: dict[Theme, dict[str, str]]
    accents: dict[str, str]
    track_default_mode: str
    default_archive: str
    detail_only: tuple[str, ...]
    detail_sentinel: str
    roam: tuple[float, float, float, float]

    @property
    def modes(self) -> tuple[str, ...]:
        """Every mode key in legend order, the fallback last as the file writes it."""
        return tuple(self.mode_labels)

    def road_rgb(self, theme: Theme, trips: float | None) -> tuple[int, int, int]:
        """A road's colour, off the road ramp, from its weekly trip count."""
        if trips is None:
            return hex_to_rgb(self.unavailable(theme))
        return self._along(self.road_ramp[theme], trips)

    def mode_rgb(
        self, theme: Theme, mode: str | None, trips: float | None
    ) -> tuple[int, int, int]:
        """A non-road feature's colour, off its own mode's ramp.

        Flat at the middle of the ramp where there is no trip count, which is what
        the viewer draws and for the reason `map.toml` gives: a way of track carries
        a service *count*, and that is not the journeys a day every other colour on
        the map means.
        """
        key = mode if mode in self.mode_labels else FALLBACK_MODE
        if trips is None:
            return hex_to_rgb(self.mode_mids[theme][key])
        return self._along(self.mode_ramps[theme][key], trips)

    def position(self, trips: float | None) -> float:
        """How far along a ramp weekly `trips` lands, from 0 to 1.

        Log10 of journeys a day against the same stops the viewer interpolates
        over, clamped at both ends as MapLibre's `interpolate` clamps: journeys a
        day span three orders of magnitude, and drawn straight everything but a
        handful of city corridors sits in the first colour.
        """
        if trips is None:
            return 0.0
        stops = [math.log10(v) for v in self.ramp_at]
        at = math.log10(max(trips / self.trips_per, 0.01))
        span = stops[-1] - stops[0]
        return min(1.0, max(0.0, (at - stops[0]) / span))

    def _along(self, ramp: list[str], trips: float) -> tuple[int, int, int]:
        """Where weekly `trips` lands on a six-step ramp, interpolated in sRGB.

        sRGB because that is the space MapLibre mixes two ramp stops in, and a
        render that mixed them in OKLab would not match the map it is a picture
        of. Piecewise between the stops rather than evenly along the ramp,
        because the half-decade sequence is not evenly spaced in log10: the gaps
        alternate 0.523 and 0.477, so spreading six colours evenly puts every
        one of them slightly off the number the legend says it means.
        """
        stops = [math.log10(v) for v in self.ramp_at]
        at = math.log10(max(trips / self.trips_per, 0.01))
        if at <= stops[0]:
            return hex_to_rgb(ramp[0])
        if at >= stops[-1]:
            return hex_to_rgb(ramp[-1])
        i = next(k for k in range(len(stops) - 1) if at < stops[k + 1])
        u = (at - stops[i]) / (stops[i + 1] - stops[i])
        lo, hi = hex_to_rgb(ramp[i]), hex_to_rgb(ramp[i + 1])
        return tuple(round(a + (b - a) * u) for a, b in zip(lo, hi, strict=True))  # type: ignore[return-value]

    @property
    def named_modes(self) -> tuple[str, ...]:
        """The modes a feed names, in legend order, without the fallback."""
        return tuple(m for m in self.mode_labels if m != FALLBACK_MODE)

    def hover(self, theme: Theme) -> str:
        return self.accents[f"hover_{theme}"]

    def unavailable(self, theme: Theme) -> str:
        return self.accents[f"unavailable_{theme}"]


def ramp_from(seed: str, low: Low, steps: int = RAMP_STEPS) -> list[str]:
    """A mode's ramp, with the mode's own colour as its top step."""
    top_l, top_c, hue = hex_to_oklch(seed)
    lo_l, lo_c = low.lightness(top_l), top_c * low.chroma
    out = []
    for i in range(steps):
        u = i / (steps - 1)
        step_l = lo_l + (top_l - lo_l) * u
        step_c = lo_c + (top_c - lo_c) * u
        out.append(oklch_to_hex(step_l, fit_chroma(step_l, step_c, hue), hue))
    return out


@lru_cache(maxsize=1)
def load(path: Path = MAP_TOML) -> Palette:
    """Read `map.toml` and derive every ramp in it."""
    raw: dict[str, Any] = tomllib.loads(path.read_text())
    lows = {t: Low(**raw["mode_ramp"][t]) for t in THEMES}
    modes: dict[str, dict[str, str]] = raw["modes"]

    # The sentinel is what the viewer reads to tell a detail-band feature from an
    # overview one, and it can only do that if the overview bands do not carry it.
    # Written as two entries because the viewer needs the one name on its own, and
    # checked here because the two going out of step is the failure with nothing
    # to show for it.
    bands = raw["bands"]
    if bands["sentinel"] not in bands["detail_only"]:
        raise ValueError(
            f"bands.sentinel {bands['sentinel']!r} is not in bands.detail_only "
            f"{bands['detail_only']}, so every band carries it and the viewer "
            "cannot tell the two feature-id spaces apart"
        )

    # The three checks `map.schema.json` cannot make. The schema states the shape
    # and an editor shows it while the file is typed; these are the relations
    # between one value and another, which JSON Schema has no way to say.
    at = raw["road_ramp"]["at"]
    if any(b <= a for a, b in itertools.pairwise(at)):
        raise ValueError(
            f"road_ramp.at {at} is not strictly ascending, so a stop sits at or "
            "below the one before it and the ramp reverses between them"
        )
    track_mode = raw["track"]["default_mode"]
    if track_mode not in modes:
        raise ValueError(
            f"track.default_mode {track_mode!r} is not in [modes], so a track "
            "feature with no `mode` of its own is drawn in nothing"
        )
    if FALLBACK_MODE not in modes:
        raise ValueError(
            f"[modes.{FALLBACK_MODE}] is missing, and it is what a mode this "
            "palette has never heard of is drawn as"
        )
    roam = raw["roam"]

    return Palette(
        layers=raw["layers"],
        road_ramp={t: raw["road_ramp"][t] for t in THEMES},
        ramp_at=raw["road_ramp"]["at"],
        trips_per=raw["road_ramp"]["trips_per"],
        ramp_needs=raw["road_ramp"]["needs"],
        mode_labels={m: v["label"] for m, v in modes.items()},
        mode_seeds={t: {m: v[t] for m, v in modes.items()} for t in THEMES},
        mode_ramps={
            t: {m: ramp_from(v[t], lows[t]) for m, v in modes.items()} for t in THEMES
        },
        mode_mids={
            t: {m: ramp_from(v[t], lows[t], _MID_STEPS)[1] for m, v in modes.items()}
            for t in THEMES
        },
        accents=raw["accents"],
        track_default_mode=raw["track"]["default_mode"],
        default_archive=raw["archive"]["default"],
        detail_only=tuple(bands["detail_only"]),
        detail_sentinel=bands["sentinel"],
        roam=(roam["west"], roam["south"], roam["east"], roam["north"]),
    )
