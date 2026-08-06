from __future__ import annotations

import pytest

from wayfare import polyline


def test_roundtrip_precision6():
    points = [(53.48, -2.245), (53.4801, -2.2400), (53.4805, -2.2312)]
    got = polyline.decode(polyline.encode(points, 6), 6)
    assert got == pytest.approx(points, abs=1e-6)


def test_known_precision5_vector():
    """The Google reference example, which pins the codec against a third party."""
    assert polyline.decode("_p~iF~ps|U_ulLnnqC_mqNvxq`@", 5) == pytest.approx(
        [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)], abs=1e-5
    )


def test_precision_matters():
    """A precision-6 string decoded as 5 is off by a factor of ten, not a rounding
    error -- the exact failure this module's docstring warns about."""
    points = [(53.48, -2.245), (53.49, -2.24)]
    wrong = polyline.decode(polyline.encode(points, 6), 5)
    assert abs(wrong[0][0]) > 100


def test_empty():
    assert polyline.decode("") == []
    assert polyline.encode([]) == ""
