# Install Local Agent with Codex

Use local Codex with terminal access on the computer you want to install.
For a different computer, explicitly select it and provide an already trusted
connection. Codex is the temporary installer; Local Agent runs as a native
service after installation.

## Start here

Copy the installation prompt from [README](README.md). Codex must read this
file and the selected platform guide before making installation changes.
Install a stable tagged release and resolve it to a full immutable Git SHA.
Do not install from an unreviewed moving branch or a source archive lacking
verified source identity.

You need:

- A machine covered by [the support table](docs/SUPPORT.md).
- GitHub sign-in and your own private task/result repository.
- Local permission to run the native service as your chosen OS user.
- ChatGPT GitHub access that can publish a new task and read its result.

Codex can prepare tools and configuration within the requested installation.
A missing account authorization or genuine OS-admin credential requires your
action. Routine installation steps do not need repeated confirmations.

## Choose the platform

| Platform | Procedure |
| --- | --- |
| macOS | [Native launchd installation](platforms/macos/installer/INSTALL_WITH_CODEX.md) |
| Debian 13 | [User-systemd installation](platforms/linux/local-agent/INSTALL_WITH_CODEX.md) |
| Pi Zero 2 W / Debian ARM64 | [Pi installation](platforms/raspberry-pi/local-agent/INSTALL_WITH_CODEX.md) |
| Android / Termux | [Private runit installation](platforms/android/local-agent/INSTALL_WITH_CODEX.md) |

## Installation contract for Codex

1. Inspect OS, CPU, OS user, Python, Git, SSH, service manager and any existing
   Agent. Preserve existing source, services, keys, configuration and history.
   Handle an existing installation through its upgrade procedure.
2. Clone the public source, select the stable tag, resolve its SHA and keep
   that source separate from private runtime state and Git transport.
3. Ask the customer to select or authorize creation of a **private** Git task
   repository. Prepare its agent-control branch and verify authenticated
   read/write access. Never use the public source repository as an inbox.
4. Generate a unique host ID and installation UUID. Set private permissions
   on configuration/state. Explicitly configure the customer's repository,
   exact origin/branch and one local github_writers host-intake choice.
   Examples and developer defaults are not customer enrollment.
5. Use the existing platform tools. Read their actual --help, render/preview
   first, and start one native service from outside the Agent. Projects are
   optional; a host-only installation is the starting configuration.
6. Verify the actual service PID, user, source SHA, identities, private paths,
   Git access and STOP. Verify native startup persistence where applicable;
   distinguish a service restart from a physical reboot test.
7. Publish one new harmless task addressed to the exact host and installation.
   Read its authoritative Git result and compare it with durable private
   evidence. Check that the command executed once and returned the expected
   identity. Follow [the task contract](docs/HOST_CONTROL_PLANE.md).
8. Leave a private installation receipt containing source, identity, paths,
   service commands, STOP/recovery instructions and actual PASS/PENDING/FAIL.
   Do not print private keys/tokens or claim success from source tests alone.

## Completion and recovery

A successful installation has a running native service and a fresh verified
Git task/result. If GitHub authorization or native persistence is unverified,
report PENDING and the precise remaining action. Do not invent a PASS.

Keep a safe stop/uninstall path. Upgrades must preserve an independently
usable prior source/runtime and all newer task history. Never update/restart
an Agent through its own queued task, delete claims to retry work, or restore
old ledgers over later results.

[Operations](docs/OPERATIONS.md) · [security](docs/SECURITY_MODEL.md) ·
[support boundaries](docs/SUPPORT.md).
