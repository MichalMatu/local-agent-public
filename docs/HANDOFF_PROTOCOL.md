# Offline maintainer handoff

The public repository carries product source and customer documentation.
Private installation status, operational history and credentials stay outside
it. A source snapshot cannot assert current machine or GitHub liveness.

From a clean exact-SHA disposable checkout:

```sh
python scripts/verify.py --only quality
python scripts/handoff.py --check
python scripts/handoff.py --format markdown
```

Store generated snapshots privately outside the repository. The generator is
read-only/offline, rejects dirty sources and stale SHA-scoped evidence, and
cannot assert tests passed or installations are healthy. Operator-provided
receipts retain their actual attribution and missing/skipped/unknown state.

The receiving maintainer follows the customer's assigned task, reads AGENTS
and relevant product docs, and refreshes live evidence only where needed.
No standing audit, deployment or replay follows from generating a snapshot.
Preserve immutable tasks, UNKNOWN, private ledgers and independent recovery.
