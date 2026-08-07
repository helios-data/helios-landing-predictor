"""Publish landing predictions (and config acks) as Helios events.

Event routing (plan section 4.4): ``LandingPrediction`` bytes are published with
``event_name="landing_prediction"`` on this node's own address,
``Helios.Services.LandingPredictor``. A ``PredictionConfig`` ack is published on
``event_name="prediction_config"`` whenever the runtime config changes.
"""

from __future__ import annotations

import logging
import time

from src.config import PredictionConfig
from src.generated import LandingPoint, LandingPrediction
from src.predictor import PredictionResult

logger = logging.getLogger(__name__)

NODE_ADDRESS = "Helios.Services.LandingPredictor"
PREDICTION_EVENT = "landing_prediction"
CONFIG_ACK_EVENT = "prediction_config"

STATUS_NOT_DESCENDING = "not_descending"
STATUS_PREDICTING = "predicting"
STATUS_FINAL = "final"


def _points(pairs: list[tuple[float, float]]) -> list[LandingPoint]:
    return [LandingPoint(lat=lat, lon=lon) for lat, lon in pairs]


def build_prediction(
    result: PredictionResult,
    *,
    based_on_counter: int,
    final: bool,
    current_lat: float,
    current_lon: float,
    current_alt_agl_m: float,
    current_source: str,
    flight_state: int,
) -> LandingPrediction:
    best_lat, best_lon = result.best_estimate
    return LandingPrediction(
        based_on_packet_counter=based_on_counter,
        computed_at_ms=int(time.time() * 1000),
        final=final,
        best_estimate=LandingPoint(lat=best_lat, lon=best_lon),
        dispersion_cloud=_points(result.dispersion_cloud),
        ellipse_50=_points(result.ellipse_50),
        ellipse_90=_points(result.ellipse_90),
        current_lat=current_lat,
        current_lon=current_lon,
        current_source=current_source,
        wind_source=result.wind_source,
        descent_model=result.descent_model,
        current_alt_agl=current_alt_agl_m,
        flight_state=float(flight_state),
        status=STATUS_FINAL if final else STATUS_PREDICTING,
        wind_speed_ms=result.wind_speed_ms,
        wind_dir_deg=result.wind_dir_deg,
    )


def build_idle(flight_state: int) -> LandingPrediction:
    """Low-rate heartbeat so consumers can distinguish 'off' from 'not connected'."""
    return LandingPrediction(
        computed_at_ms=int(time.time() * 1000),
        final=False,
        flight_state=float(flight_state),
        status=STATUS_NOT_DESCENDING,
    )


class Publisher:
    def __init__(self, client) -> None:
        self.client = client

    async def publish_prediction(self, prediction: LandingPrediction) -> None:
        await self.client.publish_event(
            event_name=PREDICTION_EVENT,
            data=bytes(prediction),
            override_address=NODE_ADDRESS,
        )

    async def publish_config_ack(self, config: PredictionConfig) -> None:
        await self.client.publish_event(
            event_name=CONFIG_ACK_EVENT,
            data=bytes(config.to_proto()),
            override_address=NODE_ADDRESS,
        )
