"""Compile this repo's protobufs into ``src/generated`` using the betterproto2 plugin.

Mirrors ``helios-python-sdk/scripts/hatch_build.py`` so the generated dataclasses match
the SDK's style (``Msg.parse(bytes)`` / ``bytes(msg)``). Cross-platform: locates the
plugin executable in the active venv, so it works on Windows (``.exe``) and POSIX alike.

Sources compiled:
  * ``falcon-protos/*.proto``      -> TelemetryPacket (downlink telemetry we consume)
  * ``protos-proposed/*.proto``    -> LandingPrediction / PredictionConfig (our output)

``AprsPacket`` is NOT compiled here; it ships pre-generated in the SDK under
``helios.generated.helios.transport`` and is imported from there (same as helios-dashboard).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "src" / "generated"
PROTO_ROOTS = [ROOT / "falcon-protos", ROOT / "protos-proposed"]


def find_plugin() -> Path:
    bin_dir = Path(sys.executable).parent
    candidates = list(bin_dir.glob("protoc-gen-python_betterproto2*"))
    if not candidates:
        # uv may place console scripts in a Scripts/ sibling on Windows.
        candidates = list(bin_dir.glob("**/protoc-gen-python_betterproto2*"))
    if not candidates:
        raise SystemExit(
            "Could not find 'protoc-gen-python_betterproto2'. "
            "Run `make deps` (installs the dev group with betterproto2-compiler)."
        )
    return candidates[0]


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "__init__.py").touch(exist_ok=True)

    proto_files: list[str] = []
    includes: list[str] = []
    for root in PROTO_ROOTS:
        if not root.is_dir():
            continue
        includes.append(f"-I={root}")
        proto_files.extend(str(p) for p in root.glob("*.proto"))

    if not proto_files:
        raise SystemExit("No .proto files found. Are the submodules initialised?")

    plugin = find_plugin()
    cmd = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"--plugin=protoc-gen-python_betterproto2={plugin}",
        *includes,
        f"--python_betterproto2_out={OUT_DIR}",
        *proto_files,
    ]
    print("Generating protos:\n  " + "\n  ".join(proto_files))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("STDOUT:", result.stdout)
        print("STDERR:", result.stderr)
        raise SystemExit(f"protoc failed (exit {result.returncode})")
    print(f"Generated {len(proto_files)} proto file(s) into {OUT_DIR}")


if __name__ == "__main__":
    main()
