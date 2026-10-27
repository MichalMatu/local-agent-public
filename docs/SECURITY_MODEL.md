# Security model

Local Agent is not a sandbox. Commands execute with the installed service OS
user's actual permissions and do not gain root automatically.

## Trust

The customer and GitHub manage GitHub login, private repository access and
credentials. In github_writers mode every effective writer of the selected
private task namespace is trusted to publish commands. One explicit local
host-intake choice enables that installation. There is no second login,
per-command approval, individual writer proof or task-signing service.

Preserve exact chosen repository/origin/branch and host+installation identity.
Project tasks also require matching canonical agent_binding. Missing or
mismatched trusted identity fails closed before effects. Public source and
example bindings do not enroll a customer installation.

## Execution boundaries

Global emergency disable overrides all intake. Claims are durable before
effects. Task IDs/digests and terminal results are immutable. Interrupted or
uncertain effects remain UNKNOWN and never authorize automatic replay.
Publication retry may resend a saved result without running commands again.

Use bounded time, output and memory, registered OS process groups, resource
leases and inherited execution descriptors. STOP/recovery must consider real
descendants and held leases. One production service runs per installation;
there is no cross-host failover, central scheduler or periodic capacity feed.

Attached repositories execute in private snapshots. Never reset/clean user
checkouts or collect/delete task history as routine recovery. Source updates
run outside Agent tasks with drain, durable backups and independent rollback.

## Data

Commands, output and Git results may contain sensitive data; arbitrary output
is not automatically redacted. Keep transport private and private keys outside
source, task JSON and chat messages. Use customer-controlled Git credentials
and verified SSH host trust. Never publish private ledgers or installation
receipts in the public source repository.

Report potential vulnerabilities privately to the maintainer through an
available private GitHub security-reporting channel. Do not include live
credentials or customer data in a public issue.

[Protocol](HOST_CONTROL_PLANE.md) · [operations](OPERATIONS.md).
