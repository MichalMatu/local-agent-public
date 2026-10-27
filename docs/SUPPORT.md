# Supported platforms and requirements

Local Agent 4.20.13 is the first public source release. Support describes the
named tested profiles and available implementation, not every device or OS
combination. Each new installation requires its own acceptance task.

| Profile | Service | Implemented scope |
| --- | --- | --- |
| Apple Silicon macOS | launchd | Host commands, maintained Host Ops and optional project repositories |
| Debian 13 x86-64 | user-systemd | Bounded host commands and explicit Linux project configuration |
| Pi Zero 2 W, Debian 13 ARM64 | user-systemd | Shared Linux engine with Pi admission and resource controls |
| Samsung S22+, Android 16, Termux | Private runit | Host command bundle; no project execution |

The native pilot interpreters are Python 3.13.9 on Mac, 3.13.5 on Debian/Pi
and 3.14.6 on Termux. Source syntax targets Python 3.12+, which does not imply
native qualification of every interpreter. Git and an authenticated private
GitHub transport are required. Mac runtime dependencies are hash-locked.
Linux/Pi/Termux command engines use Python's standard library and Git/SSH.

## Limits

- Windows and iOS do not have a supported native Agent.
- Ubuntu, Raspberry Pi OS and other hardware need their own native checks.
- Android force-stop, Doze, battery restrictions and OEM background policy
  can stop the app. A wake lock and one successful task are not a 24/7 guarantee.
- The macOS GUI/browser extension prototypes are frozen and are not the
  first-install route. Use the CLI/native service.
- Commands use the service OS user's permissions; privileged actions still
  need actual OS permission. Local Agent is not a sandbox.
- Task/result transport must be private. The public source repository is
  never the customer's execution inbox.

## Qualification

Release source checks are separate from installation acceptance. Actual native
service identity, persistence, STOP, independent recovery and a fresh Git
roundtrip must be checked on the selected machine. Keep skipped, failed and
unverified evidence explicit. New-customer provisioning is not certified by
historical pilot receipts alone.

[Install](../INSTALL_WITH_CODEX.md) · [operations](OPERATIONS.md).
