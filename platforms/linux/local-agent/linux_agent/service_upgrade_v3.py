"""Third independent Debian service transaction; previous v1/v2 journals immutable.

Never invoke this from Git-hosted tasks. The owner operates it in a separate
interactive terminal with the service's external lifecycle/rollback contract.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from linux_agent.release_controller import ServiceSwitchError
from linux_agent.service_plan import ReleasePair
from linux_agent.service_upgrade import (
    PersistentServiceUpgrade, _read_private_json, _external_only, _digest,
)
from linux_agent.service_upgrade_v2 import (
    SecondPersistentServiceUpgrade, _V1_KEYS,
)


class ThirdPersistentServiceUpgrade(PersistentServiceUpgrade):
    """One new v2-installed baseline -> pinned optimized candidate transaction."""

    def __init__(self, pair: ReleasePair):
        super().__init__(pair)
        self.journal = pair.state / "linux-service-upgrade-v3.json"
        self.lock = pair.state / "linux-service-upgrade-v3.lock"

    def _original_receipts(self) -> None:
        """Verify committed v2, earlier v1 and original install chain."""
        record = _read_private_json(self.pair.state / "linux-service-upgrade-v2.json")
        if (
            set(record) != _V1_KEYS
            or record.get("version") != 1
            or record.get("unit") != "local-agent-linux.service"
            or record.get("phase") != "committed"
            or record.get("candidate_sha") != self.pair.rollback_sha
            or record.get("candidate_unit_sha256") != _digest(self.old_unit)
            or not isinstance(record.get("baseline_sha"), str)
        ):
            raise ServiceSwitchError("previous v2 committed upgrade provenance mismatch")

        # The v2 controller verifies its own exact journal identity, the
        # previous installed source and the full v1 original-install chain.
        prior_root = self.pair.rollback.parent / record["baseline_sha"]
        if prior_root.is_symlink() or not prior_root.is_dir():
            raise ServiceSwitchError("previous v2 baseline source missing/unsafe")
        prior_pair = ReleasePair(
            candidate=self.pair.rollback,
            candidate_sha=self.pair.rollback_sha,
            rollback=prior_root,
            rollback_sha=record["baseline_sha"],
            python=self.pair.python,
            config=self.pair.config,
            state=self.pair.state,
        )
        from linux_agent.release_controller import verify_clean_exact_source
        prior_pair.validate()
        verify_clean_exact_source(prior_root, record["baseline_sha"])
        prev = SecondPersistentServiceUpgrade(prior_pair)
        if prev._journal_phase() != "committed":
            raise ServiceSwitchError("previous v2 journal identity/phase mismatch")
        prev._original_receipts()

    def _preflight(self) -> None:
        super()._preflight()
        self._original_receipts()


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "switch", "rollback", "reconcile"))
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--baseline-sha", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args(argv)
    pair = ReleasePair(
        candidate=args.candidate_root, candidate_sha=args.candidate_sha,
        rollback=args.baseline_root, rollback_sha=args.baseline_sha,
        python=Path(sys.executable).resolve(),
        config=args.config, state=args.state,
    )
    try:
        _external_only()
        output: dict[str, Any] = getattr(
            ThirdPersistentServiceUpgrade(pair), args.action
        )()
    except (ServiceSwitchError, ValueError, OSError, AssertionError) as exc:
        print("Third Debian upgrade REFUSED: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
