"""Independent fixed/adaptive polling rollout preserving the live Mac Git inbox."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import ExitStack
import fcntl
import hashlib
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest import mock

from scripts import macos_cli_polling_rollout as cutover


class ExternalMacReleaseControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        fence = mock.patch.object(cutover, "_execution_fence", return_value=ExitStack())
        fence.start()
        self.addCleanup(fence.stop)
        tmp = tempfile.TemporaryDirectory(prefix="la-mvp-external-controller-")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        (self.home / "Library/LaunchAgents").mkdir(parents=True)
        self.root = self.home / cutover.RELEASE_NAME
        self.root.mkdir(mode=0o700, parents=True)
        self.state = self.home / "state"
        self.state.mkdir()
        self.candidate = self.root / "candidate-cli"
        self.candidate.mkdir()
        self.baseline = self.root / "baseline-host-mvp"
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
        self.live_config = {
            "version": 1, "enabled": True, "host_id": cutover.HOST_ID,
            "control_checkout": str(control),
            "host_intake": {"enabled": True, "authorization": "github_writers"},
        }
        self.config_data = (json.dumps(self.live_config, sort_keys=True) + "\n").encode()
        (config_backup / "supervisor-control.json").write_bytes(self.config_data)
        (self.state / "supervisor-control.json").write_bytes(self.config_data)
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
            return cutover.BASE_SHA if _path.name == "baseline-host-mvp" else cutover.CANDIDATE_SHA
        if argv[0] == "status":
            return ""
        if argv[0] == "remote":
            return "https://github.com/MichalMatu/local-agent-public"
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
        self.assertEqual((self.state / "supervisor-control.json").read_bytes(),
                         self.config_data)
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

    def test_sidecar_python_release_refuses_tamper_before_plist_mutation(self) -> None:
        manifest = self.root / "staging-manifest.json"
        data = json.loads(manifest.read_text())
        data["candidate_python"] = str(self.root / "candidate-runtime/bin/python3")
        manifest.write_text(json.dumps(data))
        with self.assertRaises(cutover.CutoverBlocked):
            self.prepare()
        self.assertEqual(self.plist.read_bytes(), self.raw)
        self.assertFalse((self.root / cutover.JOURNAL).exists())

    def test_sidecar_python_is_written_to_new_plist_only(self) -> None:
        manifest = self.root / "staging-manifest.json"
        data = json.loads(manifest.read_text())
        runtime = self.root / "candidate-runtime"
        runtime.mkdir()
        (runtime / "bin").mkdir()
        (runtime / "bin/python3").write_text("mock interpreter")
        (runtime / "bin/python3").chmod(0o700)
        config = runtime / "pyvenv.cfg"
        config.write_text("home = /opt/test")
        lock = self.root / "candidate-cli/requirements-runtime.lock"
        lock.write_text("locked dependency fixture")
        target = str(runtime / "bin/python3")
        data["candidate_python"] = target
        manifest.write_text(json.dumps(data))
        record = {
            "version": 1, "candidate_python": target,
            "baseline_python": self.original["ProgramArguments"][0],
            "requirements_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
            "pyvenv_cfg_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        }
        (self.root / "candidate-python.json").write_text(json.dumps(record))
        with mock.patch.object(cutover.subprocess, "run",
                               return_value=mock.Mock(returncode=0)):
            self.prepare()
        proposal = plistlib.loads((self.root / "mvp-candidate.plist").read_bytes())
        self.assertEqual(proposal["ProgramArguments"][0], target)
        self.assertEqual(plistlib.loads(self.plist.read_bytes()), self.original)
        lock.write_text("tampered dependency closure")
        with self.assertRaisesRegex(cutover.CutoverBlocked, "Python release identity"):
            cutover._validated_candidate_python(self.root, data, record["baseline_python"])

    def test_explicit_arm_survives_next_login_without_prepare_autoarming(self) -> None:
        self.prepare()
        installed = self.home / "Library/LaunchAgents" / (
            cutover.CONTROLLER_LABEL + ".plist"
        )
        self.assertFalse(installed.exists(), "prepare must remain inert")
        with mock.patch.object(cutover, "_launchctl") as launch:
            response = cutover.arm(self.home)
        self.assertTrue(response["armed"])
        self.assertEqual(installed.read_bytes(),
                         (self.root / "mvp-watch.plist").read_bytes())
        self.assertIn("bootstrap", [part for call in launch.call_args_list
                                    for part in call.args])
        self.assertIn(str(installed), [part for call in launch.call_args_list
                                       for part in call.args])
        self.assertEqual(cutover._load_state(self.root)["state"], "prepared")
        self.assertEqual(self.plist.read_bytes(), self.raw)

    def test_drain_fences_new_turns_without_interrupting_existing_admission(self) -> None:
        from local_agent.supervisor.macos_release_admission import (
            MacReleaseAdmissionBlocked, macos_task_admission,
        )
        self.prepare()
        config = self.state / "macos-release-admission.json"
        config.write_text('{"version":1,"enabled":true}\n')
        config.chmod(0o600)
        with mock.patch("sys.platform", "darwin"):
            with macos_task_admission(self.state):
                with mock.patch.object(cutover, "_queue_idle", return_value=False):
                    self.assertTrue(cutover.watch(self.home)["waiting_for_safe_drain"])
                    self.assertTrue(cutover.watch(self.home)["waiting_for_safe_drain"])
                self.assertEqual(cutover._json(self.state / cutover.FROZEN),
                                 cutover._drain_record())
                with self.assertRaises(MacReleaseAdmissionBlocked):
                    with macos_task_admission(self.state):
                        self.fail("new turn admitted during release drain")
                with self.assertRaises(BlockingIOError):
                    cutover._freeze(self.state)
            fd = cutover._freeze(self.state)
            import os
            os.close(fd)
        self.assertEqual(self.plist.read_bytes(), self.raw)

    def test_drain_never_overwrites_foreign_freeze(self) -> None:
        self.prepare()
        marker = self.state / cutover.FROZEN
        marker.write_text('{"state":"frozen","owner":"someone-else"}\n')
        marker.chmod(0o600)
        original = marker.read_bytes()
        with self.assertRaises(cutover.CutoverBlocked):
            cutover.watch(self.home)
        self.assertEqual(marker.read_bytes(), original)
        self.assertEqual(self.plist.read_bytes(), self.raw)

    def test_arm_refuses_existing_user_launchagent_without_overwrite(self) -> None:
        self.prepare()
        installed = self.home / "Library/LaunchAgents" / (
            cutover.CONTROLLER_LABEL + ".plist"
        )
        installed.write_text("owned user data")
        with mock.patch.object(cutover, "_launchctl") as launch:
            with self.assertRaises(cutover.CutoverBlocked):
                cutover.arm(self.home)
        launch.assert_not_called()
        self.assertEqual(installed.read_text(), "owned user data")

    def test_retire_refuses_unaccepted_release_and_keeps_reboot_registration(self) -> None:
        self.prepare()
        installed = self.home / "Library/LaunchAgents" / (
            cutover.CONTROLLER_LABEL + ".plist"
        )
        with mock.patch.object(cutover, "_launchctl"):
            cutover.arm(self.home)
        with mock.patch.object(cutover, "_launchctl") as launch:
            with self.assertRaises(cutover.CutoverBlocked):
                cutover.retire(self.home)
        self.assertTrue(installed.is_file())
        launch.assert_not_called()
        self.assertEqual(self.plist.read_bytes(), self.raw)

    def test_retire_removes_only_own_accepted_persistent_watchdog(self) -> None:
        from types import SimpleNamespace

        self.prepare()
        installed = self.home / "Library/LaunchAgents" / (
            cutover.CONTROLLER_LABEL + ".plist"
        )
        with mock.patch.object(cutover, "_launchctl"):
            cutover.arm(self.home)
        self.assertTrue(installed.is_file())
        cutover._save_state(self.root, cutover._load_state(self.root),
                            "accepted")
        (self.state / "status.json").write_text(json.dumps({
            "self_revision": cutover.CANDIDATE_SHA, "state": "idle",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }))
        with (
            mock.patch.object(cutover, "_generation", return_value="candidate"),
            mock.patch.object(cutover, "_launchctl", side_effect=[
                SimpleNamespace(returncode=0, stdout="state = running\\npid = 501\\n"),
                SimpleNamespace(returncode=0, stdout=str(self.root / "mvp-controller.py")),
                SimpleNamespace(returncode=0, stdout=""),
                SimpleNamespace(returncode=113, stdout=""),
            ]) as launch,
        ):
            response = cutover.retire(self.home)
        self.assertEqual(response["state"], "accepted")
        self.assertTrue(response["retired"])
        self.assertFalse(installed.exists())
        self.assertEqual(self.plist.read_bytes(), self.raw)
        self.assertEqual(launch.call_count, 4)
        self.assertIn("bootout", launch.call_args_list[2].args)

    def test_retire_refuses_stale_accepted_service_heartbeat(self) -> None:
        from types import SimpleNamespace

        self.prepare()
        installed = self.home / "Library/LaunchAgents" / (
            cutover.CONTROLLER_LABEL + ".plist"
        )
        with mock.patch.object(cutover, "_launchctl"):
            cutover.arm(self.home)
        cutover._save_state(self.root, cutover._load_state(self.root),
                            "accepted")
        (self.state / "status.json").write_text(json.dumps({
            "self_revision": cutover.CANDIDATE_SHA, "state": "idle",
            "updated_at": "2020-01-01T00:00:00+00:00",
        }))
        with (
            mock.patch.object(cutover, "_generation", return_value="candidate"),
            mock.patch.object(cutover, "_launchctl", return_value=SimpleNamespace(
                returncode=0, stdout="state = running\\npid = 501\\n",
            )) as launch,
        ):
            with self.assertRaisesRegex(cutover.CutoverBlocked, "healthy current heartbeat"):
                cutover.retire(self.home)
        self.assertTrue(installed.is_file())
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(self.plist.read_bytes(), self.raw)

    def test_retire_refuses_changed_watcher_without_unloading_other_job(self) -> None:
        self.prepare()
        installed = self.home / "Library/LaunchAgents" / (
            cutover.CONTROLLER_LABEL + ".plist"
        )
        with mock.patch.object(cutover, "_launchctl"):
            cutover.arm(self.home)
        installed.write_bytes(b"other user's job")
        cutover._save_state(self.root, cutover._load_state(self.root),
                            "accepted")
        with mock.patch.object(cutover, "_launchctl") as launch:
            with self.assertRaises(cutover.CutoverBlocked):
                cutover.retire(self.home)
        self.assertEqual(installed.read_bytes(), b"other user's job")
        launch.assert_not_called()

    def test_prepare_refuses_journal_reinitialization_without_overwrite(self) -> None:
        self.prepare()
        with self.assertRaisesRegex(cutover.CutoverBlocked, "existing cutover journal"):
            cutover.prepare(self.home)

    def test_prepare_wrong_existing_baseline_fails_before_service_mutation(self) -> None:
        self.plist.write_bytes(b"tampered")
        with self.assertRaisesRegex(cutover.CutoverBlocked, "no longer equals"):
            cutover.prepare(self.home)
        self.assertFalse((self.root / cutover.JOURNAL).exists())

    def test_release_drain_accepts_five_enabled_of_nine_registrations(self) -> None:
        identities = ("example-project", "example-analytics", "example-hub", "example-tracker",
                      "example-device", "example-map", "example-calls",
                      "example-hardware", "local-agent")
        active = {"example-project", "example-device", "example-hub", "example-map",
                  "local-agent"}
        (self.state / "status.json").write_text(json.dumps({
            "state": "idle", "self_revision": cutover.BASE_SHA,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }))
        (self.state / "repositories.json").write_text(json.dumps({
            "version": 1, "repositories": [
                {"id": name, "enabled": name in active} for name in identities
            ],
        }))
        for name in identities:
            ledger = self.state / "repositories" / name
            for leaf in ("claims", "result-spool"):
                (ledger / leaf).mkdir(parents=True)
        for leaf in ("claims", "result-spool"):
            (self.state / leaf).mkdir()
        self.assertTrue(cutover._queue_idle(self.state))
        # Publication is durable and does not imply a live executor. The
        # actual exclusive admission lease is still required by watch().
        pending = (self.state / "repositories" / "example-hardware" /
                   "result-spool" / "unpublished.json")
        pending.write_text("{}")
        self.assertTrue(cutover._queue_idle(self.state))
        pending.unlink()
        self.assertTrue(cutover._queue_idle(self.state))
        status = json.loads((self.state / "status.json").read_text())
        (self.state / "status.json").write_text(json.dumps({**status, "active_repository_ids": ["example-project"]}))
        self.assertFalse(cutover._queue_idle(self.state))
        (self.state / "status.json").write_text(json.dumps(status))

    def test_release_drain_allows_host_only_and_more_than_64_projects(self) -> None:
        self.test_release_drain_accepts_five_enabled_of_nine_registrations()
        registry = self.state / "repositories.json"
        payload = json.loads(registry.read_text())
        payload["repositories"] = []
        registry.write_text(json.dumps(payload))
        self.assertTrue(cutover._queue_idle(self.state))

        ids = [f"repo-{index}" for index in range(70)]
        payload["repositories"] = [
            {"id": ident, "enabled": index % 2 == 0}
            for index, ident in enumerate(ids)
        ]
        for ident in ids:
            for leaf in ("claims", "result-spool"):
                (self.state / "repositories" / ident / leaf).mkdir(parents=True)
        registry.write_text(json.dumps(payload))
        self.assertTrue(cutover._queue_idle(self.state))
        # Inactive publication stays durable without masquerading as execution.
        pending = self.state / "repositories/repo-69/result-spool/waiting.json"
        pending.write_text("{}", encoding="utf-8")
        self.assertTrue(cutover._queue_idle(self.state))
        pending.unlink()
        self.assertTrue(cutover._queue_idle(self.state))

    def test_release_drain_rejects_path_traversal_and_case_aliases(self) -> None:
        self.test_release_drain_accepts_five_enabled_of_nine_registrations()
        registry = self.state / "repositories.json"
        original = json.loads(registry.read_text())
        for ident in ("..", ".", "../other", "unsafe/path", "other\\\\path"):
            with self.subTest(ident=ident):
                altered = {**original, "repositories": [
                    {**original["repositories"][0], "id": ident},
                    *original["repositories"][1:],
                ]}
                registry.write_text(json.dumps(altered))
                with self.assertRaisesRegex(cutover.CutoverBlocked, "ambiguous"):
                    cutover._queue_idle(self.state)
        duplicate = {**original, "repositories": [
            *original["repositories"],
            {"id": "EXAMPLE-PROJECT", "enabled": False},
        ]}
        registry.write_text(json.dumps(duplicate))
        with self.assertRaisesRegex(cutover.CutoverBlocked, "ambiguous"):
            cutover._queue_idle(self.state)

    def test_release_drain_rejects_ambiguous_disabled_flags(self) -> None:
        self.test_release_drain_accepts_five_enabled_of_nine_registrations()
        path = self.state / "repositories.json"
        registry = json.loads(path.read_text())
        registry["repositories"][-2]["enabled"] = "false"
        path.write_text(json.dumps(registry))
        with self.assertRaisesRegex(cutover.CutoverBlocked, "ambiguous"):
            cutover._queue_idle(self.state)

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
        self.assertEqual((self.state / "supervisor-control.json").read_bytes(),
                         self.config_data)
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

    def test_failed_candidate_restores_old_plist_and_preserves_host_config(self) -> None:
        self.prepare()
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
        self.assertEqual((self.state / "supervisor-control.json").read_bytes(),
                         self.config_data)
        unload.assert_called_once()
        bootstrap.assert_called_once()
        heartbeat.assert_called_once()

    def test_changed_host_config_stops_rollback_before_service_change(self) -> None:
        self.prepare()
        config = self.state / "supervisor-control.json"
        config.write_text('{"unexpected":"user change"}')
        with mock.patch.object(cutover, "_launchctl") as launch:
            with self.assertRaisesRegex(cutover.CutoverBlocked, "host intake changed"):
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


class StagedSourceOriginTests(unittest.TestCase):
    def test_release_path_uses_private_full_sha_namespace(self) -> None:
        self.assertEqual(
            Path(cutover.RELEASE_NAME),
            Path("Library/Application Support/local-agent/releases") / cutover.CANDIDATE_SHA,
        )

    def test_validation_rejects_local_origin_for_candidate_or_rollback(self) -> None:
        for rejected in ("candidate-cli", "baseline-host-mvp", None):
            with self.subTest(rejected=rejected), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                root = home / cutover.RELEASE_NAME
                root.mkdir(mode=0o700, parents=True)
                state = home / "Library/Application Support/local-agent"
                state.mkdir(parents=True, exist_ok=True)
                (state / "host-identity.json").write_text(json.dumps({
                    "host_id": cutover.HOST_ID, "installation_id": cutover.INSTALL_ID,
                }))
                (root / "staging-manifest.json").write_text(json.dumps({
                    "status": "staged_not_activated", "baseline_sha": cutover.BASE_SHA,
                    "candidate_sha": cutover.CANDIDATE_SHA, "host_id": cutover.HOST_ID,
                    "installation_id": cutover.INSTALL_ID,
                }))
                for leaf in ("candidate-cli", "baseline-host-mvp"):
                    (root / leaf).mkdir()
                    (root / leaf / "agent_entrypoint.py").touch()

                def git(path, *args):
                    if args[0] == "remote" and path.name == rejected:
                        return "/private/local-clone"
                    return ExternalMacReleaseControllerTests.git_result(path, *args)

                with mock.patch.object(cutover.sys, "platform", "darwin"), \
                        mock.patch.object(cutover, "_git", side_effect=git):
                    if rejected:
                        with self.assertRaisesRegex(cutover.CutoverBlocked, "canonical GitHub"):
                            cutover._validate_paths(home)
                    else:
                        self.assertEqual(cutover._validate_paths(home)[0], root)


class ReleaseExecutionFenceTests(unittest.TestCase):
    def test_project_free_host_descendant_shared_machine_lease_blocks_cutover(self):
        from scripts import macos_host_service as service

        with tempfile.TemporaryDirectory(prefix="release-host-descendant-fence-") as tmp:
            home = Path(tmp)
            state = home / "Library/Application Support/local-agent"
            directory = state / "locks/machine-resources"
            directory.mkdir(parents=True)
            path = directory / "machine.lock"
            path.touch(mode=0o600)
            with path.open("r+") as descendant:
                fcntl.flock(descendant, fcntl.LOCK_SH | fcntl.LOCK_NB)
                with mock.patch.object(service, "_run", return_value="[]"):
                    with self.assertRaises(BlockingIOError):
                        cutover._execution_fence(home, state, home / "candidate")

    def test_descendant_execution_lease_blocks_cutover_even_without_active_worker(self):
        from scripts import macos_host_service as service

        with tempfile.TemporaryDirectory(prefix="release-descendant-fence-") as tmp:
            home = Path(tmp)
            state = home / "state"
            directory = state / "locks/repositories"
            directory.mkdir(parents=True)
            path = directory / ("a" * 64 + ".lock")
            path.write_text("historical PID is not ownership")
            path.chmod(0o600)
            with path.open("r+") as descendant:
                fcntl.flock(descendant, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with mock.patch.object(service, "_registered_leases", return_value=[path]):
                    with self.assertRaises(BlockingIOError):
                        cutover._execution_fence(home, state, home / "candidate")
            self.assertEqual(path.read_text(), "historical PID is not ownership")
            with mock.patch.object(service, "_registered_leases", return_value=[path]):
                fence = cutover._execution_fence(home, state, home / "candidate")
                with path.open("r+") as contender:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fence.close()
            with path.open("r+") as contender:
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)


if __name__ == "__main__":
    unittest.main()
