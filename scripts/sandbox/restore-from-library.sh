#!/usr/bin/env bash
set -euo pipefail
# Standalone offline bootstrap. Materialize verified Library files before invoking.
# Usage: bash restore-from-library.sh <sha> <source-dir> <wheelhouse.tgz>
#        <wheelhouse.tgz.sha256> <openssh-client.deb> <fresh-output-dir>
SHA="${1:?exact Git commit SHA required}"
INPUT="${2:?directory containing source bundle + checksum + source-git-sha.txt required}"
WHEELS="${3:?Linux wheelhouse .tar.gz required}"
WHEELS_SHA="${4:?Linux wheelhouse .sha256 required}"
DEB="${5:?pinned Debian openssh-client .deb required}"
DEST="${6:?fresh destination directory required}"
[[ "$SHA" =~ ^[a-f0-9]{40}$ ]] || { echo 'UNVERIFIED: invalid source SHA' >&2; exit 2; }
[[ ! -e "$DEST" ]] || { echo 'UNVERIFIED: destination exists; refusing overwrite' >&2; exit 2; }
[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || {
  echo 'UNVERIFIED: Linux x86_64 required' >&2; exit 2;
}
python3 - <<'PY'
from pathlib import Path
import platform, sys
assert sys.version_info[:2] == (3, 13), 'UNVERIFIED: Python 3.13 required'
assert platform.system() == 'Linux' and platform.machine() == 'x86_64'
info = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
assert info.get('ID', '').strip('"') == 'debian' and info.get('VERSION_ID', '').strip('"') == '13', 'UNVERIFIED: Debian 13 required'
PY
[[ -r "$WHEELS" && -r "$WHEELS_SHA" && -r "$DEB" ]] || {
  echo 'UNVERIFIED: missing offline dependencies' >&2; exit 2;
}
[[ -s "$INPUT/source-git-sha.txt" ]] || { echo 'UNVERIFIED: missing source manifest' >&2; exit 2; }
[[ "$(cat "$INPUT/source-git-sha.txt")" == "$SHA" ]] || {
  echo 'UNVERIFIED: source manifest mismatch' >&2; exit 2;
}
BUNDLE="local-agent-source-${SHA}.bundle"
[[ -s "$INPUT/$BUNDLE" && -s "$INPUT/$BUNDLE.sha256" ]] || {
  echo 'UNVERIFIED: exact-SHA Git bundle missing' >&2; exit 2;
}
python3 - "$INPUT" "$BUNDLE" "$WHEELS" "$WHEELS_SHA" "$DEB" <<'PY'
import hashlib
from pathlib import Path
import sys
src, bundle, wheels, wheelsha, deb = sys.argv[1:]
expected_deb = '8fff343654f86c3b3266f94cd338c7b4da2e386da029327815118ea00bce03b9'
def check(file: Path, checksum: Path):
    fields = checksum.read_text(encoding='ascii').strip().split('  ')
    if len(fields) != 2 or fields[1] != file.name or len(fields[0]) != 64:
        raise SystemExit(f'UNVERIFIED: invalid SHA-256 manifest {checksum}')
    if hashlib.sha256(file.read_bytes()).hexdigest() != fields[0]:
        raise SystemExit(f'UNVERIFIED: checksum mismatch {file.name}')
    return fields[0]
check(Path(src)/bundle, Path(src)/(bundle + '.sha256'))
check(Path(wheels), Path(wheelsha))
if hashlib.sha256(Path(deb).read_bytes()).hexdigest() != expected_deb:
    raise SystemExit('UNVERIFIED: Debian package checksum mismatch')
print('VERIFIED_SOURCE_WHEELS_AND_DEBIAN', flush=True)
PY
mkdir -p "$DEST"
# A failure can leave only a disposable, clearly named destination. No overwrite.
mkdir -p "$DEST/input" "$DEST/runtime"
# Reuse the repository's own exact Git-SHA source validation, without network.
# Bootstrap has no checked-out source yet; perform equivalent hash/HEAD guards.
git clone --quiet --no-checkout "$INPUT/$BUNDLE" "$DEST/source"
git -C "$DEST/source" checkout --quiet --detach "$SHA"
[[ "$(git -C "$DEST/source" rev-parse HEAD)" == "$SHA" ]] || exit 3
# Cached Linux wheels remain valid only while direct source pins match.
python3 - "$WHEELS" "$DEST/source/scripts/sandbox/requirements-linux-py313.txt" <<'PIN_CHECK'
from pathlib import Path
import sys
import tarfile
archive, pins = Path(sys.argv[1]), Path(sys.argv[2])
if not pins.is_file():
    raise SystemExit('UNVERIFIED: source Linux dependency pins missing')
with tarfile.open(archive, 'r:gz') as tf:
    try:
        member = tf.getmember('offline/requirements-linux-py313.txt')
    except KeyError:
        raise SystemExit('UNVERIFIED: offline dependency pins missing')
    if not member.isfile() or member.size > 16384:
        raise SystemExit('UNVERIFIED: invalid offline dependency pins')
    content = tf.extractfile(member)
    if content is None or content.read(16385) != pins.read_bytes():
        raise SystemExit('UNVERIFIED: offline dependency pins differ from source; rebuild wheelhouse')
print('VERIFIED_OFFLINE_DEPENDENCY_PINS_MATCH_SOURCE', flush=True)
PIN_CHECK
[[ -z "$(git -C "$DEST/source" status --porcelain --untracked-files=normal)" ]] || exit 3
python3 - "$WHEELS" "$DEST/input" <<'PY'
import sys, tarfile
from pathlib import Path, PurePosixPath
archive, target = Path(sys.argv[1]), Path(sys.argv[2])
with tarfile.open(archive, 'r:gz') as tf:
    seen = set()
    members = tf.getmembers()
    if not members or sum(m.size for m in members) > 512 * 1024 * 1024:
        raise SystemExit('UNVERIFIED: wheelhouse empty or unbounded')
    for member in members:
        name = PurePosixPath(member.name)
        if (name.is_absolute() or '..' in name.parts or
                (not member.isfile() and not member.isdir()) or
                member.name in seen or name.parts[0] != 'offline'):
            raise SystemExit('UNVERIFIED: unsafe wheelhouse member')
        seen.add(member.name)
    tf.extractall(target, filter='data')
print('VERIFIED_OFFLINE_ARCHIVE_EXTRACTED', flush=True)
PY
if [[ -e /usr/bin/ssh ]]; then
  # An already available real system SSH is acceptable; never overwrite it.
  python3 - "$DEST/source" <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from scripts.verify_local import system_openssh_ready
if not system_openssh_ready():
    raise SystemExit('UNVERIFIED: existing system SSH is unusable')
PY
  echo 'SYSTEM_OPENSSH_EXISTING_REAL_CLIENT_VERIFIED'
else
  bash "$DEST/source/scripts/sandbox/restore-system-openssh.sh" "$DEB"
fi
bash "$DEST/source/scripts/sandbox/restore-linux-wheels.sh" "$DEST/input/offline" "$DEST/runtime/venv"
(
  cd "$DEST/source"
  export PATH="$DEST/runtime/venv/bin:$PATH" PYTHONNOUSERSITE=1
  python scripts/sandbox/doctor.py
  test "$(git rev-parse HEAD)" = "$SHA"
  test -z "$(git status --porcelain --untracked-files=normal)"
)
printf 'SOURCE_DIR=%s\nVENV_DIR=%s\nSOURCE_SHA=%s\n' "$DEST/source" "$DEST/runtime/venv" "$SHA"
echo 'SANDBOX_READY: run verify_local.py --profile sandbox for this exact SHA'
