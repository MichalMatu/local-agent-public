"""Safe, offline regression checks for the Debian external upgrade controller.

These tests intentionally DO NOT spoof the operator gate or run systemctl
mutations from the installed Agent's task channel. External cutover E2E must
be executed separately by an independent operator process.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from linux_agent.release_controller import ServiceSwitchError
from linux_agent.service_plan import ReleasePair
from linux_agent.service_upgrade import (
    PersistentServiceUpgrade, _external_only, _read_private_json,
)


class UpgradeSourceOfflineTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="linux-upgrade-source-only-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.root.chmod(0o700)
        candidate, baseline = "a" * 40, "b" * 40
        self.candidate = self.root / "releases" / candidate
        self.baseline = self.root / "releases" / baseline
        for root in (self.candidate, self.baseline):
            target = root / "platforms/linux/local-agent/linux_agent"
            target.mkdir(parents=True)
            (target / "__main__.py").write_text("pass\n")
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.config = self.root / "config.json"
        self.config.write_text("{}")
        self.config.chmod(0o600)
        self.pair = ReleasePair(
            candidate=self.candidate, candidate_sha=candidate,
            rollback=self.baseline, rollback_sha=baseline,
            python=Path(sys.executable).resolve(),
            config=self.config, state=self.state,
        )
        self.up = PersistentServiceUpgrade(self.pair)
        self.up.stage.unit_dir = self.root / "systemd"
        self.up.stage.unit_dir.mkdir(mode=0o700)
        self.up.stage.unit_file = self.up.stage.unit_dir / "local-agent-linux.service"
        self.up.stage.unit_file.write_text(self.up.old_unit)
        # Original installation was staged when the baseline, NOT the new
        # candidate, was the service_install candidate SHA.
        old_staged = {
            "version": 1, "phase": "staged",
            "unit_name": "local-agent-linux.service",
            "candidate_sha": self.pair.rollback_sha,
            "rollback_sha": "c" * 40,
            "unit_sha256": hashlib.sha256(self.up.old_unit.encode()).hexdigest(),
        }
        self.up.stage.journal.write_text(json.dumps(old_staged) + "\n")
        activation = {
            "version": 1, "phase": "running",
            "unit": "local-agent-linux.service",
            "candidate_sha": self.pair.rollback_sha,
            "rollback_sha": old_staged["rollback_sha"],
            "unit_sha256": hashlib.sha256(self.up.old_unit.encode()).hexdigest(),
        }
        (self.state / "linux-service-activation-v1.json").write_text(
            json.dumps(activation) + "\n"
        )

    def test_identity_and_original_receipts_are_consistent(self):
        self.up._original_receipts()
        self.assertEqual(self.up._unit_bytes(), self.up.old_unit)
        self.assertNotEqual(self.up._identity()["candidate_unit_sha256"],
                            self.up._identity()["baseline_unit_sha256"])

    def test_tampered_original_provenance_is_not_accepted(self):
        path = self.state / "linux-service-activation-v1.json"
        obj = json.loads(path.read_text())
        obj["candidate_sha"] = self.pair.candidate_sha
        path.write_text(json.dumps(obj))
        with self.assertRaisesRegex(ServiceSwitchError, "activation receipt mismatch"):
            self.up._original_receipts()

    def test_changed_existing_unit_is_not_overwritten(self):
        self.up.stage.unit_file.write_text("unrelated service file")
        with self.assertRaisesRegex(ServiceSwitchError, "outside release transaction"):
            self.up._unit_bytes()
        self.assertEqual(self.up.stage.unit_file.read_text(), "unrelated service file")

    def test_symlink_receipt_refused(self):
        p = self.state / "receipt.json"
        outside = self.root / "other.json"
        outside.write_text('{"ok": true}')
        p.symlink_to(outside)
        with self.assertRaisesRegex(ServiceSwitchError, "receipt missing or unsafe"):
            _read_private_json(p)

    def test_duplicate_receipt_field_refused(self):
        p = self.state / "receipt.json"
        p.write_text('{"phase":"running","phase":"stopped"}')
        with self.assertRaisesRegex(ServiceSwitchError, "duplicate durable journal field"):
            _read_private_json(p)

    def test_upgrade_journal_identity_cannot_change(self):
        self.up._record("fencing")
        self.assertEqual(self.up._journal_phase(), "fencing")
        obj = json.loads(self.up.journal.read_text())
        obj["baseline_sha"] = "c" * 40
        self.up.journal.write_text(json.dumps(obj))
        with self.assertRaisesRegex(ServiceSwitchError, "identity/phase"):
            self.up._journal_phase()

    def test_controller_rejects_existing_upgrade_transaction(self):
        self.up._record("fencing")
        other = PersistentServiceUpgrade(self.pair)
        self.assertEqual(other._journal_phase(), "fencing")

    def test_task_guard_refuses_both_inherited_lease_markers(self):
        for name in ("LOCAL_AGENT_LEASE_FDS", "LOCAL_AGENT_LEASE_KEYS_DIGEST"):
            with self.subTest(name=name):
                with patch.dict(os.environ, {name: "protected"}, clear=True):
                    with self.assertRaisesRegex(ServiceSwitchError,
                                                "forbidden inside agent task"):
                        _external_only()

    def test_switch_gate_precedes_any_service_effect(self):
        # No guard bypass, even for a simulated local release pair.
        with patch.dict(os.environ, {"LOCAL_AGENT_LEASE_FDS": "123"}, clear=True):
            with patch.object(self.up, "_preflight",
                              side_effect=AssertionError("preflight reached")):
                with self.assertRaisesRegex(ServiceSwitchError, "forbidden"):
                    self.up.switch()
        self.assertFalse(self.up.journal.exists())
        self.assertEqual(self.up._unit_bytes(), self.up.old_unit)

    def test_rollback_and_reconcile_guard_precedes_any_effect(self):
        with patch.dict(os.environ, {"LOCAL_AGENT_LEASE_FDS": "123"}, clear=True):
            for action in (self.up.rollback, self.up.reconcile):
                with patch.object(self.up, "_preflight",
                                  side_effect=AssertionError("preflight reached")):
                    with self.assertRaisesRegex(ServiceSwitchError, "forbidden"):
                        action()
        self.assertFalse(self.up.journal.exists())

    def test_drain_never_mutates_existing_claims(self):
        claims = self.state / "claims"
        results = self.state / "results"
        claims.mkdir()
        results.mkdir()
        (claims / "abc.json").write_text('{"test":"claim"}')
        (results / "abc.json").write_text('{"test":"result"}')
        self.up._drain(timeout=1.5)
        self.assertTrue((claims / "abc.json").is_file())
        self.assertTrue((results / "abc.json").is_file())

    def test_pending_claim_refuses_cutover(self):
        claims = self.state / "claims"
        claims.mkdir()
        (claims / "uncertain.json").write_text("{}")
        with self.assertRaisesRegex(ServiceSwitchError, "not drained"):
            self.up._drain(timeout=0.01)
        self.assertTrue((claims / "uncertain.json").is_file())


if __name__ == "__main__":
    unittest.main()
