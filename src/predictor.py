"""Single-point + Monte-Carlo dispersion landing prediction (plan sections 4.3).

Given the current position/altitude, a :class:`~src.descent_model.DescentModel`, and a
:class:`~src.wind_model.WindProfile`, integrate the remaining descent to the ground:

* a deterministic **best-estimate** landing point, and
* a **Monte-Carlo cloud** (jittered wind + descent rate) whose covariance yields the
  50% / 90% confidence **ellipses** — the actual predicted landing *area*.

The Monte Carlo is vectorised over (iterations x altitude bands) with numpy; no Python
per-sample loop. Nothing here imports Helios protos, so it is fully offline-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from src.config import PredictionConfig
from src.descent_model import DescentModel

# Confidence-ellipse scale factors for a 2-D Gaussian: k = sqrt(-2 ln(1 - p)).
_K50 = float(np.sqrt(-2.0 * np.log(1.0 - 0.50)))
_K90 = float(np.sqrt(-2.0 * np.log(1.0 - 0.90)))

_METERS_PER_DEG_LAT = 111320.0


@dataclass
class PredictionResult:
    best_estimate: tuple[float, float]           # (lat, lon)
    dispersion_cloud: list[tuple[float, float]]  # sparse sampled MC landing points
    ellipse_50: list[tuple[float, float]]        # polygon vertices (lat, lon)
    ellipse_90: list[tuple[float, float]]
    descent_model: str
    wind_source: str
    drift_east_m: float = 0.0                     # deterministic drift, for diagnostics
    drift_north_m: float = 0.0
    metadata: dict = field(default_factory=dict)


def _enu_to_latlon(
    ref_lat: float, ref_lon: float, east_m: np.ndarray, north_m: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Equirectangular local-tangent offset -> lat/lon (adequate for few-km dispersions)."""
    lat = ref_lat + north_m / _METERS_PER_DEG_LAT
    lon = ref_lon + east_m / (_METERS_PER_DEG_LAT * np.cos(np.radians(ref_lat)))
    return lat, lon


def _ellipse_vertices(
    mean_e: float, mean_n: float, cov: np.ndarray, k: float, n: int = 48
) -> tuple[np.ndarray, np.ndarray]:
    """Vertices of the k-sigma confidence ellipse of a 2-D covariance, in E/N meters."""
    vals, vecs = np.linalg.eigh(cov)
    vals = np.clip(vals, 0.0, None)
    t = np.linspace(0.0, 2.0 * np.pi, n, endpoint=False)
    unit = np.stack([np.cos(t), np.sin(t)], axis=0)          # (2, n)
    axes = vecs @ np.diag(k * np.sqrt(vals))                 # scale + rotate
    pts = axes @ unit                                        # (2, n)
    return mean_e + pts[0], mean_n + pts[1]


def _band_setup(
    alt_agl_m: float, ground_alt_msl_m: float, state: int, model: DescentModel, steps: int
):
    """Precompute per-band mid altitude, thickness, and nominal descent rate."""
    edges = np.linspace(alt_agl_m, 0.0, steps + 1)
    mid_agl = 0.5 * (edges[:-1] + edges[1:])
    dz = -np.diff(edges)                                     # positive band thickness (m)
    rates = np.empty_like(mid_agl)
    for i, magl in enumerate(mid_agl):
        eff = model.effective_state(magl, state)
        rates[i] = model.descent_rate(magl + ground_alt_msl_m, eff)
    rates = np.clip(rates, 0.1, None)                        # guard against div-by-zero
    return mid_agl, dz, rates


