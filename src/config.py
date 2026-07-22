"""Runtime-tunable prediction configuration.

These are *operator* knobs (Monte-Carlo iteration count, error bars, wind source mode),
distinct from the fixed rocket physical constants in ``rocket_config.json`` (see
``descent_model.RocketConfig``). Defaults live here; they can be overridden from the
environment at startup and updated at runtime from a ``PredictionConfig`` command event
(plan section 4.3). The predictor always computes against the latest received config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid importing generated protos for pure-model unit tests
    from src.generated import PredictionConfig as PredictionConfigProto


@dataclass(frozen=True)
class PredictionConfig:
    # --- wire fields (mirror protos-proposed/landing_prediction.proto PredictionConfig) ---
    mc_iterations: int = 800
    wind_speed_error_pct: float = 0.15       # 1-sigma, fractional
    descent_rate_error_pct: float = 0.10     # 1-sigma, fractional
    wind_source_mode: str = "live"           # "live" only for now
    wind_dir_error_deg: float = 10.0         # 1-sigma, absolute degrees
    recompute_min_interval_s: float = 1.0    # throttle floor, independent of packet rate

    # --- local operational knobs (not on the wire) ---
    srad_stale_timeout_s: float = 3.0        # SRAD -> COTS fallback threshold (plan section 3.2)
    dispersion_cloud_size: int = 150         # sparse cloud size for transport (plan section 4.4)
    integration_steps: int = 200             # altitude bands used when integrating descent
    seed: int | None = None                  # fix for reproducible Monte Carlo (tests)

    @classmethod
    def from_env(cls) -> PredictionConfig:
        """Build defaults, overridden by ``LP_*`` environment variables where present."""
        cfg = cls()
        env = os.environ

        def _f(name: str, cur: float) -> float:
            return float(env[name]) if name in env else cur

        def _i(name: str, cur: int) -> int:
            return int(env[name]) if name in env else cur

        return replace(
            cfg,
            mc_iterations=_i("LP_MC_ITERATIONS", cfg.mc_iterations),
            wind_speed_error_pct=_f("LP_WIND_SPEED_ERROR_PCT", cfg.wind_speed_error_pct),
            descent_rate_error_pct=_f("LP_DESCENT_RATE_ERROR_PCT", cfg.descent_rate_error_pct),
            wind_source_mode=env.get("LP_WIND_SOURCE_MODE", cfg.wind_source_mode),
            wind_dir_error_deg=_f("LP_WIND_DIR_ERROR_DEG", cfg.wind_dir_error_deg),
            recompute_min_interval_s=_f("LP_RECOMPUTE_MIN_INTERVAL_S", cfg.recompute_min_interval_s),
            srad_stale_timeout_s=_f("LP_SRAD_STALE_TIMEOUT_S", cfg.srad_stale_timeout_s),
            seed=_i("LP_SEED", cfg.seed) if "LP_SEED" in env else cfg.seed,
        )

    def with_proto(self, proto: PredictionConfigProto) -> PredictionConfig:
        """Return a copy updated from a received ``PredictionConfig`` proto.

        Only fields the operator actually set (non-zero / non-empty) are applied, so a
        partial config command changes one thing at a time without clobbering the rest.
        """
        changes: dict[str, object] = {}
        if proto.mc_iterations:
            changes["mc_iterations"] = int(proto.mc_iterations)
        if proto.wind_speed_error_pct:
            changes["wind_speed_error_pct"] = float(proto.wind_speed_error_pct)
        if proto.descent_rate_error_pct:
            changes["descent_rate_error_pct"] = float(proto.descent_rate_error_pct)
        if proto.wind_source_mode:
            changes["wind_source_mode"] = proto.wind_source_mode
        if proto.wind_dir_error_deg:
            changes["wind_dir_error_deg"] = float(proto.wind_dir_error_deg)
        if proto.recompute_min_interval_s:
            changes["recompute_min_interval_s"] = float(proto.recompute_min_interval_s)
        return replace(self, **changes)

    def to_proto(self) -> PredictionConfigProto:
        from src.generated import PredictionConfig as PredictionConfigProto

        return PredictionConfigProto(
            mc_iterations=self.mc_iterations,
            wind_speed_error_pct=self.wind_speed_error_pct,
            descent_rate_error_pct=self.descent_rate_error_pct,
            wind_source_mode=self.wind_source_mode,
            wind_dir_error_deg=self.wind_dir_error_deg,
            recompute_min_interval_s=self.recompute_min_interval_s,
        )
