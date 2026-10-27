"""Hermetic source and environment gate: no hosted Actions or task execution."""
from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import verify_local as gate

HEAD = "a" * 40


class VerifyLocalGateTests(unittest.TestCase):
    def test_exact_source_sha_and_clean_checkout_required(self) -> None:
        good = subprocess.CompletedProcess([], 0, HEAD + "\n", "")
        clean = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(gate.subprocess, "run", side_effect=[good, clean]) as run:
            self.assertEqual(gate.exact_head(HEAD, root=Path("/tmp/isolated-root")), HEAD)
            self.assertEqual(run.call_count, 2)
        with self.assertRaisesRegex(ValueError, "exact lowercase"):
            gate.exact_head("not-a-sha")
        bad = subprocess.CompletedProcess([], 0, "b" * 40 + "\n", "")
        with mock.patch.object(gate.subprocess, "run", return_value=bad):
            with self.assertRaisesRegex(ValueError, "HEAD mismatch"):
                gate.exact_head(HEAD)
        changed = subprocess.CompletedProcess([], 0, " M test.txt\n", "")
        with mock.patch.object(gate.subprocess, "run", side_effect=[good, changed]):
            with self.assertRaisesRegex(ValueError, "not clean"):
                gate.exact_head(HEAD)

    def test_plan_is_explicit_and_pins_interpreter(self) -> None:
        core = gate.planned_commands("core", python="/tmp/python")
        self.assertEqual(len(core), 4)
        self.assertTrue(all(stage[1][0] == "/tmp/python" for stage in core))
        full = gate.planned_commands("full", include_browser=True,
                                     include_python314=True, python="/tmp/python")
        self.assertEqual(len(full), 12)
        self.assertEqual(full[-2][1][0], "python3.14")
        self.assertEqual(full[-1][0], "isolated Chromium")
        self.assertIn("--cov-fail-under=85", full[7][1])

    def test_sandbox_plan_is_portable_and_does_not_claim_mac_smoke(self) -> None:
        sandbox = gate.planned_commands("sandbox", python="/sandbox/bin/python")
        self.assertEqual(len(sandbox), 9)
        self.assertEqual(sandbox[0][0], "compile")
        self.assertEqual(sandbox[3][0], "unit and integration")
        self.assertEqual(sandbox[-1][0], "core coverage report")
        self.assertTrue(all(stage[1][0] == "/sandbox/bin/python"
                            for stage in sandbox))
        self.assertFalse(any("macOS smoke" in label for label, _ in sandbox))
        self.assertIn("--cov-fail-under=85", sandbox[6][1])
        self.assertEqual(len(gate.planned_commands("full")), 10)
        self.assertEqual(len(gate.planned_commands("core")), 4)

    @unittest.skipUnless(importlib.util.find_spec("coverage"), "coverage required for full verification")
    def test_coverage_gate_cannot_be_inflated_by_covered_test_code(self) -> None:
        import coverage
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            package = root / "local_agent"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "fixture.py").write_text(
                "def used():\n    return 1\ndef untested():\n"
                + "".join(f"    value_{index} = {index}\n" for index in range(30))
            )
            (root / "test_fixture.py").write_text(
                "import unittest\nfrom local_agent.fixture import used\n"
                "class Fixture(unittest.TestCase):\n    def test_used(self):\n"
                + "        self.assertEqual(used(), 1)\n" * 100
            )
            command = dict(gate.planned_commands("full", python=sys.executable))["core coverage"]
            result = subprocess.run(
                command, cwd=root, env={**os.environ, "COVERAGE_FILE": str(root / ".coverage")},
                capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = coverage.Coverage(data_file=str(root / ".coverage"), config_file=False)
            data.load()
            self.assertTrue(all(Path(path).resolve().is_relative_to(package.resolve())
                                for path in data.get_data().measured_files()))
            self.assertLess(data.report(file=io.StringIO()), 70)

    def test_sandbox_gate_requires_linux_and_portable_test_dependencies(self) -> None:
        with mock.patch.object(gate.sys, "platform", "darwin"), \
             mock.patch.object(gate.shutil, "which", return_value="/usr/bin/node"), \
             mock.patch.object(gate.importlib.util, "find_spec", return_value=object()):
            self.assertIn("Linux sandbox host", gate.missing_dependencies("sandbox"))
        with mock.patch.object(gate.sys, "platform", "linux"), \
             mock.patch.object(gate.shutil, "which", return_value="/usr/bin/node"), \
             mock.patch.object(gate.importlib.util, "find_spec", return_value=object()), \
             mock.patch.object(gate, "system_openssh_ready", return_value=True):
            self.assertEqual(gate.missing_dependencies("sandbox"), [])

    def test_sandbox_preflight_requires_real_system_openssh(self) -> None:
        with mock.patch.object(gate.sys, "platform", "linux"), \
             mock.patch.object(gate.shutil, "which", return_value="/usr/bin/node"), \
             mock.patch.object(gate.importlib.util, "find_spec", return_value=object()), \
             mock.patch.object(gate, "system_openssh_ready", return_value=False):
            self.assertIn("real system OpenSSH client (/usr/bin/ssh)",
                          gate.missing_dependencies("sandbox"))
            self.assertNotIn("real system OpenSSH client (/usr/bin/ssh)",
                             gate.missing_dependencies("core"))

    def test_openssh_identity_check_rejects_missing_or_fake_cli(self) -> None:
        with mock.patch.object(type(gate._SYSTEM_SSH), "is_file", return_value=False):
            self.assertFalse(gate.system_openssh_ready())
        with mock.patch.object(type(gate._SYSTEM_SSH), "is_file", return_value=True), \
             mock.patch.object(gate.os, "access", return_value=True), \
             mock.patch.object(gate.subprocess, "run",
                               return_value=subprocess.CompletedProcess(
                                   [], 0, stdout="", stderr="Fake SSH 1.0"
                               )):
            self.assertFalse(gate.system_openssh_ready())
        with mock.patch.object(type(gate._SYSTEM_SSH), "is_file", return_value=True), \
             mock.patch.object(gate.os, "access", return_value=True), \
             mock.patch.object(gate.subprocess, "run",
                               return_value=subprocess.CompletedProcess(
                                   [], 0, stdout="", stderr="OpenSSH_10.0p1, OpenSSL 3"
                               )):
            self.assertTrue(gate.system_openssh_ready())

    def test_sanitization_is_explicit_and_does_not_mutate_daemon_environment(self) -> None:
        env = {"LOCAL_AGENT_LEASE_FDS": "3", "LOCAL_AGENT_LEASE_KEYS_DIGEST": "abc",
               "LOCAL_AGENT_RESOURCE_LEASE_FDS": "4", "UNRELATED": "safe"}
        with mock.patch.dict(os.environ, env, clear=True):
            normal = gate.subprocess_environment()
            self.assertEqual(normal["LOCAL_AGENT_LEASE_FDS"], "3")
            hermetic = gate.subprocess_environment(sanitize_test_lease_markers=True)
            self.assertEqual(hermetic["UNRELATED"], "safe")
            self.assertTrue(all(k not in hermetic for k in gate._LEASE_MARKERS))
            self.assertEqual(os.environ["LOCAL_AGENT_LEASE_FDS"], "3")

    def test_isolated_browser_module_preflight_uses_exact_requested_module(self) -> None:
        isolated = "/tmp/verified-test-node_modules/playwright"
        resolved = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.dict(os.environ, {"LOCAL_AGENT_PLAYWRIGHT_MODULE": isolated}), \
             mock.patch.object(gate.shutil, "which", return_value="/usr/bin/node"), \
             mock.patch.object(gate.importlib.util, "find_spec", return_value=object()), \
             mock.patch.object(gate.subprocess, "run", return_value=resolved) as spawn:
            self.assertEqual(gate.missing_dependencies("core", include_browser=True), [])
        self.assertEqual(spawn.call_args.args[0][-1], isolated)

    def test_unresolved_isolated_browser_module_blocks_browser_gate(self) -> None:
        with mock.patch.dict(os.environ, {"LOCAL_AGENT_PLAYWRIGHT_MODULE": "/missing/playwright"}), \
             mock.patch.object(gate.shutil, "which", return_value="/usr/bin/node"), \
             mock.patch.object(gate.importlib.util, "find_spec", return_value=object()), \
             mock.patch.object(gate.subprocess, "run",
                               side_effect=subprocess.CalledProcessError(1, ["node"])):
            self.assertIn("Node playwright module",
                          gate.missing_dependencies("core", include_browser=True))

    def test_missing_dependencies_stops_before_running_any_test(self) -> None:
        with mock.patch.object(gate, "exact_head", return_value=HEAD), \
             mock.patch.object(gate, "missing_dependencies", return_value=["httpx2"]), \
             mock.patch.object(gate.subprocess, "run") as execute:
            self.assertEqual(gate.main(["--expected-sha", HEAD]), 2)
            execute.assert_not_called()

    def test_failed_stage_does_not_report_success(self) -> None:
        with mock.patch.object(gate, "exact_head", return_value=HEAD), \
             mock.patch.object(gate, "missing_dependencies", return_value=[]), \
             mock.patch.object(gate, "planned_commands", return_value=[("fail", ["false"])]), \
             mock.patch.object(gate.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 1)):
            self.assertEqual(gate.main(["--expected-sha", HEAD]), 1)


if __name__ == "__main__":
    unittest.main()
