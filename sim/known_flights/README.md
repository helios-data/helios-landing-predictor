# Known-flight calibration fixtures

Real recorded descents used to fit and sanity-check the `fitted` descent-rate model
(plan section 4.1, tier 3) and to predict-then-compare against an actual recorded
landing point (plan section 6).

## Format

Any of:
- **KML** with timestamped `<gx:Track>` / `<Placemark>` points carrying GPS + altitude, or
- **CSV** with at least `time_s, altitude_agl_m` (and optionally `lat, lon`).

`src.descent_model.DescentModel.fit_from_track(times_s, altitudes_agl_m)` turns a
timestamped altitude track into `(altitude, descent_rate)` samples; feed those to
`fit_curve(...)` to enable the `fitted` model.

## Pending

- **`beauty_and_the_beast.kml`** — the KML from the Slack thread is to be dropped in here
  as the first calibration fixture (see plan section 4.1). Once present, add a loader
  (a tiny KML parser or `csv` export) and a calibration test under `tests/` that fits the
  curve and asserts the predicted landing is within tolerance of the recorded touchdown.
