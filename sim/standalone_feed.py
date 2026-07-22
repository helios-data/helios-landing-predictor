"""Canned descent for STANDALONE mode (no core connection).

Generates a synthetic full descent (STANDBY -> ASCENT -> DROGUE -> MAIN -> LANDED) as a
sequence of ``(Fix, flight_state, ground_alt_msl)`` tuples, so the prediction engine and
FlightState gating can be exercised end-to-end without Helios. Launch site is Spaceport
America-ish (ground ~1400 m MSL); the descent drifts gently east.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

from src.descent_model import DROGUE_DESCENT, MAIN_DESCENT
from src.generated import FlightState
from src.helios_bridge import Fix

LAUNCH_LAT = 32.9903
LAUNCH_LON = -106.9750
GROUND_ALT_MSL = 1400.0
APOGEE_AGL = 3000.0
MAIN_DEPLOY_AGL = 450.0


def canned_descent() -> Iterator[tuple[Fix, int, float]]:
    counter = 0

    # A few pre-descent idle frames.
    for state in (FlightState.STANDBY, FlightState.ASCENT, FlightState.MACH_LOCK):
        counter += 1
        yield _fix(counter, LAUNCH_LAT, LAUNCH_LON, GROUND_ALT_MSL + APOGEE_AGL), int(state), GROUND_ALT_MSL

    # Descent: integrate downward, drifting east ~5 m per step.
    alt_agl = APOGEE_AGL
    lat, lon = LAUNCH_LAT, LAUNCH_LON
    while alt_agl > 0:
        state = DROGUE_DESCENT if alt_agl > MAIN_DEPLOY_AGL else MAIN_DESCENT
        step = 120.0 if state == DROGUE_DESCENT else 20.0
        alt_agl = max(0.0, alt_agl - step)
        lon += 0.00007  # small eastward drift
        counter += 1
        yield _fix(counter, lat, lon, GROUND_ALT_MSL + alt_agl), state, GROUND_ALT_MSL

    # Final LANDED frame.
    counter += 1
    yield _fix(counter, lat, lon, GROUND_ALT_MSL), int(FlightState.LANDED), GROUND_ALT_MSL


def _fix(counter: int, lat: float, lon: float, alt_msl: float) -> Fix:
    return Fix(
        lat=lat,
        lon=lon,
        alt_msl_m=alt_msl,
        timestamp_ms=int(time.time() * 1000),
        recv_monotonic=time.monotonic(),
        source="srad",
        counter=counter,
    )
