# Security Model

## Scope

`host-ops` intentionally provides broad local authority. Security therefore comes from explicit boundaries, deterministic execution, narrow adapters and evidence rather than pretending that arbitrary user-level shell access is a sandbox.

This document defines the durable authority model for the repository. It does not claim OS-level confinement that does not exist.

## Trust boundaries

### Planner

The planner decides the requested operation. Planner intent is not itself an execution guarantee.

### Local Agent

Local Agent is the execution boundary in front of this repository. It owns repository binding, immutable task identity, bounded task execution, cancellation, watchdogs and task/run/result evidence.

`host-ops` must not reimplement those responsibilities or provide a bypass around them.

### host-ops

`host-ops` converts an explicit operation into deterministic platform/device actions. It owns capability-specific validation, prerequisite checks, target identity, bounded local subprocess use, structured results and secret-aware output handling.

### Operating systems and external targets

macOS, remote SSH hosts, Android devices, browsers and network services are external systems. Their live state must be measured when a claim depends on it.

## Authority principles

1. **Explicit target identity** — consequential actions require a target that can be identified deterministically.
2. **Fail clearly** — missing prerequisites, ambiguous devices, host-key failures and authentication failures are errors, not best-effort success.
3. **Least accidental persistence** — ephemeral browser sessions and temporary files are the default where persistence is not required.
4. **No credential replication** — rely on user/OS credential facilities rather than copying secrets into repo config or results.
5. **Bounded evidence** — stdout, stderr, screenshots and other artifacts must have explicit bounds before they become routine automation surfaces.
6. **One effect owner** — process lifecycle, SSH semantics, ADB selection, browser state and macOS GUI control each have one code owner.
7. **No hidden self-escalation** — a capability must not silently widen its own permissions, disable validation or rewrite Local Agent authority state.

## Canonical effect and authority classification

Host Ops classifies every maintained operation on two orthogonal axes. This is the canonical
classification consumed by the Phase C Tool Runtime contract; it is descriptive contract metadata, not a new runtime
registry or scheduler.

Semantic effect describes what the operation can do to observable state:

| Class | Meaning |
| --- | --- |
| PASSIVE_READ | Reads local state without intentionally interacting with an external target or changing persistent state. |
| ACTIVE_READ | Performs an externally observable network/device/remote read or probe while intending no persistent mutation. |
| MUTATION | Can create or change persistent state while remaining within a bounded non-destructive contract. |
| DISRUPTIVE | Can disconnect, stop, remove, reset, or otherwise materially disturb target availability/state. |

Authority ceiling describes the strongest execution authority exposed by the capability:

| Class | Meaning |
| --- | --- |
| NONE | No child program, remote command, or direct device I/O is required by the effect implementation. |
| FIXED_LOCAL_EXEC | Executes capability-selected local commands/helpers with bounded, non-general command shape. |
| FIXED_REMOTE_DEVICE_EXEC | Executes a bounded fixed command set against an explicit remote host/device. |
| FIXED_DEVICE_IO | Performs bounded direct device/protocol I/O where opening or writing may itself affect hardware. |
| ARBITRARY_CODE_LIKE | Accepted inputs can select general executable/command/code behavior; treat this as the authority ceiling even when a specific invocation is read-only. |

Classification rules:

- classify the strongest behavior reachable through the accepted operation inputs, not the friendly
  command name or the intent of one typical call;
- a composite workflow inherits the strongest semantic effect and authority ceiling of any stage it
  can execute; conditional disruptive branches should be called out explicitly;
- process-backed inspection is not NONE merely because its returned data is read-only;
- ARBITRARY_CODE_LIKE is an authority label, not a claim of sandbox escape or malicious intent;
- effect/authority classification never creates scheduler resources and never substitutes for target
  identity; Local Agent resource admission remains an independent conflict-control decision;
- tool target identity, transport locator, effect classification and scheduler resource identity are
  four separate concerns and must not be collapsed into one field.

The maintained-operation mapping is owned by docs/host_ops/TOOL_INVENTORY.md. New or broadened
operations must update that matrix and focused tests/documentation before the authority is treated as
part of the stable Tool Runtime contract.

## Credentials and secrets

Never commit or generate into repository-tracked paths:

- SSH private keys;
- passwords;
- API keys or bearer tokens;
- browser cookies/session storage/profile data;
- Android app secrets extracted from a device;
- macOS Keychain exports;
- environment dumps containing credentials.

Preferred credential sources:

- external OpenSSH identity files referenced by literal path from machine-local host-ops config;
- macOS Keychain or application-native stores for future capabilities that explicitly support them;
- external browser profile directories when persistent login is explicitly required;
- process environment only when a concrete integration requires it and redaction is defined.

