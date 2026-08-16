"""The data half of a render: which edges, weighted how, grouped by what.

`art.QuerySpec` is a closed vocabulary substituted into hand-assembled SQL, so the
failures it can have are runtime ones that no type checker sees: a fragment that
does not compose, or a bound parameter list that has drifted out of step with the
`?` holes. Both are pinned here against a real database.
"""

from __future__ import annotations

import itertools

import builders
import pytest

from wayfare import art, db

BOUNDS = art.Bounds(*builders.WINDOW)
BBOX = [
    round(BOUNDS.max_lon * 1e6),
    round(BOUNDS.min_lon * 1e6),
    round(BOUNDS.max_lat * 1e6),
    round(BOUNDS.min_lat * 1e6),
]

# No filter, each filter on its own, then all of them at once. The last case is the
# one that catches a fragment whose parameters were appended in the wrong order.
FILTERS: list[dict[str, object]] = [
    {},
    {"operator": ("FIRST",)},
    {"service": ("42",)},
    {"road_class": ("primary",)},
    {"min_trips": 40},
    {
        "operator": ("FIRST",),
        "service": ("42",),
        "road_class": ("secondary",),
        "min_trips": 40,
    },
]


@pytest.fixture(scope="module")
def net(tmp_path_factory):
    """A four-edge network with every awkward case the spec has to survive.

    Edge 3 carries no services at all, which is what tells the two join semantics
    apart; edge 4 has a null road_name and a second way_id, so the group coercion
    has something to coerce.

    Module-scoped, and every test here only reads: the vocabulary is a product of
    six weights, five groups and five orders, and a fresh database per case costs
    more than all the queries put together.
    """
    con = db.connect(tmp_path_factory.mktemp("spec") / "spec.duckdb")
    for edge_id, lon in enumerate((-3200000, -3190000, -3180000), start=1):
        builders.insert_edge(con, edge_id, lon_e6=lon, span_e6=1000)
    builders.insert_edge(
        con,
        4,
        lon_e6=-3170000,
        span_e6=1000,
        road_class="primary",
        road_name=None,
        way_id=99,
    )
    builders.insert_services(con, 1, (("42", "OP1"), ("9A", "OP2")), n_trips=100)
    builders.insert_services(con, 2, (("42", "FIRST"),), n_trips=50)
    builders.insert_services(con, 4, (("7", "OP1"),), n_trips=30)
    yield con
    con.close()


def _window(con, **kwargs):
    return art.Window(BOUNDS, con, spec=art.QuerySpec(**kwargs))


def _ids(con, **kwargs):
    return sorted(e.edge_id for e in _window(con, **kwargs).edges())


def _edges_where(con, predicate):
    """The edge ids a raw `edge_services` predicate picks out, as the reference set."""
    sql = f"SELECT DISTINCT edge_id FROM edge_services WHERE {predicate}"
    return {r[0] for r in con.execute(sql).fetchall()}


def _every_query(sql):
    """Every query the spec can produce, as (text, parameters) pairs."""
    return (
        sql.edges_query(with_groups=True, by_weight=True),
        sql.edges_query(with_groups=False, by_weight=False),
        sql.weights_query(),
        sql.group_query(),
        sql.grouped_query(),
        sql.cardinality_query(),
    )


# --- The vocabulary ---------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "bad", "expected"),
    [
        ("weight", "trip", "trips"),
        ("group", "services", "service"),
        ("order", "widest_first", "widest"),
    ],
)
def test_an_unknown_key_names_the_alternatives(field, bad, expected):
    """A typo has to say what was meant, because the vocabulary is the whole API."""
    with pytest.raises(ValueError, match=expected):
        art.QuerySpec(**{field: bad})


# --- selective --------------------------------------------------------------


def test_the_default_spec_is_not_selective():
    assert art.DEFAULT_SPEC.selective is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"operator": ("FIRST",)},
        {"service": ("42",)},
        {"road_class": ("primary",)},
        {"min_trips": 1},
    ],
)
def test_each_filter_alone_makes_the_spec_selective(kwargs):
    """min_trips is the easy one to miss: it is an int, not a tuple, so a truthiness
    check written over the tuples only would silently leave the join a LEFT JOIN."""
    assert art.QuerySpec(**kwargs).selective is True


# --- key --------------------------------------------------------------------


def test_equal_specs_share_a_key():
    assert art.QuerySpec(weight="services", service=("42",)).key == (
        art.QuerySpec(weight="services", service=("42",)).key
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"weight": "services"},
        {"group": "operator"},
        {"order": "busiest"},
        {"operator": ("FIRST",)},
        {"service": ("42",)},
        {"road_class": ("primary",)},
        {"min_trips": 40},
    ],
)
def test_any_difference_changes_the_key(kwargs):
    """The key is what a render cache is keyed on, so two specs that draw different
    pictures must never share one -- that would serve one spec's image for another."""
    assert art.QuerySpec(**kwargs).key != art.DEFAULT_SPEC.key


# --- Generated SQL ----------------------------------------------------------


