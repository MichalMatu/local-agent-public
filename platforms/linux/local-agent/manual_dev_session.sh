#!/usr/bin/env bash
# Operator-attended, bounded Linux Agent development session. Never installs a service.
set -Eeuo pipefail

usage() {
  echo "usage: bash manual_dev_session.sh --check|--serve CONFIG STATE RELEASE_ROOT SOURCE_SHA PILOT_STOP HOST_ID INSTALLATION_ID [SECONDS]" >&2
  exit 64
}
[[ $# -ge 8 ]] || usage
mode=$1; shift
[[ "$mode" == "--check" || "$mode" == "--serve" ]] || usage
if [[ "$mode" == "--check" ]]; then
  [[ $# -eq 7 ]] || usage
else
  [[ $# -eq 8 ]] || usage
fi
cfg=$1 state=$2 source_root=$3 expected_sha=$4 pilot_stop=$5 expected_host=$6 expected_installation=$7
duration=${8:-}
py=${LINUX_AGENT_PYTHON:-$(command -v python3 || :)}

[[ "$cfg" == /* && "$state" == /* && "$source_root" == /* && "$pilot_stop" == /* ]] || usage
[[ "$expected_sha" =~ ^[a-f0-9]{40}$ ]] || usage
[[ -f "$pilot_stop" && ! -L "$pilot_stop" ]] || { echo "original pilot STOP missing or unsafe" >&2; exit 1; }
[[ -f "$state/EMERGENCY_STOP" && ! -L "$state/EMERGENCY_STOP" ]] || { echo "isolated MVP STOP required" >&2; exit 1; }
[[ -f "$cfg" && ! -L "$cfg" && -d "$state" && ! -L "$state" ]] || { echo "missing or unsafe MVP config/state" >&2; exit 1; }
[[ -d "$source_root" && ! -L "$source_root" ]] || { echo "release source missing or unsafe" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" && "$(id -u)" -ne 0 ]] || { echo "requires unprivileged Linux account" >&2; exit 1; }
[[ -x "$py" ]] || { echo "Linux Python executable missing" >&2; exit 1; }
[[ "$(git -C "$source_root" rev-parse HEAD)" == "$expected_sha" ]] || { echo "release SHA mismatch" >&2; exit 1; }
[[ -z "$(git -C "$source_root" status --porcelain --untracked-files=all)" ]] || { echo "release tree is dirty" >&2; exit 1; }
if [[ "$mode" == "--serve" ]]; then
  [[ "$duration" =~ ^[0-9]+$ ]] || usage
  (( 10#$duration >= 30 && 10#$duration <= 3600 )) || { echo "session duration must be 30..3600 seconds" >&2; exit 1; }
fi

export PYTHONPATH="$source_root/platforms/linux/local-agent"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1

"$py" - "$cfg" "$state" "$pilot_stop" "$expected_host" "$expected_installation" <<'PY'
import os
import sys
from pathlib import Path
from linux_agent.agent import Agent, config_from_file, daemon_lock

config_file, state, pilot_stop = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
host, installation = sys.argv[4], sys.argv[5]
config = config_from_file(config_file)
assert config["host_id"] == host, "wrong target host"
assert config["installation_id"] == installation, "wrong installation"
assert config.get("enabled") is True, "disabled configuration"
assert config["host_intake"] == {"enabled": True, "authorization": "github_writers"}
assert state.resolve() != pilot_stop.parent.resolve(), "pilot state alias"
assert pilot_stop.is_file() and not pilot_stop.is_symlink(), "pilot STOP missing"
assert (state / "EMERGENCY_STOP").is_file(), "new STOP missing"
Agent(config, state)
with daemon_lock(state):
    pass  # Must not share a live scheduler's lock.
print("PREFLIGHT_PASS: exact host/installation, private state, both STOPs, free daemon lock", flush=True)
PY

if [[ "$mode" == "--check" ]]; then
  echo "CHECK_ONLY: no intake enabled; both STOP files retained"
  exit 0
fi

rearm() {
  "$py" -B -m linux_agent --config "$cfg" --state "$state" disable ||
    { echo "CRITICAL: could not rearm isolated STOP at $state/EMERGENCY_STOP" >&2; return 1; }
}
trap rearm EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
rm -- "$state/EMERGENCY_STOP"
echo "SESSION_STARTED: bounded foreground Linux Agent on $expected_host / $expected_installation"
set +e
timeout -s TERM -k 10s "$duration" "$py" -u -B -m linux_agent --config "$cfg" --state "$state" serve
rc=$?
set -e
echo "SERVE_EXIT_CODE=$rc"
# The supervisor has exited. Close admission before any potentially failing
# diagnostic; the EXIT trap remains armed until status is verified.
rearm
if [[ "$rc" -ne 0 && "$rc" -ne 124 ]]; then
  echo "serve failed unexpectedly; isolated STOP will be rearmed" >&2
  exit 1
fi
# timeout's expected 124 is acceptable only if its child released daemon flock.
"$py" - "$state" <<'PY'
import sys
from pathlib import Path
from linux_agent.agent import daemon_lock
with daemon_lock(Path(sys.argv[1])):
    pass
print("PROCESS_STOPPED: lifetime daemon lock released", flush=True)
PY
status=$("$py" -B -m linux_agent --config "$cfg" --state "$state" status)
printf '%s\n' "$status"
"$py" -c 'import json,sys; assert json.loads(sys.argv[1])["status"] == "disabled"' "$status"
[[ -f "$pilot_stop" && -f "$state/EMERGENCY_STOP" ]] || exit 1
trap - EXIT INT TERM
echo "SESSION_FINISHED: original and independent STOP preserved"
