"""Wind-by-altitude model (plan section 4.2).

For now the only implemented source is :class:`LiveWindSource`, which fetches a
wind-by-altitude profile from Open-Meteo (free, no API key) at the launch-site
coordinates and caches it in memory between refreshes. ``WindSource`` is left as an
extension point so a ``StaticWindSource`` / ``ManualWindSource`` can be dropped in later
for the offline case (deferred — see README "Future work") without restructuring the
predictor.

Meteorological convention: ``direction_deg`` is the direction the wind blows *from*
(0 = N, 90 = E). The drift a descending rocket experiences points the opposite way, so
the drift vector is ``(u_east, v_north) = (-s*sin(dir), -s*cos(dir))``.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)

# Open-Meteo height-level wind fields (m AGL). Above the top level we hold the top wind
# (a documented approximation; pressure-level winds aloft are a future refinement).
_OPEN_METEO_LEVELS_M = (10, 80, 120, 180)
_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


@dataclass(frozen=True)
class WindProfile:
    """A wind profile: speed and meteorological direction at a set of AGL altitudes."""

    altitudes_agl_m: np.ndarray   # ascending
    speed_ms: np.ndarray
    direction_deg: np.ndarray     # direction wind blows FROM
    source: str = "live"

    @classmethod
    def calm(cls, source: str = "calm") -> WindProfile:
        """Zero-wind profile — used for STANDALONE mode and as a safe fallback."""
        return cls(
            altitudes_agl_m=np.array([0.0, 5000.0]),
            speed_ms=np.array([0.0, 0.0]),
            direction_deg=np.array([0.0, 0.0]),
            source=source,
        )

    def sample(self, altitudes_agl_m: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Interpolate (speed, direction_deg) to the given altitudes, clamped at both ends.

        Direction is interpolated as a unit vector to avoid the 0/360 wraparound artifact.
        """
        alt = np.asarray(altitudes_agl_m, dtype=float)
        speed = np.interp(alt, self.altitudes_agl_m, self.speed_ms)
        dir_rad = np.radians(self.direction_deg)
        cx = np.interp(alt, self.altitudes_agl_m, np.cos(dir_rad))
        cy = np.interp(alt, self.altitudes_agl_m, np.sin(dir_rad))
        direction = np.degrees(np.arctan2(cy, cx)) % 360.0
        return speed, direction

    def drift_vectors(self, altitudes_agl_m: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Drift (u_east, v_north) in m/s at the given altitudes (downwind direction)."""
        speed, direction = self.sample(altitudes_agl_m)
        dir_rad = np.radians(direction)
        u_east = -speed * np.sin(dir_rad)
        v_north = -speed * np.cos(dir_rad)
        return u_east, v_north


class WindSource(ABC):
    """Interface for a wind-by-altitude source. Only :class:`LiveWindSource` is built now."""

    @abstractmethod
    async def get_profile(self) -> WindProfile:
        """Return the current wind profile (may be cached)."""


class LiveWindSource(WindSource):
    """Open-Meteo live wind-by-altitude fetch, cached and refreshed on an interval."""

    def __init__(
        self,
        latitude: float,
        longitude: float,
        *,
        refresh_interval_s: float = 300.0,
        timeout_s: float = 10.0,
    ) -> None:
        self.latitude = latitude
        self.longitude = longitude
        self.refresh_interval_s = refresh_interval_s
        self.timeout_s = timeout_s
        self._cache: WindProfile | None = None
        self._fetched_monotonic: float | None = None

    async def get_profile(self) -> WindProfile:
        import time as _time

        now = _time.monotonic()
        fresh = (
            self._cache is not None
            and self._fetched_monotonic is not None
            and (now - self._fetched_monotonic) < self.refresh_interval_s
        )
        if fresh:
            return self._cache  # type: ignore[return-value]

        try:
            profile = await self._fetch()
            self._cache = profile
            self._fetched_monotonic = now
            return profile
        except Exception as exc:  # network down, bad response, etc.
            logger.warning("Open-Meteo wind fetch failed: %s", exc)
            if self._cache is not None:
                return self._cache  # serve stale rather than nothing
            return WindProfile.calm(source="live-unavailable")

    async def _fetch(self) -> WindProfile:
        import aiohttp

        fields = []
        for lvl in _OPEN_METEO_LEVELS_M:
            fields.append(f"wind_speed_{lvl}m")
            fields.append(f"wind_direction_{lvl}m")
        params = {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "hourly": ",".join(fields),
            "wind_speed_unit": "ms",
            "forecast_days": 1,
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(_OPEN_METEO_URL, params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()
        return self._parse(data)

    @staticmethod
    def _parse(data: dict) -> WindProfile:
        hourly = data["hourly"]
        # Use the current hour: index of "now" in the returned time series (first entry is
        # midnight of the forecast day). Fall back to index 0 if we can't line it up.
        idx = _current_hour_index(hourly.get("time", []))
        alts, speeds, dirs = [], [], []
        for lvl in _OPEN_METEO_LEVELS_M:
            s = hourly[f"wind_speed_{lvl}m"][idx]
            d = hourly[f"wind_direction_{lvl}m"][idx]
            if s is None or d is None:
                continue
            alts.append(float(lvl))
            speeds.append(float(s))
            dirs.append(float(d))
        if not alts:
            return WindProfile.calm(source="live-empty")
        return WindProfile(
            altitudes_agl_m=np.array(alts),
            speed_ms=np.array(speeds),
            direction_deg=np.array(dirs),
            source="live",
        )


def _current_hour_index(times: list[str]) -> int:
    """Index into Open-Meteo's hourly time array closest to now (UTC)."""
    if not times:
        return 0
    import datetime as _dt

    now = _dt.datetime.now(_dt.UTC).replace(minute=0, second=0, microsecond=0)
    target = now.strftime("%Y-%m-%dT%H:00")
    try:
        return times.index(target)
    except ValueError:
        return 0