def predict(
    *,
    current_lat: float,
    current_lon: float,
    altitude_agl_m: float,
    ground_alt_msl_m: float,
    state: int,
    descent_model: DescentModel,
    wind_profile,
    config: PredictionConfig,
) -> PredictionResult:
    """Compute the best-estimate landing point and Monte-Carlo dispersion ellipses."""
    wind_source = getattr(wind_profile, "source", "unknown")

    # Already on the ground (or below): landing point is the current point.
    if altitude_agl_m <= 0.5:
        pt = (current_lat, current_lon)
        return PredictionResult(
            best_estimate=pt,
            dispersion_cloud=[pt],
            ellipse_50=[pt],
            ellipse_90=[pt],
            descent_model=descent_model.mode,
            wind_source=wind_source,
        )

    steps = max(2, int(config.integration_steps))
    mid_agl, dz, rate_nom = _band_setup(
        altitude_agl_m, ground_alt_msl_m, state, descent_model, steps
    )
    speed_b, dir_b = wind_profile.sample(mid_agl)            # (B,), (B,)
    dir_rad_b = np.radians(dir_b)

    # --- deterministic best estimate -----------------------------------------------------
    dt_nom = dz / rate_nom
    u_nom = -speed_b * np.sin(dir_rad_b)
    v_nom = -speed_b * np.cos(dir_rad_b)
    east_nom = float(np.sum(u_nom * dt_nom))
    north_nom = float(np.sum(v_nom * dt_nom))
    best_lat, best_lon = _enu_to_latlon(
        current_lat, current_lon, np.array(east_nom), np.array(north_nom)
    )
    best_estimate = (float(best_lat), float(best_lon))

    # --- Monte Carlo dispersion ----------------------------------------------------------
    k = int(config.mc_iterations)
    rng = np.random.default_rng(config.seed)
    sigma_dr = config.descent_rate_error_pct
    sigma_ws = config.wind_speed_error_pct
    sigma_dir = np.radians(config.wind_dir_error_deg)

    dr_factor = np.clip(1.0 + rng.normal(0.0, sigma_dr, size=(k, 1)), 0.1, None)
    dt = dz[None, :] / (rate_nom[None, :] * dr_factor)                       # (K, B)
    s_k = np.clip(speed_b[None, :] * (1.0 + rng.normal(0.0, sigma_ws, (k, len(speed_b)))), 0.0, None)
    dir_k = dir_rad_b[None, :] + rng.normal(0.0, sigma_dir, (k, len(dir_rad_b)))
    east = np.sum(-s_k * np.sin(dir_k) * dt, axis=1)                         # (K,)
    north = np.sum(-s_k * np.cos(dir_k) * dt, axis=1)

    mean_e, mean_n = float(np.mean(east)), float(np.mean(north))
    cov = np.cov(np.stack([east, north]))
    if cov.shape != (2, 2):                                                  # degenerate (K==1)
        cov = np.zeros((2, 2))

    e50, n50 = _ellipse_vertices(mean_e, mean_n, cov, _K50)
    e90, n90 = _ellipse_vertices(mean_e, mean_n, cov, _K90)
    lat50, lon50 = _enu_to_latlon(current_lat, current_lon, e50, n50)
    lat90, lon90 = _enu_to_latlon(current_lat, current_lon, e90, n90)

    # Sparse cloud for transport/visualisation (plan section 4.4).
    cloud_n = min(int(config.dispersion_cloud_size), k)
    sel = rng.choice(k, size=cloud_n, replace=False) if cloud_n < k else np.arange(k)
    lat_c, lon_c = _enu_to_latlon(current_lat, current_lon, east[sel], north[sel])

    return PredictionResult(
        best_estimate=best_estimate,
        dispersion_cloud=list(zip(lat_c.tolist(), lon_c.tolist(), strict=True)),
        ellipse_50=list(zip(lat50.tolist(), lon50.tolist(), strict=True)),
        ellipse_90=list(zip(lat90.tolist(), lon90.tolist(), strict=True)),
        descent_model=descent_model.mode,
        wind_source=wind_source,
        drift_east_m=east_nom,
        drift_north_m=north_nom,
        metadata={"mc_iterations": k, "integration_steps": steps},
    )
