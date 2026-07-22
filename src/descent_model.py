"""Descent-rate model, three tiers (plan section 4.1):

1. ``constant``  — terminal velocity from drag coefficients in ``rocket_config.json``,
   switching drogue/main by ``FlightState``, using sea-level ISA density.
2. ``banded``    — same terminal-velocity relation but with ISA air density evaluated at
   each altitude (thinner air lower down changes the rate).
3. ``fitted``    — piecewise-linear descent-rate-vs-altitude curve fitted from a real
   recorded descent (``sim/known_flights/``), preferred when available.

Nothing here touches Helios protos, so it is fully unit-testable offline. ``FlightState``
is referenced only by its integer value (3 = DROGUE_DESCENT, 4 = MAIN_DESCENT) to avoid a
generated-proto import in the pure model.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# FlightState enum integer values (from falcon-protos/TelemetryPacket.proto).
DROGUE_DESCENT = 3
MAIN_DESCENT = 4

G = 9.80665  # m/s^2

# International Standard Atmosphere, troposphere (0–11 km) — enough for a sub-orbital descent.
_ISA_RHO0 = 1.225      # kg/m^3 at sea level
_ISA_T0 = 288.15       # K
_ISA_L = 0.0065        # K/m lapse rate
_ISA_P0 = 101325.0     # Pa
_ISA_R = 287.058       # J/(kg*K)


def isa_density(altitude_msl_m: float) -> float:
    """ISA air density (kg/m^3) at a geopotential altitude, clamped to the troposphere."""
    h = max(0.0, min(altitude_msl_m, 11000.0))
    temp = _ISA_T0 - _ISA_L * h
    pressure = _ISA_P0 * (temp / _ISA_T0) ** (G / (_ISA_R * _ISA_L))
    return pressure / (_ISA_R * temp)


def _opt_float(value) -> float | None:
    return None if value is None else float(value)


def terminal_velocity(mass_kg: float, drag_coeff: float, area_m2: float, rho: float) -> float:
    """Steady-state descent speed (m/s, positive downward): v = sqrt(2 m g / (rho Cd A))."""
    denom = rho * drag_coeff * area_m2
    if denom <= 0:
        raise ValueError("rho * drag_coeff * area must be positive")
    return float(np.sqrt(2.0 * mass_kg * G / denom))


@dataclass(frozen=True)
class RocketConfig:
    """Fixed rocket + launch-site constants, read from the mounted mission config at startup.

    The mission config file (bound into the container at ``/app/config/rocket_config.json``)
    is the shared launcher config; only the physical/launch fields are consumed here:
    drag coefficients, canopy reference areas, mass, and the expected ground station. Descent
    happens after burnout, so ``mass_kg`` is taken from ``dry_mass_kg`` (propellant expended).
    ``main_deploy_alt_agl_m`` lets the predictor switch drogue->main rate mid-descent while the
    current state is still DROGUE_DESCENT; it is not in the mission file, so it defaults here.
    """

    drogue_drag_coeff: float
    main_drag_coeff: float
    mass_kg: float
    drogue_area_m2: float
    main_area_m2: float
    main_deploy_alt_agl_m: float = 450.0
    # From ``expected_ground_station`` — used to seed the wind fetch location and the
    # ground-altitude reference before live SRAD ``ground_altitude`` arrives.
    launch_lat: float | None = None
    launch_lon: float | None = None
    ground_alt_msl_m: float | None = None
    expected_apogee_m: float | None = None

    @classmethod
    def defaults(cls) -> RocketConfig:
        """Reasonable stand-ins when the mission config file is absent (local dev/tests)."""
        return cls(
            drogue_drag_coeff=0.85,
            main_drag_coeff=2.2,
            mass_kg=16.61,
            drogue_area_m2=0.87,
            main_area_m2=1.72,
        )

    @classmethod
    def load(cls, path: str | Path = "/app/config/rocket_config.json") -> RocketConfig:
        p = Path(path)
        if not p.exists():
            logger.warning("mission config %s not found; using built-in defaults", p)
            return cls.defaults()
        data = json.loads(p.read_text())
        gs = data.get("expected_ground_station", {}) or {}
        # Descent mass = dry mass (propellant expended); fall back to wet, then a default.
        mass = data.get("dry_mass_kg", data.get("mass_kg", data.get("wet_mass_kg", 16.61)))
        return cls(
            drogue_drag_coeff=float(data["drogue_drag_coeff"]),
            main_drag_coeff=float(data["main_drag_coeff"]),
            mass_kg=float(mass),
            drogue_area_m2=float(data.get("drogue_area_m2", 0.87)),
            main_area_m2=float(data.get("main_area_m2", 1.72)),
            main_deploy_alt_agl_m=float(data.get("main_deploy_alt_agl_m", 450.0)),
            launch_lat=_opt_float(gs.get("lat")),
            launch_lon=_opt_float(gs.get("lon")),
            ground_alt_msl_m=_opt_float(gs.get("alt_m")),
            expected_apogee_m=_opt_float(data.get("expected_apogee_m")),
        )


class DescentModel:
    """Returns descent rate (m/s, positive down) as a function of altitude and effective state.

    ``mode`` selects the tier. ``fitted`` requires a fitted curve (see :meth:`fit_curve`);
    if unset it transparently falls back to ``banded``.
    """

    def __init__(self, rocket: RocketConfig, mode: str = "banded") -> None:
        self.rocket = rocket
        self.mode = mode
        self._fit_alts: np.ndarray | None = None   # altitude AGL (m), ascending
        self._fit_rates: np.ndarray | None = None   # descent rate (m/s) at those altitudes

    # --- tier 3: fit from real flight data ------------------------------------------------
    def fit_curve(self, altitudes_agl_m: np.ndarray, descent_rates_ms: np.ndarray) -> None:
        """Store a piecewise-linear descent-rate-vs-altitude curve from recorded data."""
        alts = np.asarray(altitudes_agl_m, dtype=float)
        rates = np.asarray(descent_rates_ms, dtype=float)
        if alts.size < 2:
            raise ValueError("need >= 2 points to fit a descent-rate curve")
        order = np.argsort(alts)
        self._fit_alts = alts[order]
        self._fit_rates = rates[order]

    @staticmethod
    def fit_from_track(times_s: np.ndarray, altitudes_agl_m: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Derive (altitude, descent_rate) samples from a timestamped descent track.

        Descent rate at a point is ``-d(alt)/dt`` (positive while falling). Only the
        descending portion (rate > 0) is returned.
        """
        t = np.asarray(times_s, dtype=float)
        a = np.asarray(altitudes_agl_m, dtype=float)
        order = np.argsort(t)
        t, a = t[order], a[order]
        dt = np.diff(t)
        dt[dt == 0] = np.nan
        rate = -np.diff(a) / dt              # positive while descending
        mid_alt = 0.5 * (a[:-1] + a[1:])
        mask = np.isfinite(rate) & (rate > 0)
        return mid_alt[mask], rate[mask]

    # --- rate lookup ----------------------------------------------------------------------
    def _terminal(self, state: int, altitude_msl_m: float) -> float:
        rho = _ISA_RHO0 if self.mode == "constant" else isa_density(altitude_msl_m)
        if state == MAIN_DESCENT:
            cd, area = self.rocket.main_drag_coeff, self.rocket.main_area_m2
        else:  # DROGUE_DESCENT (and any pre-main descent state)
            cd, area = self.rocket.drogue_drag_coeff, self.rocket.drogue_area_m2
        return terminal_velocity(self.rocket.mass_kg, cd, area, rho)

    def descent_rate(self, altitude_msl_m: float, state: int) -> float:
        """Descent rate (m/s, positive down) at an MSL altitude for the given effective state."""
        if self.mode == "fitted" and self._fit_alts is not None and self._fit_rates is not None:
            # fitted curve is indexed by AGL; here altitude arg is MSL, but the fit was built
            # from AGL — callers pass AGL-consistent values (ground ~ 0). np.interp clamps.
            return float(np.interp(altitude_msl_m, self._fit_alts, self._fit_rates))
        return self._terminal(state, altitude_msl_m)

    def effective_state(self, altitude_agl_m: float, current_state: int) -> int:
        """Drogue->main switch: if still on drogue but below main-deploy altitude, use main."""
        if current_state == DROGUE_DESCENT and altitude_agl_m <= self.rocket.main_deploy_alt_agl_m:
            return MAIN_DESCENT
        return current_state