A path such as `~/.ssh/host_ops_termux_ed25519` may be stored in machine-local host-ops config. The
private-key bytes at that path must never be copied into Git, task payloads or results. Routine SSH
execution requires this explicit identity reference; it does not select an unrelated default key.

Secret values must not be echoed in structured results merely to prove that authentication succeeded.

## Remote SSH safety

- Strict host-key verification must never be globally disabled.
- A new or changed host key is an explicit trust event, not a retry condition.
- Routine SSH calls use `-F /dev/null`; general `~/.ssh/config` and system SSH config are not implicit
  execution inputs.
- The remote user is mandatory and explicit; falling back to the local account name is rejected.
- The identity-file path is mandatory, literal and explicit; `%...` tokens and `${...}` environment
  expansion are rejected.
- Routine authentication is public-key-only, with `IdentitiesOnly=yes` and the configured path passed
  through `-i`.
- Automatic host-key update and key-agent mutation are disabled with `UpdateHostKeys=no` and
  `AddKeysToAgent=no`.
- Forwarding, agent forwarding, local commands, multiplex persistence and post-authentication
  daemonization are disabled for routine adapter calls.
- Target hosts use literal DNS/IP-style syntax; alternate `ssh://...` destination URI syntax is not
  accepted through the host field.
- `ProxyCommand`, `ProxyJump`, `Match exec` and similar behavior require a future explicit contract;
  they must not arrive as hidden user-config side effects.
- Remote command exit status must be preserved.
- Connection and command duration must be bounded.
- `scp`/`sftp`/`rsync` style transfer destinations must be explicit and observable.

## Android / ADB safety

- Physical device selection must be explicit when more than one target is possible.
- Device identity should use stable reported properties/serials rather than list order.
- Destructive operations such as factory reset, package-data clearing, bootloader operations or broad file deletion require explicit task intent.
- `adb shell` is powerful authority; successful transport is not proof that the intended device was selected.

## Browser safety

- DevTools inspection/attach endpoints are loopback-only, explicit and bounded; returned URLs are sanitized.
- Read-only CDP diagnostics expose bounded metadata/counts rather than page content, cookies/storage or arbitrary JavaScript results.
- The only direct attached-page mutation primitive is guarded exact-target reload. Content-script recovery may invoke it at most once after a qualifying diagnosis and must re-check readiness.
- Persistent managed sessions use a dedicated host-ops-owned profile, exact profile/process identity and a dynamic loopback CDP port. A non-empty unowned profile is never adopted.
- Interactive mode may run only against that owned profile and deliberately disables CDP/extension launch flags; it exists for short user-driven steps rather than automated page control.
- The disposable probe uses a fresh context, one bounded HTTP(S) navigation and no operator profile.
- Page contents, typed secrets, cookies and storage must not be persisted in routine results/logs.
- Downloads remain untrusted files; general download/upload orchestration and broader page automation require separate explicit contracts.

## macOS automation safety

macOS GUI automation may require Accessibility and Screen Recording privileges. Missing permission must surface as a prerequisite failure.

Coordinate-driven input is environment-sensitive. Prefer application APIs, CLIs or accessibility elements when available.

System-wide destructive actions — disk erasure, account deletion, firewall changes, credential-store deletion, service-wide security disabling and similar operations — require explicit task intent and must not be hidden inside convenience helpers.

### External storage control

`python -m local_agent.host_ops macos mount`, `unmount` and `eject` accept only canonical `diskN` / `diskNsN` identifiers. Before every effect, the capability runs pinned `diskutil info -plist` and requires `Internal=false`; unknown, malformed or internal targets fail closed. Eject additionally requires `Whole=true`, so a partition cannot be mistaken for a whole-disk eject target.

These operations do not format, partition, erase or rewrite filesystems. Such destructive operations require a separate capability and explicit authorization contract.

### Local artifact deployment

`python -m local_agent.host_ops artifact deploy` writes exactly one regular source file into one existing local directory. Source and destination-directory symlinks are rejected, the destination name must be one plain filename, and replacement of an existing regular file requires explicit `--replace` intent.

The write is staged in the destination directory and the staged file is flushed and fsynced. Replacement uses atomic `os.replace`; no-clobber mode first reserves an absent destination with exclusive creation so normally concurrent creators cannot be silently overwritten, then replaces only that reservation. The final destination is reread and verified for both byte length and SHA-256. Directory fsync is attempted and its support state is included in the result rather than silently assumed.

Artifact deployment deliberately does not discover or mount removable media and does not infer firmware names. The planner must choose the target directory and filename explicitly. Device-specific flashing policy remains outside this capability.

### Local serial safety

`python -m local_agent.host_ops serial transact` requires one explicit absolute device path below `/dev`; it never selects the first or only discovered serial device. The resolved target must be a character device and the caller must supply a baudrate supported by the host POSIX TTY implementation.

