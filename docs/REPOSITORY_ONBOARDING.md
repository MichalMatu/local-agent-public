# Optional project repositories

A host-only installation with repositories: [] is valid. Add projects only
when the customer explicitly selects a repository/checkout. Source code and
private control/checkpoint paths have separate roles.

On Mac, inspect the actual CLI before changes:

```sh
python -m local_agent.repository.admin --help
python -m local_agent.repository.admin list
```

Use plan-add for a dry run. Inspect the explicit add --apply transaction and
its current options when applying a selected configuration. Generate a unique
canonical binding; do not copy source example IDs. IDs/remotes are unique
case-insensitively and bindings are unique. Control, work and checkpoint paths
must not collide through aliases or nesting.

Attached-checkout tasks run in a private snapshot and preserve the owner's
branch, index, staged/unstaged changes and untracked files. Never pass the
owner checkout to destructive disposable-work preparation or git clean/reset.
Review retained snapshots through supported tooling.

Linux project requests use the Linux project schema and exact host/binding;
unsupported Mac fields such as work_branch or allow_write are rejected.
Termux does not expose project execution. Use [host tasks](HOST_CONTROL_PLANE.md)
when no project is required.


## Registration ledger preparation

macOS repository registration prepares durable private
`repositories/<id>/{claims,result-spool,corrupt-claims}` state before publishing
the registry entry. A complete empty layout is published from private staging;
an interrupted preparation leaves the registry unchanged. Explicit retry reuses
the matching binding, checkouts and complete state. Existing incomplete ledgers
remain an error because missing directories may represent lost history.
Maintenance still checks every ledger before restarting the service.
