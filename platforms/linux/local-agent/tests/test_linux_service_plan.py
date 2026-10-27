"""Static, non-installing systemd/rollback plan contract."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from linux_agent.service_plan import ReleasePair, source_layout, source_pythonpath


class ServicePlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        a = "a" * 40
        b = "b" * 40
        releases = self.root / "releases"
        for sha in (a, b):
            pkg = releases / sha / "platforms/linux/local-agent/linux_agent"
            pkg.mkdir(parents=True)
            (pkg / "__main__.py").write_text("# isolated fixture\n")
        python = self.root / "venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text("fixture")
        config = self.root / "private/pilot.json"
        config.parent.mkdir()
        config.write_text("{}")
        config.chmod(0o600)
        state = self.root / "state"
        state.mkdir(mode=0o700)
        self.pair = ReleasePair(
            releases / a, a, releases / b, b, python, config, state
        )

    def test_rendered_units_are_distinct_without_filesystem_effects(self):
        planned = self.pair.dry_run()
        self.assertEqual(planned["status"], "plan_only_no_host_mutations")
        self.assertNotEqual(planned["candidate_unit"], planned["rollback_unit"])
        self.assertIn("KillMode=control-group", planned["candidate_unit"])
        self.assertIn("Environment=PYTHONDONTWRITEBYTECODE=1", planned["candidate_unit"])
        self.assertIn(" -B -m linux_agent ", planned["candidate_unit"])
        self.assertIn("TimeoutStopSec=40", planned["candidate_unit"])
        self.assertIn("serve", planned["candidate_unit"])
        self.assertIn(self.pair.candidate_sha, planned["candidate_unit"])
        self.assertIn(self.pair.rollback_sha, planned["rollback_unit"])
        self.assertFalse((self.root / "private/local-agent-linux.service").exists())

    def test_no_fake_rollback_or_unsafe_paths(self):
        bad = ReleasePair(
            self.pair.candidate, self.pair.candidate_sha,
            self.pair.candidate, self.pair.candidate_sha,
            self.pair.python, self.pair.config, self.pair.state
        )
        with self.assertRaises(ValueError):
            bad.dry_run()
        bad = ReleasePair(
            self.pair.candidate / "../other", self.pair.candidate_sha,
            self.pair.rollback, self.pair.rollback_sha,
            self.pair.python, self.pair.config, self.pair.state
        )
        with self.assertRaises(ValueError):
            bad.dry_run()
        self.pair.config.chmod(0o644)
        with self.assertRaises(ValueError):
            self.pair.dry_run()


    def test_mixed_old_rollback_and_new_shared_core_candidate(self):
        """A new release must retain the exact old immutable rollback."""
        candidate = self.pair.candidate
        rollback = self.pair.rollback
        source = candidate / "platforms/linux/core/linux_agent/__main__.py"
        source.parent.mkdir(parents=True)
        source.write_text("# new shared core fixture\\n")
        bridge = candidate / "platforms/linux/local-agent/linux_agent/__init__.py"
        bridge.write_text("# source-local adapter fixture\\n")
        old_entry = candidate / "platforms/linux/local-agent/linux_agent/__main__.py"
        old_entry.unlink()
        self.assertEqual(source_layout(candidate)[1], source)
        self.assertEqual(
            source_pythonpath(candidate),
            f"{candidate}/platforms/linux/local-agent:{candidate}/platforms/linux/core",
        )
        self.assertEqual(
            source_pythonpath(rollback),
            f"{rollback}/platforms/linux/local-agent",
        )
        planned = self.pair.dry_run()
        self.assertIn(source_pythonpath(candidate), planned["candidate_unit"])
        self.assertIn(source_pythonpath(rollback), planned["rollback_unit"])
        self.assertNotIn("platforms/linux/core", planned["rollback_unit"])

    def test_missing_new_adapter_bridge_refuses_before_unit_render(self):
        source = self.pair.candidate / "platforms/linux/core/linux_agent/__main__.py"
        source.parent.mkdir(parents=True)
        source.write_text("# candidate fixture\\n")
        with self.assertRaisesRegex(ValueError, "adapter"):
            source_layout(self.pair.candidate)
        with self.assertRaisesRegex(ValueError, "candidate must contain"):
            self.pair.dry_run()

    def test_symlinked_core_does_not_fall_back_to_legacy(self):
        source = self.pair.candidate / "platforms/linux/core/linux_agent/__main__.py"
        source.parent.mkdir(parents=True)
        source.symlink_to(
            self.pair.candidate / "platforms/linux/local-agent/linux_agent/__main__.py"
        )
        with self.assertRaisesRegex(ValueError, "symlink"):
            source_layout(self.pair.candidate)
        with self.assertRaisesRegex(ValueError, "candidate must contain"):
            self.pair.dry_run()

    def test_symlinked_core_parent_rejected(self):
        source = self.pair.candidate / "platforms/linux/core"
        source.symlink_to(self.pair.candidate / "platforms/linux/local-agent",
                          target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            source_layout(self.pair.candidate)
        with self.assertRaisesRegex(ValueError, "candidate must contain"):
            self.pair.dry_run()


if __name__ == "__main__":
    unittest.main()
