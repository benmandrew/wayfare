from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from wayfare import db


def test_a_row_per_point_shapes_table_migrates_in_place(tmp_path: Path):
    """A national match run costs a day or two. A schema change it cannot survive
    is a schema change nobody applies, so the old layout is rewritten, not re-read."""
    path = tmp_path / "old.duckdb"
    old = duckdb.connect(str(path))
    old.execute(
        "CREATE TABLE shapes (shape_id VARCHAR, seq INTEGER, lat DOUBLE, lon DOUBLE)"
    )
    old.executemany(
        "INSERT INTO shapes VALUES (?, ?, ?, ?)",
        [
            ("SH1", 2, 53.48, -2.24),  # deliberately out of order on disk
            ("SH1", 1, 53.48, -2.245),
            ("SH1", 3, 53.48, -2.235),
        ],
    )
    old.close()

    con = db.connect(path)
    lat_e6, lon_e6 = con.execute("SELECT lat_e6, lon_e6 FROM shapes").fetchone()
    assert lon_e6 == [-2245000, -2240000, -2235000]
    assert lat_e6 == [53480000] * 3
    con.close()


def _one_live_pattern(con) -> None:
    db.set_meta(con, "feed_version", "F1")
    con.execute(
        "INSERT INTO patterns "
        "VALUES (1, 'R1', 'OP1', '42', 0, 'SH1', 4, 10, 1.0, 'F1', 'F1')"
    )


def test_prune_refuses_while_patterns_are_still_pending(con):
    _one_live_pattern(con)
    con.execute("INSERT INTO shapes VALUES ('SH1', [53480000], [-2245000])")

    with pytest.raises(RuntimeError, match="still unmatched"):
        db.prune_shapes(con)
    assert con.execute("SELECT count(*) FROM shapes").fetchone()[0] == 1


def test_prune_drops_shapes_once_every_pattern_is_resolved(con):
    _one_live_pattern(con)
    con.execute("INSERT INTO shapes VALUES ('SH1', [53480000], [-2245000])")
    con.execute(
        "INSERT INTO match_status "
        "VALUES (1, 'ok', 'shape', 0.9, 100.0, 1.0, 2, NULL, now())"
    )

    assert db.prune_shapes(con) == 1
    assert con.execute("SELECT count(*) FROM shapes").fetchone()[0] == 0
