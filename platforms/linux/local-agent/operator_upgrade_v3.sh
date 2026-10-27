#!/usr/bin/env bash
# Debian user terminal ONLY: never from a Local Agent task or Mac Agent SSH.
# Immutable third operator release. Do not rerun after v3 journal committed.
set -Eeuo pipefail
umask 077
[[ "$#" -eq 1 && ( "$1" == "--prepare" || "$1" == "--switch" ) ]] || {
  echo "Usage: bash operator_upgrade_v3.sh --prepare | --switch" >&2; exit 2;
}
MODE="$1"
[[ "$(id -un)" == "admin" && -t 0 && -t 1 ]] || {
  echo "REFUSED: independent interactive Debian terminal as admin required" >&2; exit 2;
}
[[ -z "${LOCAL_AGENT_LEASE_FDS:-}" && -z "${LOCAL_AGENT_LEASE_KEYS_DIGEST:-}" ]] || {
  echo "REFUSED: inherited Agent task lease" >&2; exit 2;
}
ROOT=/home/admin/Projects/local-agent
RELEASES=/home/admin/.local/share/local-agent/releases
STATE=/home/admin/.local/state/linux-agent/mvp-20261024-01
CONFIG=/home/admin/.config/linux-agent/mvp-20261024-01.json
PY=/usr/bin/python3.13
BASE=c6782dbd17e8164c8f98a19cc7f971f8ee9a7c88
NEW=c5b8341fbd62ad2caa685614a4464d4e843c7742
BASELINE="$RELEASES/$BASE"
CANDIDATE="$RELEASES/$NEW"
test -x "$PY"
test -d "$ROOT/.git"
test -d "$BASELINE/.git"
test -f "$CONFIG"
test -d "$STATE"
test ! -e "$STATE/EMERGENCY_STOP"
test -f "$STATE/linux-service-upgrade-v1.json"
test -f "$STATE/linux-service-upgrade-v2.json"
test ! -e "$STATE/linux-service-upgrade-v3.json"
test "$(systemctl --user is-active local-agent-linux.service)" = active
test "$(systemctl --user is-enabled local-agent-linux.service)" = enabled
test "$(git -C "$BASELINE" rev-parse HEAD)" = "$BASE"
test -z "$(git -C "$BASELINE" status --porcelain --untracked-files=all)"
test -z "$(git -C "$ROOT" status --porcelain --untracked-files=all)"
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
echo "Testing exact Debian v3 fastpath source: $NEW"
"$PY" -B -m compileall -q "$CANDIDATE/platforms/linux/local-agent/linux_agent"
"$PY" -B -m unittest discover -s "$CANDIDATE/platforms/linux/local-agent/tests" -p 'test_linux_service_upgrade*.py' -q
test -z "$(git -C "$CANDIDATE" status --porcelain --untracked-files=all)"
echo "Verifying prior v1/v2 journal provenance, active service and proposed new unit"
"$PY" -B -m linux_agent.service_upgrade_v3 inspect "${ARGS[@]}"
if [[ "$MODE" == "--prepare" ]]; then
  echo "PREPARED_ONLY: release staged, all checks PASS; live systemd service unchanged"
  exit 0
fi
echo "Independent Debian operator transaction v3 requested"
"$PY" -B -m linux_agent.service_upgrade_v3 switch "${ARGS[@]}"
echo "Verifying live installed service after third transaction"
"$PY" -B -m linux_agent.service_upgrade_v3 inspect "${ARGS[@]}"
systemctl --user show local-agent-linux.service --property=MainPID --property=ActiveState --property=FragmentPath
echo "V3_SWITCH_COMPLETED: run fresh Git acceptance task; do NOT rerun v3 switch"
