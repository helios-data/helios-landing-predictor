# Helios LandingPredictor service image.
FROM python:3.13-slim

ENV UV_NO_SYNC=1

# uv for dependency management (matches sibling repos).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    protobuf-compiler \
    libprotobuf-dev \
    && rm -rf /var/lib/apt/lists/*

# protoc toolchain comes in via the dev dependency group (grpcio-tools ships protoc).
WORKDIR /app

# Copy dependency manifests + vendored submodules first for layer caching.
COPY pyproject.toml uv.lock ./
COPY helios-python-sdk/ ./helios-python-sdk/
COPY falcon-protos/ ./falcon-protos/
COPY protos-proposed/ ./protos-proposed/

RUN uv sync --frozen --extra dev --no-install-project

# Compile protobufs into src/generated (betterproto2, matches the SDK).
COPY src/ ./src/
RUN if [ -f falcon-protos/TelemetryPacket.proto ]; then \
      mkdir -p src/generated && \
      uv run protoc \
        -I falcon-protos -I protos-proposed \
        --python_betterproto2_out=src/generated \
        $(find falcon-protos protos-proposed -name '*.proto'); \
    else echo "protos not compiled (submodule absent) — STANDALONE only"; fi

# Remaining runtime files. The mission config is bind-mounted at runtime by the launcher
# (config.json volumes -> /app/config/rocket_config.json), so it is NOT copied here.
COPY config.json entrypoint.sh ./
COPY sim/ ./sim/

ENV PYTHONUNBUFFERED=1
EXPOSE 8091

RUN chmod +x entrypoint.sh
ENTRYPOINT ["./entrypoint.sh"]
