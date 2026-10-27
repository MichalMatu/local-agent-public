from __future__ import annotations

import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import local_agent.platform.macos_launchd as macos_launchd
from local_agent.platform.macos_launchd import (
    LABEL,
    build_launch_agent,
    build_program_arguments,
    default_launch_agent_path,
    render_launch_agent,
    restart_launch_agent,
    validate_checkout,
    wait_until_unloaded,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class MacOSLaunchdTests(unittest.TestCase):
    def test_restart_from_old_checkout_keeps_exact_installed_release_definition(self):
        from scripts import macos_launchd as command

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = default_launch_agent_path(home)
            path.parent.mkdir(parents=True)
            raw = plistlib.dumps({"Label": LABEL, "ProgramArguments": ["accepted-python", "/accepted/agent_entrypoint.py"],
                                  "WorkingDirectory": "/accepted", "EnvironmentVariables": {"LOCAL_AGENT_MACOS_ADMISSION_REQUIRED": "1"}})
            path.write_bytes(raw)
            with (
                mock.patch.object(sys, "argv", ["macos_launchd.py", "restart", "--home", str(home)]),
                mock.patch.object(command, "REPO_ROOT", Path("/old")),
                mock.patch.object(command, "_require_macos"),
                mock.patch.object(command, "restart_launch_agent") as restart,
                mock.patch.object(command, "_render", side_effect=AssertionError("restart must not render")),
            ):
                self.assertEqual(command.main(), 0)
            restart.assert_called_once_with(path)
            self.assertEqual(path.read_bytes(), raw)

    def test_restart_rejects_source_reconfiguration_instead_of_ignoring_flags(self):
        from scripts import macos_launchd as command

        with (mock.patch.object(sys, "argv", ["macos_launchd.py", "restart", "--max-workers", "1"]),
              mock.patch.object(command, "restart_launch_agent") as restart):
            with self.assertRaises(SystemExit) as raised:
                command.main()
        self.assertEqual(raised.exception.code, 2)
        restart.assert_not_called()

    def setUp(self) -> None:
        self.home = Path("/Users/tester")
        self.repo = self.home / "src" / "local-agent"

    def test_parallel_definition_uses_resolved_paths(self) -> None:
        payload = build_launch_agent(
            "parallel",
            repo_root=self.repo,
            home=self.home,
            max_workers=2,
        )
        self.assertEqual(payload["Label"], LABEL)
        self.assertEqual(payload["WorkingDirectory"], str(self.repo))
        self.assertEqual(
            payload["ProgramArguments"],
            [
                str(self.repo / ".venv" / "bin" / "python"),
                str(self.repo / "agent_entrypoint.py"),
                "--registry",
                str(
                    self.home
                    / "Library"
                    / "Application Support"
                    / "local-agent"
                    / "repositories.json"
                ),
                "--max-workers",
                "2",
            ],
        )
        self.assertEqual(
            payload["ExitTimeOut"],
            macos_launchd.LAUNCHD_EXIT_TIMEOUT_SECONDS,
        )
        self.assertGreater(
            macos_launchd.UNLOAD_TIMEOUT_SECONDS,
            macos_launchd.LAUNCHD_EXIT_TIMEOUT_SECONDS,
        )
        environment = payload["EnvironmentVariables"]
        self.assertEqual(environment["HOME"], str(self.home))
        self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertNotIn("/Users/michal", repr(payload))

    def test_all_modes_select_expected_entrypoint(self) -> None:
        expected = {
            "parallel": "agent_entrypoint.py",
            "multirepo": "agent_multirepo.py",
            "single": "agentd.py",
        }
        for mode, entrypoint in expected.items():
            with self.subTest(mode=mode):
                arguments = build_program_arguments(
                    mode,
                    repo_root=self.repo,
                    home=self.home,
                )
                self.assertTrue(arguments[1].endswith(entrypoint))

    def test_parallel_worker_bound_matches_scheduler_contract(self) -> None:
        arguments = build_program_arguments(
            "parallel",
            repo_root=self.repo,
            home=self.home,
            max_workers=macos_launchd.MAX_MAX_WORKERS,
        )
        self.assertEqual(arguments[-1], str(macos_launchd.MAX_MAX_WORKERS))
        for value in (0, macos_launchd.MAX_MAX_WORKERS + 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                build_program_arguments(
                    "parallel",
                    repo_root=self.repo,
                    home=self.home,
                    max_workers=value,
                )

    def test_rendered_plist_round_trips(self) -> None:
        rendered = render_launch_agent(
            "parallel",
            repo_root=self.repo,
            home=self.home,
        )
        payload = plistlib.loads(rendered)
        self.assertEqual(payload["Label"], LABEL)
        self.assertEqual(payload["WorkingDirectory"], str(self.repo))
        self.assertEqual(
            payload["ExitTimeOut"],
            macos_launchd.LAUNCHD_EXIT_TIMEOUT_SECONDS,
        )

    def test_render_cli_runs_from_outside_repository_with_max_workers_four(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "scripts" / "macos_launchd.py"),
                    "render",
                    "--home",
                    str(self.home),
                    "--repo-root",
                    str(self.repo),
                    "--max-workers",
                    "4",
                ],
                cwd=tmp,
                text=False,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        payload = plistlib.loads(result.stdout)
        self.assertEqual(payload["Label"], LABEL)
        self.assertEqual(payload["WorkingDirectory"], str(self.repo))
        self.assertEqual(payload["ProgramArguments"][-2:], ["--max-workers", "4"])
        self.assertEqual(
            payload["ExitTimeOut"],
            macos_launchd.LAUNCHD_EXIT_TIMEOUT_SECONDS,
        )

    def test_checkout_validation_requires_packaged_workers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            files = (
                ".venv/bin/python", "agentd.py", "agent_entrypoint.py",
                "agent_parallel.py", "agent_multirepo.py", "local_agent/__init__.py",
                "platforms/macos/local-agent/paths.py",
                "platforms/macos/local-agent/daemon/service.py",
                "platforms/macos/local-agent/entrypoint.py",
                "platforms/macos/local-agent/supervisor/orchestrator.py",
                "platforms/macos/local-agent/supervisor/serial.py",
                "platforms/macos/local-agent/supervisor/worker.py",
                "platforms/macos/local-agent/repository/worker.py",
            )
            for filename in files:
                path = checkout / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            validate_checkout(checkout)
            (checkout / "platforms/macos/local-agent/supervisor/worker.py").unlink()
            with self.assertRaisesRegex(FileNotFoundError, "supervisor/worker.py"):
                validate_checkout(checkout)

    def test_relocated_checkout_requires_root_import_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            for relative in (
                ".venv/bin/python", "agentd.py", "agent_entrypoint.py",
                "agent_multirepo.py", "agent_parallel.py",
                "platforms/macos/local-agent/paths.py",
                "platforms/macos/local-agent/daemon/service.py",
                "platforms/macos/local-agent/entrypoint.py",
                "platforms/macos/local-agent/supervisor/orchestrator.py",
                "platforms/macos/local-agent/supervisor/serial.py",
                "platforms/macos/local-agent/supervisor/worker.py",
                "platforms/macos/local-agent/repository/worker.py",
            ):
                file = checkout / relative
                file.parent.mkdir(parents=True, exist_ok=True)
                file.touch()
            with self.assertRaisesRegex(FileNotFoundError, "local_agent/__init__.py"):
                validate_checkout(checkout)
            (checkout / "local_agent").mkdir()
            (checkout / "local_agent/__init__.py").touch()
            validate_checkout(checkout)

    def test_default_install_location_is_user_launch_agents(self) -> None:
        self.assertEqual(
            default_launch_agent_path(self.home),
            self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist",
        )

    def test_wait_until_unloaded_polls_until_launchd_forgets_service(self) -> None:
        loaded = subprocess.CompletedProcess(["launchctl"], 0, stdout="loaded", stderr="")
        absent = subprocess.CompletedProcess(["launchctl"], 113, stdout="", stderr="not found")
        with (
            mock.patch.object(macos_launchd, "print_status", side_effect=[loaded, absent]) as status,
            mock.patch.object(macos_launchd.time, "sleep") as sleep,
        ):
            wait_until_unloaded(timeout=1.0, poll_interval=0.01)
        self.assertEqual(status.call_count, 2)
        sleep.assert_called_once_with(0.01)

    def test_wait_until_unloaded_times_out_fail_closed(self) -> None:
        loaded = subprocess.CompletedProcess(["launchctl"], 0, stdout="loaded", stderr="")
        with (
            mock.patch.object(macos_launchd, "print_status", return_value=loaded),
            mock.patch.object(macos_launchd.time, "monotonic", side_effect=[0.0, 0.5, 1.1]),
            mock.patch.object(macos_launchd.time, "sleep"),
        ):
            with self.assertRaisesRegex(TimeoutError, "did not unload"):
                wait_until_unloaded(timeout=1.0, poll_interval=0.01)

    def test_restart_retries_transient_exit_five_after_confirming_absent(self) -> None:
        plist = Path("/Users/tester/Library/LaunchAgents/com.michal.local-agent.plist")
        transient = subprocess.CalledProcessError(
            5,
            ["launchctl", "bootstrap"],
            output="",
            stderr="Bootstrap failed: 5: Input/output error\nBad request.\n",
        )
        success = subprocess.CompletedProcess(["launchctl", "bootstrap"], 0, "", "")
        absent = subprocess.CompletedProcess(["launchctl", "print"], 113, "", "not found")
        with (
            mock.patch.object(macos_launchd, "bootout") as bootout,
            mock.patch.object(macos_launchd, "wait_until_unloaded") as wait,
            mock.patch.object(macos_launchd, "bootstrap", side_effect=[transient, success]) as bootstrap,
            mock.patch.object(macos_launchd, "print_status", return_value=absent),
            mock.patch.object(macos_launchd.time, "sleep") as sleep,
        ):
            result = restart_launch_agent(plist, bootstrap_attempts=2, retry_delay=0.01)
        self.assertEqual(result.returncode, 0)
        bootout.assert_called_once_with(uid=None, check=False)
        wait.assert_called_once()
        self.assertEqual(bootstrap.call_count, 2)
        sleep.assert_called_once_with(0.01)

    def test_restart_does_not_retry_non_transient_bootstrap_failure(self) -> None:
        plist = Path("/Users/tester/Library/LaunchAgents/com.michal.local-agent.plist")
        failure = subprocess.CalledProcessError(
            5,
            ["launchctl", "bootstrap"],
            output="",
            stderr="Invalid property list",
        )
        with (
            mock.patch.object(macos_launchd, "bootout"),
            mock.patch.object(macos_launchd, "wait_until_unloaded"),
            mock.patch.object(macos_launchd, "bootstrap", side_effect=failure) as bootstrap,
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                restart_launch_agent(plist, bootstrap_attempts=3, retry_delay=0.01)
        bootstrap.assert_called_once()


if __name__ == "__main__":
    unittest.main()
