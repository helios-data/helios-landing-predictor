import numpy as np

from src.descent_model import (
    DROGUE_DESCENT,
    MAIN_DESCENT,
    DescentModel,
    RocketConfig,
    isa_density,
    terminal_velocity,
)

ROCKET = RocketConfig(
    drogue_drag_coeff=1.5,
    main_drag_coeff=2.2,
    mass_kg=22.0,
    drogue_area_m2=0.5,
    main_area_m2=7.0,
    main_deploy_alt_agl_m=450.0,
)


def test_isa_density_decreases_with_altitude():
    assert isa_density(0) > isa_density(1500) > isa_density(3000)
    assert abs(isa_density(0) - 1.225) < 1e-3


def test_terminal_velocity_relation():
    # v = sqrt(2 m g / (rho Cd A))
    v = terminal_velocity(22.0, 2.2, 7.0, 1.225)
    expected = np.sqrt(2 * 22.0 * 9.80665 / (1.225 * 2.2 * 7.0))
    assert abs(v - expected) < 1e-9


def test_main_descent_slower_than_drogue():
    m = DescentModel(ROCKET, mode="banded")
    drogue = m.descent_rate(2000, DROGUE_DESCENT)
    main = m.descent_rate(2000, MAIN_DESCENT)
    assert main < drogue  # bigger, higher-drag canopy => slower fall


def test_banded_faster_higher_up():
    # Thinner air aloft => higher terminal velocity for the same drag.
    m = DescentModel(ROCKET, mode="banded")
    assert m.descent_rate(3000, DROGUE_DESCENT) > m.descent_rate(0, DROGUE_DESCENT)


def test_constant_mode_altitude_independent():
    m = DescentModel(ROCKET, mode="constant")
    assert m.descent_rate(3000, DROGUE_DESCENT) == m.descent_rate(0, DROGUE_DESCENT)


def test_effective_state_switches_to_main_below_deploy():
    m = DescentModel(ROCKET, mode="banded")
    assert m.effective_state(1000, DROGUE_DESCENT) == DROGUE_DESCENT
    assert m.effective_state(400, DROGUE_DESCENT) == MAIN_DESCENT
    assert m.effective_state(400, MAIN_DESCENT) == MAIN_DESCENT


def test_fit_from_track_recovers_constant_rate():
    # Fall at a constant 5 m/s from 500 m.
    t = np.arange(0, 100, 1.0)
    alt = 500 - 5.0 * t
    alt = np.clip(alt, 0, None)
    alts, rates = DescentModel.fit_from_track(t, alt)
    assert np.allclose(rates[rates > 0], 5.0, atol=1e-6)

    m = DescentModel(ROCKET, mode="fitted")
    m.fit_curve(alts, rates)
    assert abs(m.descent_rate(250, DROGUE_DESCENT) - 5.0) < 1e-6


def test_fitted_falls_back_to_banded_when_no_curve():
    m = DescentModel(ROCKET, mode="fitted")  # no fit_curve called
    banded = DescentModel(ROCKET, mode="banded")
    assert m.descent_rate(2000, DROGUE_DESCENT) == banded.descent_rate(2000, DROGUE_DESCENT)
