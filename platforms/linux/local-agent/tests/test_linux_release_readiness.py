"""Native, offline readiness of TWO independently runnable exact-SHA releases."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from linux_agent.release_controller import ServiceSwitchError
from linux_agent.release_readiness import _probe_release, inspect_pair, main
from linux_agent.service_plan import ReleasePair


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, timeout=25,
    ).stdout.strip()


class ReleaseReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="linux-readiness-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        seed = self.root / "seed"
        git("init", "-q", str(seed))
        # Commit the actual Linux package (not a stub print-only __main__).
        source = Path(__file__).resolve().parents[1] / "linux_agent"
        dest = seed / "platforms/linux/local-agent/linux_agent"
        dest.mkdir(parents=True)
        for path in source.rglob("*.py"):
            target = dest / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        core_source = Path(__file__).resolve().parents[2] / "core" / "linux_agent"
        core_dest = seed / "platforms/linux/core/linux_agent"
        core_dest.mkdir(parents=True)
        for path in core_source.rglob("*.py"):
            target = core_dest / path.relative_to(core_source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        git("-C", str(seed), "add", ".")
        git("-C", str(seed), "-c", "user.name=Fixture", "-c",
            "user.email=fixture@local.invalid", "commit", "-qm", "baseline")
        baseline = git("-C", str(seed), "rev-parse", "HEAD")
        (dest / "release-marker.txt").write_text("independent candidate\n")
        git("-C", str(seed), "add", ".")
        git("-C", str(seed), "-c", "user.name=Fixture", "-c",
            "user.email=fixture@local.invalid", "commit", "-qm", "candidate")
        candidate = git("-C", str(seed), "rev-parse", "HEAD")
        releases = self.root / "releases"
        releases.mkdir()
        for sha in (baseline, candidate):
            git("clone", "-q", "--local", "--no-hardlinks", str(seed),
                str(releases / sha))
            git("-C", str(releases / sha), "checkout", "-q", "--detach", sha)
        state = self.root / "state"
        state.mkdir(mode=0o700)
        (state / "EMERGENCY_STOP").write_text("disabled\n")
        config = self.root / "private-config.json"
        config.write_text(json.dumps({
            "version": 1, "host_id": "readiness-fixture",
            "installation_id": "123e4567-e89b-12d3-a456-426614174000",
            "repository": str(self.root / "offline-control.git"),
            "branch": "agent-control", "enabled": True,
            "host_intake": {"enabled": True,
                            "authorization": "github_writers"},
        }))
        config.chmod(0o600)
        self.pair = ReleasePair(
            candidate=releases / candidate, candidate_sha=candidate,
            rollback=releases / baseline, rollback_sha=baseline,
            python=Path(sys.executable).resolve(), config=config, state=state,
        )

    def test_actual_two_releases_import_and_stop_status_read_only(self) -> None:
        before = sorted(str(p) for p in self.pair.state.rglob("*"))
        report = inspect_pair(self.pair)
        self.assertEqual(report["status"], "two_runnable_releases_not_installed")
        self.assertEqual(report["candidate"]["sha"], self.pair.candidate_sha)
        self.assertEqual(report["rollback"]["sha"], self.pair.rollback_sha)
        self.assertTrue(report["rollback"]["imports_exact_source"])
        self.assertTrue(report["candidate"]["cli_serve_present"])
        self.assertEqual(report["stop"], "preserved")
        self.assertEqual(report["activation"], "not_performed")
        self.assertEqual(before, sorted(str(p) for p in self.pair.state.rglob("*")))
        self.assertTrue((self.pair.state / "EMERGENCY_STOP").exists())

    def test_absent_stop_refuses_without_running_release(self) -> None:
        (self.pair.state / "EMERGENCY_STOP").unlink()
        with patch("linux_agent.release_readiness._probe_release",
                   side_effect=AssertionError("must not import released code")):
            with self.assertRaisesRegex(ServiceSwitchError, "EMERGENCY_STOP"):
                inspect_pair(self.pair)

    def test_dirty_candidate_fails_before_any_release_execution(self) -> None:
        (self.pair.candidate / "altered").write_text("untracked")
        with patch("linux_agent.release_readiness._probe_release",
                   side_effect=AssertionError("must not run unclean release")):
            with self.assertRaisesRegex(ServiceSwitchError, "modified or untracked"):
                inspect_pair(self.pair)

    def test_dirty_rollback_fails_before_candidate_execution(self) -> None:
        (self.pair.rollback / "altered").write_text("untracked")
        with patch("linux_agent.release_readiness._probe_release",
                   side_effect=AssertionError("must not run candidate yet")):
            with self.assertRaisesRegex(ServiceSwitchError, "modified or untracked"):
                inspect_pair(self.pair)

    def test_different_real_host_config_refused_before_probe(self) -> None:
        config = json.loads(self.pair.config.read_text())
        config["branch"] = "main"
        self.pair.config.write_text(json.dumps(config))
        with patch("linux_agent.release_readiness._probe_release",
                   side_effect=AssertionError("must not probe")):
            with self.assertRaisesRegex(ValueError, "branch mismatch"):
                inspect_pair(self.pair)

    def test_import_path_mismatch_refused(self) -> None:
        with patch("linux_agent.release_readiness._run_probe",
                   return_value=json.dumps({"package": "/elsewhere/linux_agent/__init__.py",
                                            "entrypoint": "/elsewhere/linux_agent/__main__.py"})):
            with self.assertRaisesRegex(ServiceSwitchError, "exact source root"):
                _probe_release(self.pair, self.pair.candidate,
                               self.pair.candidate_sha)

    def test_cli_entrypoint_missing_serve_refused(self) -> None:
        expected = self.pair.candidate / "platforms/linux/local-agent/linux_agent"
        valid_import = json.dumps({
            "package": str(expected / "__init__.py"),
            "entrypoint": str(self.pair.candidate / "platforms/linux/core/linux_agent/__main__.py"),
        })
        with patch("linux_agent.release_readiness._run_probe",
                   side_effect=[valid_import, "usage: status only"]):
            with self.assertRaisesRegex(ServiceSwitchError, "serve/status"):
                _probe_release(self.pair, self.pair.candidate,
                               self.pair.candidate_sha)

    def test_runtime_status_not_stopped_refused(self) -> None:
        expected = self.pair.candidate / "platforms/linux/local-agent/linux_agent"
        valid_import = json.dumps({
            "package": str(expected / "__init__.py"),
            "entrypoint": str(self.pair.candidate / "platforms/linux/core/linux_agent/__main__.py"),
        })
        with patch("linux_agent.release_readiness._run_probe",
                   side_effect=[valid_import, "serve status",
                                '{"status":"enabled","claimed":0,"spooled":0}']):
            with self.assertRaisesRegex(ServiceSwitchError, "EMERGENCY_STOP"):
                _probe_release(self.pair, self.pair.candidate,
                               self.pair.candidate_sha)

    def test_probe_failure_cannot_mark_release_runnable(self) -> None:
        with patch("linux_agent.release_readiness.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 1, "", "private")):
            with self.assertRaisesRegex(ServiceSwitchError, "probe failed"):
                _probe_release(self.pair, self.pair.candidate,
                               self.pair.candidate_sha)

    def test_cli_error_does_not_report_readiness(self) -> None:
        with patch("linux_agent.release_readiness.inspect_pair",
                   side_effect=ServiceSwitchError("injected")):
            argv = [
                "--candidate-root", str(self.pair.candidate),
                "--candidate-sha", self.pair.candidate_sha,
                "--rollback-root", str(self.pair.rollback),
                "--rollback-sha", self.pair.rollback_sha,
                "--config", str(self.pair.config),
                "--state", str(self.pair.state),
            ]
            import io
            from contextlib import redirect_stderr
            captured = io.StringIO()
            with redirect_stderr(captured):
                self.assertEqual(main(argv), 2)
            self.assertIn("REFUSED", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
