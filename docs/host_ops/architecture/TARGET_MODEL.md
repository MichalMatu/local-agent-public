# Host Ops target model

**Status: frozen Phase B baseline / active Phase C contract input.** Inventory and hardening are complete. The active work is to encode these target/identity invariants in the shared internal Local Agent <-> Tool Runtime contract before any capability expansion.

## Purpose

`host-ops` is a deterministic layer for bounded machine and remote-host effects. Local Agent owns repository identity, scheduling, resources, watchdogs and task evidence; `host-ops` owns input validation, effect boundaries, bounded execution and structured results.

```text
ChatGPT / planner
    -> Local Agent
        -> host-ops
            -> OS / Git / browser / ADB / serial / storage / SSH / remote host
```

It is not an automation platform, planner, scheduler, credential store or device-protocol layer. It may grow into a broad deterministic tool runtime, but authority, planning, admission, scheduling and durable ChatGPT control remain Local Agent/GitHub responsibilities.

## Stable capability set

The current baseline includes:

- shared bounded process execution, configuration, diagnostics and normalized results;
- read-only local Git context plus generic host, network and tool inspection;
- bounded ADB discovery/identity/logcat plus verified one-file push/pull for an explicit serial;
- browser process/loopback DevTools inspection;
- explicit Chromium CDP inventory, bounded page metadata/selector counts, extension readiness and worker diagnostics;
- exactly guarded attached-page reload plus one-shot content-script recovery built from readiness + reload;
- persistent Chromium session start/status/stop for an isolated host-ops-owned profile with loopback dynamic CDP;
- interactive no-CDP start/status/stop for that same owned profile, intended for short user-driven steps such as authentication;
- one disposable Playwright HTTP(S) navigation probe with bounded sanitized evidence;
- read-only macOS host/USB/serial/storage discovery plus guarded external-media mount/unmount/eject;
- verified local artifact inspection/deployment and removable-media deploy composition;
- bounded raw POSIX serial transactions with explicit port, baudrate, byte/time limits and idle completion;
- hardened OpenSSH execution and verified one-file transfer;
- exact-revision remote Git workspace execution plus explicit lock-aware cache inventory/removal.

These are generic primitives. Device protocols, project commands and task policy remain downstream.

## Target, locator, lock-scope and scheduler-resource identity

Host Ops and Local Agent use four different identity concepts. They are intentionally not aliases:

| Concept | Meaning |
| --- | --- |
| operation target | the explicit logical object an operation intends to address |
| transport locator | the current path/address used to reach that target; it may change without the target changing |
| durable identity evidence | stable target properties used to reject accidental retargeting when the platform exposes them |
| tool-runtime lock scope | a capability/workflow-internal correctness lock such as a remote-Git workspace lock |
| scheduler resource | an explicit Local Agent cross-task contention key; it controls admission but never proves target identity |

Canonical rules by maintained target family:

- Serial: a resolved /dev character-device path is an operation locator, not durable physical identity.
  Prefer USB VID:PID plus hardware serial number as durable evidence when available. macOS location_id is
  a topology/re-discovery hint, not unit identity. Current discovery exposes USB and serial records
  separately and does not yet prove a durable binding between them, so consequential project-device
  work must rediscover immediately before use and fail on ambiguity.
- ADB: AdbDevice.serial is the current operation selector. For wireless ADB, IP:port is only a
  transport locator and must never be persisted as durable handset identity. Re-list immediately
  before an effect and validate the ready device plus the fixed AdbIdentity profile. That profile is
  mismatch evidence, not a universally unique immutable hardware identity.
- Removable storage: diskN, diskNsN, /dev/disk*, mount point and volume name are locators/current
  state, not durable media identity. Re-inventory before each effect and revalidate external,
  whole/partition and read-only relationships. After disconnect/reconnect, require fresh selection
  rather than assuming the same disk number is the same physical medium. A future durable identity
  may use volume/media UUID or physical serial where the platform exposes trustworthy values.
- SSH: HostTarget.alias is the stable logical configured target. host:port is its transport locator,
  identity_file is a credential selector, and strict host-key verification authenticates the live
  endpoint. Scheduler resources are not derived from the alias, address or credential path.
- Remote Git: the remote target is the configured SSH target plus the exact repository_url string;
  the source snapshot is the full revision. A workspace is a cache scope bound to that exact URL by
  marker validation. The mandatory workspace lock and optional broader host lock are tool-runtime
  correctness locks. They are not automatically Local Agent scheduler resources.

Scheduler-resource rules remain separate:

- resources: [] is the normal contract for project-dedicated hardware and ordinary target-specific
  work;
- add a named scheduler resource only for a real shared external conflict that must serialize across
  repositories or tasks;
- reserve machine for true whole-host exclusivity;
- a scheduler resource key never substitutes for live target validation, and a tool-runtime lock
  never becomes a scheduler resource merely because both mechanisms use locking.

This distinction is a required invariant for the Phase C Tool Runtime contract. Future target improvements should add better
durable evidence where needed without introducing a second scheduler or a universal target registry.

## Boundary rules

The frozen boundary is responsibility, not today's feature count:

- Host Ops/tooling may implement broad deterministic machine primitives when their inputs, effects, bounds and results are explicit;
- Local Agent remains the only owner of planning, repository/task admission, scheduling, resource arbitration and durable execution evidence;
- GitHub remains the only durable ChatGPT <-> Local Agent control/evidence plane; a future ChatGPT plugin is an ergonomic GitHub-backed surface, not a direct execution transport;
- Conversation Fabric owns child/campaign semantics and reasoning orchestration; deterministic browser/chat-UI lifecycle effects may be Tool Runtime primitives, but delegation policy and synthesis do not move into Host Ops;
- destructive or arbitrary-code-like primitives require correspondingly explicit Local Agent policy/resource admission rather than being made "safe" by hiding the underlying capability;
- project/device-specific policy such as calibration sequences, firmware choice or motion strategy remains outside generic tooling unless it becomes a demonstrably reusable deterministic workflow.

Existing narrow boundaries (for example current ADB and attached-browser surfaces) remain the production contract until intentionally expanded with focused tests and policy coverage. Consolidation must not silently broaden authority.

## Extension test

A new runtime capability is justified only when:

1. a concrete project cannot express the required effect safely with current primitives;
2. the missing responsibility is a reusable external integration boundary rather than project policy;
3. validation, authority, timeout/failure semantics and structured evidence are clear;
4. focused success/failure tests and the canonical verifier can prove the change;
5. dependency direction `cli -> workflows -> capabilities -> core` remains intact.

Otherwise keep the behavior in the consuming project.

## Consolidation sequence

The active order is:

1. inventory and normalize the existing tool surface without changing behavior;
2. debug, test and harden each existing capability/workflow;
3. define and regression-protect the internal Local Agent <-> Tool Runtime contract;
4. migrate existing tools onto that contract without semantic drift;
5. expand the reusable multi-tool surface only after the contract is proven;
6. prepare future ChatGPT-plugin ergonomics over the unchanged GitHub control/evidence plane.

Do not invert this order by building a generic plugin framework first.

`docs/DEVELOPMENT_PLAN.md` is the canonical implementation sequence. `docs/plans/ROADMAP.md` may still record possible on-demand extensions, but it is not authority to skip inventory/hardening.
