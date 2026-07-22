"""Helios ingress: subscribe to both telemetry streams, track state, expose the current fix.

Implements plan section 3:

* Subscribe to **both** ``telemetry`` (SRAD) and ``aprs`` (COTS) for the life of the process;
  ``get_event`` once on each at connect to seed state immediately.
* Keep a **2-point rolling history per source** so a heading/velocity estimate is available
  the instant SRAD fails over to COTS — no waiting for two fresh APRS packets.
* **SRAD primary, COTS fallback**: prefer the latest SRAD fix; fall back to COTS when SRAD
  is stale (no packet within ``srad_stale_timeout_s``) or its position is invalid.
* Track ``FlightState`` from SRAD packets (APRS carries none); hold last state if SRAD drops.
* Reconnect with backoff.

The predictor loop (``main.py``) reads :meth:`current_fix` / :attr:`flight_state`; this class
owns no prediction logic.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

from src.generated import FlightState, TelemetryPacket

try:  # AprsPacket ships pre-generated in the SDK (same import path as helios-dashboard)
    from helios.generated.helios.transport import AprsPacket
except Exception:  # pragma: no cover - only if SDK generated code is missing
    AprsPacket = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

SRAD_ADDRESS = "Helios.FALCON.SRAD_Telemetry"
SRAD_EVENT = "telemetry"
COTS_ADDRESS = "Helios.FALCON.APRS_Telemetry"
COTS_EVENT = "aprs"

_FT_TO_M = 0.3048
_MIN_EVENT_LEN = 15  # skip obviously-truncated frames (mirrors sibling repos)


@dataclass
class Fix:
    """A normalised position sample from either stream."""

    lat: float
    lon: float
    alt_msl_m: float
    timestamp_ms: int
    recv_monotonic: float
    source: str                 # "srad" | "cots"
    counter: int = 0


@dataclass
class BridgeState:
    srad_history: deque[Fix] = field(default_factory=lambda: deque(maxlen=2))
    cots_history: deque[Fix] = field(default_factory=lambda: deque(maxlen=2))
    flight_state: int = int(FlightState.STANDBY)
    ground_alt_msl_m: float = 0.0
    ground_alt_known: bool = False

    srad_packets: int = 0
    cots_packets: int = 0
    srad_errors: int = 0
    cots_errors: int = 0
    last_counter: int = 0


class HeliosBridge:
    def __init__(self, client, *, srad_stale_timeout_s: float = 3.0) -> None:
        self.client = client
        self.srad_stale_timeout_s = srad_stale_timeout_s
        self.state = BridgeState()
        # Signalled whenever a new fix from the *current* source arrives, so the predictor
        # loop can recompute promptly rather than polling.
        self.updated = asyncio.Event()

    # --- public accessors -------------------------------------------------------------------
    @property
    def flight_state(self) -> int:
        return self.state.flight_state

    def current_fix(self) -> Fix | None:
        """The fix that should drive prediction: SRAD if fresh & valid, else latest COTS."""
        now = time.monotonic()
        srad = self.state.srad_history[-1] if self.state.srad_history else None
        cots = self.state.cots_history[-1] if self.state.cots_history else None
        srad_fresh = srad is not None and (now - srad.recv_monotonic) <= self.srad_stale_timeout_s
        if srad_fresh:
            return srad
        return cots or srad  # fall back to COTS; if no COTS, stale SRAD is better than nothing

    def altitude_agl_m(self, fix: Fix) -> float:
        return fix.alt_msl_m - self.state.ground_alt_msl_m

    # --- run loop ---------------------------------------------------------------------------
    async def run(self) -> None:
        """Seed both streams, then subscribe to both concurrently, forever (with reconnect)."""
        await self._seed()
        await asyncio.gather(
            self._subscribe_loop(SRAD_ADDRESS, SRAD_EVENT, self._handle_srad, "srad"),
            self._subscribe_loop(COTS_ADDRESS, COTS_EVENT, self._handle_cots, "cots"),
        )

    async def _seed(self) -> None:
        for address, event, handler in (
            (SRAD_ADDRESS, SRAD_EVENT, self._handle_srad),
            (COTS_ADDRESS, COTS_EVENT, self._handle_cots),
        ):
            try:
                ev = await self.client.get_event(address=address, event_name=event)
                if ev is not None and ev.data:
                    handler(ev.data)
            except Exception as exc:
                logger.info("seed get_event(%s) failed (ok at startup): %s", address, exc)

    async def _subscribe_loop(self, address, event_name, handler, label) -> None:
        backoff = 1.0
        while True:
            try:
                async with self.client.subscribe_event(address=address, event_name=event_name) as events:
                    logger.info("subscribed to %s/%s", address, event_name)
                    backoff = 1.0
                    async for ev in events:
                        if ev is None or ev.data is None or len(ev.data) < _MIN_EVENT_LEN:
                            continue
                        try:
                            handler(ev.data)
                        except Exception as exc:
                            logger.warning("%s parse error: %s", label, exc)
                            if label == "srad":
                                self.state.srad_errors += 1
                            else:
                                self.state.cots_errors += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("%s subscription dropped: %s; retrying in %.1fs", label, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    # --- packet handlers --------------------------------------------------------------------
    def _handle_srad(self, data: bytes) -> None:
        pkt = TelemetryPacket.parse(data)
        self.state.srad_packets += 1
        self.state.flight_state = int(pkt.state)
        self.state.last_counter = int(pkt.counter)

        # Ground reference (used for AGL of both SRAD and COTS fixes).
        if pkt.ground_altitude:
            self.state.ground_alt_msl_m = float(pkt.ground_altitude)
            self.state.ground_alt_known = True

        if not self._srad_position_valid(pkt):
            return
        alt = self._srad_altitude_msl(pkt)
        fix = Fix(
            lat=float(pkt.gps_latitude),
            lon=float(pkt.gps_longitude),
            alt_msl_m=alt,
            timestamp_ms=int(pkt.timestamp_ms),
            recv_monotonic=time.monotonic(),
            source="srad",
            counter=int(pkt.counter),
        )
        self.state.srad_history.append(fix)
        self.updated.set()

    def _handle_cots(self, data: bytes) -> None:
        if AprsPacket is None:
            return
        pkt = AprsPacket.parse(data)
        self.state.cots_packets += 1
        pos = getattr(pkt, "position", None)
        if pos is None or not (pos.latitude or pos.longitude):
            return
        alt_msl = float(pos.altitude_ft) * _FT_TO_M if pos.altitude_ft else self.state.ground_alt_msl_m
        fix = Fix(
            lat=float(pos.latitude),
            lon=float(pos.longitude),
            alt_msl_m=alt_msl,
            timestamp_ms=int(time.time() * 1000),
            recv_monotonic=time.monotonic(),
            source="cots",
        )
        self.state.cots_history.append(fix)
        self.updated.set()

    # --- SRAD altitude / validity helpers ---------------------------------------------------
    @staticmethod
    def _srad_position_valid(pkt: TelemetryPacket) -> bool:
        return bool(pkt.gps_fix) and (pkt.gps_latitude != 0.0 or pkt.gps_longitude != 0.0)

    @staticmethod
    def _srad_altitude_msl(pkt: TelemetryPacket) -> float:
        """Baro-average altitude restricted to healthy barometers, else kf_altitude.

        Mirrors helios-mission-control's altitude rule (its plan section 3.1) so the two
        nodes agree on the headline altitude.
        """
        healthy = []
        if pkt.baro0_healthy:
            healthy.append(pkt.baro0_altitude)
        if pkt.baro1_healthy:
            healthy.append(pkt.baro1_altitude)
        if healthy:
            return float(sum(healthy) / len(healthy))
        return float(pkt.kf_altitude)
