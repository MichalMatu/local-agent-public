#!/usr/bin/env bash
set -euo pipefail
# Usage: bash scripts/sandbox/pack-source.sh <expected-40-char-sha> <new-output-directory>
# Run only from a clean, exact-SHA source checkout. No network, secrets or daemon effects.
EXPECTED="${1:?Exact Git SHA required}"
OUTPUT="${2:?New output directory required}"
[[ "$EXPECTED" =~ ^[0-9a-f]{40}$ ]] || { echo "Invalid expected SHA" >&2; exit 2; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
[[ "$(git -C "$ROOT" rev-parse HEAD)" == "$EXPECTED" ]] || { echo "HEAD mismatch" >&2; exit 3; }
[[ -z "$(git -C "$ROOT" status --porcelain --untracked-files=normal)" ]] || {
  echo "Dirty checkout" >&2; exit 4;
}
[[ ! -e "$OUTPUT" ]] || { echo "Output directory already exists" >&2; exit 5; }
mkdir -p "$OUTPUT"
trap 'rm -f "$OUTPUT/.partial.bundle"' EXIT
git -C "$ROOT" bundle create "$OUTPUT/.partial.bundle" HEAD
git -C "$ROOT" bundle verify "$OUTPUT/.partial.bundle" >/dev/null
HEADS="$(git bundle list-heads "$OUTPUT/.partial.bundle")"
[[ "$(printf '%s\n' "$HEADS" | wc -l | tr -d ' ')" == "1" ]] || exit 6
[[ "$HEADS" == "$EXPECTED "* ]] || { echo "Bundle head mismatch" >&2; exit 7; }
NAME="local-agent-source-${EXPECTED}.bundle"
mv "$OUTPUT/.partial.bundle" "$OUTPUT/$NAME"
if command -v sha256sum >/dev/null; then
  HASH="$(sha256sum "$OUTPUT/$NAME" | cut -d ' ' -f 1)"
else
  HASH="$(shasum -a 256 "$OUTPUT/$NAME" | cut -d ' ' -f 1)"
fi
printf '%s  %s\n' "$HASH" "$NAME" > "$OUTPUT/$NAME.sha256"
printf '%s\n' "$EXPECTED" > "$OUTPUT/source-git-sha.txt"
printf 'SOURCE_BUNDLE_SHA=%s\nBUNDLE_SHA256=%s\n' "$EXPECTED" "$HASH"
echo "Offline source bundle only. Python/Node dependencies must be packed separately."
