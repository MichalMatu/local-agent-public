#!/usr/bin/env bash
# Build only an explicitly requested isolated macOS .app; never install it.
set -euo pipefail

usage() {
  printf 'Usage: bash scripts/build_desktop_menu_macos.sh --check\n' >&2
  printf '   or: bash scripts/build_desktop_menu_macos.sh --build ABSOLUTE_NEW_OUTPUT_DIR PYTHON_VENV\n' >&2
  exit 2
}

if [[ "$(uname -s)" != "Darwin" ]]; then
  printf 'macOS .app prototypes must be built on macOS.\n' >&2
  exit 2
fi

here="$(cd "$(dirname "$0")/.." && pwd -P)"
spec="$here/platforms/macos/native/packaging/LocalAgentMenu.spec"
if [[ ! -f "$spec" || ! -f "$here/scripts/desktop_menu_entry.py" ]]; then
  printf 'Missing desktop packaging input.\n' >&2
  exit 3
fi

if [[ "$#" -eq 1 && "$1" == "--check" ]]; then
  printf 'Desktop bundle inputs present. No packaging or installation performed.\n'
  exit 0
fi
if [[ "$#" -ne 3 || "$1" != "--build" ]]; then usage; fi
out="$2"
python_bin="$3"
case "$out" in
  /*) ;;
  *) printf 'Output must be an absolute path.\n' >&2; exit 3 ;;
esac
if [[ -e "$out" || -L "$out" ]]; then
  printf 'Refusing to overwrite an existing output directory.\n' >&2
  exit 3
fi
if [[ ! -x "$python_bin" ]]; then
  printf 'Missing explicitly selected isolated Python venv.\n' >&2
  exit 3
fi

"$python_bin" - <<'PY'
import importlib.util
import sys
if importlib.util.find_spec("PyInstaller") is None:
    sys.exit("PyInstaller absent in selected isolated Python venv")
if importlib.util.find_spec("PySide6") is None:
    sys.exit("PySide6 absent in selected isolated Python venv")
PY

mkdir "$out"
export PYINSTALLER_CONFIG_DIR="$out/config"
"$python_bin" -m PyInstaller \
  --noconfirm \
  --distpath "$out/dist" \
  --workpath "$out/build" \
  "$spec"

bundle="$out/dist/Local Agent Menu.app"
if [[ ! -d "$bundle" || ! -x "$bundle/Contents/MacOS/LocalAgentMenu" ]]; then
  printf 'PyInstaller did not create the expected macOS app bundle.\n' >&2
  exit 4
fi
/usr/libexec/PlistBuddy -c 'Print :LSUIElement' "$bundle/Contents/Info.plist" | grep -qx 'true'
/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$bundle/Contents/Info.plist" | grep -qx 'local.localagent.menu.preview'
codesign --verify --deep --strict "$bundle"
printf 'MACOS_BUNDLE_PROTOTYPE_PASS %s\n' "$bundle"
printf 'This is NOT notarized, distributed, installed or auto-started.\n'
