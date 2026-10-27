# Quality tooling

Run checks from a clean disposable exact-SHA source checkout, never from an
installed release or customer checkout. Isolate inherited production-only
LOCAL_AGENT admission/lease variables from nested fixtures. Keep actual
commands, SHA and PASS/FAIL/SKIP/TIMEOUT evidence.

```sh
python scripts/verify.py --only quality
python scripts/verify.py --profile quick
python scripts/verify.py --profile full
python scripts/verify.py --profile macos-smoke
```

Secret scanning has two independent scopes: current files and complete Git
history. Before public release, run both from a complete non-shallow Git
checkout with Gitleaks installed:

```sh
python scripts/quality/security_audit.py --mode all
```

The history scanner uses `gitleaks git --log-opts=--all --full-history` to
inspect commits reachable from local refs. Fetch all intended remote branches
and tags first; unreachable objects and refs not present locally are not
covered. Missing Gitleaks, missing Git, or shallow history is **UNVERIFIED**,
never a clean pass. Neither scan prints secret values or raw findings.
See [signed releases](RELEASE_SIGNING.md) and
[security reporting](../SECURITY.md).

The quality stage checks documentation links, scoped ownership, architecture
and module budgets plus offline handoff contracts. Quick/full also run their
configured compile, Ruff and source tests. Reuse the existing verifier rather
than creating another full test engine. Inspect --help for available profiles.

Native platform commands use the source-local adapters and core:

```sh
export PYTHONPATH="$PWD/platforms/linux/local-agent:$PWD/platforms/linux/core"
python3 -B -m unittest discover -s platforms/linux/local-agent/tests -q
```

Pi adds platforms/raspberry-pi/local-agent to PYTHONPATH and runs its tests;
Termux adds platforms/android/local-agent and runs the Android tests on-device.
A Mac test does not prove Linux process/lease behavior or Android endurance.
Opt-in native launchd/systemd tests use isolated disposable service identities,
never production paths or customer data.

Runtime dependencies are pinned; Mac's requirements-runtime.lock includes
artifact hashes. Optional GUI/browser dependencies are separate. Gitleaks
scans use full redaction. Missing scanners/native dependencies are UNVERIFIED,
not PASS. Hosted workflows are not required or automatically dispatched.

[Contributing](../CONTRIBUTING.md) · [handoff](HANDOFF_PROTOCOL.md).
