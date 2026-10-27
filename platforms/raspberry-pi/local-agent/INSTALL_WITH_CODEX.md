# Raspberry Pi installation with Codex

Read the [installation contract](../../../INSTALL_WITH_CODEX.md) and
[Debian procedure](../../linux/local-agent/INSTALL_WITH_CODEX.md).
The tested profile is Pi Zero 2 W with Debian 13 aarch64.

Use a distinct installation identity, private state/transport and
local-agent-pi.service unit. Detect an existing service before changes;
never run two production executors against the same host state.

```sh
export PYTHONPATH="$PWD/platforms/raspberry-pi/local-agent:$PWD/platforms/linux/local-agent:$PWD/platforms/linux/core"
python3 -B -m pi_agent probe
```

The native probe must accept the actual hardware. Use the
[Pi unit template](../../../deploy/linux/local-agent-pi.service.example)
with exact source/config/state/Python paths. It includes ExecStartPre,
192 MiB MemoryMax, TasksMax=32 and bounded engine memory settings.
Check the actual kernel memory/pids controllers and effective limits;
a rendered unit is not proof that those limits are enforced.

Follow the fresh-install STOP, unit verification, cold rollback and Git
acceptance steps from the Debian guide. A later upgrade needs an independent
real baseline. Preserve all private task history and existing projects.

Low-RAM workload and other boards/distributions require their own checks.
Do not revive the frozen Mac-bundled Linux scheduler for this installation.

[Support](../../../docs/SUPPORT.md) · [operations](../../../docs/OPERATIONS.md).
