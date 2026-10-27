#!/usr/bin/env bash
# Operator-only Finder test: no background GUI launch, install, auto-start or daemon changes.
set -euo pipefail
readonly GUI_SHA="9bfd102c0d663304acba52272a9be04e9e39fc4b"
readonly BRANCH="work/independent-host-control-plane-20261015"
readonly MARKER=".local-agent-menu-finder-preview"
usage() {
  printf 'Usage: bash scripts/prepare_desktop_menu_finder_macos.sh --check|--prepare\n' >&2
  printf '       bash scripts/prepare_desktop_menu_finder_macos.sh --cleanup ABSOLUTE_PREVIEW_DIRECTORY\n' >&2
  exit 2
}
if [[ "$#" -ne 1 && "$#" -ne 2 ]]; then usage; fi
mode="$1"
case "$mode" in
  --check|--prepare) [[ "$#" -eq 1 ]] || usage ;;
  --cleanup) [[ "$#" -eq 2 ]] || usage ;;
  *) usage ;;
esac
if [[ "$(uname -s)" != "Darwin" ]]; then
  printf 'This Finder preview is supported on macOS only.\n' >&2
  exit 2
fi
safe_preview_dir() {
  local dir="$1"
  [[ "$dir" =~ ^/private/tmp/local-agent-menu-finder-[A-Za-z0-9]{8}$ ]] || return 1
  [[ -d "$dir" && ! -L "$dir" ]] || return 1
  [[ "$(stat -f %u "$dir")" == "$(id -u)" ]] || return 1
  [[ "$(stat -f %Lp "$dir")" == "700" ]] || return 1
  [[ -f "$dir/$MARKER" && ! -L "$dir/$MARKER" ]] || return 1
  [[ "$(cat "$dir/$MARKER")" == "Local Agent Finder Preview $GUI_SHA" ]] || return 1
}
if [[ "$mode" == "--cleanup" ]]; then
  if ! safe_preview_dir "$2"; then
    printf 'Refusing cleanup: unrecognized or unsafe preview directory.\n' >&2
    exit 3
  fi
  printf 'Removing Finder preview AFTER Quit Menu: %s\n' "$2"
  rm -rf -- "$2"
  exit 0
fi
readonly SOURCE="$HOME/local-agent"
if [[ ! -x "$SOURCE/.venv/bin/python" ]]; then
  printf 'Missing Local Agent Python interpreter.\n' >&2
  exit 3
fi
origin="$(git -C "$SOURCE" remote get-url origin)"
case "$origin" in
  git@github.com:MichalMatu/local-agent-public|git@github.com:MichalMatu/local-agent-public.git|\
  https://github.com/MichalMatu/local-agent-public|https://github.com/MichalMatu/local-agent-public.git|\
  ssh://git@github.com/MichalMatu/local-agent-public|ssh://git@github.com/MichalMatu/local-agent-public.git) ;;
  *) printf 'Refusing unrecognized Git origin.\n' >&2; exit 3 ;;
esac
if [[ "$mode" == "--check" ]]; then
  command -v git >/dev/null
  command -v open >/dev/null
  printf 'FINDER_PREVIEW_PREFLIGHT_PASS; no build or GUI launch.\n'
  exit 0
fi
if [[ ! -t 0 || ! -t 1 ]]; then
  printf 'Use a logged-in interactive Mac Terminal for --prepare.\n' >&2
  exit 2
fi
umask 077
root="$(mktemp -d /private/tmp/local-agent-menu-finder-XXXXXXXX)"
prepared=0
cleanup_on_failure() {
  if [[ "$prepared" != "1" && -d "$root" && ! -L "$root" ]]; then
    rm -rf -- "$root"
  fi
}
trap cleanup_on_failure EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
printf 'Local Agent Finder Preview %s\n' "$GUI_SHA" > "$root/$MARKER"
git clone --quiet --no-tags --single-branch --branch "$BRANCH" "$origin" "$root/source"
git -C "$root/source" checkout --detach --quiet "$GUI_SHA"
test "$(git -C "$root/source" rev-parse HEAD)" = "$GUI_SHA"
"$SOURCE/.venv/bin/python" -m venv "$root/qt-env"
"$root/qt-env/bin/python" -m pip install \
  --quiet --no-input --no-cache-dir --disable-pip-version-check --only-binary=:all: \
  -r "$root/source/requirements-desktop.txt" \
  -r "$root/source/requirements-desktop-build.txt"
(cd "$root/source" && bash scripts/build_desktop_menu_macos.sh \
  --build "$root/output" "$root/qt-env/bin/python")
app="$root/output/dist/Local Agent Menu.app"
test -d "$app"
test -x "$app/Contents/MacOS/LocalAgentMenu"
"$app/Contents/MacOS/LocalAgentMenu" --check
# Persist one private cleanup helper independent of the bootstrap checkout.
cp -- "$0" "$root/cleanup-preview.sh"
chmod 700 "$root/cleanup-preview.sh"
# Reveal in Finder without launching the app or changing Login Items.
open -R "$app"
prepared=1
printf 'FINDER_PREVIEW_READY: %s\n' "$app"
printf 'AFTER QUIT, CLEANUP WITH:\n'
printf 'bash "%s/cleanup-preview.sh" --cleanup "%s"\n' "$root" "$root"
printf 'Keep the directory until the GUI has exited.\n'
