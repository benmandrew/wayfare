from __future__ import annotations

from wayfare import aggregate, db


def test_a_pattern_that_should_never_have_been_matched_is_not_drawn_as_road(con):
    """A database matched before the mode filter existed holds `pattern_edges` for
    ferries and metros. Great Britain's held 1,726,822 of them for the Underground
    alone, and 16,833 edges were reachable from nothing else -- roads drawn as though
    buses used them. `build_segments` draws those patterns properly; drawing them twice
    with one copy wrong is worse than either."""
    db.set_meta(con, "feed_version", "F1")
    for pid, mode in ((1, "bus"), (2, "ferry")):
        con.execute(
            "INSERT INTO patterns (pattern_id, route_id, agency_id, short_name, "
            "direction, n_stops, n_trips, mode, first_seen, last_seen) "
            "VALUES (?, 'R', 'OP1', ?, 0, 2, 10, ?, 'F1', 'F1')",
            [pid, f"S{pid}", mode],
        )
    con.execute(
        "INSERT INTO edges VALUES (1, 1, 'R', 'secondary', 100.0, [0], [0], 0, 0, 0, 0)"
    )
    con.execute(
        "INSERT INTO edges VALUES (2, 2, 'R', 'secondary', 100.0, [0], [0], 0, 0, 0, 0)"
    )
    con.execute("INSERT INTO pattern_edges VALUES (1, 0, 1)")
    con.execute("INSERT INTO pattern_edges VALUES (2, 0, 2)")

    aggregate.build(con)

    drawn = {
        r[0] for r in con.execute("SELECT DISTINCT edge_id FROM edge_services").fetchall()
    }
    assert drawn == {1}
