"""Offline on-demand service/ledger inspection tests; no systemd mutations."""
from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from linux_agent import host_ops_status as status


class StatusTests(unittest.TestCase):
    def test_private_disposable_state_counts_not_as_live_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp) / "state"
            state.mkdir(mode=0o700)
            (state / "claims").mkdir(mode=0o700)
            (state / "results").mkdir(mode=0o700)
            (state / "claims" / "task-a.json").write_text("{}")
            (state / "claims" / "task-b.json").write_text("{}")
            (state / "results" / "task-a.json").write_text("{}")
            (state / "EMERGENCY_STOP").write_text("stop")
            with mock.patch.object(status, "_service", return_value={"available": True}):
                observed = status.inspect_status(str(state))
            self.assertEqual(observed["local_claim_count"], 2)
            self.assertEqual(observed["local_spooled_result_count"], 1)
            self.assertEqual(observed["claims_without_local_spool"], 1)
            self.assertTrue(observed["emergency_stop_present"])
            self.assertIsNone(observed["git_pending_tasks"])
            self.assertTrue(observed["claim_result_counts_are_not_live_queue_size"])
            (state / "EMERGENCY_STOP").unlink()
            with mock.patch.object(status, "_service", return_value={"available": False}):
                self.assertFalse(status.inspect_status(str(state))["emergency_stop_present"])

    def test_invalid_state_symlink_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            state = root / "state"
            state.mkdir(mode=0o700)
            (state / "claims").mkdir(mode=0o700)
            (state / "results").mkdir(mode=0o700)
            link = root / "linked"
            link.symlink_to(state, target_is_directory=True)
            for path in ("relative", str(link), str(root / "missing")):
                with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                    status.inspect_status(path)
            state.chmod(0o755)
            with self.assertRaisesRegex(status.StatusInspectionError, "owner-private"):
                status.inspect_status(str(state))
            state.chmod(0o700)
            (state / "claims" / "task-a.json").symlink_to("/dev/null")
            with self.assertRaisesRegex(status.StatusInspectionError, "unsafe"):
                status.inspect_status(str(state))

    def test_readonly_systemctl_query_unavailable_and_fixed(self):
        good = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=b"ActiveState=active\nUnitFileState=enabled\nMainPID=922\n",
            stderr=b"",
        )
        with mock.patch.object(status.subprocess, "run", return_value=good) as run:
            service = status._service()
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "/usr/bin/systemctl")
        self.assertIn("--user", argv)
        self.assertEqual(argv[-1], "local-agent-linux.service")
        self.assertEqual(service["main_pid"], 922)
        self.assertTrue(service["available"])
        with mock.patch.object(status.subprocess, "run", side_effect=OSError()):
            self.assertFalse(status._service()["available"])


if __name__ == "__main__":
    unittest.main()
