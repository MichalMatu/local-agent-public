# Android / Termux installation with Codex

Read the [installation contract](../../../INSTALL_WITH_CODEX.md) first.
This installs a Termux service, not an APK. Inspect the actual app-private
HOME/PREFIX, Python, Git, SSH, runit, optional Termux:Boot and Android policy.
Preserve unrelated services, keys, boot scripts and any existing Agent.

## Package and configure

From clean pinned source, inspect package.py --help and build a new bundle:

```sh
python3 platforms/android/local-agent/package.py --help
```

Use --source-sha with the resolved full Git SHA and a new --output destination.
Verify the resulting per-file manifest, including LICENSE. Keep bundle,
credentials, config, transport, claims and results inside app-private HOME;
never use /sdcard. Generate new host/installation identities and explicitly
configure the customer's private Git repository and github_writers intake.

Run the verified bundle's termux_agent preflight and diagnostics with its
actual --config and --state. Keep EMERGENCY_STOP active during preparation.

## Private service

Use [the run script](../../../deploy/termux/run.example) and
[dedicated boot hook](../../../deploy/termux/boot.example), replacing source
and private paths. Create a private runsvdir services directory, service down
marker and bounded svlogd log. The dedicated boot hook brings up this private
manager and calls sv up; it does not change unrelated Termux services.

Inspect before starting, remove only the fresh installation's owned STOP and
verify native PID, UID and actual bundle path. Prove one new exact-target Git
task/result and compare it with private durable evidence. Test independent
stop/recovery. Verify reboot startup separately when the phone is available.

Android Doze, OEM force-stop and sustained battery behavior require separate
endurance tests; a wake lock does not guarantee survival. Never use the
historical one-shot operator_fastpath.py as a customer updater. For upgrades,
retain the previous runnable bundle, service script, config and independent
rollback; preserve all newer claims/results.

[Support](../../../docs/SUPPORT.md) · [operations](../../../docs/OPERATIONS.md).
