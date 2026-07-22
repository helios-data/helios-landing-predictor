from src.config import PredictionConfig
from src.generated import PredictionConfig as Proto


def test_from_env_defaults(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith("LP_"):
            monkeypatch.delenv(k, raising=False)
    cfg = PredictionConfig.from_env()
    assert cfg.mc_iterations == 800
    assert cfg.wind_source_mode == "live"


def test_from_env_overrides(monkeypatch):
    monkeypatch.setenv("LP_MC_ITERATIONS", "1500")
    monkeypatch.setenv("LP_WIND_SPEED_ERROR_PCT", "0.25")
    monkeypatch.setenv("LP_SEED", "9")
    cfg = PredictionConfig.from_env()
    assert cfg.mc_iterations == 1500
    assert cfg.wind_speed_error_pct == 0.25
    assert cfg.seed == 9


def test_with_proto_partial_update():
    base = PredictionConfig(mc_iterations=800, wind_speed_error_pct=0.15)
    updated = base.with_proto(Proto(wind_speed_error_pct=0.3))
    assert updated.wind_speed_error_pct == 0.3
    assert updated.mc_iterations == 800  # untouched (proto field was 0)


def test_to_proto_roundtrip():
    cfg = PredictionConfig(mc_iterations=500, wind_dir_error_deg=12.0, descent_rate_error_pct=0.2)
    proto = cfg.to_proto()
    back = PredictionConfig().with_proto(proto)
    assert back.mc_iterations == 500
    assert back.wind_dir_error_deg == 12.0
    assert back.descent_rate_error_pct == 0.2
