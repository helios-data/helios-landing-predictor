# Helios LandingPredictor service image.
FROM python:3.13-slim

# uv for dependency management (matches sibling repos).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# protoc toolchain comes in via the dev dependency group (grpcio-tools ships protoc).
WORKDIR /app

# Copy dependency manifests + vendored submodules first for layer caching.
COPY pyproject.toml uv.lock ./
COPY helios-python-sdk/ ./helios-python-sdk/
COPY falcon-protos/ ./falcon-protos/
COPY protos-proposed/ ./protos-proposed/
COPY scripts/ ./scripts/

RUN uv sync --frozen --extra dev --no-install-project

# Compile protobufs into src/generated.
COPY src/ ./src/
RUN uv run python scripts/gen_protos.py

# Remaining runtime files. The mission config is bind-mounted at runtime by the launcher
# (config.json volumes -> /app/config/rocket_config.json), so it is NOT copied here.
COPY config.json entrypoint.sh ./
COPY sim/ ./sim/

ENV PYTHONUNBUFFERED=1
EXPOSE 8091

RUN chmod +x entrypoint.sh
ENTRYPOINT ["./entrypoint.sh"]
