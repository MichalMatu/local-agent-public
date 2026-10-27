#!/usr/bin/env bash
# External Debian user terminal only. Never run via an Agent task or Mac Agent SSH.
set -Eeuo pipefail
umask 077

usage() {
  echo "Usage: bash operator_upgrade_v2.sh --prepare | --switch" >&2
  exit 2
}
[[ "$#" -eq 1 ]] || usage
MODE="$1"
[[ "$MODE" == "--prepare" || "$MODE" == "--switch" ]] || usage
[[ "$(id -un)" == "admin" ]] || { echo "REFUSED: not Debian admin" >&2; exit 2; }
[[ -t 0 && -t 1 ]] || { echo "REFUSED: independent interactive terminal required" >&2; exit 2; }
[[ -z "${LOCAL_AGENT_LEASE_FDS:-}" && -z "${LOCAL_AGENT_LEASE_KEYS_DIGEST:-}" ]] || {
  echo "REFUSED: inherited Agent task lease" >&2; exit 2;
}
ROOT=/home/admin/Projects/local-agent
RELEASES=/home/admin/.local/share/local-agent/releases
STATE=/home/admin/.local/state/linux-agent/mvp-20261024-01
CONFIG=/home/admin/.config/linux-agent/mvp-20261024-01.json
PY=/usr/bin/python3.13
BASE=624564e6149efa7dabf358384fe72460a877b95b
NEW=c6782dbd17e8164c8f98a19cc7f971f8ee9a7c88
CANDIDATE="$RELEASES/$NEW"
BASELINE="$RELEASES/$BASE"

test -x "$PY"
test -d "$ROOT/.git"
test -d "$BASELINE/.git"
test -f "$CONFIG"
test -d "$STATE"
test ! -e "$STATE/EMERGENCY_STOP"
test -f "$STATE/linux-service-upgrade-v1.json"
test ! -e "$STATE/linux-service-upgrade-v2.json"
test "$(systemctl --user is-active local-agent-linux.service)" = active
test "$(systemctl --user is-enabled local-agent-linux.service)" = enabled
test "$(git -C "$BASELINE" rev-parse HEAD)" = "$BASE"
test -z "$(git -C "$BASELINE" status --porcelain --untracked-files=all)"
test -z "$(git -C "$ROOT" status --porcelain --untracked-files=all)"
# Only the operator's development checkout is fetched; installed service untouched.
git -C "$ROOT" fetch --no-tags origin refs/heads/main
git -C "$ROOT" cat-file -e "$NEW^{commit}"
if [[ ! -e "$CANDIDATE" ]]; then
  git clone --no-checkout -q "$ROOT" "$CANDIDATE"
  git -C "$CANDIDATE" checkout -q --detach "$NEW"
fi
test -d "$CANDIDATE/.git"
test "$(git -C "$CANDIDATE" rev-parse HEAD)" = "$NEW"
test -z "$(git -C "$CANDIDATE" status --porcelain --untracked-files=all)"
export PYTHONPATH="$CANDIDATE/platforms/linux/local-agent"
export PYTHONDONTWRITEBYTECODE=1
ARGS=(
  --candidate-root "$CANDIDATE" --candidate-sha "$NEW"
  --baseline-root "$BASELINE" --baseline-sha "$BASE"
  --config "$CONFIG" --state "$STATE"
)
echo "Checking native v2 controller + publication safety regressions at $NEW"
"$PY" -B -m unittest discover -s "$CANDIDATE/platforms/linux/local-agent/tests" -p 'test_linux_service_upgrade*.py' -q
test -z "$(git -C "$CANDIDATE" status --porcelain --untracked-files=all)"
echo "Operator inspection (read-only)"
"$PY" -B -m linux_agent.service_upgrade_v2 inspect "${ARGS[@]}"
if [[ "$MODE" == "--prepare" ]]; then
  echo "PREPARED_ONLY: installed Debian service unchanged"
  exit 0
fi
echo "External operator switch requested; preserving prior journals and rollback"
"$PY" -B -m linux_agent.service_upgrade_v2 switch "${ARGS[@]}"
echo "Inspecting newly installed service"
"$PY" -B -m linux_agent.service_upgrade_v2 inspect "${ARGS[@]}"
echo "Installed MainPID / state:"
systemctl --user show local-agent-linux.service --property=MainPID --property=ActiveState --property=FragmentPath
echo "SWITCH_COMPLETED: run an independent new Git task/result latency check next"
