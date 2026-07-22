import numpy as np

from src.config import PredictionConfig
from src.descent_model import DROGUE_DESCENT, DescentModel, RocketConfig
from src.predictor import predict
from src.wind_model import WindProfile

ROCKET = RocketConfig(
    drogue_drag_coeff=1.5,
    main_drag_coeff=2.2,
    mass_kg=22.0,
    drogue_area_m2=0.5,
    main_area_m2=7.0,
)


def _model():
    return DescentModel(ROCKET, mode="banded")


def _west_wind(speed=10.0):
    return WindProfile(
        altitudes_agl_m=np.array([0.0, 3000.0]),
        speed_ms=np.array([speed, speed]),
        direction_deg=np.array([270.0, 270.0]),
        source="test",
    )


def _run(config, wind=None, alt=2500.0, state=DROGUE_DESCENT):
    return predict(
        current_lat=33.0,
        current_lon=-107.0,
        altitude_agl_m=alt,
        ground_alt_msl_m=1400.0,
        state=state,
        descent_model=_model(),
        wind_profile=wind or _west_wind(),
        config=config,
    )


def test_west_wind_moves_estimate_east():
    res = _run(PredictionConfig(seed=1, mc_iterations=500))
    assert res.drift_east_m > 0
    assert res.best_estimate[1] > -107.0  # longitude increased => east


def test_seed_is_reproducible():
    a = _run(PredictionConfig(seed=7, mc_iterations=400))
    b = _run(PredictionConfig(seed=7, mc_iterations=400))
    assert a.ellipse_90 == b.ellipse_90
    assert a.dispersion_cloud == b.dispersion_cloud


def test_ellipse_90_contains_more_area_than_50():
    res = _run(PredictionConfig(seed=3, mc_iterations=800))

    def _area(poly):
        lat = np.array([p[0] for p in poly])
        lon = np.array([p[1] for p in poly])
        return 0.5 * abs(np.dot(lon, np.roll(lat, 1)) - np.dot(lat, np.roll(lon, 1)))

    assert _area(res.ellipse_90) > _area(res.ellipse_50)


def test_cloud_is_sparse():
    res = _run(PredictionConfig(seed=3, mc_iterations=1000, dispersion_cloud_size=150))
    assert len(res.dispersion_cloud) == 150


def test_calm_wind_lands_near_current_position():
    res = _run(PredictionConfig(seed=2, mc_iterations=300), wind=WindProfile.calm(), alt=1000.0)
    assert abs(res.best_estimate[0] - 33.0) < 1e-4
    assert abs(res.best_estimate[1] - (-107.0)) < 1e-4


def test_zero_wind_bigger_error_bars_widen_ellipse():
    tight = _run(
        PredictionConfig(seed=5, mc_iterations=800, wind_speed_error_pct=0.05, wind_dir_error_deg=5),
    )
    loose = _run(
        PredictionConfig(seed=5, mc_iterations=800, wind_speed_error_pct=0.4, wind_dir_error_deg=40),
    )

    def _spread(res):
        lat = np.array([p[0] for p in res.ellipse_90])
        lon = np.array([p[1] for p in res.ellipse_90])
        return (lat.max() - lat.min()) + (lon.max() - lon.min())

    assert _spread(loose) > _spread(tight)


def test_already_landed_returns_current_point():
    res = _run(PredictionConfig(seed=1), alt=0.0)
    assert res.best_estimate == (33.0, -107.0)
    assert res.ellipse_50 == [(33.0, -107.0)]
