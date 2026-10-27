#!/usr/bin/env bash
set -euo pipefail
# Usage: bash scripts/sandbox/pack-linux-wheels.sh <new-output-directory>
# Produces a Linux x86_64 CPython 3.13-only wheelhouse, never alters daemon env.
OUTPUT="${1:?New output directory required}"
[[ ! -e "$OUTPUT" ]] || { echo "Output already exists" >&2; exit 2; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REQ="$ROOT/scripts/sandbox/requirements-linux-py313.txt"
[[ -s "$REQ" ]] || exit 3
PYTHON="${PACK_PYTHON:-python3}"
"$PYTHON" -m pip --version >/dev/null
mkdir -p "$OUTPUT/wheels"
# Download binary artifacts for the *target* Linux, never macOS wheel binaries.
# Permitted at packaging time only; the sandbox restore is strictly offline.
"$PYTHON" -m pip download \
  --disable-pip-version-check \
  --no-cache-dir --retries 1 --timeout 10 \
  --only-binary=:all: \
  --implementation cp \
  --python-version 3.13 \
  --abi cp313 \
  --platform manylinux_2_17_x86_64 \
  --platform manylinux2014_x86_64 \
  --platform manylinux_2_28_x86_64 \
  --platform manylinux_2_31_x86_64 \
  --dest "$OUTPUT/wheels" \
  --requirement "$REQ"
cp "$REQ" "$OUTPUT/requirements-linux-py313.txt"
"$PYTHON" - "$OUTPUT" <<'PY'
import hashlib
from pathlib import Path
import sys

output=Path(sys.argv[1])
wheels=sorted(output.joinpath("wheels").glob("*.whl"))
if not wheels:
    raise SystemExit("No wheel artifacts produced")
# Reject non-Linux platform wheels and unexpected source archives.
if sorted(output.joinpath("wheels").iterdir()) != wheels:
    raise SystemExit("Non-wheel artifact in wheelhouse")
for wheel in wheels:
    name=wheel.name.lower()
    if ("macosx" in name or "win_" in name or "arm64" in name
            or ("manylinux" not in name and "none-any.whl" not in name)):
        raise SystemExit(f"Nonportable wheel: {wheel.name}")
names=["requirements-linux-py313.txt"] + [f"wheels/{w.name}" for w in wheels]
entries=[]
for rel in names:
    digest=hashlib.sha256((output/rel).read_bytes()).hexdigest()
    entries.append(f"{digest}  {rel}\n")
(output/"files.sha256").write_text("".join(entries),encoding="ascii")
key=hashlib.sha256((
    "local-agent-linux-cp313-x86_64-v1\n"+ "".join(entries)
).encode("ascii")).hexdigest()
(output/"dependency-key.txt").write_text(key+"\n",encoding="ascii")
print(f"LINUX_WHEELS={len(wheels)}")
print(f"DEPENDENCY_KEY={key}")
print(f"WHEEL_BYTES={sum(w.stat().st_size for w in wheels)}")
PY
echo "PACKED_LINUX_WHEELS_ONLY"
