#!/usr/bin/env python3
"""Measure line coverage of `wayfare` and draw the badge the README embeds.

    python scripts/coverage_badge.py            # run the suite, write docs/coverage.svg
    python scripts/coverage_badge.py --check    # fail if the committed badge is stale
    python scripts/coverage_badge.py --json r.json   # reuse a report already measured

The badge is a file in the repository rather than a call out to a badge service, so
the README renders the same on a fork with no secrets, in an offline clone and in a
Docker build context. That only works if the file is regenerated when the number
moves, which is what `--check` is for.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BADGE = ROOT / "docs" / "coverage.svg"

LABEL = "coverage"

# Thresholds and colours are the shields.io flat palette, the same steps
# github.com/benmandrew/tiber uses, so the two repositories' badges read alike.
COLOURS: tuple[tuple[float, str], ...] = (
    (95.0, "#44cc11"),
    (85.0, "#97ca00"),
    (75.0, "#a4a61d"),
    (65.0, "#dfb317"),
    (50.0, "#fe7d37"),
    (0.0, "#e05d44"),
)

# Advance widths of Verdana at 11px, in hundredths of a pixel, for the alphabet a
# badge can contain. Baked in rather than measured at run time: `--check` compares
# bytes, and measuring through the local font stack would have the badge depend on
# which fonts the machine happens to have, so two developers would generate two
# different files from one coverage run. Verdana because it is what the SVG asks
# for first, and what shields.io sizes its own badges with.
_ADVANCE: dict[str, int] = {
    " ": 387, "%": 1184, ".": 400, "0": 699, "1": 699, "2": 699, "3": 699, "4": 699,
    "5": 699, "6": 699, "7": 699, "8": 699, "9": 699, "a": 661, "b": 685, "c": 573,
    "d": 685, "e": 655, "f": 387, "g": 685, "h": 696, "i": 302, "j": 379, "k": 651,
    "l": 302, "m": 1070, "n": 696, "o": 668, "p": 685, "q": 685, "r": 469, "s": 573,
    "t": 433, "u": 696, "v": 651, "w": 900, "x": 651, "y": 651, "z": 578,
}  # fmt: skip

# 5px of clear space each side of every string, which is the shields.io flat metric.
PADDING = 10
HEIGHT = 20


def text_width(s: str) -> int:
    """Width of `s` in whole pixels at 11px Verdana."""
    missing = sorted(set(s) - set(_ADVANCE))
    if missing:
        raise ValueError(
            f"no baked advance width for {missing}; extend _ADVANCE by measuring the "
            "character in Verdana at 11px"
        )
    return round(sum(_ADVANCE[c] for c in s) / 100)


def shown_percent(percent: float) -> int:
    """The whole number the badge prints.

    Rounding is held below 100 until coverage actually reaches it, because a badge
    reading 100% is a claim about the suite that 99.6% does not support.
    """
    shown = round(percent)
    return 99 if shown == 100 and percent < 100.0 else shown


def colour_for(shown: int) -> str:
    """The band colour, taken from the printed number rather than the measured one.

    Reading the true value here instead puts 49.9% on the badge as a red `50%`, so
    the two halves disagree about which side of a threshold the run landed on.
    """
    return next(colour for floor, colour in COLOURS if shown >= floor)


def render(percent: float) -> str:
    shown = shown_percent(percent)
    status = f"{shown}%"
    label_w = text_width(LABEL) + PADDING
    status_w = text_width(status) + PADDING
    total = label_w + status_w
    label_x = label_w / 2
    status_x = label_w + status_w / 2
    return f"""\
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
     width="{total}" height="{HEIGHT}" role="img" aria-label="{LABEL}: {status}">
  <title>{LABEL}: {status}</title>

  <linearGradient id="smooth" x2="0" y2="100%">
    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>
    <stop offset="1" stop-opacity=".1"/>
  </linearGradient>

  <mask id="round">
    <rect width="{total}" height="{HEIGHT}" rx="3" fill="#fff"/>
  </mask>

  <g mask="url(#round)">
    <rect width="{label_w}" height="{HEIGHT}" fill="#555"/>
    <rect x="{label_w}" width="{status_w}" height="{HEIGHT}" fill="{colour_for(shown)}"/>
    <rect width="{total}" height="{HEIGHT}" fill="url(#smooth)"/>
  </g>

  <g fill="#fff" text-anchor="middle"
     font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">
    <text x="{label_x:g}" y="15" fill="#010101" fill-opacity=".3">{LABEL}</text>
    <text x="{label_x:g}" y="14">{LABEL}</text>
    <text x="{status_x:g}" y="15" fill="#010101" fill-opacity=".3">{status}</text>
    <text x="{status_x:g}" y="14">{status}</text>
  </g>
</svg>
"""


def measure(pytest_args: list[str]) -> float:
    """Run the suite under coverage and return the percentage of lines covered."""
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "coverage.json"
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--cov=wayfare",
            f"--cov-report=json:{report}",
            "--cov-report=term-missing:skip-covered",
            *pytest_args,
        ]
        # A failing suite means the number is measured against code that does not
        # work, so refuse rather than stamp it onto the badge.
        # S603: `cmd` is this file's own pytest invocation, a fixed argv, no shell.
        if subprocess.run(cmd, cwd=ROOT, check=False).returncode != 0:  # noqa: S603
            raise SystemExit("the test suite failed; badge not written")
        return read_report(report)


def read_report(path: Path) -> float:
    # CI hands `--json` the report the test step wrote, and runs this step even when
    # that step failed, so that a formatting slip does not hide a broken test. A
    # collection error leaves no report at all, and the bare traceback from that reads
    # as a fault in the badge rather than in the run that was supposed to produce it.
    if not path.exists():
        raise SystemExit(f"{path} does not exist; the run that writes it did not finish")
    totals = json.loads(path.read_text())["totals"]
    percent: float = totals["percent_covered"]
    return percent


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the committed badge does not match a fresh measurement",
    )
    ap.add_argument(
        "--json",
        type=Path,
        help="read an existing coverage JSON report instead of running the suite",
    )
    ap.add_argument(
        "pytest_args",
        nargs="*",
        help="extra arguments forwarded to pytest, e.g. -n auto",
    )
    args = ap.parse_args()

    percent = read_report(args.json) if args.json else measure(args.pytest_args)
    svg = render(percent)

    if args.check:
        current = BADGE.read_text() if BADGE.exists() else ""
        if current != svg:
            raise SystemExit(
                f"{BADGE.relative_to(ROOT)} is stale: coverage is {percent:.1f}%. "
                "Run `python scripts/coverage_badge.py` and commit the result."
            )
        print(f"{BADGE.relative_to(ROOT)} is current at {percent:.1f}%")
        return

    BADGE.parent.mkdir(parents=True, exist_ok=True)
    BADGE.write_text(svg)
    print(f"coverage {percent:.1f}% -> {BADGE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
