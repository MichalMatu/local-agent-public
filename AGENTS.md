# Local Agent — instructions for Codex

## First installation

When the user asks to install or run Local Agent, read README.md and
[INSTALL_WITH_CODEX.md](INSTALL_WITH_CODEX.md), then the selected platform guide.
Work on the explicitly selected computer. Inspect existing installations before
changes. Pin the stable source to a full Git SHA and use one native service.

The public source repository is not a task inbox. Prepare the customer's own
private Git task/result repository, unique host ID and installation UUID.
Never reuse example identities, developer credentials or existing task history.
In github_writers mode one explicit local host-intake choice is sufficient;
GitHub/user access rights define trusted task publishers. Do not invent another
login, per-command approval or writer-signing system.

Finish with actual native service/source checks and one new harmless Git
roundtrip. Keep PASS, PENDING, FAIL, SKIP and UNKNOWN separate. A rendered unit
or source test alone is not a completed installation. Record private stop,
recovery and uninstall instructions. Ask for missing account authorization or
real OS-admin credentials only when needed.

## Execution and ownership

Preserve immutable task IDs/digests, exact repository/origin/branch and
host+installation targeting, durable claims/results, bounded execution,
resource leases, process groups and emergency STOP. Uncertain effects remain
UNKNOWN; never automatically replay commands. Saved-result publication may
retry without executing commands again.

Local Agent is not a sandbox. Commands use the service OS account. Projects
are optional; attached project tasks run in private snapshots and preserve the
owner's checkout. Never reset/clean customer work or erase claims to resume.
Do not select hosts automatically, fail over, add a cross-machine scheduler
or publish periodic capacity telemetry.

An assigned code/docs task authorizes ordinary edits, fixes and scoped tests.
Keep code, logs and technical documentation in English. When replacing an
internal contract, migrate callers and remove its old inputs/fallbacks in the
same change unless the user explicitly requests compatibility.

## Maintenance

Use [CONTRIBUTING](CONTRIBUTING.md) and nearest scoped instructions. Reuse the
existing CLI/Core and verification entrypoint; do not add another daemon or
test runner. Preserve frozen GUI/browser prototypes; they are not installers.

Source checks run in disposable exact-SHA checkouts, never installed releases.
Use focused tests, Ruff/compile and affected contracts during iteration. A
release also needs clean-source full/native gates for affected platforms.

Service updates run outside Agent tasks after admission-first drain, actual
execution leases, durable backups and independent rollback. Verify a new
exact-target Git result after switching. Preserve newer ledgers during recovery.
First-install rollback may be stopped/disabled; do not fabricate an older SHA.

[Quality](docs/QUALITY_TOOLING.md) · [operations](docs/OPERATIONS.md) ·
[security](docs/SECURITY_MODEL.md).
