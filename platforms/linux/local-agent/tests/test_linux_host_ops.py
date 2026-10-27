"""Deterministic offline tests for the request-only Linux Host Ops diagnostics."""
from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from linux_agent import host_ops


class HostOpsTests(unittest.TestCase):
    def test_profile_and_capacity_are_diagnostics(self):
        with mock.patch.object(host_ops, "_require_linux"):
            report = host_ops.inspect("overview")
        self.assertEqual(report["schema_version"], 1)
        self.assertTrue(report["diagnostic_only"])
        self.assertTrue(report["observed_at_utc"].endswith("+00:00"))
        self.assertIn("hostname", report["data"]["profile"])
        self.assertEqual(report["data"]["capacity"]["admission_slots"], None)
        self.assertIs(report["data"]["capacity"]["load_average_is_cpu_usage"], False)

    def test_missing_and_bounded_proc_are_unknown_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "meminfo"
            self.assertEqual(host_ops._memory(path)["MemAvailable"], None)
            path.write_bytes(b"MemTotal: 4096 kB\nMemAvailable: 2048 kB\n")
            self.assertEqual(host_ops._memory(path), {"MemTotal": 4194304, "MemAvailable": 2097152})
            path.write_bytes(b"MemAvailable: 123 kB\nMemAvailable: 456 kB\n")
            self.assertIsNone(host_ops._memory(path)["MemAvailable"])
            path.write_bytes(b"MemTotal: garbage\nMemAvailable: 100 MB\n")
            self.assertIsNone(host_ops._memory(path)["MemTotal"])
            self.assertIsNone(host_ops._memory(path)["MemAvailable"])
            path.write_bytes(b"x" * (host_ops._MEMINFO_MAX_BYTES + 1))
            self.assertEqual(host_ops._memory(path), {"MemTotal": None, "MemAvailable": None})

    def test_invalid_and_duplicate_names(self):
        with mock.patch.object(host_ops, "_require_linux"):
            for names in ((), ("../ssh",), ("/usr/bin/git",), ("bad name",), ("x",) * 65):
                with self.subTest(names=names[:2]), self.assertRaises(ValueError):
                    host_ops.tools(names)
            with mock.patch.object(host_ops.shutil, "which", return_value=None) as which:
                report = host_ops.tools(("missing-tool", "missing-tool"))
            self.assertEqual(len(report["items"]), 1)
            self.assertEqual(which.call_count, 1)
            self.assertIsNone(report["items"][0]["path"])
            self.assertFalse(report["probe_executed"])

    def test_tools_are_metadata_only_never_executed(self):
        with mock.patch.object(host_ops, "_require_linux"), \
             mock.patch.object(host_ops.shutil, "which", return_value="/usr/bin/ssh"), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("must not execute")):
            report = host_ops.inspect("tools", ("ssh",))
        self.assertTrue(report["data"]["items"][0]["available"])
        self.assertEqual(report["data"]["items"][0]["version_status"], "not_probed")

    def test_invalid_load_and_unavailable_storage(self):
        with mock.patch.object(host_ops.os, "getloadavg", return_value=(float("nan"), 1, 2)):
            self.assertEqual(host_ops._load(), [None, None, None])
        with mock.patch.object(host_ops.shutil, "disk_usage", side_effect=OSError("unavailable")):
            self.assertEqual(host_ops._storage(), {"total_bytes": None, "free_bytes": None})

    def test_cli_json_text_and_bad_arguments(self):
        with mock.patch.object(host_ops, "_require_linux"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(host_ops.main(["tools", "git", "--format", "json"]), 0)
            report = json.loads(out.getvalue())
            self.assertEqual(report["kind"], "tools")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(host_ops.main(["capacity", "--format", "text"]), 0)
            self.assertIn("diagnostic only", out.getvalue())
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(host_ops.main(["profile", "unexpected"]), 2)
            self.assertIn("unexpected", err.getvalue())


    def test_effect_classes_and_failed_ssh_exit_code(self):
        with mock.patch.object(host_ops, "copy_artifact", return_value={"verified": True}):
            report = host_ops.inspect("copy", ("/tmp/src", "/tmp/dst"))
        self.assertEqual(report["effect_class"], "local_mutation")
        self.assertFalse(report["diagnostic_only"])
        with mock.patch.object(host_ops, "_require_linux"):
            self.assertEqual(host_ops.inspect("capacity")["effect_class"], "passive_read")
        with mock.patch.object(host_ops, "ssh_operation",
                               return_value={"ok": False, "exit_code": 255}):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(host_ops.main([
                    "ssh-exec", "localhost", "admin", "/tmp/key",
                    "/tmp/known", "true"
                ]), 3)
            report = json.loads(output.getvalue())
        self.assertEqual(report["effect_class"], "remote_exec")
        self.assertFalse(report["diagnostic_only"])



if __name__ == "__main__":
    unittest.main()
