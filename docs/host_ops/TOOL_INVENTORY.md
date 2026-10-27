# Host Ops capability boundaries

The maintained namespace is local_agent.host_ops. Inspect its actual --help
for capabilities available in the selected installation. Optional native tools,
credentials and devices remain machine-local prerequisites.

| Capability | Effect | Execution class |
| --- | --- | --- |
| ADB devices / identity / logcat | ACTIVE_READ | FIXED_REMOTE_DEVICE_EXEC |
| serial transact | DISRUPTIVE | FIXED_DEVICE_IO |
| macOS unmount / eject | DISRUPTIVE | FIXED_LOCAL_EXEC |
| artifact deploy | MUTATION | FIXED_REMOTE_DEVICE_EXEC |
| SSH exec | DISRUPTIVE | ARBITRARY_CODE_LIKE |
| remote-Git run / run-current | DISRUPTIVE | ARBITRARY_CODE_LIKE |
| remote-Git cache list | MUTATION | FIXED_REMOTE_DEVICE_EXEC |
| managed / interactive browser start | MUTATION | FIXED_LOCAL_EXEC |

Device/transport locators do not establish durable identity. A changing IP or
disk number is not treated as durable physical-media identity. Treat an
AdbIdentity profile as mismatch evidence rather than a universally unique immutable hardware identifier.

Browser, serial, SSH workspace and device ownership locks are tool-runtime correctness scopes; do not automatically translate them into scheduler resources.
The selected task owns scheduler resources separately. A capability annotation
never grants authority or changes global scheduling/admission.

Use explicit selected targets, bounded output/time and the actual service OS
user's permissions. No capability selects another host or performs implicit
privilege elevation. Preserve owner files and require exact intended target
identity before effects.
