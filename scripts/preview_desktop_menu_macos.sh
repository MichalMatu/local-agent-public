#!/usr/bin/env bash
# Operator-initiated disposable macOS preview only: never changes the agent.
set -euo pipefail

usage() {
  printf 'Usage: bash scripts/preview_desktop_menu_macos.sh --check|--preview\n' >&2
  exit 2
}

if [[ "$#" -ne 1 ]]; then usage; fi
mode="$1"
case "$mode" in
  --check|--preview) ;;
  *) usage ;;
esac

if [[ "$(uname -s)" != "Darwin" ]]; then
  printf 'This preview is for interactive macOS only.\n' >&2
  exit 2
fi
if [[ "$mode" == "--preview" ]] && { [[ ! -t 0 ]] || [[ ! -t 1 ]]; }; then
  printf 'Interactive preview requires a local user Terminal; background agents must not launch it.\n' >&2
  exit 2
fi

readonly source_root="$HOME/local-agent"
readonly tested_sha="84255bc868730ffc25da07189a59245095a77446"
readonly branch="work/independent-host-control-plane-20261015"
origin="$(git -C "$source_root" remote get-url origin)"
case "$origin" in
  git@github.com:MichalMatu/local-agent-public|git@github.com:MichalMatu/local-agent-public.git|\
  https://github.com/MichalMatu/local-agent-public|https://github.com/MichalMatu/local-agent-public.git|\
  ssh://git@github.com/MichalMatu/local-agent-public|ssh://git@github.com/MichalMatu/local-agent-public.git) ;;
  *)
    printf 'Refusing an unrecognized Local Agent Git origin.\n' >&2
    exit 3
    ;;
esac

root="$(mktemp -d /private/tmp/local-agent-menu-preview-XXXXXXXX)"
cleanup() {
  # Cleanup is confined to our own freshly-created directory.
  if [[ -d "$root" ]]; then
    rm -rf -- "$root"
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

git clone --quiet --no-tags --single-branch --branch "$branch" "$origin" "$root/source"
git -C "$root/source" checkout --detach --quiet "$tested_sha"
if [[ "$(git -C "$root/source" rev-parse HEAD)" != "$tested_sha" ]]; then
  printf 'Exact preview source SHA verification failed.\n' >&2
  exit 4
fi

"$source_root/.venv/bin/python" -m venv "$root/qt-env"
"$root/qt-env/bin/python" -m pip install \
  --quiet --no-input --no-cache-dir --disable-pip-version-check \
  --only-binary=:all: -r "$root/source/requirements-desktop.txt"

# Qt uses the operator's native user session for --preview. Inherited
# debugging/plugin flags must not silently redirect it to an offscreen UI.
unset QT_QPA_PLATFORM QT_PLUGIN_PATH QT_QPA_PLATFORM_PLUGIN_PATH
unset PYTHONPATH PYTHONHOME

cd "$root/source"
if [[ "$mode" == "--check" ]]; then
  "$root/qt-env/bin/python" -m local_agent.desktop.tray --check
  printf 'Desktop preview preflight PASS, no window/menu opened.\n'
else
  printf 'Starting the temporary Local Agent menu. Choose Quit Menu to exit.\n'
  "$root/qt-env/bin/python" -m local_agent.desktop.tray
  printf 'Temporary Local Agent menu exited; cleaning preview files.\n'
fi
