"""Entrypoint: wire the Helios bridge, prediction engine, and publisher together.

Tasks:
  * bridge.run()          — subscribe to both telemetry streams, track state/fixes
  * prediction_loop()     — FlightState-gated, throttled recompute + publish
  * config_loop()         — accept PredictionConfig commands over the wire, ack them
  * health server (8091)  — minimal debug/health surface (optional)

Env flags (sibling-repo convention):
  VERBOSE=1     debug logging
  STANDALONE=1  no core connection; drive the engine from a canned descent (sim/standalone_feed)
  CORE_ADDRESS / CORE_PORT   override Helios core location (defaults Helios:5000)
  HEALTH_PORT   override 8091; HEALTH_DISABLE=1 to skip the HTTP surface
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from src.config import PredictionConfig
from src.descent_model import RocketConfig
from src.engine import PredictionEngine
from src.generated import PredictionConfig as PredictionConfigProto
from src.helios_bridge import HeliosBridge
from src.publisher import NODE_ADDRESS, Publisher

logger = logging.getLogger("landing_predictor")

IDLE_HEARTBEAT_S = 5.0
CONFIG_EVENT = "config"


def _setup_logging() -> None:
    level = logging.DEBUG if os.environ.get("VERBOSE") else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def prediction_loop(bridge: HeliosBridge, engine: PredictionEngine, publisher: Publisher) -> None:
    last_compute = 0.0
    last_idle = 0.0
    while True:
        try:
            await asyncio.wait_for(bridge.updated.wait(), timeout=1.0)
        except TimeoutError:
            pass
        bridge.updated.clear()

        now = time.monotonic()
        fs = bridge.flight_state
        if engine.frozen:
            continue

        if engine.is_idle(fs):
            if now - last_idle >= IDLE_HEARTBEAT_S:
                await publisher.publish_prediction(engine.idle_frame(fs))
                last_idle = now
            continue

        if engine.is_active(fs):
            if now - last_compute < engine.config.recompute_min_interval_s:
                continue
            fix = bridge.current_fix()
            if fix is None:
                continue
            try:
                pred = await engine.predict(fix, fs, bridge.state.ground_alt_msl_m)
                await publisher.publish_prediction(pred)
                last_compute = now
            except Exception as exc:
                logger.exception("prediction failed: %s", exc)


async def config_loop(client, engine: PredictionEngine, publisher: Publisher) -> None:
    backoff = 1.0
    while True:
        try:
            async with client.subscribe_event(address=NODE_ADDRESS, event_name=CONFIG_EVENT) as events:
                backoff = 1.0
                async for ev in events:
                    if ev is None or not ev.data:
                        continue
                    try:
                        proto = PredictionConfigProto.parse(ev.data)
                        engine.update_config(engine.config.with_proto(proto))
                        await publisher.publish_config_ack(engine.config)
                        logger.info("applied PredictionConfig update: %s", engine.config)
                    except Exception as exc:
                        logger.warning("bad PredictionConfig command: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("config subscription dropped: %s; retry in %.1fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


async def start_health_server(bridge: HeliosBridge) -> asyncio.Task | None:
    if os.environ.get("HEALTH_DISABLE"):
        return None
    try:
        import uvicorn
        from fastapi import FastAPI
    except Exception:
        logger.info("FastAPI/uvicorn not available; skipping health server")
        return None

    app = FastAPI()

    @app.get("/health")
    async def health() -> dict:
        s = bridge.state
        fix = bridge.current_fix()
        return {
            "flight_state": s.flight_state,
            "current_source": fix.source if fix else None,
            "ground_alt_msl_m": s.ground_alt_msl_m,
            "srad_packets": s.srad_packets,
            "cots_packets": s.cots_packets,
            "srad_errors": s.srad_errors,
            "cots_errors": s.cots_errors,
        }

    port = int(os.environ.get("HEALTH_PORT", "8091"))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    return asyncio.create_task(server.serve())


async def run_standalone(engine: PredictionEngine, publisher: Publisher | None) -> None:
    """Drive the engine from a canned descent — no core connection (STANDALONE=1)."""
    from sim.standalone_feed import canned_descent

    logger.info("STANDALONE mode: driving engine from canned descent")
    for fix, flight_state, ground_alt in canned_descent():
        if engine.is_idle(flight_state):
            pred = engine.idle_frame(flight_state)
        elif engine.is_active(flight_state):
            pred = await engine.predict(fix, flight_state, ground_alt)
        else:
            continue
        logger.info(
            "state=%s status=%s best=%s",
            flight_state,
            pred.status,
            (round(pred.best_estimate.lat, 5), round(pred.best_estimate.lon, 5))
            if pred.best_estimate
            else None,
        )
        await asyncio.sleep(0.2)
    logger.info("STANDALONE descent complete")


async def async_main() -> None:
    _setup_logging()
    standalone = bool(os.environ.get("STANDALONE"))
    if standalone:
        # No mounted mission config in standalone dev — use built-in hardcoded constants.
        rocket = RocketConfig.defaults()
    else:
        rocket = RocketConfig.load(os.environ.get("ROCKET_CONFIG", "/app/config/rocket_config.json"))
    config = PredictionConfig.from_env()
    engine = PredictionEngine(
        rocket=rocket,
        config=config,
        descent_mode=os.environ.get("DESCENT_MODE", "banded"),
        standalone=standalone,
    )

    if standalone:
        await run_standalone(engine, None)
        return

    from helios import HeliosClient

    core_address = os.environ.get("CORE_ADDRESS", "Helios")
    core_port = int(os.environ.get("CORE_PORT", "5000"))
    client = HeliosClient(core_address=core_address, core_port=core_port, node_uri=NODE_ADDRESS)

    # Reconnect loop around the whole client session.
    backoff = 1.0
    while True:
        try:
            await client.connect()
            logger.info("connected to Helios core at %s:%s", core_address, core_port)
            backoff = 1.0
            bridge = HeliosBridge(client, srad_stale_timeout_s=config.srad_stale_timeout_s)
            # Seed the ground-altitude reference from the mission config until SRAD's live
            # ground_altitude arrives (keeps AGL sane for early COTS-only fixes).
            if rocket.ground_alt_msl_m is not None:
                bridge.state.ground_alt_msl_m = rocket.ground_alt_msl_m
            publisher = Publisher(client)
            health_task = await start_health_server(bridge)
            tasks = [
                asyncio.create_task(bridge.run()),
                asyncio.create_task(prediction_loop(bridge, engine, publisher)),
                asyncio.create_task(config_loop(client, engine, publisher)),
            ]
            if health_task:
                tasks.append(health_task)
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("session error: %s; reconnecting in %.1fs", exc, backoff)
            try:
                await client.disconnect()
            except Exception:
                pass
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
