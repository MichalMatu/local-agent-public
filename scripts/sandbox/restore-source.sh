#!/usr/bin/env bash
set -euo pipefail
# Usage: bash scripts/sandbox/restore-source.sh <bundle-dir> <exact-sha> <new-checkout-dir>
# Never downloads packages or checks out unrelated branches.
INPUT="${1:?Bundle directory required}"
EXPECTED="${2:?Exact 40-char SHA required}"
OUTPUT="${3:?New checkout directory required}"
[[ "$EXPECTED" =~ ^[0-9a-f]{40}$ ]] || { echo "Invalid SHA" >&2; exit 2; }
[[ ! -e "$OUTPUT" ]] || { echo "Target exists; refusing overwrite" >&2; exit 3; }
NAME="local-agent-source-${EXPECTED}.bundle"
[[ -s "$INPUT/$NAME" && -s "$INPUT/$NAME.sha256" ]] || {
  echo "Exact-SHA bundle or checksum missing" >&2; exit 4;
}
[[ "$(cat "$INPUT/source-git-sha.txt")" == "$EXPECTED" ]] || {
  echo "Bundle manifest SHA mismatch" >&2; exit 5;
}
EXPECTED_HASH="$(awk 'NR==1 {print $1}' "$INPUT/$NAME.sha256")"
[[ "$EXPECTED_HASH" =~ ^[0-9a-f]{64}$ ]] || exit 6
if command -v sha256sum >/dev/null; then
  ACTUAL_HASH="$(sha256sum "$INPUT/$NAME" | cut -d ' ' -f 1)"
else
  ACTUAL_HASH="$(shasum -a 256 "$INPUT/$NAME" | cut -d ' ' -f 1)"
fi
[[ "$ACTUAL_HASH" == "$EXPECTED_HASH" ]] || { echo "Checksum mismatch" >&2; exit 7; }
HEADS="$(git bundle list-heads "$INPUT/$NAME")"
[[ "$(printf '%s\n' "$HEADS" | wc -l | tr -d ' ')" == "1" ]] || exit 8
[[ "$HEADS" == "$EXPECTED "* ]] || { echo "Bundle reference mismatch" >&2; exit 9; }
git clone --quiet --no-checkout "$INPUT/$NAME" "$OUTPUT"
git -C "$OUTPUT" checkout --quiet --detach "$EXPECTED"
[[ "$(git -C "$OUTPUT" rev-parse HEAD)" == "$EXPECTED" ]] || exit 10
[[ -z "$(git -C "$OUTPUT" status --porcelain --untracked-files=normal)" ]] || exit 11
printf 'RESTORED_EXACT_SHA=%s\nVERIFIED_SHA256=%s\n' "$EXPECTED" "$ACTUAL_HASH"
echo "Source only: run doctor and restore separately pinned Linux dependencies before testing."
