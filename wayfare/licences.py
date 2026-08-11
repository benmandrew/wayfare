"""The licences this project's sources are published under, and how to say so.

Every source here carries an obligation, and they are not the same obligation. Some
of what is drawn is Open Government Licence, some Creative Commons Attribution, and
the road geometry underneath the matched routes is OpenStreetMap's under the Open
Database License. The list only grows -- Transport for London publishes under an
amended OGL v2.0 with three attribution strings of its own, and Network Rail's feeds
are not open at all -- so the names and their URIs live here rather than scattered
through the module that also holds paths and tunables.

This module knows nothing about regions or feeds. It is the vocabulary and the
rendering; `config.credit_parts` is what decides which credits a given region owes,
because that needs the `Feed` and `Feed` needs the licence names from here. The
dependency runs one way, and it has to: `config` imports this, never the reverse.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- The licences ------------------------------------------------------------

OGL = "Open Government Licence v3.0"
CC_BY_4 = "Creative Commons Attribution 4.0"
# Not a spelling mistake and not to be tidied to the British form the rest of this
# codebase uses: "Open Database License" is the licence's own name.
ODBL = "Open Database License"

# CC BY 4.0 requires the licence to be *identified*, which in practice means a name
# and a URI; OGL and ODbL ask for the same. A table keyed on the licence rather than
# a field on `Feed`, so two publishers under one licence cannot disagree about where
# it is, and a credit raises on a licence with no entry rather than quietly omitting
# it.
URLS = {
    OGL: "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/",
    CC_BY_4: "https://creativecommons.org/licenses/by/4.0/",
    ODBL: "https://opendatacommons.org/licenses/odbl/",
}

OSM_COPYRIGHT = "https://www.openstreetmap.org/copyright"


# --- What a credit is ---------------------------------------------------------


@dataclass(frozen=True)
class Credit:
    """One thing that has to be acknowledged: what it is, whose it is, its licence."""

    what: str
    who: str
    licence: str
    # Where the work itself lives, where the publisher gives one. The licence's own
    # URI is looked up from `URLS` and is not optional.
    who_url: str | None = None


# The one credit that is not a property of any feed. Every map-matched edge is an
# OpenStreetMap way, so an archive holding them is a derived database whatever the
# timetable's licence says. Named here because it is the same for every region.
#
# Only the noun varies, and it has to: since `wayfare trace` an archive may hold OSM
# geometry that is track rather than road -- the Underground drawn from route
# relations -- and an archive holding only that would credit "Road geometry" for a
# tube tunnel. Which noun applies is `config.credit_parts`'s to decide, because only
# it knows what was built.
def openstreetmap(what: str = "Road geometry") -> Credit:
    return Credit(what, "OpenStreetMap contributors", ODBL, OSM_COPYRIGHT)


OPENSTREETMAP = openstreetmap()


# --- Rendering ----------------------------------------------------------------


def html(parts: tuple[Credit, ...]) -> str:
    """The credit as a map attribution control wants it.

    `publish` stamps this into the tileset metadata, which is the one place a licence
    condition travels with the data: an archive copied to a bucket takes its credit
    with it, where a line in the viewer or a field in `/archives.json` would be left
    behind.
    """
    return " &middot; ".join(
        f"{c.what}: &copy; {_link(c.who, c.who_url)}, {_link(c.licence, URLS[c.licence])}"
        for c in parts
    )


def lines(parts: tuple[Credit, ...], *, links: bool = True) -> tuple[str, ...]:
    """The credit as plain text, one line per thing being credited.

    `links=False` drops the URIs. That is for the one place they cost more than they
    carry: a credit burned into the corner of a picture, where a URI is unclickable,
    doubles the length of a line that has to fit across the canvas, and is spelled
    out in full in the same file's metadata anyway. Everywhere else keeps them,
    because identifying the licence is what the licence asks for.
    """
    return tuple(
        f"{c.what}: \N{COPYRIGHT SIGN} {c.who}"
        + (f" <{c.who_url}>" if c.who_url and links else "")
        + f", {c.licence}"
        + (f" <{URLS[c.licence]}>." if links else ".")
        for c in parts
    )


def text(parts: tuple[Credit, ...]) -> str:
    """The same credit with the links spelled out, for anywhere HTML is not read.

    A PNG `tEXt` chunk, an SVG `<metadata>` block, a log line. The copyright sign is
    deliberate and safe in all three: it is in Latin-1, which is what `tEXt` allows.
    """
    return " ".join(lines(parts))


def _link(text: str, url: str | None) -> str:
    return f'<a href="{url}">{text}</a>' if url else text
