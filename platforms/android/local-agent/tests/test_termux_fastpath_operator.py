"""Independent Termux operator fail-closed validation; never toggles real runit."""
from __future__ import annotations

# ruff: noqa: E402 -- test imports local Android subset

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parents[1] / 'linux' / 'local-agent'))
import operator_fastpath as ops
from package import package


class FastpathOperatorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="termux-operator-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.st = self.home / ".local/state/local-agent/termux"
        self.st.mkdir(parents=True)
        self.p = ops.paths(self.home)
        self.p["control"].mkdir(parents=True)
        self.p["candidate_parent"].mkdir(parents=True)
        self.p["service"].mkdir(parents=True)
        self.p["run"].write_bytes(
            ("#!/data/data/com.termux/files/usr/bin/sh\n"
             "export PYTHONPATH=" + str(self.p["baseline"]) +
             "\nexec python3 -B -m termux_agent --poll-seconds 30 serve\n").encode()
        )
        self.sha = "d" * 40

    def test_script_updates_only_source_and_polling(self):
        script = ops.candidate_run(
            self.p, self.p["candidate_parent"] / self.sha, self.p["run"].read_bytes())
        self.assertIn(b"--poll-seconds 10 serve", script)
        self.assertNotIn(ops.BASELINE.encode(), script)
        self.assertEqual(script.count(self.sha.encode()), 1)
        self.assertIn(b"-m termux_agent", script)

    def test_rejects_wrong_or_missing_poll_option(self):
        with self.assertRaisesRegex(ops.Refused, "template"):
            ops.candidate_run(self.p, self.p["candidate_parent"] / self.sha,
                              self.p["run"].read_bytes().replace(
                                  b"--poll-seconds 30", b"--poll-seconds 120"))

    def test_rejects_wrong_existing_source(self):
        with self.assertRaisesRegex(ops.Refused, "template"):
            ops.candidate_run(self.p, self.p["candidate_parent"] / self.sha,
                              self.p["run"].read_bytes().replace(
                                  ops.BASELINE.encode(), b"f" * 40))

    def test_candidate_manifest_matches_new_bundle_length(self):
        candidate = self.p["candidate_parent"] / self.sha
        manifest = package(candidate, self.sha)
        self.assertEqual(len(manifest["files_sha256"]), 25)
        self.assertIn("LICENSE", manifest["files_sha256"])
        ops.verify_bundle(candidate, self.sha)
        rel = candidate / next(iter(manifest["files_sha256"]))
        rel.write_bytes(rel.read_bytes() + b"\n#tamper\n")
        with self.assertRaisesRegex(ops.Refused, "checksum"):
            ops.verify_bundle(candidate, self.sha)

    def test_no_replay_of_existing_transaction(self):
        self.p["journal"].write_text(json.dumps({
            "version": 1, "phase": "running_pending_git_acceptance",
            "baseline_sha": ops.BASELINE, "candidate_sha": self.sha,
            "host": ops.HOST, "installation": ops.INSTALLATION,
        }))
        with self.assertRaisesRegex(ops.Refused, "not prepared"):
            ops.switch(self.p, self.sha)

    def test_candidate_rejects_modified_or_unlisted_license(self):
        candidate = self.p["candidate_parent"] / self.sha
        manifest = package(candidate, self.sha)
        license_file = candidate / "LICENSE"
        original = license_file.read_bytes()
        license_file.write_bytes(b"altered license")
        with self.assertRaisesRegex(ops.Refused, "checksum"):
            ops.verify_bundle(candidate, self.sha)
        license_file.write_bytes(original)
        del manifest["files_sha256"]["LICENSE"]
        (candidate / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ops.Refused, "provenance"):
            ops.verify_bundle(candidate, self.sha)

    def test_operator_rejects_git_worker_lease_before_any_cutover(self):
        with patch.dict("os.environ", {"LOCAL_AGENT_LEASE_FDS": "8"}):
            with self.assertRaisesRegex(ops.Refused, "external interactive"):
                ops.operator_environment()

    def test_preserves_old_script_when_preclaim_incomplete(self):
        candidate = self.p["candidate_parent"] / self.sha
        package(candidate, self.sha)
        old = self.p["run"].read_bytes()
        next_run = ops.candidate_run(self.p, candidate, old)
        self.p["candidate_run"].write_bytes(next_run)
        self.p["candidate_run"].chmod(0o700)
        manifest_bytes = (candidate / "manifest.json").read_bytes()
        self.p["journal"].write_text(json.dumps({
            "version": 1, "phase": "prepared", "baseline_sha": ops.BASELINE,
            "candidate_sha": self.sha, "host": ops.HOST,
            "installation": ops.INSTALLATION,
            "candidate_manifest_sha256": ops.sha256(manifest_bytes),
            "candidate_run_sha256": ops.sha256(next_run),
        }))
        (self.st / "claims").mkdir()
        (self.st / "results").mkdir()
        (self.st / "claims" / "pending.json").write_text("{}")
        with patch.object(ops, "baseline_preflight", return_value=old):
            with self.assertRaisesRegex(ops.Refused, "unfinished command claim"):
                ops.switch(self.p, self.sha)
        self.assertEqual(self.p["run"].read_bytes(), old)
        self.assertFalse((self.st / "EMERGENCY_STOP").exists())

    def test_unobservable_candidate_process_is_not_accepted(self):
        with patch.object(ops.Path, "read_bytes", side_effect=PermissionError):
            self.assertFalse(ops.running_candidate_pid(12345, self.home))

    def test_fsynced_private_atomic_file(self):
        p = self.p["control"] / "transaction.json"
        ops.save_json(p, {"phase": "prepared"})
        self.assertEqual(json.loads(p.read_text())["phase"], "prepared")
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
