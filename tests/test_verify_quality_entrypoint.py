"""Verifier output stays bounded without altering invoked commands."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from scripts import verify


class VerifyPresentationTests(unittest.TestCase):
    def test_short_command_is_printed_in_full(self) -> None:
        self.assertEqual(
            verify._display_command(["python", "-m", "unittest", "-q"]),
            "python -m unittest -q",
        )

    def test_long_compile_command_is_summarized_not_modified(self) -> None:
        command = ["python", "-m", "py_compile", *[f"module_{n:03d}.py" for n in range(80)]]
        shown = verify._display_command(command)
        self.assertIn("additional arguments", shown)
        self.assertLess(len(shown), 240)
        self.assertEqual(command[-1], "module_079.py")

    def test_closed_lease_fd_is_not_inherited_by_source_verifier(self) -> None:
        with mock.patch.dict(os.environ, {"LOCAL_AGENT_LEASE_FDS": "999999"}, clear=True), \
             mock.patch("builtins.print"):
            environment = verify._verification_environment()
        self.assertNotIn("LOCAL_AGENT_LEASE_FDS", environment)

    def test_live_lease_fd_is_preserved_by_source_verifier(self) -> None:
        with tempfile.TemporaryFile() as handle:
            raw = str(handle.fileno())
            with mock.patch.dict(os.environ, {"LOCAL_AGENT_LEASE_FDS": raw},
                                 clear=True):
                environment = verify._verification_environment()
            self.assertEqual(environment["LOCAL_AGENT_LEASE_FDS"], raw)

    def test_valid_lease_fd_is_actually_passed_to_child(self) -> None:
        with tempfile.TemporaryFile() as handle:
            fd = handle.fileno()
            with (
                mock.patch.dict(os.environ, {"LOCAL_AGENT_LEASE_FDS": str(fd)},
                                     clear=True),
                mock.patch.object(verify.subprocess, "run") as run,
                mock.patch("builtins.print"),
            ):
                verify._run("lease test", ["python", "-V"])
            self.assertIn(fd, run.call_args.kwargs["pass_fds"])
            self.assertEqual(run.call_args.kwargs["env"]["LOCAL_AGENT_LEASE_FDS"],
                             str(fd))

    def test_compile_source_inventory_covers_all_host_packages(self) -> None:
        sources = verify._python_sources()
        for package in (
            "platforms/macos/local-agent/",
            "platforms/linux/core/",
            "platforms/linux/local-agent/",
            "platforms/raspberry-pi/local-agent/",
            "platforms/android/local-agent/",
        ):
            self.assertTrue(any(item.startswith(package) for item in sources), package)
        self.assertFalse(any("/tests/" in path for path in sources))

    def test_platform_source_smoke_selects_all_accepted_implementations(self) -> None:
        profiles = verify.PLATFORM_SOURCE_SMOKE_TESTS
        self.assertEqual(len(profiles), 5)
        directories = {item[1] for item in profiles}
        self.assertEqual(directories, {
            "platforms/linux/local-agent/tests",
            "platforms/raspberry-pi/local-agent/tests",
            "platforms/android/local-agent/tests",
        })
        for _, directory, pattern, python_roots in profiles:
            self.assertTrue((verify.ROOT / directory / pattern).is_file())
            self.assertTrue(all((verify.ROOT / relative).is_dir()
                                for relative in python_roots))

    def test_source_smoke_uses_only_scoped_absolute_pythonpath(self) -> None:
        with mock.patch.object(verify, "_run") as run:
            verify.run_platform_source_smoke()
        self.assertEqual(run.call_count, 5)
        for call in run.call_args_list:
            env = call.kwargs["env_overrides"]
            self.assertEqual(set(env), {"PYTHONPATH"})
            for root in env["PYTHONPATH"].split(os.pathsep):
                self.assertTrue(root.startswith(str(verify.ROOT)))

    def test_run_scoped_environment_does_not_modify_parent(self) -> None:
        with (
            mock.patch.dict(os.environ, {"PYTHONPATH": "/untrusted/test-path"}),
            mock.patch.object(verify.subprocess, "run") as run,
            mock.patch("builtins.print"),
        ):
            verify._run("source test", ["python", "-V"],
                        env_overrides={"PYTHONPATH": "/verified/source"})
            self.assertEqual(run.call_args.kwargs["env"]["PYTHONPATH"],
                             "/verified/source")
            self.assertEqual(os.environ["PYTHONPATH"], "/untrusted/test-path")

    def test_execution_receives_every_argument(self) -> None:
        command = ["python", "-m", "py_compile", *[f"file_{n:03d}.py" for n in range(80)]]
        with mock.patch.object(verify.subprocess, "run") as run, \
             mock.patch("builtins.print"):
            verify._run("compile", command)
        self.assertEqual(run.call_args.args[0], command)
        self.assertEqual(run.call_args.kwargs["cwd"], verify.ROOT)
        self.assertTrue(run.call_args.kwargs["check"])


if __name__ == "__main__":
    unittest.main()
