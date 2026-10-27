# Host task and result contract

Each installation explicitly selects a private GitHub owner/repository and
agent-control branch. The public source repository is never the task inbox.
The customer chooses the exact host ID and installation UUID before a task.

## Git namespace

```text
.agent/hosts/<host-id>/supervisor/<installation-id>/tasks/<task-id>.json
.agent/hosts/<host-id>/supervisor/<installation-id>/task-results/<task-id>.json
```

Create a new immutable task ID. Never edit a published request to change its
command, target or digest. The selected Agent checks current Git bytes,
identity, local intake, STOP and admission before effects. A request commit
alone is not execution or delivery proof.

## Bounded host command example

Replace the example IDs with the actual private installation receipt. Choose
an existing absolute cwd on the selected machine; /tmp is not a Termux path.

```json
{
  "version": 1,
  "id": "first-check-unique-id",
  "mode": "commands",
  "target_host_id": "YOUR-HOST-ID",
  "target_installation_id": "YOUR-INSTALLATION-UUID",
  "cwd": "/tmp",
  "commands": ["python3 -c 'import os; print(os.getuid())'"],
  "resources": [],
  "command_timeout": 10,
  "idle_timeout": 10,
  "task_timeout": 90,
  "memory_limit_mb": 128,
  "output_policy": "summary"
}
```

Read the result independently from the exact task-results path. Compare
its IDs/digest and durable private evidence. Successful commands have the
expected UID/output and exit zero. Interrupted/uncertain effects stay UNKNOWN.
A publication outage may retry a saved result, never the command.

## Mac local configuration

The private supervisor-control.json selects the transport and one local
host-intake choice. Generate identity with diagnostics init-host-identity.

```json
{
  "version": 1,
  "enabled": true,
  "host_id": "YOUR-HOST-ID",
  "identity_file": "/ABSOLUTE/PRIVATE/host-identity.json",
  "control_checkout": "/ABSOLUTE/PRIVATE/transport/agent-control",
  "repository": "YOUR-ACCOUNT/YOUR-PRIVATE-INBOX",
  "host_intake": {"enabled": true, "authorization": "github_writers"}
}
```

Use the real private config and explicit --repository for producer setup.
Source examples/defaults do not enroll a host. Host tasks do not use a project
agent_binding. Project tasks require their own exact canonical binding and
platform-supported schema; Mac project fields are not Linux project fields.

[Install](../INSTALL_WITH_CODEX.md) · [security](SECURITY_MODEL.md) ·
[onboarding](REPOSITORY_ONBOARDING.md).
