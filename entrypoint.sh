#!/usr/bin/env bash
# Entrypoint for the LandingPredictor service container.
#
# Env flags (same convention as sibling Helios repos):
#   VERBOSE=1     -> debug-level logging
#   STANDALONE=1  -> run against a canned telemetry sequence, no core connection
#                    (see src/main.py / sim/standalone_feed.py)
set -euo pipefail

# Protos are compiled at build time (see Dockerfile); regenerate if missing.
if [ ! -f /app/src/generated/__init__.py ]; then
  echo "[entrypoint] generated protos missing, compiling..."
  mkdir -p src/generated
  uv run protoc -I falcon-protos -I protos-proposed \
    --python_betterproto2_out=src/generated \
    $(find falcon-protos protos-proposed -name '*.proto')
fi

exec uv run python -m src.main "$@"