@pytest.mark.parametrize(
    ("weight", "group"), list(itertools.product(sorted(art.WEIGHTS), sorted(art.GROUPS)))
)
def test_every_weight_and_group_combination_executes(net, weight, group):
    """The queries are built by string assembly, so a fragment that does not compose
    is a runtime error rather than a type error. This is the test that finds it."""
    for order in sorted(art.ORDERS):
        sql = art._Sql(
            art.QuerySpec(weight=weight, group=group, order=order),
            art.DEFAULT_SOURCE,
            BBOX,
        )
        queries = [sql.group_query(), sql.grouped_query()]
        if order == art.DEFAULT_SPEC.order:  # order-independent, so run them once
            queries += [
                sql.edges_query(with_groups=True, by_weight=True),
                sql.edges_query(with_groups=False, by_weight=False),
                sql.weights_query(),
                sql.cardinality_query(),
            ]
        for query, params in queries:
            net.execute(query, params).fetchall()


@pytest.mark.parametrize("filters", FILTERS, ids=lambda f: "+".join(f) or "none")
@pytest.mark.parametrize("order", sorted(art.ORDERS))
@pytest.mark.parametrize("group", sorted(art.GROUPS))
@pytest.mark.parametrize("weight", sorted(art.WEIGHTS))
def test_bound_parameters_match_the_holes(weight, group, order, filters):
    """A `?` with no parameter behind it, or the other way round, is a bind error at
    render time. The services fragment appears twice in the grouped queries, so its
    parameters have to be repeated in textual order -- which is exactly the mistake
    that got through once already."""
    spec = art.QuerySpec(weight=weight, group=group, order=order, **filters)
    sql = art._Sql(spec, art.DEFAULT_SOURCE, BBOX)
    for query, params in _every_query(sql):
        assert query.count("?") == len(params)


# --- Filters ----------------------------------------------------------------


def test_unfiltered_the_whole_window_is_drawn(net):
    assert _ids(net) == [1, 2, 3, 4]


def test_an_operator_filter_keeps_only_that_operator(net):
    ids = _ids(net, operator=("FIRST",))
    assert set(ids) < {1, 2, 3, 4}
    assert set(ids) == _edges_where(net, "agency_id = 'FIRST'")


def test_a_service_filter_keeps_only_edges_that_service_uses(net):
    ids = _ids(net, service=("42",))
    assert set(ids) < {1, 2, 3, 4}
    assert set(ids) == _edges_where(net, "short_name = '42'")


def test_a_road_class_filter_shrinks_the_scan_itself(net):
    ids = _ids(net, road_class=("primary",))
    assert set(ids) < {1, 2, 3, 4}
    for e in _window(net, road_class=("primary",)).edges():
        assert e.road_class == "primary"


def test_min_trips_is_a_floor_on_weekly_trips(net):
    assert _ids(net, min_trips=40) == [1, 2]  # 200 and 50; edge 4 carries 30
    assert _ids(net, min_trips=1000) == []


HOSTILE = "read_csv('/etc/passwd')"


@pytest.mark.parametrize("field", ["operator", "service", "road_class"])
def test_a_filter_value_is_bound_rather_than_written_into_the_sql(net, field):
    """A filter value arrives from a query string on a server, and DuckDB's
    `read_only` stops a write without stopping `read_csv` from reading any file the
    process can. Binding the value is the whole of what keeps `/art` from being a
    file-read primitive, so pin it in the generated text and again at execution."""
    sql = art._Sql(art.QuerySpec(**{field: (HOSTILE,)}), art.DEFAULT_SOURCE, BBOX)
    for query, params in _every_query(sql):
        assert HOSTILE not in query
        assert HOSTILE in params
    # And a value that reaches the database as data selects nothing, rather than
    # being evaluated as the call it is spelled to look like.
    assert _ids(net, **{field: (HOSTILE,)}) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"operator": ("FIRST",)},
        {"service": ("42",)},
        {"road_class": ("primary",)},
        # min_trips was the case that failed: it is applied in the edge_w CTE, and
        # `pair` did not join edge_w, so a below-floor edge still drew as long as one
        # of its groups survived. Fixed by that join; keep it parametrised here.
        {"min_trips": 100},
    ],
    ids=lambda k: "+".join(k),
)
def test_a_filter_removes_the_same_edges_from_both_query_shapes(net, kwargs):
    """One spec, one set of edges, whichever query shape a style happens to use.

    `density` walks the flat query and `strands` walks the grouped one. If a filter
    reached only one of them, the same spec would draw two different networks
    depending on the style, which is the one thing a spec must never do.
    """
    spec = art.QuerySpec(**kwargs)
    # The grouped query returns geometry without an edge id, so the first vertex
    # stands in for identity -- every edge here starts at a different longitude.
    flat = {round(e.coords[0][0] * 1e6) for e in art.Window(BOUNDS, net, spec=spec).edges()}
    grouped = {
        round(coords[0][0] * 1e6)
        for _name, _weight, coords in art.Window(
            BOUNDS, net, with_groups=True, spec=spec
        ).groups()
    }
    assert flat and grouped  # a vacuous pass would prove nothing
    assert grouped <= flat


