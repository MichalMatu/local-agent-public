"""Safe fixture coverage for the single external Mac MVP cutover controller."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest import mock

from scripts import macos_mvp_release_controller as cutover


class ExternalMacReleaseControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="la-mvp-external-controller-")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        (self.home / "Library/LaunchAgents").mkdir(parents=True)
        self.root = self.home / cutover.RELEASE_NAME
        self.root.mkdir(mode=0o700)
        self.state = self.home / "state"
        self.state.mkdir()
        self.candidate = self.root / "candidate-7df83dcd"
        self.candidate.mkdir()
        self.baseline = self.root / "baseline-B1c"
        self.baseline.mkdir()
        self.plist = self.home / "com.michal.local-agent.plist"
        self.original = {
            "Label": cutover.LABEL,
            "ProgramArguments": [
                "/opt/test/python", str(self.baseline / "agent_entrypoint.py"),
                "--registry", "/private/registry", "--max-workers", "4",
            ],
            "WorkingDirectory": str(self.baseline),
            "RunAtLoad": True,
            "KeepAlive": True,
            "EnvironmentVariables": {"LOCAL_AGENT_MACOS_ADMISSION_REQUIRED": "1"},
        }
        self.raw = plistlib.dumps(self.original)
        self.plist.write_bytes(self.raw)
        config_backup = self.root / "configuration-backup"
        config_backup.mkdir()
        (config_backup / "com.michal.local-agent.plist").write_bytes(self.raw)
        (self.root / "staging-manifest.json").write_text(json.dumps({
            "status": "staged_not_activated",
            "candidate_sha": cutover.CANDIDATE_SHA,
            "baseline_sha": cutover.BASE_SHA,
            "host_id": cutover.HOST_ID,
            "installation_id": cutover.INSTALL_ID,
            "baseline_plist_sha256": hashlib.sha256(self.raw).hexdigest(),
            "baseline_source_path": str(self.baseline),
        }))
        control = self.root / "host-control"
        control.mkdir()
        (control / ".git").mkdir()
        self.gate = mock.patch.object(
            cutover, "_validate_paths",
            return_value=(self.root, self.state, self.candidate, self.plist),
        )
        self.gate.start()
        self.addCleanup(self.gate.stop)
        self.git = mock.patch.object(cutover, "_git", side_effect=self.git_result)
        self.git.start()
        self.addCleanup(self.git.stop)

    @staticmethod
    def git_result(_path: Path, *argv: str) -> str:
        if argv[0] == "rev-parse":
            return cutover.BASE_SHA if _path.name == "baseline-B1c" else cutover.CANDIDATE_SHA
        if argv[0] == "status":
            return ""
        if argv[0] == "remote":
            return "https://github.com/example-owner/local-agent"
        if argv[0] == "branch":
            return "agent-control"
        if argv[0] == "config":
            return "local-test-value"
        raise AssertionError(argv)

    def prepare(self) -> dict:
        with mock.patch.object(cutover, "_launchctl") as launch:
            result = cutover.prepare(self.home)
        launch.assert_not_called()
        return result

    def test_prepare_preserves_baseline_and_does_not_enable_host_intake(self) -> None:
        result = self.prepare()
        self.assertEqual(result["state"], "prepared")
        self.assertFalse(result["production_service_changed"])
        self.assertEqual(self.plist.read_bytes(), self.raw)
        self.assertFalse((self.state / "supervisor-control.json").exists())
        self.assertEqual(cutover._load_state(self.root)["state"], "prepared")
        proposed = plistlib.loads((self.root / "mvp-candidate.plist").read_bytes())
        self.assertEqual(proposed["ProgramArguments"][1],
                         str(self.candidate / "agent_entrypoint.py"))
        self.assertEqual(proposed["EnvironmentVariables"],
                         self.original["EnvironmentVariables"])
        independent = plistlib.loads((
            self.root / "mvp-watch.plist"
        ).read_bytes())
        self.assertEqual(independent["Label"], cutover.CONTROLLER_LABEL)
        self.assertNotIn(cutover.LABEL, independent["ProgramArguments"])
        self.assertTrue(independent["RunAtLoad"])
        self.assertIn("StartInterval", independent)

    def test_prepare_refuses_journal_reinitialization_without_overwrite(self) -> None:
        self.prepare()
        with self.assertRaisesRegex(cutover.CutoverBlocked, "existing cutover journal"):
            cutover.prepare(self.home)

    def test_prepare_wrong_existing_baseline_fails_before_service_mutation(self) -> None:
        self.plist.write_bytes(b"tampered")
        with self.assertRaisesRegex(cutover.CutoverBlocked, "no longer equals"):
            cutover.prepare(self.home)
        self.assertFalse((self.root / cutover.JOURNAL).exists())

    def test_watch_waits_without_effect_if_any_queue_not_drained(self) -> None:
        self.prepare()
        with (mock.patch.object(cutover, "_queue_idle", return_value=False),
              mock.patch.object(cutover, "_launchctl") as launch):
            outcome = cutover.watch(self.home)
        self.assertEqual(outcome["state"], "prepared")
        self.assertTrue(outcome["waiting_for_safe_drain"])
        launch.assert_not_called()
        self.assertEqual(self.plist.read_bytes(), self.raw)

    def test_watch_succeeds_only_with_fresh_candidate_heartbeat(self) -> None:
        self.prepare()
        (self.state / "status.json").write_text(json.dumps({
            "self_revision": cutover.CANDIDATE_SHA, "state": "idle",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }))
        import os
        fd = os.open(self.state / "fake-freeze-lock", os.O_CREAT | os.O_RDWR, 0o600)
        with (mock.patch.object(cutover, "_queue_idle", return_value=True),
              mock.patch.object(cutover, "_freeze", return_value=fd),
              mock.patch.object(cutover, "_bootout_and_wait") as unload,
              mock.patch.object(cutover, "_bootstrap_with_retry") as bootstrap,
              mock.patch.object(cutover, "_generation",
                                side_effect=["baseline", "candidate"])):
            outcome = cutover.watch(self.home)
        self.assertEqual(outcome["state"], "accepted")
        self.assertEqual(cutover._load_state(self.root)["state"], "accepted")
        self.assertTrue((self.state / "supervisor-control.json").is_file())
        unload.assert_called_once()
        bootstrap.assert_called_once()
        self.assertFalse((self.state / cutover.FROZEN).exists())

    def test_crashed_candidate_does_not_get_second_bootstrap(self) -> None:
        self.prepare()
        journal = cutover._load_state(self.root)
        cutover._save_state(self.root, journal, "switching")
        with mock.patch.object(cutover, "_restore", return_value={}) as recovery:
            self.assertEqual(cutover.watch(self.home)["state"], "rolled_back")
        recovery.assert_called_once()

    def test_failed_candidate_restores_old_plist_and_removes_only_own_config(self) -> None:
        self.prepare()
        config = (self.root / "mvp-host-config.json").read_bytes()
        (self.state / "supervisor-control.json").write_bytes(config)
        self.plist.write_bytes((self.root / "mvp-candidate.plist").read_bytes())
        (self.state / cutover.FROZEN).write_bytes(b'{"state":"frozen"}\n')
        journal = cutover._save_state(self.root, cutover._load_state(self.root),
                                      "switching")
        with (mock.patch.object(cutover, "_generation",
                                side_effect=["candidate", "baseline"]),
              mock.patch.object(cutover, "_bootout_and_wait") as unload,
              mock.patch.object(cutover, "_bootstrap_with_retry") as bootstrap,
              mock.patch.object(cutover, "_wait_healthy_baseline") as heartbeat):
            result = cutover._restore(self.root, self.state, self.plist, journal)
        self.assertEqual(result["state"], "rolled_back")
        self.assertEqual(self.plist.read_bytes(), self.raw)
        self.assertFalse((self.state / "supervisor-control.json").exists())
        unload.assert_called_once()
        bootstrap.assert_called_once()
        heartbeat.assert_called_once()

    def test_changed_host_config_stops_rollback_before_service_change(self) -> None:
        self.prepare()
        config = self.state / "supervisor-control.json"
        config.write_text('{"unexpected":"user change"}')
        with mock.patch.object(cutover, "_launchctl") as launch:
            with self.assertRaisesRegex(cutover.CutoverBlocked, "unexpectedly changed"):
                cutover._restore(self.root, self.state, self.plist,
                                 cutover._load_state(self.root))
        launch.assert_not_called()
        self.assertEqual(config.read_text(), '{"unexpected":"user change"}')
        self.assertEqual(self.plist.read_bytes(), self.raw)

    def test_bootstrap_error_five_is_accepted_if_target_is_registered(self) -> None:
        from types import SimpleNamespace
        with (mock.patch.object(cutover, "_launchctl",
                                return_value=SimpleNamespace(returncode=5)) as launch,
              mock.patch.object(cutover, "_generation", return_value="baseline")):
            cutover._bootstrap_with_retry(
                "gui/501", self.plist, "gui/501/com.michal.local-agent",
                expected="baseline", baseline="old", candidate="new",
            )
        launch.assert_called_once()
        self.assertEqual(launch.call_args.kwargs["acceptable"], (0, 5))

    def test_bootstrap_error_five_retries_after_confirmed_absence(self) -> None:
        from types import SimpleNamespace
        with (mock.patch.object(cutover, "_launchctl",
                                side_effect=[SimpleNamespace(returncode=5),
                                             SimpleNamespace(returncode=0)]) as launch,
              mock.patch.object(cutover, "_generation",
                                side_effect=["absent", "candidate"]),
              mock.patch.object(cutover.time, "monotonic", side_effect=[0, 4, 5]),
              mock.patch.object(cutover.time, "sleep")):
            cutover._bootstrap_with_retry(
                "gui/501", self.plist, "gui/501/com.michal.local-agent",
                expected="candidate", baseline="old", candidate="new",
            )
        self.assertEqual(launch.call_count, 2)

    def test_bootout_waits_until_previous_service_fully_disappears(self) -> None:
        with (mock.patch.object(cutover, "_generation",
                                side_effect=["baseline", "absent"]) as read,
              mock.patch.object(cutover.time, "monotonic", side_effect=[0, 1]),
              mock.patch.object(cutover.time, "sleep") as sleep):
            cutover._wait_until_absent("gui/501/test", "old", "new")
        self.assertEqual(read.call_count, 2)
        sleep.assert_called_once()

    def test_rollback_journal_alone_is_not_live_service_proof(self) -> None:
        self.prepare()
        cutover._save_state(self.root, cutover._load_state(self.root), "rolled_back")
        with mock.patch.object(cutover, "_generation", return_value="absent"):
            with self.assertRaisesRegex(cutover.CutoverBlocked, "not registered"):
                cutover.watch(self.home)

    def test_contention_defers_without_service_mutation(self) -> None:
        self.prepare()
        with (mock.patch.object(cutover, "_queue_idle", return_value=True),
              mock.patch.object(cutover, "_freeze",
                                side_effect=BlockingIOError(35, "busy")),
              mock.patch.object(cutover, "_launchctl") as launch):
            result = cutover.watch(self.home)
        self.assertTrue(result["waiting_for_active_task_lease"])
        self.assertEqual(cutover._load_state(self.root)["state"], "prepared")
        launch.assert_not_called()

    def test_failed_baseline_bootstrap_keeps_freeze_and_retryable_state(self) -> None:
        self.prepare()
        (self.state / "supervisor-control.json").write_bytes(
            (self.root / "mvp-host-config.json").read_bytes(),
        )
        self.plist.write_bytes((self.root / "mvp-candidate.plist").read_bytes())
        (self.state / cutover.FROZEN).write_bytes(b'{"state":"frozen"}\n')
        journal = cutover._save_state(self.root, cutover._load_state(self.root), "switching")
        with (mock.patch.object(cutover, "_generation", return_value="absent"),
              mock.patch.object(cutover, "_bootstrap_with_retry",
                                side_effect=cutover.CutoverBlocked("launchd still absent"))):
            with self.assertRaisesRegex(cutover.CutoverBlocked, "launchd still absent"):
                cutover._restore(self.root, self.state, self.plist, journal)
        self.assertTrue((self.state / cutover.FROZEN).exists())
        self.assertEqual(cutover._load_state(self.root)["state"], "switching")

    def test_registered_but_not_running_baseline_is_not_certified_as_rollback(self) -> None:
        from types import SimpleNamespace
        # A launchd registration by itself is insufficient. This is the
        # previously observed production false-positive journal state.
        with (mock.patch.object(cutover, "_launchctl",
                                return_value=SimpleNamespace(
                                    returncode=0,
                                    stdout="state = waiting\\n" +
                                           str(self.baseline / "agent_entrypoint.py"),
                                )),
              mock.patch.object(cutover.time, "monotonic", side_effect=[0, 1, 36]),
              mock.patch.object(cutover.time, "sleep") as sleep):
            with self.assertRaisesRegex(cutover.CutoverBlocked,
                                        "no stable live supervisor heartbeat"):
                cutover._wait_healthy_baseline(
                    self.state, "gui/501/test",
                    str(self.baseline / "agent_entrypoint.py"),
                    str(self.candidate / "agent_entrypoint.py"),
                )
        sleep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
