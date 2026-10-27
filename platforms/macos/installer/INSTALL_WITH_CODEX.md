# macOS installation with Codex

Read the [installation contract](../../../INSTALL_WITH_CODEX.md) first.
Use local Codex and native launchd. The graphical prototype is frozen.

## Inspect and prepare

Confirm macOS/Apple Silicon, the OS account, Python, Git, SSH and launchd.
Inspect existing Agent registrations and private state before creating files.
Do not replace an existing service through this fresh-install procedure.

Clone the public source into a stable release location, select its stable tag
and record the full SHA. Create a source-local .venv and install the pinned
runtime with .venv/bin/python -m pip install --require-hashes -r requirements-runtime.lock.
Keep the release source unchanged after acceptance.

Use ~/Library/Application Support/local-agent for private runtime state,
with 0700 directories and 0600 configuration. Keep its Git transport at
transport/agent-control, separate from release source and projects.

## Configure the customer's host

1. Run the selected source's diagnostics init-host-identity with --host-id and
   --output-file pointing to a new private host-identity.json. Record its UUID.
2. Prepare an authenticated clone of the customer's private repository at its
   agent-control branch. Set a customer-approved Git author for results.
3. Write supervisor-control.json following the [host contract](../../../docs/HOST_CONTROL_PLANE.md).
   Always set repository to the customer's explicit owner/name and use its
   real identity_file/control_checkout. Set host_intake to enabled:true and
   authorization:github_writers only for the selected customer installation.
4. Create repositories.json with {"repositories": []} for a host-only install.
   Project onboarding is optional. Do not copy source example enrollments.

## Render, install and start

Inspect the actual supported options:

```sh
.venv/bin/python scripts/macos_launchd.py --help
.venv/bin/python -m local_agent.cli.diagnostics --help
.venv/bin/python scripts/macos_launchd.py render --mode parallel --max-workers 4
```

Render with the selected --repo-root and --registry absolute paths. After
checking the resulting source/interpreter/state selection, use install with
the same options. It writes the LaunchAgent without starting it. Start the
newly installed definition with the separate restart action. Run these
commands outside the Agent; do not add the browser-pairing sidecar.

Verify launchd PID, OS user, source SHA and fresh diagnostics. Prove one
harmless exact-target Git task/result, inspect STOP behavior and check login
persistence. Record a physical reboot check separately if performed.

First-install rollback disables/removes only the new verified service and
retains private state. Existing-install upgrades require independent previous
source/runtime and the external release controller. Never copy or replay a
historical pilot cutover script for a new customer.

[Operations](../../../docs/OPERATIONS.md) · [support](../../../docs/SUPPORT.md).