# --- selective join semantics -----------------------------------------------


def test_an_edge_with_no_services_still_draws_when_unfiltered(net):
    """The original behaviour, and worth keeping: a road nothing runs over renders
    black at weight zero rather than disappearing."""
    weights = {e.edge_id: e.weight for e in _window(net).edges()}
    assert weights[3] == 0.0


def test_an_edge_with_no_surviving_service_vanishes_when_filtered(net):
    """Filtered, weight zero is wrong rather than merely dim: it would draw a black
    line through the middle of a picture of one operator's network."""
    for kwargs in ({"operator": ("FIRST",)}, {"service": ("42",)}, {"min_trips": 1}):
        assert 3 not in _ids(net, **kwargs), kwargs


# --- Weights ----------------------------------------------------------------


def test_density_returns_a_fractional_weight(net):
    """Traffic per metre is not a count. `Weights` is fed through array("d") for
    exactly this reason -- array("q") would have raised on the first fractional one."""
    weights = {e.edge_id: e.weight for e in _window(net, weight="density").edges()}
    assert weights[4] == pytest.approx(0.3)
    assert not float(weights[4]).is_integer()


@pytest.mark.parametrize("weight", sorted(art.WEIGHTS))
def test_every_weight_arrives_as_a_float(net, weight):
    for e in _window(net, weight=weight).edges():
        assert isinstance(e.weight, float)


# --- Group keys -------------------------------------------------------------


def test_a_non_string_group_key_comes_back_as_a_string(net):
    """way_id is a BIGINT. `strands` hashes the group name to pick a hue, so a
    non-string here is a TypeError deep inside drawing rather than a query error."""
    window = art.Window(BOUNDS, net, with_groups=True, spec=art.QuerySpec(group="way"))
    names = {name for name, _weight, _coords in window.groups()}
    assert names == {"1", "99"}
    assert all(isinstance(n, str) for n in names)


def test_a_null_group_key_becomes_unknown(net):
    """road_name is frequently null nationally, and a null hue is a crash."""
    window = art.Window(
        BOUNDS, net, with_groups=True, spec=art.QuerySpec(group="road_name")
    )
    names = {name for name, _weight, _coords in window.groups()}
    assert names == {"R", "unknown"}
    for e in art.Window(
        BOUNDS, net, with_groups=True, spec=art.QuerySpec(group="road_name")
    ).edges():
        assert all(isinstance(g, str) for g in e.groups)


# --- MAX_GROUPS -------------------------------------------------------------


def test_too_many_groups_is_refused_before_anything_is_drawn(net, monkeypatch):
    """`group=way` over a city is one group per OSM way, which is one composited
    stroke per edge. Better a message than a render that never ends."""
    monkeypatch.setattr(art, "MAX_GROUPS", 1)
    window = art.Window(BOUNDS, net, with_groups=True, spec=art.QuerySpec(group="way"))
    with pytest.raises(ValueError, match="group='way' gives 2 groups"):
        list(window.groups())


def test_a_window_inside_the_cap_is_fine(net, monkeypatch):
    monkeypatch.setattr(art, "MAX_GROUPS", 2)
    window = art.Window(BOUNDS, net, with_groups=True, spec=art.QuerySpec(group="way"))
    assert {name for name, _w, _c in window.groups()} == {"1", "99"}


# --- Determinism ------------------------------------------------------------


# The sequence each order draws over `net`. Service 42 spans edges 1 and 2 and
# carries 150 trips, 9A one edge and 100, 7 one edge and 30 -- so `narrowest` and
# `name` both need the group key to break a tie, and pinning the sequence is what
# says the tiebreak is there. Two runs agreeing would not: an undefined order over
# four rows is a stable one.
ORDERED_NAMES = {
    "widest": ["42", "42", "7", "9A"],
    "narrowest": ["7", "9A", "42", "42"],
    "busiest": ["42", "42", "9A", "7"],
    "quietest": ["7", "9A", "42", "42"],
    "name": ["42", "42", "7", "9A"],
}


@pytest.mark.parametrize("order", sorted(art.ORDERS))
def test_every_order_draws_a_pinned_sequence(net, order):
    """The order *inside* a ribbon matters. A PNG hides it -- SCREEN compositing is
    commutative -- but an SVG records stroke order, and two runs of `strands` once
    differed in 180,365 of 293,842 bytes. The edge_id tiebreak in _order_sql is what
    fixes it, so pin the full sequence rather than just the group order."""
    window = art.Window(BOUNDS, net, with_groups=True, spec=art.QuerySpec(order=order))
    assert [name for name, _w, _c in window.groups()] == ORDERED_NAMES[order]


def test_the_default_order_is_widest_first(net):
    """The default ordering puts trunk routes underneath, and equally broad services
    fall back to their name so the scan order cannot decide the picture."""
    window = art.Window(BOUNDS, net, with_groups=True)
    names = [name for name, _w, _c in window.groups()]
    assert names == ORDERED_NAMES["widest"]
