"""Prediction orchestration + FlightState gating (plan section 3, decoupled from I/O).

The engine owns the *decision* of when a prediction is meaningful and the *composition* of
descent model + wind model + Monte Carlo into a publishable ``LandingPrediction``. It holds
no sockets, so the idle -> active -> frozen state machine is unit-testable by feeding it
fixes and flight states directly.
"""

from __future__ import annotations

import logging

from src.config import PredictionConfig
from src.descent_model import DescentModel, RocketConfig
from src.generated import FlightState, LandingPrediction
from src.helios_bridge import Fix
from src.predictor import predict
from src.publisher import build_idle, build_prediction
from src.wind_model import LiveWindSource, WindProfile, WindSource

logger = logging.getLogger(__name__)

_IDLE_STATES = {int(FlightState.STANDBY), int(FlightState.ASCENT), int(FlightState.MACH_LOCK)}
_ACTIVE_STATES = {int(FlightState.DROGUE_DESCENT), int(FlightState.MAIN_DESCENT)}
_LANDED = int(FlightState.LANDED)


class PredictionEngine:
    def __init__(
        self,
        *,
        rocket: RocketConfig,
        config: PredictionConfig,
        descent_mode: str = "banded",
        standalone: bool = False,
    ) -> None:
        self.rocket = rocket
        self.config = config
        self.descent_model = DescentModel(rocket, mode=descent_mode)
        self.standalone = standalone
        self.wind_source: WindSource | None = None
        self.frozen = False
        self._final_published = False

    # --- gating ----------------------------------------------------------------------------
    @staticmethod
    def is_idle(flight_state: int) -> bool:
        return flight_state in _IDLE_STATES

    def is_active(self, flight_state: int) -> bool:
        return flight_state in _ACTIVE_STATES or (
            flight_state == _LANDED and not self.frozen
        )

    def is_final_tick(self, flight_state: int) -> bool:
        return flight_state == _LANDED and not self.frozen

    # --- wind source -----------------------------------------------------------------------
    def _ensure_wind_source(self, lat: float, lon: float) -> None:
        if self.wind_source is not None or self.standalone:
            return
        if self.config.wind_source_mode == "live":
            # Prefer the configured ground-station coordinates; fall back to the live fix.
            wlat = self.rocket.launch_lat if self.rocket.launch_lat is not None else lat
            wlon = self.rocket.launch_lon if self.rocket.launch_lon is not None else lon
            self.wind_source = LiveWindSource(wlat, wlon)

    async def _wind_profile(self, lat: float, lon: float) -> WindProfile:
        if self.standalone or self.config.wind_source_mode != "live":
            return WindProfile.calm(source="standalone" if self.standalone else "disabled")
        self._ensure_wind_source(lat, lon)
        assert self.wind_source is not None
        return await self.wind_source.get_profile()

    # --- compute ---------------------------------------------------------------------------
    def idle_frame(self, flight_state: int) -> LandingPrediction:
        return build_idle(flight_state)

    async def predict(
        self, fix: Fix, flight_state: int, ground_alt_msl_m: float
    ) -> LandingPrediction:
        """Compute one prediction for the given fix and freeze after the LANDED tick."""
        final = self.is_final_tick(flight_state)
        alt_agl = fix.alt_msl_m - ground_alt_msl_m
        profile = await self._wind_profile(fix.lat, fix.lon)

        result = predict(
            current_lat=fix.lat,
            current_lon=fix.lon,
            altitude_agl_m=max(alt_agl, 0.0),
            ground_alt_msl_m=ground_alt_msl_m,
            state=flight_state,
            descent_model=self.descent_model,
            wind_profile=profile,
            config=self.config,
        )
        prediction = build_prediction(
            result,
            based_on_counter=fix.counter,
            final=final,
            current_lat=fix.lat,
            current_lon=fix.lon,
            current_alt_agl_m=alt_agl,
            current_source=fix.source,
            flight_state=flight_state,
        )
        if final:
            self.frozen = True
            self._final_published = True
            logger.info("LANDED: published final prediction, freezing")
        return prediction

    def update_config(self, config: PredictionConfig) -> None:
        self.config = config
        # Rebuild wind source if the mode changed; keep coords by dropping the cache.
        if config.wind_source_mode != "live":
            self.wind_source = None
