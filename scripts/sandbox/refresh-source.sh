#!/usr/bin/env bash
set -euo pipefail
# Refresh a Library Git bundle to an exact GitHub Connector-resolved branch SHA.
# Usage: refresh-source.sh <expected-sha> <branch> <cached.bundle> <cached.bundle.sha256> <new-output-dir> [remote]
EXPECTED="${1:?expected full Git SHA required}"
BRANCH="${2:?GitHub branch name required}"
CACHED="${3:?cached Git bundle required}"
CHECKSUM="${4:?cached Git bundle checksum required}"
OUTPUT="${5:?new destination required}"
REMOTE="${6:-https://github.com/MichalMatu/local-agent-public.git}"
fail() { printf 'UNVERIFIED: %s\n' "$*" >&2; exit 2; }
[[ "$EXPECTED" =~ ^[0-9a-f]{40}$ ]] || fail 'invalid expected Git SHA'
[[ ! -e "$OUTPUT" ]] || fail 'destination already exists; refusing overwrite'
[[ -s "$CACHED" && -s "$CHECKSUM" ]] || fail 'cached Git bundle/checksum missing'
git check-ref-format --branch "$BRANCH" >/dev/null 2>&1 || fail 'invalid branch'
python3 - "$CACHED" "$CHECKSUM" <<'PY' || exit 2
import hashlib
from pathlib import Path
import sys
bundle, manifest = map(Path, sys.argv[1:])
parts = manifest.read_text(encoding='ascii').strip().split('  ')
if (len(parts) != 2 or len(parts[0]) != 64 or
    any(c not in '0123456789abcdef' for c in parts[0]) or
    parts[1] != bundle.name or
    hashlib.sha256(bundle.read_bytes()).hexdigest() != parts[0]):
    raise SystemExit('UNVERIFIED: invalid cached bundle checksum')
PY
PARENT="$(dirname "$OUTPUT")"
[[ -d "$PARENT" ]] || fail 'destination parent missing'
TMP="$(mktemp -d "$PARENT/.source-refresh.XXXXXXXX")"
trap 'rm -rf -- "$TMP"' EXIT
# A disposable clone is the only checkout touched; no installed daemon state.
git clone -q --no-checkout -- "$CACHED" "$TMP/checkout" || fail 'cached Git bundle cannot be cloned'
if ! git -C "$TMP/checkout" cat-file -e "${EXPECTED}^{commit}" 2>/dev/null; then
    # Resolve head a second time: a moving branch must not be silently accepted.
    LIVE="$(GIT_TERMINAL_PROMPT=0 git -c http.lowSpeedLimit=1024 -c http.lowSpeedTime=15 \
        ls-remote --exit-code "$REMOTE" "refs/heads/$BRANCH" 2>/dev/null)" || fail 'remote source unavailable; use Connector or a lightweight source export'
    [[ "$LIVE" == "$EXPECTED"$'\t'"refs/heads/$BRANCH" ]] || fail 'remote branch SHA differs from pinned expected SHA'
    GIT_TERMINAL_PROMPT=0 git -C "$TMP/checkout" -c http.lowSpeedLimit=1024 -c http.lowSpeedTime=15 \
        fetch -q --no-tags --no-recurse-submodules "$REMOTE" \
        "+refs/heads/$BRANCH:refs/remotes/sandbox/pinned" || fail 'remote Git fetch failed'
    [[ "$(git -C "$TMP/checkout" rev-parse refs/remotes/sandbox/pinned)" == "$EXPECTED" ]] || fail 'branch moved during fetch'
fi
git -C "$TMP/checkout" checkout -q --detach "$EXPECTED" || fail 'exact Git commit missing'
[[ "$(git -C "$TMP/checkout" rev-parse HEAD)" == "$EXPECTED" ]] || fail 'exact HEAD mismatch'
[[ -z "$(git -C "$TMP/checkout" status --porcelain --untracked-files=normal)" ]] || fail 'dirty disposable checkout'
[[ -f "$TMP/checkout/scripts/sandbox/pack-source.sh" ]] || fail 'source packer missing'
bash "$TMP/checkout/scripts/sandbox/pack-source.sh" "$EXPECTED" "$TMP/pack" || fail 'source bundle packaging failed'
NEW="$TMP/pack/local-agent-source-${EXPECTED}.bundle"
[[ -s "$NEW" && -s "$NEW.sha256" ]] || fail 'source export incomplete'
[[ "$(git bundle list-heads "$NEW")" == "$EXPECTED HEAD" ]] || fail 'bundle HEAD mismatch'
git clone -q --no-checkout -- "$NEW" "$TMP/independent" || fail 'bundle re-import failed'
git -C "$TMP/independent" checkout -q --detach "$EXPECTED" || fail 'bundle lacks exact commit'
[[ "$(git -C "$TMP/independent" rev-parse HEAD)" == "$EXPECTED" ]] || fail 'independent Git identity mismatch'
mv -- "$TMP/pack" "$OUTPUT" || fail 'cannot publish verified bundle'
printf 'SOURCE_SYNC_READY sha=%s bundle=%s\n' "$EXPECTED" "$OUTPUT/local-agent-source-${EXPECTED}.bundle"
