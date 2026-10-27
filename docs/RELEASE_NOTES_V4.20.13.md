# Local Agent 4.20.13

First public source release with English customer documentation and
installation through local Codex.

- Narrow Mac recovery for proven redundant pre-boot Git shallow locks, with
  durable backup and stable ownership/mutation checks; access-time changes
  caused by reads do not count as mutations.
- Durable invalid_task_file rejection for exactly addressed invalid Linux/Pi
  project input, preserving later valid work and publication-only recovery.
- Shared Linux command/Git/recovery engine with independent Debian, Pi and
  Termux adapters, native service boundaries, STOP and UNKNOWN/no replay.
- Customer-owned private Git transport, generic example identities and explicit
  source/transport separation. Fresh Linux/Pi first installation uses a cold
  stopped rollback instead of fabricating an older source commit.

[Support scope](SUPPORT.md) · [installation](../INSTALL_WITH_CODEX.md).
