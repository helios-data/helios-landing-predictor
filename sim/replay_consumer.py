"""End-to-end test consumer: subscribe to this node's landing_prediction output and print it.

Usage (three terminals against a running Helios core):
  1. helios-mission-control's ``sim/replay.py`` publishes a synthetic flight.
     NOTE: that simulator currently publishes on the OLD addresses
     ``Helios.FALCON.Telemetry`` / ``Helios.Services.TeleGPS``. This node subscribes to the
     RENAMED addresses ``Helios.FALCON.SRAD_Telemetry`` / ``Helios.FALCON.APRS_Telemetry``
     (see README "Coordination with other repos"). Until that rename lands, either update
     replay.py's addresses or set SRAD_ADDRESS/COTS_ADDRESS overrides.
  2. ``make run`` runs the predictor.
  3. ``uv run python -m sim.replay_consumer`` (this script) prints predictions as they arrive.
"""

from __future__ import annotations

import asyncio
import os

from helios import HeliosClient

from src.generated import LandingPrediction
from src.publisher import NODE_ADDRESS, PREDICTION_EVENT


async def main() -> None:
    client = HeliosClient(
        core_address=os.environ.get("CORE_ADDRESS", "Helios"),
        core_port=int(os.environ.get("CORE_PORT", "5000")),
        node_uri="Helios.Services.LandingPredictor.ReplayConsumer",
    )
    await client.connect()
    print(f"subscribed to {NODE_ADDRESS}/{PREDICTION_EVENT}")
    async with client.subscribe_event(address=NODE_ADDRESS, event_name=PREDICTION_EVENT) as events:
        async for ev in events:
            if not ev or not ev.data:
                continue
            p = LandingPrediction.parse(ev.data)
            best = (round(p.best_estimate.lat, 6), round(p.best_estimate.lon, 6)) if p.best_estimate else None
            print(
                f"[{p.status}] counter={p.based_on_packet_counter} src={p.current_source} "
                f"alt_agl={p.current_alt_agl:.0f}m model={p.descent_model} wind={p.wind_source} "
                f"best={best} ell90={len(p.ellipse_90)}pts final={p.final}"
            )


if __name__ == "__main__":
    asyncio.run(main())
