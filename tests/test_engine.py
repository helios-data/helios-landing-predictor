import time

from src.config import PredictionConfig
from src.descent_model import DROGUE_DESCENT, MAIN_DESCENT, RocketConfig
from src.engine import PredictionEngine
from src.generated import FlightState
from src.helios_bridge import Fix

ROCKET = RocketConfig(
    drogue_drag_coeff=1.5,
    main_drag_coeff=2.2,
    mass_kg=22.0,
    drogue_area_m2=0.5,
    main_area_m2=7.0,
)


def _engine():
    # standalone=True => calm wind, no network in tests.
    return PredictionEngine(
        rocket=ROCKET, config=PredictionConfig(seed=1, mc_iterations=200), standalone=True
    )


def _fix(alt_msl=3900.0):
    return Fix(
        lat=33.0,
        lon=-107.0,
        alt_msl_m=alt_msl,
        timestamp_ms=int(time.time() * 1000),
        recv_monotonic=time.monotonic(),
        source="srad",
        counter=42,
    )


def test_idle_states_are_idle():
    e = _engine()
    for s in (FlightState.STANDBY, FlightState.ASCENT, FlightState.MACH_LOCK):
        assert e.is_idle(int(s))
        assert not e.is_active(int(s))


def test_descent_states_are_active():
    e = _engine()
    assert e.is_active(DROGUE_DESCENT)
    assert e.is_active(MAIN_DESCENT)
    assert not e.is_idle(DROGUE_DESCENT)


def test_idle_frame_status():
    e = _engine()
    frame = e.idle_frame(int(FlightState.ASCENT))
    assert frame.status == "not_descending"
    assert not frame.final


async def test_active_predict_produces_estimate():
    e = _engine()
    pred = await e.predict(_fix(), DROGUE_DESCENT, 1400.0)
    assert pred.status == "predicting"
    assert not pred.final
    assert pred.based_on_packet_counter == 42
    assert pred.current_source == "srad"
    assert len(pred.ellipse_90) > 2


async def test_landed_freezes_after_final():
    e = _engine()
    assert e.is_active(int(FlightState.LANDED))  # not yet frozen
    pred = await e.predict(_fix(alt_msl=1400.0), int(FlightState.LANDED), 1400.0)
    assert pred.final
    assert pred.status == "final"
    assert e.frozen
    # Once frozen, LANDED is no longer "active" (loop stops recomputing).
    assert not e.is_active(int(FlightState.LANDED))


async def test_config_update_applies_partial():
    from src.generated import PredictionConfig as Proto

    e = _engine()
    e.update_config(e.config.with_proto(Proto(mc_iterations=1234)))
    assert e.config.mc_iterations == 1234
    # unspecified fields unchanged
    assert e.config.wind_source_mode == "live"


async def test_full_flight_gating_sequence():
    """idle -> active -> frozen across a realistic state sequence."""
    e = _engine()
    published = []
    for state in (FlightState.STANDBY, FlightState.ASCENT, FlightState.DROGUE_DESCENT,
                  FlightState.MAIN_DESCENT, FlightState.LANDED, FlightState.LANDED):
        s = int(state)
        if e.frozen:
            published.append(("frozen", None))
        elif e.is_idle(s):
            published.append(("idle", e.idle_frame(s).status))
        elif e.is_active(s):
            pred = await e.predict(_fix(), s, 1400.0)
            published.append(("active", pred.status))
    statuses = [p[0] for p in published]
    assert statuses == ["idle", "idle", "active", "active", "active", "frozen"]
    assert published[-2][1] == "final"