Write payloads, retained response bytes, whole-transaction time and response-idle time are bounded. The capability performs raw 8N1 byte transport only. It does not interpret command syntax, line framing, G-code, firmware protocols or bootloader state, and it does not retry or widen authority on its own.

A transaction that writes bytes flushes buffered input before sending so stale bytes are not misreported as the new response. A passive read does not flush input. Returned response bytes are explicit task evidence and may contain device-provided data, so callers must not use this generic primitive to expose secrets unless the surrounding task defines appropriate redaction/storage handling.

## Filesystem and process safety

The shared execution layer owns process-group cleanup, timeout behavior and output bounds. Capability adapters should pass argv rather than concatenate shell strings when a shell is not required.

When invoked directly beneath Local Agent, the `host-ops` process retains inherited repository and
resource lease descriptors for the lifetime of the operation. Project-free host tasks carry only
resource descriptors: they do not invent a repository lease or digest. A repository digest without
repository descriptors is still invalid. These are process-group/lifecycle markers, not grants.
A wrapper process may legitimately
close all inherited lease descriptors while forwarding the structurally valid lease metadata. Both
forms remain in the outer Local Agent process group and scrub lease metadata before spawning external
children. Mixed open/closed descriptor state, malformed or duplicate descriptors, overlapping
repository/resource descriptors and missing or malformed digest state fail before process creation.
`host-ops` does not independently recompute the expected digest or validate lease-file identity.
Those authoritative checks belong to Local Agent before task execution, so the inherited lease
variables are trusted executor metadata rather than a standalone authentication mechanism.

The shared runner keeps its subprocess in the outer Local Agent process group. Child programs are
**not** relied upon to retain Local Agent's internal lease descriptors; some programs, including
OpenSSH, deliberately close extra file descriptors at startup. Local Agent lease environment fields
are scrubbed from external child environments. Correctness therefore comes from the retained parent
lease plus outer process-group cleanup, not from leaking Local Agent descriptors into external tools.

Standalone execution may own a dedicated process group but must detect and terminate descendants
before reporting success.

Repository code must not create convenience helpers that recursively delete arbitrary user paths without an explicit, narrow contract and negative tests.

## Local Agent self-protection

Repository binding is not an OS sandbox. An accepted user-level command may otherwise have the authority of the Local Agent process.

`host-ops` must therefore avoid adding convenience paths that mutate Local Agent installation/control state. The broader Local Agent project is separately evaluating deterministic self-protection floors for its own runtime/control surfaces. `host-ops` must remain compatible with those floors rather than work around them.

## Evidence classification

Claims must identify their evidence level:

- `UNIT`: deterministic isolated test;
- `CONTRACT`: architecture/schema/policy test;
- `INTEGRATION`: real local dependency in a hermetic or controlled environment;
- `LIVE`: actual external target such as the user's Mac GUI, phone, SSH host or network service.

Never elevate one level into another in documentation or completion reports.

## Security review gate

For every new external capability, review:

- exact authority exposed;
- target identity mechanism;
- credential source and redaction behavior;
- timeout/output bounds;
- destructive/error paths;
- persistent state created;
- cleanup behavior;
- unit and negative tests;
- integration/live evidence required for truthful verification.

## Remote Git workspace security

`python -m local_agent.host_ops remote git` is an orchestration workflow over the existing SSH capability, not a new trust channel. The configured SSH target identity, strict host-key verification and explicit identity-file policy remain authoritative.

Remote Git inputs are bounded by the workflow contract: symbolic revisions are rejected in favor of one full object id; workspace/lock names use a restricted filename-safe alphabet; HTTP(S) repository URLs with embedded credentials, query strings or fragments are rejected; caller argv is passed as positional data rather than interpolated into the bootstrap script.

The remote cache is disposable execution state. Each workspace is kernel-locked and bound to one exact worker-visible repository URL; an optional host-wide lock can serialize CPU-heavy jobs. Cache roots, workspace/repository directories, repository markers and lock paths reject unexpected symlink/type state, and lock files are opened non-truncating before `flock`. Legacy pre-binding workspaces are accepted only when their existing `origin` matches the requested URL. Failed initial clones remove partial workspaces. `worktree` clean mode deliberately preserves ignored caches for speed, while `full` mode removes ignored state when a colder build is required. Neither mode is a sandbox: the requested project command executes with the permissions of the configured remote user.

Requested revisions must be fetched and reachable from refs fetched from the effective worker-visible origin; stale cached objects alone are not provenance. Remote project commands are expected to remain in the foreground until completion; intentionally backgrounded descendants are outside timeout and lock-lifecycle guarantees.

Project/toolchain secrets must remain in external target configuration and must not be embedded in repository URLs, task payloads, logs or structured results.
