"""Offline v2 migration provenance and no-replay checks; no live systemd calls."""
from __future__ import annotations

# ruff: noqa: E402 -- standalone test imports need explicit source sys.path

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from linux_agent.release_controller import ServiceSwitchError
from linux_agent.service_plan import ReleasePair
from linux_agent.service_upgrade_v2 import SecondPersistentServiceUpgrade


class SecondUpgradeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="local-agent-upgrade-v2-")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.home.chmod(0o700)
        self.c = self.home / "releases" / ("c" * 40)
        self.b = self.home / "releases" / ("b" * 40)
        for root in (self.c, self.b):
            f = root / "platforms/linux/local-agent/linux_agent/__main__.py"
            f.parent.mkdir(parents=True)
            f.write_text("pass\n")
        self.state = self.home / "state"
        self.state.mkdir(mode=0o700)
        self.config = self.home / "config"
        self.config.write_text("{}")
        self.config.chmod(0o600)
        self.pair = ReleasePair(
            candidate=self.c, candidate_sha=self.c.name,
            rollback=self.b, rollback_sha=self.b.name,
            python=Path(sys.executable).resolve(),
            config=self.config, state=self.state,
        )
        self.up = SecondPersistentServiceUpgrade(self.pair)
        digest = hashlib.sha256(self.up.old_unit.encode()).hexdigest()
        prior = {
            "version": 1, "unit": "local-agent-linux.service",
            "baseline_sha": "a" * 40,
            "candidate_sha": self.b.name,
            "baseline_unit_sha256": "1" * 64,
            "candidate_unit_sha256": digest,
            "phase": "committed",
        }
        self.v1 = self.state / "linux-service-upgrade-v1.json"
        self.v1.write_text(json.dumps(prior) + "\n")
        staged = {
            "version": 1, "phase": "staged",
            "unit_name": "local-agent-linux.service",
            "candidate_sha": "a" * 40,
            "rollback_sha": "z" * 40,
            "unit_sha256": "1" * 64,
        }
        (self.state / "linux-service-stage-v1.json").write_text(json.dumps(staged))
        (self.state / "linux-service-activation-v1.json").write_text(json.dumps({
            "version": 1, "phase": "running",
            "unit": "local-agent-linux.service",
            "candidate_sha": staged["candidate_sha"],
            "rollback_sha": staged["rollback_sha"],
            "unit_sha256": staged["unit_sha256"],
        }))

    def test_previous_committed_chain_accepted_without_mutation(self):
        before = self.v1.read_bytes()
        self.up._original_receipts()
        self.assertEqual(self.v1.read_bytes(), before)
        self.assertEqual(self.up.journal.name, "linux-service-upgrade-v2.json")
        self.assertNotEqual(self.up.journal, self.v1)

    def test_refuses_missing_v1_without_creating_v2(self):
        self.v1.unlink()
        with self.assertRaisesRegex(ServiceSwitchError, "receipt"):
            self.up._original_receipts()
        self.assertFalse(self.up.journal.exists())

    def test_refuses_incomplete_previous_transaction(self):
        d = json.loads(self.v1.read_text())
        d["phase"] = "fencing"
        self.v1.write_text(json.dumps(d))
        with self.assertRaisesRegex(ServiceSwitchError, "provenance"):
            self.up._original_receipts()

    def test_refuses_wrong_baseline_digest(self):
        d = json.loads(self.v1.read_text())
        d["candidate_unit_sha256"] = "f" * 64
        self.v1.write_text(json.dumps(d))
        with self.assertRaisesRegex(ServiceSwitchError, "provenance"):
            self.up._original_receipts()

    def test_refuses_tampered_original_activation(self):
        f = self.state / "linux-service-activation-v1.json"
        d = json.loads(f.read_text())
        d["candidate_sha"] = "f" * 40
        f.write_text(json.dumps(d))
        with self.assertRaisesRegex(ServiceSwitchError, "original installation"):
            self.up._original_receipts()

    def test_second_journal_separate_and_no_replay(self):
        original = self.v1.read_bytes()
        self.up._record("fencing")
        self.assertEqual(self.up._journal_phase(), "fencing")
        self.assertEqual(self.v1.read_bytes(), original)
        with patch.object(self.up, "_preflight", side_effect=AssertionError("preflight called")):
            with patch.dict("os.environ", {"LOCAL_AGENT_LEASE_FDS": "42"}):
                with self.assertRaisesRegex(ServiceSwitchError, "forbidden"):
                    self.up.switch()
        self.assertEqual(self.v1.read_bytes(), original)

    def test_v2_failed_unit_identity_refused(self):
        self.up._record("fencing")
        self.up.pair = self.pair
        another = SecondPersistentServiceUpgrade(self.pair)
        self.assertEqual(another._journal_phase(), "fencing")
        d = json.loads(self.up.journal.read_text())
        d["candidate_sha"] = "a" * 40
        self.up.journal.write_text(json.dumps(d))
        with self.assertRaisesRegex(ServiceSwitchError, "identity/phase"):
            another._journal_phase()

    def test_previous_journal_symlink_refused(self):
        copy = self.home / "copy"
        copy.write_bytes(self.v1.read_bytes())
        self.v1.unlink()
        self.v1.symlink_to(copy)
        with self.assertRaisesRegex(ServiceSwitchError, "receipt"):
            self.up._original_receipts()


if __name__ == "__main__":
    unittest.main()
