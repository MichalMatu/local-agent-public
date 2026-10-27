# Contributing

Start from main and read [AGENTS](AGENTS.md), [architecture](docs/ARCHITECTURE.md)
and [security](docs/SECURITY_MODEL.md). Use a disposable checkout and preserve
installed services, private state and attached user repositories.

## Verify a change

Use Python 3.12+ and the pinned runtime/development dependencies. Runtime Mac
dependencies are in requirements-runtime.lock; keep hash checking enabled.
Ruff policy lives in pyproject.toml.

```sh
python scripts/verify.py --only quality
python scripts/verify.py --profile quick
python scripts/verify.py --profile full
```

Run focused tests during iteration. Final runtime changes require clean-source
and appropriate native checks; a Mac fixture is not Linux/Android acceptance.
Keep failed, skipped and uncertain evidence explicit. Do not run hosted CI or
change production services as a side effect of source verification.

## Changes and pull requests

Explain the concrete behavior changed and its validation. Add meaningful
positive/negative regressions for identity, admission, STOP, publication,
crash/no-replay, process/resource or owner-data boundaries. Keep changes small
and remove superseded internal API paths with their callers.

The four thin root launchers remain operational entrypoints. Reusable runtime
code lives under the platform owners; CLI and UI call the existing Core.
GUI/browser prototypes are frozen. New hosts remain independent installations.

Keep secrets, task/result mailboxes, logs and private installation receipts out
of source commits. Publish only exact selected Git paths. Do not rewrite task
history or replace UNKNOWN with an invented success.

For a maintainer transfer use [the offline handoff](docs/HANDOFF_PROTOCOL.md).
For a new user use [installation with Codex](INSTALL_WITH_CODEX.md).
