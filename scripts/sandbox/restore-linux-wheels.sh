#!/usr/bin/env bash
set -euo pipefail
# Usage: bash scripts/sandbox/restore-linux-wheels.sh <wheelhouse-dir> <new-venv-dir>
# Only Linux x86_64 CPython 3.13, checks every wheel before a --no-index install.
INPUT="${1:?Wheelhouse directory required}"
VENV="${2:?New venv directory required}"
[[ ! -e "$VENV" ]] || { echo "venv destination already exists" >&2; exit 2; }
[[ -s "$INPUT/files.sha256" && -s "$INPUT/dependency-key.txt" ]] || exit 3
python3 - "$INPUT" <<'PY'
import hashlib
from pathlib import Path
import platform
import sys

root=Path(sys.argv[1])
if sys.version_info[:2] != (3,13) or platform.system()!="Linux" or platform.machine()!="x86_64":
    raise SystemExit("UNVERIFIED: requires CPython 3.13 Linux x86_64")
raw=(root/"files.sha256").read_text(encoding="ascii")
entries=[]
for line in raw.splitlines():
    if "  " not in line:
        raise SystemExit("Malformed wheel manifest")
    digest,name=line.split("  ",1)
    if len(digest)!=64 or any(c not in "0123456789abcdef" for c in digest):
        raise SystemExit("Invalid SHA256 checksum")
    path=Path(name)
    if (path.is_absolute() or ".." in path.parts or
            name not in ("requirements-linux-py313.txt",) and
            (len(path.parts)!=2 or path.parts[0]!="wheels" or not name.endswith(".whl"))):
        raise SystemExit("Unsafe manifest path")
    file=root/path
    if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest()!=digest:
        raise SystemExit(f"Checksum mismatch: {name}")
    entries.append(line+"\n")
actual=hashlib.sha256((
    "local-agent-linux-cp313-x86_64-v1\n"+"".join(entries)
).encode("ascii")).hexdigest()
if actual!=(root/"dependency-key.txt").read_text(encoding="ascii").strip():
    raise SystemExit("Dependency key mismatch")
expected={Path(line.split("  ",1)[1]).name for line in raw.splitlines() if line.split("  ",1)[1].startswith("wheels/")}
observed={x.name for x in (root/"wheels").iterdir()}
if expected!=observed:
    raise SystemExit("Unexpected or absent wheel files")
print("VERIFIED_LINUX_WHEEL_MANIFEST",actual,flush=True)
PY
python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --disable-pip-version-check \
  --no-index --only-binary=:all: --find-links "$INPUT/wheels" \
  -r "$INPUT/requirements-linux-py313.txt"
"$VENV/bin/python" -m pip check
echo "RESTORED_OFFLINE_LINUX_WHEELS"
