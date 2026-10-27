#!/usr/bin/env bash
set -euo pipefail
# Offline root-only installation into an ephemeral Debian 13 amd64 sandbox.
# Does not invoke maintainer scripts, apt, GitHub, or any network endpoint.
# Usage: bash scripts/sandbox/restore-system-openssh.sh <official-debian-deb>
DEB="${1:?The official pinned Debian 13 openssh-client .deb is required}"
EXPECTED="8fff343654f86c3b3266f94cd338c7b4da2e386da029327815118ea00bce03b9"
PACKAGE_VERSION="1:10.0p1-7+deb13u4"
[[ -f "$DEB" ]] || { echo "UNVERIFIED: package missing"; exit 2; }
if command -v sha256sum >/dev/null 2>&1; then
  observed="$(sha256sum "$DEB" | awk '{print $1}')"
elif command -v shasum >/dev/null 2>&1; then
  observed="$(shasum -a 256 "$DEB" | awk '{print $1}')"
else
  echo "UNVERIFIED: SHA-256 utility missing" >&2
  exit 2
fi
[[ "$observed" == "$EXPECTED" ]] || {
  echo "UNVERIFIED: Debian OpenSSH package SHA256 mismatch" >&2
  exit 2
}
[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || {
  echo "UNVERIFIED: only Linux x86_64" >&2; exit 2;
}
[[ -r /etc/os-release ]] || exit 2
# shellcheck disable=SC1091
source /etc/os-release
[[ "${ID:-}" == "debian" && "${VERSION_ID:-}" == "13" ]] || {
  echo "UNVERIFIED: only Debian 13 sandbox" >&2; exit 2;
}
command -v dpkg-deb >/dev/null || { echo "UNVERIFIED: dpkg-deb missing"; exit 2; }
[[ "$(dpkg-deb -f "$DEB" Package)" == openssh-client ]] || exit 2
[[ "$(dpkg-deb -f "$DEB" Architecture)" == amd64 ]] || exit 2
[[ "$(dpkg-deb -f "$DEB" Version)" == "$PACKAGE_VERSION" ]] || exit 2
if [[ -e /usr/bin/ssh ]]; then
  echo "UNVERIFIED: /usr/bin/ssh already exists; refusing overwrite" >&2
  exit 2
fi
[[ "$(id -u)" == 0 ]] || { echo "UNVERIFIED: sandbox root needed" >&2; exit 2; }
scratch="$(mktemp -d /tmp/local-agent-ssh-extract.XXXXXX)"
trap 'rm -rf "$scratch"' EXIT
dpkg-deb -x "$DEB" "$scratch"
[[ -f "$scratch/usr/bin/ssh" ]] || { echo "UNVERIFIED: OpenSSH binary missing in package"; exit 2; }
python3 - "$scratch/usr/bin/ssh" <<'PY'
from pathlib import Path
import sys
if Path(sys.argv[1]).read_bytes()[:4] != b"\x7fELF":
    raise SystemExit("UNVERIFIED: ELF executable required")
PY
install -m 0755 "$scratch/usr/bin/ssh" /usr/bin/ssh
if ! version="$(/usr/bin/ssh -V 2>&1)" || [[ "$version" != OpenSSH_10.0p2\ Debian-7+deb13u4* ]]; then
  rm -f /usr/bin/ssh
  echo "UNVERIFIED: extracted client fails version check; removed" >&2
  exit 2
fi
if ! /usr/bin/ssh -G -F /dev/null -o BatchMode=yes -o StrictHostKeyChecking=yes example.test >/dev/null 2>&1; then
  rm -f /usr/bin/ssh
  echo "UNVERIFIED: extracted client fails -G; removed" >&2
  exit 2
fi
echo "PINNED_DEBIAN_OPENSSH_READY=$version"
echo "PACKAGE_SHA256=$observed"
echo "SYSTEM_CLIENT_PATH=/usr/bin/ssh"
