"""Portable checks for the opt-in pairing LaunchAgent; no native activation."""

from __future__ import annotations

import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.platform import macos_bridge_pairing as pairing

ROOT = Path(__file__).resolve().parents[1]


class MacOSBridgePairingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.repo = Path(self.temp.name) / "local-agent"
        for name in (
            ".venv/bin/python",
            "local_agent/conversation/github_first_bridge.py",
            "local_agent/host_ops/capabilities/local/browser/chat_bridge_pairing.py",
            "local_agent/host_ops/capabilities/local/browser/chat_bridge_http.py",
        ):
            file = self.repo / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.touch()

    def test_render_has_separate_label_no_token_or_dispatch_and_uses_aqua(self):
        definition = plistlib.loads(pairing.render_pairing_launch_agent(
            repo_root=self.repo, home=self.home))
        self.assertEqual(definition["Label"], pairing.PAIRING_LABEL)
        self.assertTrue(definition["RunAtLoad"])
        self.assertEqual(definition["LimitLoadToSessionType"], "Aqua")
        self.assertEqual(definition["Umask"], 0o077)
        args = definition["ProgramArguments"]
        self.assertIn("--pair-only", args)
        self.assertNotIn("--serve", args)
        self.assertNotIn("--dispatch-id", args)
        self.assertEqual(args[-2:], ["--port", "8768"])
        self.assertNotIn("GITHUB", str(definition))
        self.assertNotIn("Send", str(definition))
        self.assertNotIn("com.michal.local-agent.plist", str(definition))

    def test_transport_definition_uses_same_service_and_no_dispatch_scope(self):
        args = plistlib.loads(pairing.render_pairing_launch_agent(
            repo_root=self.repo, home=self.home, transport_enabled=True))["ProgramArguments"]
        self.assertIn("--serve", args)
        self.assertNotIn("--pair-only", args)
        self.assertNotIn("--dispatch-id", args)

    def test_upgrade_changes_only_sidecar_and_handles_repeat(self):
        path = pairing.pairing_plist_path(self.home)
        path.parent.mkdir(parents=True)
        path.write_bytes(pairing.render_pairing_launch_agent(repo_root=self.repo, home=self.home))
        calls = []
        def runner(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        self.assertEqual(pairing.upgrade_pairing_to_transport(
            repo_root=self.repo, home=self.home, runner=runner), "transport_enabled")
        args = plistlib.loads(path.read_bytes())["ProgramArguments"]
        self.assertIn("--serve", args)
        self.assertEqual([x[1] for x in calls], ["print", "bootout", "bootstrap"])
        self.assertEqual(pairing.upgrade_pairing_to_transport(
            repo_root=self.repo, home=self.home, runner=runner), "already_enabled")
        self.assertEqual([x[1] for x in calls], ["print", "bootout", "bootstrap", "print"])
        self.assertNotIn("com.michal.local-agent.plist", repr(calls))

    def test_upgrade_failure_restores_original_pairing_service(self):
        path = pairing.pairing_plist_path(self.home)
        path.parent.mkdir(parents=True)
        old = pairing.render_pairing_launch_agent(repo_root=self.repo, home=self.home)
        path.write_bytes(old)
        called = []
        def runner(args, **kwargs):
            called.append(args)
            if len([x for x in called if x[1] == "bootstrap"]) == 1 and args[1] == "bootstrap":
                raise subprocess.CalledProcessError(5, args)
            return subprocess.CompletedProcess(args, 0, "", "")
        with self.assertRaisesRegex(RuntimeError, "restored"):
            pairing.upgrade_pairing_to_transport(
                repo_root=self.repo, home=self.home, runner=runner)
        self.assertEqual(path.read_bytes(), old)
        self.assertEqual([x[1] for x in called], ["print", "bootout", "bootstrap", "bootstrap"])

    def test_missing_checkout_refuses_prior_to_launchctl(self):
        (self.repo / ".venv/bin/python").unlink()
        runner = mock.Mock()
        with self.assertRaises(FileNotFoundError):
            pairing.setup_pairing_service(repo_root=self.repo, home=self.home, runner=runner)
        runner.assert_not_called()

    def test_setup_activates_only_separate_sidecar(self):
        calls = []
        def runner(args, **kwargs):
            calls.append(args)
            if args[1] == "print":
                return subprocess.CompletedProcess(args, 113, "", "Could not find service")
            return subprocess.CompletedProcess(args, 0, "", "")
        self.assertEqual(pairing.setup_pairing_service(
            repo_root=self.repo, home=self.home, runner=runner), "activated")
        self.assertEqual([call[1] for call in calls], ["print", "bootstrap"])
        self.assertEqual(calls[-1][-1], str(pairing.pairing_plist_path(self.home)))
        self.assertNotIn("com.michal.local-agent.plist", repr(calls))

    def test_idempotent_active_service_never_restarts(self):
        path = pairing.pairing_plist_path(self.home)
        path.parent.mkdir(parents=True)
        path.write_bytes(pairing.render_pairing_launch_agent(repo_root=self.repo, home=self.home))
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        self.assertEqual(pairing.setup_pairing_service(
            repo_root=self.repo, home=self.home, runner=runner), "already_running")
        self.assertEqual(runner.call_count, 1)

    def test_different_definition_refuses_without_changes(self):
        path = pairing.pairing_plist_path(self.home)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"unrelated")
        runner = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, "differs"):
            pairing.setup_pairing_service(repo_root=self.repo, home=self.home, runner=runner)
        runner.assert_not_called()
        self.assertEqual(path.read_bytes(), b"unrelated")

    def test_unknown_launchctl_failure_refuses(self):
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 3, "", "permission denied"))
        with self.assertRaisesRegex(RuntimeError, "status unknown"):
            pairing.setup_pairing_service(repo_root=self.repo, home=self.home, runner=runner)
        self.assertFalse(pairing.pairing_plist_path(self.home).exists())

    def test_bootstrap_failure_does_not_restart_main_agent(self):
        calls = []
        def runner(args, **kwargs):
            calls.append(args)
            if args[1] == "print":
                return subprocess.CompletedProcess(args, 113, "", "Could not find service")
            raise subprocess.CalledProcessError(5, args)
        with self.assertRaisesRegex(RuntimeError, "activation failed"):
            pairing.setup_pairing_service(repo_root=self.repo, home=self.home, runner=runner)
        self.assertEqual([call[1] for call in calls], ["print", "bootstrap"])

    def test_installer_opt_in_calls_only_pairing_setup(self):
        import scripts.macos_launchd as installer
        with mock.patch.object(sys, "argv", ["macos_launchd.py", "install",
                                           "--with-chat-bridge-pairing"]), \
             mock.patch.object(installer, "_require_macos"), \
             mock.patch.object(installer, "validate_checkout"), \
             mock.patch.object(installer, "write_launch_agent"), \
             mock.patch.object(installer, "restart_launch_agent") as restart, \
             mock.patch.object(pairing, "setup_pairing_service", return_value="activated") as setup:
            self.assertEqual(installer.main(), 0)
            setup.assert_called_once()
            restart.assert_not_called()

    def test_default_install_does_not_launch_pairing_service(self):
        import scripts.macos_launchd as installer
        with mock.patch.object(sys, "argv", ["macos_launchd.py", "install"]), \
             mock.patch.object(installer, "_require_macos"), \
             mock.patch.object(installer, "validate_checkout"), \
             mock.patch.object(installer, "write_launch_agent"), \
             mock.patch.object(pairing, "setup_pairing_service") as setup:
            self.assertEqual(installer.main(), 0)
            setup.assert_not_called()

    def test_installer_option_rejected_for_noninstall_operations(self):
        from subprocess import run
        cmd = [sys.executable, str(ROOT / "scripts/macos_launchd.py"),
               "render", "--with-chat-bridge-pairing"]
        result = run(cmd, capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("supported only with install", result.stderr)


if __name__ == "__main__":
    unittest.main()

