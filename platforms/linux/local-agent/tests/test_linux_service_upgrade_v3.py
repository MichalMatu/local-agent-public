"""v3 upgrade requires genuine v2+v1 provenance and a fresh journal."""
from __future__ import annotations

# ruff: noqa: E402 -- native stand-alone test imports add checkout root

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from linux_agent.release_controller import ServiceSwitchError
from linux_agent.service_plan import ReleasePair
from linux_agent.service_upgrade_v2 import SecondPersistentServiceUpgrade
from linux_agent.service_upgrade_v3 import ThirdPersistentServiceUpgrade


class ThirdUpgradeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="local-agent-v3-test-")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.home.chmod(0o700)
        self.a = self.home / "releases" / ("a" * 40)
        self.b = self.home / "releases" / ("b" * 40)
        self.c = self.home / "releases" / ("c" * 40)
        self.d = self.home / "releases" / ("d" * 40)
        for root in (self.a, self.b, self.c, self.d):
            f = root / "platforms/linux/local-agent/linux_agent/__main__.py"
            f.parent.mkdir(parents=True)
            f.write_text("pass\n")
        self.state = self.home / "state"
        self.state.mkdir(mode=0o700)
        self.config = self.home / "config.json"
        self.config.write_text("{}")
        self.config.chmod(0o600)
        self.pair = ReleasePair(
            candidate=self.d, candidate_sha=self.d.name,
            rollback=self.c, rollback_sha=self.c.name,
            python=Path(sys.executable).resolve(),
            config=self.config, state=self.state,
        )
        self.up = ThirdPersistentServiceUpgrade(self.pair)
        # This fixture uses fake 40-hex IDs for pure provenance tests;
        # exact Git SHA/cleanliness is separately enforced by real preflight.
        source = patch("linux_agent.release_controller.verify_clean_exact_source")
        source.start()
        self.addCleanup(source.stop)
        v2pair = ReleasePair(
            candidate=self.c, candidate_sha=self.c.name,
            rollback=self.b, rollback_sha=self.b.name,
            python=self.pair.python, config=self.config, state=self.state,
        )
        v2 = SecondPersistentServiceUpgrade(v2pair)
        v1pair = ReleasePair(
            candidate=self.b, candidate_sha=self.b.name,
            rollback=self.a, rollback_sha=self.a.name,
            python=self.pair.python, config=self.config, state=self.state,
        )
        from linux_agent.service_upgrade import PersistentServiceUpgrade
        v1 = PersistentServiceUpgrade(v1pair)
        self.paths = [
            self.state / "linux-service-upgrade-v1.json",
            self.state / "linux-service-upgrade-v2.json",
            self.state / "linux-service-stage-v1.json",
            self.state / "linux-service-activation-v1.json",
        ]
        stage = {
            "version": 1, "phase": "staged", "unit_name": "local-agent-linux.service",
            "candidate_sha": self.a.name, "rollback_sha": "f"*40,
            "unit_sha256": hashlib.sha256(v1.old_unit.encode()).hexdigest(),
        }
        activation = {
            "version": 1, "phase": "running", "unit": "local-agent-linux.service",
            "candidate_sha": self.a.name, "rollback_sha": "f"*40,
            "unit_sha256": stage["unit_sha256"],
        }
        for path, payload in [(self.paths[2], stage), (self.paths[3], activation)]:
            path.write_text(json.dumps(payload))
        v1._record("committed")
        v2._record("committed")

    def test_full_prior_journal_chain_passes_and_is_immutable(self):
        before = [p.read_bytes() for p in self.paths]
        self.up._original_receipts()
        self.assertEqual(before, [p.read_bytes() for p in self.paths])
        self.assertEqual(self.up.journal.name, "linux-service-upgrade-v3.json")
        self.assertFalse(self.up.journal.exists())

    def test_missing_prior_v2_refused_without_new_journal(self):
        self.paths[1].unlink()
        with self.assertRaisesRegex(ServiceSwitchError, "receipt"):
            self.up._original_receipts()
        self.assertFalse(self.up.journal.exists())

    def test_v2_uncommitted_fails_closed(self):
        data = json.loads(self.paths[1].read_text())
        data["phase"] = "rollback_requested"
        self.paths[1].write_text(json.dumps(data))
        with self.assertRaisesRegex(ServiceSwitchError, "v2 committed"):
            self.up._original_receipts()

    def test_v2_wrong_source_digest_fails_closed(self):
        data = json.loads(self.paths[1].read_text())
        data["candidate_unit_sha256"] = "f"*64
        self.paths[1].write_text(json.dumps(data))
        with self.assertRaisesRegex(ServiceSwitchError, "v2 committed"):
            self.up._original_receipts()

    def test_v2_wrong_baseline_digest_fails_closed(self):
        data = json.loads(self.paths[1].read_text())
        data["baseline_unit_sha256"] = "f"*64
        self.paths[1].write_text(json.dumps(data))
        with self.assertRaisesRegex(ServiceSwitchError, "identity/phase"):
            self.up._original_receipts()

    def test_v1_tampered_original_activation_fails_closed(self):
        path = self.paths[3]
        data = json.loads(path.read_text())
        data["candidate_sha"] = "f"*40
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ServiceSwitchError, "original installation"):
            self.up._original_receipts()

    def test_no_replay_of_existing_v3_journal(self):
        self.up._record("fencing")
        before = [p.read_bytes() for p in self.paths]
        self.assertEqual(self.up._journal_phase(), "fencing")
        with patch.dict("os.environ", {"LOCAL_AGENT_LEASE_FDS": "12"}):
            with self.assertRaisesRegex(ServiceSwitchError, "forbidden"):
                self.up.switch()
        self.assertEqual(before, [p.read_bytes() for p in self.paths])

    def test_v3_journal_identity_tamper_refused(self):
        self.up._record("fencing")
        data = json.loads(self.up.journal.read_text())
        data["baseline_sha"] = "0"*40
        self.up.journal.write_text(json.dumps(data))
        with self.assertRaisesRegex(ServiceSwitchError, "identity/phase"):
            self.up._journal_phase()

    def test_prior_journal_symlink_fails_closed(self):
        prior = self.paths[1]
        copy = self.home / "copy.json"
        copy.write_bytes(prior.read_bytes())
        prior.unlink()
        prior.symlink_to(copy)
        with self.assertRaisesRegex(ServiceSwitchError, "receipt"):
            self.up._original_receipts()


if __name__ == "__main__":
    unittest.main()
