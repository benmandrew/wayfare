from __future__ import annotations

import builders

from wayfare import aggregate, db


def test_a_pattern_that_should_never_have_been_matched_is_not_drawn_as_road(con):
    """A database matched before the mode filter existed holds `pattern_edges` for
    ferries and metros. Great Britain's held 1,726,822 of them for the Underground
    alone, and 16,833 edges were reachable from nothing else -- roads drawn as though
    buses used them. `build_segments` draws those patterns properly; drawing them twice
    with one copy wrong is worse than either."""
    db.set_meta(con, "feed_version", "F1")
    for pid, mode in ((1, "bus"), (2, "ferry")):
        builders.insert_pattern(con, pid, mode=mode)
    builders.insert_edge(con, 1, way_id=1)
    builders.insert_edge(con, 2, way_id=2)
    con.execute("INSERT INTO pattern_edges VALUES (1, 0, 1)")
    con.execute("INSERT INTO pattern_edges VALUES (2, 0, 2)")

    aggregate.build(con)

    drawn = {
        r[0] for r in con.execute("SELECT DISTINCT edge_id FROM edge_services").fetchall()
    }
    assert drawn == {1}


def test_an_edge_a_pattern_runs_over_twice_is_counted_once(con):
    """The dedupe this module exists for. A loop or an out-and-back spur lists the
    same edge twice against one pattern, and summing over both rows would say the
    service runs twice as often there as the timetable has it."""
    db.set_meta(con, "feed_version", "F1")
    builders.insert_pattern(con, 1, short_name="42", n_trips=10)
    builders.insert_edge(con, 1)
    con.execute("INSERT INTO pattern_edges VALUES (1, 0, 1), (1, 1, 1)")

    aggregate.build(con)

    assert db.row(
        con, "SELECT n_patterns, n_trips FROM edge_services WHERE edge_id = 1"
    ) == (1, 10)


def test_one_number_run_by_two_operators_stays_two_rows(con):
    """`short_name` is what makes the output legible -- "43" once rather than three
    times -- but the operator is in the group as well, because two companies running
    the same number over one road are two services and the card has to say whose."""
    db.set_meta(con, "feed_version", "F1")
    builders.insert_pattern(con, 1, short_name="42", agency_id="OP1", n_trips=10)
    builders.insert_pattern(con, 2, short_name="42", agency_id="OP2", n_trips=4)
    builders.insert_edge(con, 1)
    con.execute("INSERT INTO pattern_edges VALUES (1, 0, 1), (2, 0, 1)")

    aggregate.build(con)

    rows = con.execute(
        "SELECT agency_id, n_patterns, n_trips FROM edge_services WHERE edge_id = 1 "
        "ORDER BY agency_id"
    ).fetchall()
    assert rows == [("OP1", 1, 10), ("OP2", 1, 4)]


def test_a_withdrawn_osm_pattern_counts_as_departed(con):
    """`osmroutes` withdraws a relation by setting `last_seen` to NULL, so the live
    predicate answers NULL for it and `NOT NULL` is not true. The pattern is out of
    the feed and out of the count of what left it, which reads as a region whose rail
    never moves."""
    db.set_meta(con, "feed_version", "F1")
    builders.insert_pattern(con, 1, mode="bus", feed="F1")
    builders.insert_pattern(con, 2, mode="rail", feed="F1", last_seen=None)

    cov = aggregate.coverage(con)

    assert cov["patterns_total"] == 1
    assert cov["patterns_departed"] == 1
