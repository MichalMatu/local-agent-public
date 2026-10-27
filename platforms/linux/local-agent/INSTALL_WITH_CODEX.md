# Debian installation with Codex

Read the [installation contract](../../../INSTALL_WITH_CODEX.md) first.
The qualified profile is Debian 13 x86-64 with a user-systemd service.

## Prepare private state

Inspect OS, architecture, Python, Git/SSH, user-systemd and existing services.
Choose a stable clean exact-SHA source directory without spaces. Create a
private configuration and 0700 state directory outside that source. Generate
a new installation UUID and host ID. Use the customer's authenticated private
Git repository, agent-control branch and one explicit github_writers intake.
The [config example](../../../deploy/linux/config.example.json) contains
placeholders, not enrollment or credentials. Replace every placeholder.

Create EMERGENCY_STOP in the private state before installation. Validate the
configuration through the selected engine and run its preflight. Never enable
intake merely because a source tree or unit file exists.

```sh
export PYTHONPATH="$PWD/platforms/linux/local-agent:$PWD/platforms/linux/core"
python3 -B -m linux_agent --help
```

## Fresh installation

Use [the bounded user-unit template](../../../deploy/linux/local-agent.service.example).
Replace every /ABSOLUTE path with this installation's pinned source, actual
Python, private config and state. Inspect it, run systemd-analyze --user verify,
and ensure its unit name does not collide with an existing installation.
Stage only the selected unit in ~/.config/systemd/user, then daemon-reload.

The rollback for a first installation is **stopped and disabled**, preserving
all state. It does not require a fictional older software release. Keep STOP
while inspecting the unit, source, origin and queue. Enable/start only after
this inspection and the customer's local intake choice; remove only the STOP
created by this fresh installation. Inspect real MainPID/source and user.

If persistence after logout is requested, check user linger; an OS-admin
credential may require customer action. A successful service start is not a
physical reboot receipt.

## Acceptance and upgrades

Publish and independently read back one new harmless exact-host Git result.
Compare it with its private durable result and verify the expected UID/source.
Test safe independent stop/recovery and leave a private installation receipt.

For an existing installation, the external service_install/service_activation
and service_upgrade operators require real distinct candidate/rollback SHAs.
Inspect their --help and retained journal. Do not fabricate a previous commit,
reuse a completed transaction or use an upgrade operator as first bootstrap.
Never restart through an Agent task or overwrite newer ledgers.

[Operations](../../../docs/OPERATIONS.md) · [support](../../../docs/SUPPORT.md).
