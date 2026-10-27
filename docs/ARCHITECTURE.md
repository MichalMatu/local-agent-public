# Architecture

Local Agent is an independent machine service. Its native service fetches
immutable tasks addressed to its exact host and installation, records claims
before effects, executes within local budgets and publishes durable results.
Git is transport; there is no shared cross-host execution queue or automatic
machine selection. Projects are optional.

## Source owners

| Path | Responsibility |
| --- | --- |
| platforms/macos/local-agent | Mac CLI/Core, daemon, supervisor, repositories and Host Ops |
| local_agent | Import bridge to the Mac implementation |
| platforms/linux/core/linux_agent | Shared Linux Git, command and recovery engine |
| platforms/linux/local-agent | Debian adapters, service and release tools |
| platforms/raspberry-pi/local-agent | Pi probe, limits and independent entrypoint |
| platforms/android/local-agent | Termux adapter and verified app-private bundle |
| scripts | Source verification and independent maintenance controllers |
| agentd.py, agent_entrypoint.py, agent_parallel.py, agent_multirepo.py | Four thin operational launchers |

## Execution and recovery

Mac daemon/supervisor own lifecycle, admission and scheduling; short-lived
workers own per-repository context. One repository task runs at a time, with
up to four Mac parallel workers for independent work. Shared resources use
named leases; whole-machine exclusion is reserved for genuine exclusivity.

Linux/Pi/Termux reuse the separate Linux command/Git engine. Mac runtime is
not their scheduler. Each installation has its own OS-user boundary, STOP,
identity, claims, results, process groups and resource locks.

Public source, private task transport and private local state are separate.
Attached project tasks execute in private snapshots. Claims/results and
UNKNOWN/no replay survive restarts; publication retry sends durable results
without repeating commands. Lifecycle controllers run outside Agent tasks.

The maintained Host Ops namespace is local_agent.host_ops. Its CLI calls
workflow/capabilities/Core; it cannot become another scheduler. Frozen
GUI/browser prototypes remain source-only and are not customer installers.

[Protocol](HOST_CONTROL_PLANE.md) · [security](SECURITY_MODEL.md) ·
[operations](OPERATIONS.md).
