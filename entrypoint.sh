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
  uv run python scripts/gen_protos.py
fi

exec uv run python -m src.main "$@"
