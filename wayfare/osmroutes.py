"""Services drawn from OpenStreetMap route relations, with no timetable at all.

`trace` uses a route relation as *geometry* for a pattern that already exists in a
timetable. This uses one as the pattern itself. A PTv2 relation names its operator
and its line and lists its ways in order, so route identity and route extent are
both already there -- everything a service needs except how often it runs.

That is what makes Great Britain's heavy rail drawable at all. It is absent from
BODS, and every timetable source for it sits behind a login, a licence negotiation
or both. The relations are ODbL, which every wayfare archive already carries and
credits for its matched roads, so a rail layer built this way adds no licence to the
archive and nothing to renegotiate if it is copied.

Measured over the national Overpass body, `route=train`, Great Britain:

* 1,114 relations, of which **981 chain with zero breaks** -- the same gate `trace`
  applies, and the same order of quality as the Underground's 86.9%.
* 911 of those sit in Great Britain rather than Ireland or the continent.
* 96.6% carry `operator`, 99.7% `name`, 62.6% `ref`.
* 55,114 distinct ways, 26,454 km of unique track.

That 911 is now enforced rather than observed. `config.Feed.bounds` narrows the
Overpass window per region, below the British Isles clip a min/max over the region's
stops gives it, and `config.Feed.operators` refuses a relation whose `operator` names
only another region's rail -- which is what now keeps Iarnród Éireann's share of the
other 70 out of Great Britain's archive.

Two things this deliberately does not do:

* **It does not reorder a relation's members.** A greedy endpoint walk rescues 15 of
  the 120 that break, and the rest have between 9 and 36 loose ends -- genuine gaps
  rather than misordering. Chaining in member order stays the gate.
* **It does not invent `n_trips`.** The column is left NULL and `rail.attribute`
  fills it in if a timetable ever arrives. A count of relations is a fact about how
  thoroughly a line has been mapped, and passing one off as a service level would be
  the same mistake as judging a low zoom by feature counts.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import duckdb

from . import config, db, logs, osm

log = logs.get("osmroutes")

# The `route` tag values that become patterns, and the `patterns.mode` each becomes.
# Deliberately narrow. `config.OSM_ROUTE_VALUES` is wide because `trace` discovers
# geometry with it and lets the stop-sequence join decide; here a relation *is* a
# service, so admitting `subway` would put a second Underground beside the one BODS
# already supplies and draw every tube line twice.
ROUTE_MODES: dict[str, str] = {"train": "rail"}

# Padding on the window asked of Overpass, in degrees. A relation is kept when it
# chains, not when it fits, so this only has to be wide enough that a service near
# the edge of the region has its relation returned at all. It sits beside `trace`'s
# own pad in `config`, because the two are read together whenever either moves.
BBOX_PAD = config.ROUTES_BBOX_PAD_DEG

# An `operator` tag is a `;`-separated list where a line is run jointly. Some
# mappers write a bilingual pair with a slash instead, so both separate.
_OPERATOR_SPLIT = re.compile(r"[;/]")

_NOT_ALNUM = re.compile(r"[^a-z0-9]+")


def operator_key(name: str) -> str:
    """An `operator` value reduced to what two spellings of it agree on.

    Accents fold rather than being kept: `Iarnród Éireann` is tagged with them and
    without them across the network, and neither spelling is the wrong one.
    """
    folded = unicodedata.normalize("NFKD", name).casefold()
    bare = "".join(c for c in folded if not unicodedata.combining(c))
    return _NOT_ALNUM.sub(" ", bare).strip()


def claims(region: str | None = None) -> tuple[frozenset[str], frozenset[str]]:
    """The operator keys this region draws, and the ones another region draws.

    Both sets come out of `config.FEEDS`, so every region's run reads the same
    ownership and two of them cannot both keep a relation.
    """
    region = region or config.BODS_REGION
    mine = frozenset(operator_key(n) for n in config.feed(region).operators)
    others = frozenset(
        operator_key(n)
        for slug, other in config.FEEDS.items()
        if slug != region
        for n in other.operators
    )
    return mine, others - mine


def ours(tag: str | None, mine: frozenset[str], others: frozenset[str]) -> bool:
    """Whether this region draws a relation, from the operators it names.

    Three cases, and the third is the one a cross-border service needs.

    A tag naming nobody any region claims is left to the window. That is what keeps
    a region with no operators of its own -- Great Britain, and every BODS slug --
    drawing what it has always drawn, while still refusing Iarnród Éireann's
    relations, which reach its window because the British Isles clip admits them.

    A tag naming only another region's operators is refused outright.

    A tag naming both goes to whichever is written first. The Enterprise is run
    jointly by Iarnród Éireann and NI Railways, and both regions' runs read the one
    tag, so first-listed is arbitrary about which archive gets that line and exact
    about it landing in only one.
    """
    named = [operator_key(n) for n in _OPERATOR_SPLIT.split(tag or "")]
    claimed = [n for n in named if n in mine or n in others]
    if not claimed:
        return True
    return claimed[0] in mine


@dataclass(frozen=True)
class Built:
    """What one run produced, for the CLI to print and the tests to assert on."""

    # Every relation of a drawn route the window returned, this region's or not.
    considered: int
    # Of the ones this region draws, those whose ways form one unbroken path.
    chained: int
    patterns: int
    ways: int
    skipped_no_stops: int
    skipped_broken: int
    skipped_not_ours: int
    track_km: float


def bbox(
    con: duckdb.DuckDBPyConnection, region: str | None = None
) -> tuple[float, float, float, float] | None:
    """The window to ask Overpass for: every live pattern's stops, padded.

    Every live pattern rather than the pending non-road ones `trace.bbox` uses, and
    the difference is not cosmetic. `trace`'s window is drawn round the patterns it
    still owes an answer for -- for Great Britain the Underground, the DLR and a few
    tram systems, which is London, Manchester, Blackpool and Birmingham. Asking that
    window for National Rail would return no ScotRail at all.

    Hence a separate cache file too. Two windows sharing one cached body means
    whichever stage ran first silently decides the other's coverage.

    Clipped to the British Isles, which `patterns` has already done to the rows this
    reads. Kept here as well because the failure it prevents is silent and expensive:
    BODS carries coach to Warsaw, and a min/max that meets one of those stops asks
    Overpass for every railway between Ireland and Poland. That is the query that
    ran out of memory before the clip existed, and a database built by an older
    `patterns` would do it again.

    Then padded and clipped to `config.Feed.bounds` by `config.pad_and_clip`, which
    is the same failure one border in rather than one sea. Translink runs to Dublin,
    so Northern Ireland's stops draw a box over most of the island and every
    relation of the Republic's network comes back inside it.
    """
    row = db.row(
        con,
        f"""
        SELECT min(s.lat), min(s.lon), max(s.lat), max(s.lon)
        FROM pattern_stops ps
        JOIN stops s USING (stop_id)
        JOIN patterns p USING (pattern_id)
        WHERE {db.current_feed()}
          AND {config.british_isles_sql("s.lat", "s.lon")}
        """,
    )
    if row is None or row[0] is None:
        return None
    return config.pad_and_clip(
        (float(row[0]), float(row[1]), float(row[2]), float(row[3])),
        pad=BBOX_PAD,
        region=region,
        what="live stops",
    )


def _span_m(points: list[tuple[float, float]]) -> float:
    """Straight-line length of a chain of (lat, lon), in metres.

    Summed segment by segment on `osm.planar_m`'s plane rather than measured end to
    end, so a line that turns is its own length and not the distance between its
    terminals. Each segment is short enough that the plane costs nothing against
    `osm.haversine_m`.
    """
    return sum(osm.planar_m(a, b) for a, b in zip(points, points[1:], strict=False))


@dataclass(frozen=True)
class Candidate:
    """One relation that passed the gate, ready to become a pattern."""

    relation_id: int
    route_id: str
    agency_id: str | None
    short_name: str
    mode: str
    names: list[str]
    n_stops: int
    span_m: float
    way_ids: list[int]
    lon_e6: list[int]
    lat_e6: list[int]


@dataclass(frozen=True)
class Sifted:
    """What one body of relations came to: the ones kept, and why the rest were not.

    Every relation of a drawn route lands in exactly one of these, so the four
    refusals and the kept list add back up to `considered`. That is the property
    `chained` leans on, and a refusal that skips the count silently inflates it.
    """

    # Every relation of a drawn route, this region's or not.
    considered: int
    kept: list[Candidate]
    broken: int
    no_stops: int
    not_ours: int
    no_ways: int

    @property
    def chained(self) -> int:
        """Of the relations this region draws, those whose ways form one path.

        A relation carrying no ways chains nothing, so it comes out here beside the
        two refusals rather than being credited with a path it has no members for.
        """
        return self.considered - self.broken - self.not_ours - self.no_ways


def candidates(
    relations: list[osm.Relation],
    routes: dict[str, str] | None = None,
    region: str | None = None,
) -> Sifted:
    """The relations that can stand as a service, and counts of the four refusals.

    A relation qualifies when it carries ways at all, is this region's to draw, its
    ways chain end to end with no break, and it names at least two stops. The last
    two are the tests `trace` already applies, for the same reason: a break draws
    confident track across a gap, and a relation with no stops can be neither
    identified nor joined to anything later.

    Ownership is `ours`, and it is what the window cannot do. A window is a box, a
    border is not, and two archives are loaded onto one map -- so a relation kept by
    both regions is a line the viewer draws twice.
    """
    # `is None` rather than falsy: an empty selection is a region that draws no
    # relations at all, and `or` would hand it back the default it just refused.
    if routes is None:
        routes = ROUTE_MODES
    mine, others = claims(region)
    out: list[Candidate] = []
    considered = broken = no_stops = not_ours = no_ways = 0
    for r in relations:
        mode = routes.get(r.route or "")
        if mode is None:
            continue
        considered += 1
        if not r.ways:
            no_ways += 1
            continue
        if not ours(r.tags.get("operator"), mine, others):
            not_ours += 1
            continue
        # Measured lazily, so a relation refused for its breaks pays for the chain
        # and nothing that hangs off it.
        measured = osm.prepare(r)
        if measured.breaks:
            broken += 1
            continue
        names = [n for n in measured.names if n]
        if len(names) < 2:
            no_stops += 1
            continue
        # `ref` first: it is the line's own designation where the mapper gave one,
        # and it is what a reader recognises. The relation name is a description
        # ("CrossCountry: Plymouth -> Aberdeen") and is the fallback rather than the
        # first choice, but 99.7% carry one where only 62.6% carry a ref.
        short = (r.tags.get("ref") or r.name or "").strip() or f"r{r.relation_id}"
        out.append(
            Candidate(
                relation_id=r.relation_id,
                route_id=f"osm:r{r.relation_id}",
                agency_id=(r.tags.get("operator") or "").strip() or None,
                short_name=short,
                mode=mode,
                names=names,
                n_stops=len(names),
                span_m=_span_m([(s.lat, s.lon) for s in r.stops if s.name]),
                way_ids=list(measured.way_ids),
                lon_e6=[round(lon * 1e6) for _, lon in measured.points],
                lat_e6=[round(lat * 1e6) for lat, _ in measured.points],
            )
        )
    return Sifted(
        considered=considered,
        kept=out,
        broken=broken,
        no_stops=no_stops,
        not_ours=not_ours,
        no_ways=no_ways,
    )


def write(con: duckdb.DuckDBPyConnection, found: list[Candidate]) -> int:
    """Insert the candidates as patterns, with their geometry and their ways.

    Four tables and each is load-bearing:

    * `patterns`, stamped with the current feed version so `db.current_feed()`
      admits them. They are rebuilt from OpenStreetMap on every run rather than
      carried forward, which is what keeps a line retired in OSM from being drawn
      for ever.
    * `traces`, which is where `aggregate.build_track_services` reads the ways each
      relation runs over. `build_segments` leaves them alone, because a relation
      drawn once per way and again as a whole polyline is the same track painted
      twice and a hover that lands on the wrong one of the two.
    * `trace_status`, marked ``ok``. Without it `trace._pending_sql` selects every
      one of these -- they are live, not matchable and carry no ``shape_id``, which
      is exactly its definition of pending -- and spends a national Overpass query
      re-deriving geometry this stage already has.
    * `ways`, the per-way geometry `traces` cannot be cut back into.

    The identity hash is computed in SQL by `db.pattern_id_sql` rather than in
    Python, so these ids are minted by the same expression as the timetable's and
    cannot drift from it.
    """
    feed = db.get_meta(con, "feed_version")
    if not feed:
        raise RuntimeError("no feed_version in meta; run `wayfare patterns` first")

    con.execute("BEGIN")
    try:
        # Retire first, and retire even with nothing found. A run that draws none of
        # these is not a no-op: it is a region that has stopped drawing them, and
        # returning early here left the last run's relations live and still on the
        # map. That is how the Republic kept its second copy of every line for a
        # `routes` run after the setting that stopped drawing it.
        con.execute(
            "UPDATE patterns SET last_seen = NULL "
            "WHERE route_id LIKE 'osm:r%' AND last_seen = ?",
            [feed],
        )
        if not found:
            con.execute("COMMIT")
            return 0
        con.execute("""
            CREATE OR REPLACE TEMP TABLE osm_route_raw (
                relation_id BIGINT, route_id VARCHAR, agency_id VARCHAR,
                short_name VARCHAR, mode VARCHAR, stop_key VARCHAR,
                n_stops INTEGER, span_m DOUBLE,
                way_ids BIGINT[], lon_e6 INTEGER[], lat_e6 INTEGER[]
            )
        """)
        # Staged to a file rather than inserted a row at a time: this is one row per
        # relation now, and it is the same table a national body fills.
        db.insert_via_file(
            con,
            "osm_route_raw",
            (
                "relation_id",
                "route_id",
                "agency_id",
                "short_name",
                "mode",
                "stop_key",
                "n_stops",
                "span_m",
                "way_ids",
                "lon_e6",
                "lat_e6",
            ),
            [
                (
                    c.relation_id,
                    c.route_id,
                    c.agency_id,
                    c.short_name,
                    c.mode,
                    "".join(c.names),
                    c.n_stops,
                    c.span_m,
                    c.way_ids,
                    c.lon_e6,
                    c.lat_e6,
                )
                for c in found
            ],
        )

        pid = db.pattern_id_sql("route_id", "NULL", "stop_key")
        con.execute(f"""
            INSERT INTO patterns
                (pattern_id, route_id, agency_id, short_name, direction, shape_id,
                 n_stops, n_trips, span_m, mode, first_seen, last_seen)
            SELECT {pid}, route_id, agency_id, short_name, NULL, NULL,
                   n_stops, NULL, span_m, mode, '{feed}', '{feed}'
            FROM osm_route_raw
            ON CONFLICT (pattern_id) DO UPDATE SET
                agency_id = excluded.agency_id,
                short_name = excluded.short_name,
                n_stops = excluded.n_stops,
                span_m = excluded.span_m,
                mode = excluded.mode,
                last_seen = excluded.last_seen
        """)  # noqa: S608 - feed is a meta value this pipeline wrote
        con.execute(f"""
            INSERT OR REPLACE INTO traces
                (pattern_id, relation_id, way_ids, ways_cut, lon_e6, lat_e6)
            -- `ways_cut` is TRUE by construction rather than by any cutting: the
            -- pattern *is* the relation here, so the ways of the line and the ways
            -- under the geometry are one list. `trace` has to earn the same claim.
            SELECT {pid}, relation_id, way_ids, TRUE, lon_e6, lat_e6 FROM osm_route_raw
        """)
        con.execute(f"""
            INSERT OR REPLACE INTO trace_status
                (pattern_id, status, relation_id, osm_route, n_ways, n_stops,
                 worst_stop_m, length_m, detail, traced_at)
            SELECT {pid}, 'ok', relation_id, mode, len(way_ids), n_stops,
                   NULL, NULL, 'built from the relation itself', now()
            FROM osm_route_raw
        """)
        n = int(db.scalar(con, "SELECT count(*) FROM osm_route_raw"))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return n


def write_ways(con: duckdb.DuckDBPyConnection, relations: list[osm.Relation]) -> int:
    """Per-way geometry, deduplicated across every relation that uses the way.

    Upserted rather than rebuilt, because two stages fill this table and neither
    sees the other's relations: `routes` writes the ways of the `route=train`
    relations it turned into services, and `trace` writes the ways of the subway,
    light rail and tram relations it cut a timetable's patterns out of. A blanket
    delete here would take the tube's track out of the archive on the next `routes`
    run, and the export joins `ways` inside, so the failure is track that quietly
    stops being drawn rather than anything that raises.

    `prune_ways` is what keeps a way that has left the network from being drawn for
    ever, and it is a separate call for the same reason: it has to run after every
    writer, not after each one.
    """
    seen: dict[int, osm.Way] = {}
    for r in relations:
        for w in r.ways:
            if len(w.points) >= 2:
                seen.setdefault(w.way_id, w)
    if not seen:
        return 0
    rows = []
    for way in seen.values():
        lons = [round(lon * 1e6) for _, lon in way.points]
        lats = [round(lat * 1e6) for lat, _ in way.points]
        rows.append((way.way_id, lons, lats, min(lons), min(lats), max(lons), max(lats)))
    return db.insert_via_file(
        con,
        "ways",
        (
            "way_id",
            "lon_e6",
            "lat_e6",
            "min_lon_e6",
            "min_lat_e6",
            "max_lon_e6",
            "max_lat_e6",
        ),
        rows,
        on_conflict="replace",
    )


def prune_ways(con: duckdb.DuckDBPyConnection) -> int:
    """Drop the ways no trace runs over any more.

    A way is in the table because some pattern's geometry runs over it, so a way no
    `traces` row names is drawn by nothing -- a relation retired from OpenStreetMap,
    or one that stopped chaining, or a line the mode selection no longer admits.

    Against `traces` rather than against live patterns, because that table is a
    permanent cache and a departed service's row stays in it: the way is still the
    right geometry for the next feed that carries the service again, and the export
    reaches `ways` only through `track_services`, which is rebuilt against the live
    patterns every run. So an unreferenced way is dead weight and a referenced one
    is never drawn on its own account.
    """
    count = "SELECT count(*) FROM ways"
    before = int(db.scalar(con, count))
    # NOT EXISTS rather than NOT IN: a NULL anywhere in the unnested list makes
    # `NOT IN` unknown for every row and the delete quietly does nothing.
    con.execute("""
        DELETE FROM ways w
        WHERE NOT EXISTS (
            SELECT 1 FROM traces t, unnest(t.way_ids) AS u(way_id)
            WHERE u.way_id = w.way_id
        )
    """)
    gone = before - int(db.scalar(con, count))
    if gone:
        log.info("%d ways no trace runs over any more were dropped", gone)
    return gone


def run(
    con: duckdb.DuckDBPyConnection,
    cache: Path | None = None,
    *,
    refresh: bool = False,
    routes: dict[str, str] | None = None,
    region: str | None = None,
) -> Built:
    """Fetch the relations and turn the ones that qualify into services.

    The region is ambient, out of `WAYFARE_REGION`, exactly as it is for `acquire`
    and `publish`. It decides the window and it decides the operator gate, so a run
    against the wrong data root draws another region's rail into this one's archive
    -- which is why the log line names it.
    """
    window = bbox(con, region)
    if window is None:
        raise RuntimeError("no live patterns to derive a window from")
    # The argument wins, then the region's own selection, then the default. `is not
    # None` twice over, because `()` means "draw none" and is the whole point of the
    # setting -- `or` would read it as "unset" and draw everything.
    configured = config.feed(region).route_relations
    if routes is not None:
        wanted = routes
    elif configured is None:
        wanted = ROUTE_MODES
    else:
        wanted = {k: v for k, v in ROUTE_MODES.items() if k in configured}
    # Before the fetch, not after. A region that draws none still has to reach
    # `write`, so last run's relations retire, but asking Overpass for a body every
    # relation in it is about to be refused is a national query spent on nothing.
    relations = (
        osm.fetch(window, cache or config.RAW / "osm_routes.json", refresh=refresh)
        if wanted
        else []
    )
    sifted = candidates(relations, wanted, region)
    n_patterns = write(con, sifted.kept)
    # Only the relations that became patterns. `ways` is joined to `track_services`
    # on the way out, so a refused relation's ways are rows nothing can reach --
    # and in Northern Ireland's database they were the whole Republic's track.
    drawn = {c.relation_id for c in sifted.kept}
    n_ways = write_ways(con, [r for r in relations if r.relation_id in drawn])
    # And the ones a previous run wrote that nothing reaches any more, which is the
    # same claim over time rather than over one body. Narrowing the write keeps a
    # refused relation's ways out; this is what takes out the ways of a relation
    # that was drawn last week and is not drawn now.
    prune_ways(con)
    km = sum(c.span_m for c in sifted.kept) / 1000.0
    built = Built(
        considered=sifted.considered,
        chained=sifted.chained,
        patterns=n_patterns,
        ways=n_ways,
        skipped_no_stops=sifted.no_stops,
        skipped_broken=sifted.broken,
        skipped_not_ours=sifted.not_ours,
        track_km=km,
    )
    log.info(
        "%s: %d relations considered, %d chained, %d became patterns over %d ways "
        "(%d another region's, %d broken, %d with fewer than two named stops)",
        region or config.BODS_REGION,
        built.considered,
        built.chained,
        built.patterns,
        built.ways,
        built.skipped_not_ours,
        built.skipped_broken,
        built.skipped_no_stops,
    )
    return built
