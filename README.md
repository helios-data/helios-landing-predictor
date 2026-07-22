# Helios LandingPredictor

A companion service to `helios-mission-control` that continuously predicts the rocket's
**landing point** and **dispersion area** during descent, publishing the result as a Helios
event (`landing_prediction`) on `Helios.Services.LandingPredictor` for mission control (and
any other consumer) to display.

This repo **produces data, not UI**. It subscribes to the two telemetry streams, runs a
descent + wind + Monte-Carlo model gated by flight state, and publishes a
`LandingPrediction` with a best-estimate point plus 50% / 90% confidence ellipses.

> All code lives in this repo. It does **not** modify any other repository. Changes needed
> elsewhere are listed under [Coordination with other repos](#coordination-with-other-repos).

---

## How it works

```
Helios.FALCON.SRAD_Telemetry / telemetry  ─┐
Helios.FALCON.APRS_Telemetry / aprs       ─┤→ helios_bridge → engine (FlightState gate)
                                            │      │              → descent_model
                                            │      │              → wind_model (Open-Meteo)
                                            │      │              → predictor (Monte Carlo)
                                            │      ▼
                                            └→ publisher → Helios.Services.LandingPredictor
                                                             / landing_prediction
```

- **`helios_bridge.py`** — subscribes to **both** streams continuously, keeps a **2-point
  rolling history per source**, tracks `FlightState` from SRAD, and selects the driving fix:
  **SRAD primary, COTS fallback** when SRAD goes stale or its position is invalid.
- **`engine.py`** — FlightState gating: **idle** pre-descent (STANDBY/ASCENT/MACH_LOCK,
  low-rate `not_descending` heartbeat), **active** on DROGUE/MAIN descent (recompute +
  publish, throttled), **frozen** after LANDED (one final `final: true` frame, then stop).
- **`descent_model.py`** — descent rate from the terminal-velocity relation
  `v = sqrt(2 m g / (ρ Cd A))`, three tiers: `constant`, `banded` (ISA density per altitude),
  `fitted` (curve from a real recorded descent).
- **`wind_model.py`** — live wind-by-altitude from **Open-Meteo** (no key), cached/refreshed.
- **`predictor.py`** — integrates the remaining descent for a deterministic best estimate,
  then runs a **vectorised Monte Carlo** (jittered wind + descent rate) whose covariance
  yields the 50% / 90% ellipses. Nothing here imports Helios protos — it's fully
  offline-testable.

## Event output

`LandingPrediction` (see [`protos-proposed/landing_prediction.proto`](protos-proposed/landing_prediction.proto)),
published with `event_name="landing_prediction"` on `Helios.Services.LandingPredictor`:

| field | meaning |
|---|---|
| `best_estimate` | single best-estimate landing point (lat/lon) |
| `ellipse_50` / `ellipse_90` | confidence-ellipse polygon vertices — the predicted **area** |
| `dispersion_cloud` | sparse (~150) sampled MC points, for map viz |
| `current_lat/lon`, `current_source` | last-known driving fix and its stream (`srad`/`cots`) |
| `current_alt_agl`, `flight_state`, `status` | context (`not_descending`/`predicting`/`final`) |
| `final` | `true` once LANDED and frozen |
| `descent_model`, `wind_source` | which model tier / wind source produced the estimate |

Runtime config is accepted over the wire as a `PredictionConfig` on `event_name="config"`
and acked back on `event_name="prediction_config"`.

---

## Running

```bash
make deps        # git submodules + uv sync (incl. dev group with the proto toolchain)
make protos      # compile falcon-protos + protos-proposed -> src/generated (betterproto2)
make run         # connect to Helios core and start predicting
make test        # unit tests (pure model — no core, no protos needed)
make lint        # ruff
```

### STANDALONE (no core)

Drive the engine through a full canned descent with no Helios connection — good for pure
model development:

```bash
STANDALONE=1 make run
```

### End-to-end against the replay sim

1. Run `helios-mission-control`'s `sim/replay.py` (publishes a synthetic full flight).
   **It must be updated to the renamed addresses first** — see below.
2. `make run`
3. `uv run python -m sim.replay_consumer` — prints predictions as they arrive.

### Config / environment flags

| flag | default | meaning |
|---|---|---|
| `VERBOSE` | off | debug logging |
| `STANDALONE` | off | no core; run the canned descent |
| `CORE_ADDRESS` / `CORE_PORT` | `Helios` / `5000` | Helios core location |
| `DESCENT_MODE` | `banded` | `constant` \| `banded` \| `fitted` |
| `HEALTH_PORT` / `HEALTH_DISABLE` | `8091` / off | minimal `/health` HTTP surface |
| `LP_MC_ITERATIONS`, `LP_WIND_SPEED_ERROR_PCT`, `LP_DESCENT_RATE_ERROR_PCT`, `LP_WIND_DIR_ERROR_DEG`, `LP_RECOMPUTE_MIN_INTERVAL_S`, `LP_SRAD_STALE_TIMEOUT_S`, `LP_SEED` | see `src/config.py` | startup defaults for `PredictionConfig` |

### Mission config (bind-mounted, read-only)

The shared mission/launcher config is **bind-mounted into the container** at
`/app/config/rocket_config.json` (declared in `config.json` `volumes`; the host path is
filled in by the launcher). This node reads only the physical/launch fields it needs:

| field | used for |
|---|---|
| `drogue_drag_coeff`, `main_drag_coeff` | terminal-velocity descent rate |
| `drogue_area_m2`, `main_area_m2` | canopy reference areas |
| `dry_mass_kg` | descent mass (post-burnout; falls back to `wet_mass_kg`) |
| `expected_ground_station.{lat,lon,alt_m}` | wind-fetch location + ground-altitude seed |
| `main_deploy_alt_agl_m` *(optional)* | drogue→main rate switch; defaults to 450 m if absent |

In **STANDALONE** mode there is no mounted file, so built-in constants
(`RocketConfig.defaults()`) are used. If the file is missing in a normal run, the same
defaults apply with a warning. Override the path with `ROCKET_CONFIG`.

> Descent happens after burnout, so mass is taken from `dry_mass_kg`. **Confirm the drag
> coefficients and canopy areas with the recovery/structures team.**

---

## Model notes & known simplifications

- **Wind aloft**: Open-Meteo height-level winds (10/80/120/180 m AGL) are used; above 180 m
  the top-band wind is held for the rest of the column. A 3 km descent therefore uses a
  rough upper-air estimate — pressure-level winds are a future refinement (see below).
- **Drogue→main switch**: while `FlightState == DROGUE_DESCENT`, bands below
  `main_deploy_alt_agl_m` use the main-canopy rate. Real main-deploy altitude will vary.
- **Equirectangular geo**: dispersion is projected with a local-tangent flat-earth
  approximation — fine for few-km landing areas.
- Monte Carlo is reproducible when `LP_SEED` (or `PredictionConfig.seed`) is set — tests
  rely on this.

---

## Coordination with other repos

**None of these are implemented here** — they are required changes in *other* repos, listed
for the user to action (plan §7):

1. **`helios-data/helios-launcher`** — add a `LandingPredictor` component under `Services`
   in `IREC2026-CloudBurst.json` (port **8091**) so it spawns with the stack.
2. **`helios-data/helios-mission-control`** —
   - **Address rename (blocking):** this node subscribes to `Helios.FALCON.SRAD_Telemetry`
     and `Helios.FALCON.APRS_Telemetry`. Mission control's plan/code and `sim/replay.py`
     still use the old `Helios.FALCON.Telemetry` / `Helios.Services.TeleGPS` names. They must
     be renamed for the two repos to talk (and for `sim/replay_consumer.py` to work).
   - **WS/UI integration:** add a `{"type":"prediction", ...}` WS frame subscribing to
     `Helios.Services.LandingPredictor` / `landing_prediction`; render `best_estimate` +
     `ellipse_50/90` as a map overlay; wire `final: true` into the "Recovery mode" panel; add
     a `PredictionConfig` form on `/admin` (publishes `config`, listens for `prediction_config`).
3. **`helios-data/helios-protos` (possibly)** — decide whether `LandingPrediction` /
   `PredictionConfig` should be upstreamed as shared, versioned protos rather than living in
   this repo's `protos-proposed/`. If so, switch `make protos` to compile from the submodule.

## Future work

- **Offline wind**: a `StaticWindSource` / `ManualWindSource` behind the existing
  `WindSource` interface for the fully-offline launch site (deferred per plan §4.2).
- **Pressure-level winds** for realistic upper-air drift above 180 m AGL.
- **Fitted descent model** calibrated against the *Beauty & the Beast* KML once shared
  (drop it in `sim/known_flights/`; see that folder's README).
- **PredictionConfig source of truth**: local defaults vs. a core-published config event
  (plan Open Question 3, unresolved).
- **Measured-heading blending**: use the 2-point per-source history to blend the observed
  horizontal velocity with the wind-drift estimate.
