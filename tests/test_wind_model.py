import numpy as np

from src.wind_model import WindProfile


def _profile(speed, direction):
    return WindProfile(
        altitudes_agl_m=np.array([0.0, 1000.0]),
        speed_ms=np.array([speed, speed]),
        direction_deg=np.array([direction, direction]),
        source="test",
    )


def test_west_wind_drifts_east():
    # Wind FROM the west (270 deg) pushes an object toward the east (+u).
    u, v = _profile(10.0, 270.0).drift_vectors(np.array([500.0]))
    assert u[0] > 0
    assert abs(v[0]) < 1e-6


def test_south_wind_drifts_north():
    # Wind FROM the south (180 deg) pushes toward the north (+v).
    u, v = _profile(8.0, 180.0).drift_vectors(np.array([500.0]))
    assert v[0] > 0
    assert abs(u[0]) < 1e-6


def test_sample_clamps_outside_range():
    p = WindProfile(
        altitudes_agl_m=np.array([100.0, 200.0]),
        speed_ms=np.array([5.0, 15.0]),
        direction_deg=np.array([90.0, 90.0]),
        source="test",
    )
    speed, _ = p.sample(np.array([0.0, 150.0, 5000.0]))
    assert speed[0] == 5.0        # clamped to bottom band
    assert abs(speed[1] - 10.0) < 1e-6  # interpolated midpoint
    assert speed[2] == 15.0       # clamped to top band


def test_direction_interpolation_handles_wraparound():
    # 350 deg and 10 deg should interpolate through 0/360, not through 180.
    p = WindProfile(
        altitudes_agl_m=np.array([0.0, 100.0]),
        speed_ms=np.array([10.0, 10.0]),
        direction_deg=np.array([350.0, 10.0]),
        source="test",
    )
    _, direction = p.sample(np.array([50.0]))
    d = direction[0]
    assert (d > 355.0) or (d < 5.0)  # near 0/360, not ~180


def test_calm_profile_zero_drift():
    u, v = WindProfile.calm().drift_vectors(np.array([100.0, 2000.0]))
    assert np.allclose(u, 0.0) and np.allclose(v, 0.0)
