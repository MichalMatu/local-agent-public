"""Independent macOS operator session recovery; all launchd calls mocked."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest import mock

from scripts import macos_repository_maintenance as ctrl


class MacRepositoryMaintenanceTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(prefix="local-agent-maintenance-")
        self.addCleanup(t.cleanup)
        self.home = Path(t.name)
        self.root = self.home / "session"
        self.root.mkdir()
        self.state = self.home / "state"
        self.state.mkdir()
        self.plist = self.home / "service.plist"
        self.plist.write_bytes(b"fixture")
        self.source = self.home / "trusted-source"
        self.source.mkdir()
        self.record = {"state": "prepared", "source_sha": "a" * 40,
                       "session_id": "session-" + "a" * 24}
        (self.root / "session.lock").touch()
        original = b'{"version":1,"repositories":[]}\n'
        (self.root / "registry-backup.json").write_bytes(original)
        (self.state / "repositories.json").write_bytes(original)

    def _watch(self, state):
        self.record["state"] = state
        with (mock.patch.object(ctrl, "_verify_session",
                                return_value=(self.record, self.source, self.state,
                                              self.plist, self.home)),
              mock.patch.object(ctrl, "_retire") as retire,
              mock.patch.object(ctrl, "_rollback") as rollback,
              mock.patch.object(ctrl, "_generation", return_value="running")):
            ctrl.watch(self.root)
            return retire, rollback

    def test_owner_lease_blocks_watch_without_any_recovery(self):
        handle = ctrl._lock(self.root / "session.lock")
        try:
            with (mock.patch.object(ctrl, "_verify_session",
                                    return_value=(self.record, self.source, self.state,
                                                  self.plist, self.home)),
                  mock.patch.object(ctrl, "_retire") as retire,
                  mock.patch.object(ctrl, "_rollback") as rollback):
                ctrl.watch(self.root)
                retire.assert_not_called()
                rollback.assert_not_called()
        finally:
            ctrl._release(handle)

    def test_crashed_offline_session_invokes_independent_rollback(self):
        for state in ("switching", "offline", "restarting"):
            with self.subTest(state=state):
                retire, rollback = self._watch(state)
                rollback.assert_called_once()
                retire.assert_called_once()

    def test_prepared_crash_does_not_stop_production(self):
        retire, rollback = self._watch("prepared")
        rollback.assert_not_called()
        retire.assert_called_once()
        self.assertEqual(json.loads((self.root / ctrl.JOURNAL).read_text())["state"],
                         "rolled_back")

    def test_prepared_with_crashed_freeze_recovers_without_stopping_a_worker(self):
        (self.state / ctrl.FROZEN).write_bytes(ctrl._owned_freeze(self.record))
        retire, rollback = self._watch("prepared")
        rollback.assert_not_called()
        retire.assert_called_once()
        self.assertFalse((self.state / ctrl.FROZEN).exists())

    def test_crashed_drain_with_active_workers_only_thaws_intake(self):
        (self.state / ctrl.FROZEN).write_bytes(ctrl._owned_freeze(self.record))
        retire, rollback = self._watch("draining")
        rollback.assert_not_called()
        retire.assert_called_once()
        self.assertFalse((self.state / ctrl.FROZEN).exists())

    def test_accepted_after_freeze_crash_thaws_only_healthy_source(self):
        marker = self.state / ctrl.FROZEN
        marker.write_bytes(ctrl._owned_freeze(self.record))
        self.record["state"] = "accepted"
        with (mock.patch.object(ctrl, "_verify_session",
                                return_value=(self.record, self.source, self.state,
                                              self.plist, self.home)),
              mock.patch.object(ctrl, "_resume") as resume,
              mock.patch.object(ctrl, "_rollback") as rollback,
              mock.patch.object(ctrl, "_retire") as retire):
            ctrl.watch(self.root)
            resume.assert_called_once()
            rollback.assert_not_called()
            retire.assert_called_once()
        self.assertFalse(marker.exists())

    def test_accepted_unhealthy_source_reverts_registry(self):
        marker = self.state / ctrl.FROZEN
        marker.write_bytes(ctrl._owned_freeze(self.record))
        self.record["state"] = "accepted"
        with (mock.patch.object(ctrl, "_verify_session",
                                return_value=(self.record, self.source, self.state,
                                              self.plist, self.home)),
              mock.patch.object(ctrl, "_resume",
                                side_effect=ctrl.MaintenanceBlocked("unhealthy")),
              mock.patch.object(ctrl, "_rollback") as rollback,
              mock.patch.object(ctrl, "_retire") as retire):
            ctrl.watch(self.root)
            rollback.assert_called_once()
            retire.assert_called_once()

    def test_foreign_freeze_is_never_deleted(self):
        marker = self.state / ctrl.FROZEN
        marker.write_text("not ours")
        self.record["state"] = "accepted"
        with (mock.patch.object(ctrl, "_verify_session",
                                return_value=(self.record, self.source, self.state,
                                              self.plist, self.home)),
              mock.patch.object(ctrl, "_rollback") as rollback):
            with self.assertRaisesRegex(ctrl.MaintenanceBlocked, "untrusted"):
                ctrl.watch(self.root)
            rollback.assert_not_called()
        self.assertEqual(marker.read_text(), "not ours")

    def test_verified_launchd_transition_is_not_a_foreign_generation(self):
        source_path = str(self.source / "agent_entrypoint.py")
        for state in ("waiting", "exited", "stopping", "xpcproxy"):
            with self.subTest(state=state), mock.patch.object(
                ctrl, "_launch", return_value=mock.Mock(
                    returncode=0, stdout=f"state = {state}\narguments = {source_path}\n",
                ),
            ):
                self.assertEqual(ctrl._generation(self.source), "stopping")
        with mock.patch.object(ctrl, "_launch", return_value=mock.Mock(
            returncode=0, stdout=f"state = running\narguments = {source_path}\n",
        )):
            self.assertEqual(ctrl._generation(self.source), "running")
        with mock.patch.object(ctrl, "_launch", return_value=mock.Mock(
            returncode=0,
            stdout="state = waiting\narguments = /other/agent_entrypoint.py\n",
        )):
            with self.assertRaisesRegex(ctrl.MaintenanceBlocked, "unexpected live"):
                ctrl._generation(self.source)

    def test_wait_absent_accepts_same_generation_stopping_then_absent(self):
        source_path = str(self.source / "agent_entrypoint.py")
        with (mock.patch.object(ctrl, "_launch", side_effect=[
                mock.Mock(returncode=0, stdout=(
                    "state = waiting\narguments = " + source_path)),
                mock.Mock(returncode=0, stdout=(
                    "state = stopping\narguments = " + source_path)),
                mock.Mock(returncode=113, stdout=""),
            ]) as launch,
              mock.patch.object(ctrl.time, "sleep")):
            ctrl._wait_absent(self.source)
            self.assertEqual(launch.call_count, 3)

    def test_rollback_after_owner_bootout_in_flight_preserves_exact_bytes(self):
        original = b'{"version":1,"repositories":[{"id":"safe","enabled":true}]}\n'
        changed = b'{"version":1,"repositories":[{"id":"safe","enabled":false}]}\n'
        (self.root / "registry-backup.json").write_bytes(original)
        registry = self.state / "repositories.json"
        registry.write_bytes(changed)
        (self.state / ctrl.FROZEN).write_bytes(ctrl._owned_freeze(self.record))
        self.record["state"] = "switching"
        source_path = str(self.source / "agent_entrypoint.py")
        with (mock.patch.object(ctrl, "_launch", side_effect=[
                mock.Mock(returncode=0, stdout=(
                    "state = waiting\narguments = " + source_path)),
                mock.Mock(returncode=0, stdout=(
                    "state = waiting\narguments = " + source_path)),
                mock.Mock(returncode=113, stdout=""),
            ]) as launch,
              mock.patch.object(ctrl.time, "sleep"),
              mock.patch.object(ctrl, "_resume") as resume):
            ctrl._rollback(self.root, self.record, self.source, self.state, self.plist)
            self.assertEqual(launch.call_count, 3)
            self.assertTrue(all(c.args[0] == "print" for c in launch.call_args_list))
            resume.assert_called_once()
        self.assertEqual(registry.read_bytes(), original)
        self.assertFalse((self.state / ctrl.FROZEN).exists())
        self.assertEqual(json.loads((self.root / ctrl.JOURNAL).read_text())["state"],
                         "rolled_back")

    def test_resume_waits_for_old_generation_to_disappear_before_bootstrap(self):
        source_path = str(self.source / "agent_entrypoint.py")
        calls = [mock.Mock(returncode=0, stdout=(
                    "state = waiting\narguments = " + source_path)),
                 mock.Mock(returncode=0, stdout=(
                    "state = waiting\narguments = " + source_path)),
                 mock.Mock(returncode=113, stdout=""),
                 mock.Mock(returncode=0),
                 mock.Mock(returncode=0, stdout=(
                    "state = running\narguments = " + source_path))]
        def state(_path):
            return {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "self_revision": "a" * 40, "state": "idle",
            }
        with (mock.patch.object(ctrl, "_launch", side_effect=calls) as launch,
              mock.patch.object(ctrl, "_json", side_effect=state),
              mock.patch.object(ctrl.time, "sleep")):
            ctrl._resume(self.source, self.plist, "a" * 40, self.state)
            self.assertEqual(launch.call_count, 5)
            self.assertEqual(launch.call_args_list[3].args[0], "bootstrap")

    def test_restart_cannot_pass_on_stale_previous_process_heartbeat(self):
        old_stamp = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        snapshot = {"updated_at": old_stamp, "self_revision": "a" * 40,
                    "state": "running"}
        with (mock.patch.object(ctrl, "_generation",
                                side_effect=["absent", "running", "running"]),
              mock.patch.object(ctrl, "_launch",
                                return_value=mock.Mock(returncode=0)),
              mock.patch.object(ctrl, "_json", return_value=snapshot),
              mock.patch.object(ctrl.time, "monotonic", side_effect=[0, 1, 90]),
              mock.patch.object(ctrl.time, "sleep")):
            with self.assertRaisesRegex(ctrl.MaintenanceBlocked, "no healthy exact-source"):
                ctrl._resume(self.source, self.plist, "a" * 40, self.state)

    def test_recovery_restores_exact_original_registry_bytes_without_worktree_edits(self):
        old = b'{"version":1,"repositories":[] }\n'
        (self.root / "registry-backup.json").write_bytes(old)
        registry = self.state / "repositories.json"
        registry.write_bytes(b'{"version":1,"repositories":[{"id":"changed"}]}\n')
        workspace = self.home / "owner-data.txt"
        workspace.write_text("PRESERVE")
        self.record["state"] = "offline"
        marker = self.state / ctrl.FROZEN
        marker.write_bytes(ctrl._owned_freeze(self.record))
        with (mock.patch.object(ctrl, "_generation", return_value="absent"),
              mock.patch.object(ctrl, "_resume") as restart):
            ctrl._rollback(self.root, self.record, self.source, self.state, self.plist)
            restart.assert_called_once()
        self.assertEqual(registry.read_bytes(), old)
        self.assertEqual(workspace.read_text(), "PRESERVE")
        self.assertEqual(json.loads((self.root / ctrl.JOURNAL).read_text())["state"],
                         "rolled_back")
        self.assertFalse(marker.exists())

    def test_watcher_retirement_self_bootout_removes_exact_owned_plist(self):
        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        contents = b"verified-watcher"
        (self.root / "watch.plist").write_bytes(contents)
        target = agents / (ctrl.WATCH_LABEL + ctrl.WATCH_SUFFIX)
        target.write_bytes(contents)
        with mock.patch.object(ctrl, "_launch", side_effect=[
            mock.Mock(returncode=0, stdout=str(self.root / "controller.py")),
            mock.Mock(returncode=0, stdout=""),
        ]) as launch:
            ctrl._retire(self.root, self.home)
            self.assertEqual(launch.call_count, 2)
        self.assertFalse(target.exists())

    def test_foreign_registered_watchdog_cannot_be_retired(self):
        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        contents = b"verified-watcher"
        (self.root / "watch.plist").write_bytes(contents)
        target = agents / (ctrl.WATCH_LABEL + ctrl.WATCH_SUFFIX)
        target.write_bytes(contents)
        with mock.patch.object(ctrl, "_launch",
                               return_value=mock.Mock(returncode=0, stdout="different-script")) as launch:
            with self.assertRaisesRegex(ctrl.MaintenanceBlocked, "not this session"):
                ctrl._retire(self.root, self.home)
            launch.assert_called_once()
        self.assertEqual(target.read_bytes(), contents)

    def test_failed_watch_bootout_restores_owned_plist(self):
        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        contents = b"verified-watcher"
        (self.root / "watch.plist").write_bytes(contents)
        target = agents / (ctrl.WATCH_LABEL + ctrl.WATCH_SUFFIX)
        target.write_bytes(contents)
        with mock.patch.object(ctrl, "_launch", side_effect=[
            mock.Mock(returncode=0, stdout=str(self.root / "controller.py")),
            mock.Mock(returncode=1, stdout=""),
        ]):
            with self.assertRaisesRegex(ctrl.MaintenanceBlocked, "bootout failed"):
                ctrl._retire(self.root, self.home)
        self.assertEqual(target.read_bytes(), contents)

    def test_accepted_without_marker_rechecks_service_before_retirement(self):
        self.record["state"] = "accepted"
        with (mock.patch.object(ctrl, "_verify_session",
                                return_value=(self.record, self.source, self.state,
                                              self.plist, self.home)),
              mock.patch.object(ctrl, "_resume") as check,
              mock.patch.object(ctrl, "_retire") as retire):
            ctrl.watch(self.root)
            check.assert_called_once()
            retire.assert_called_once()

    def test_probe_success_returns_unchanged_registry_and_restarts_service(self):
        self.record["source_sha"] = "a" * 40
        (self.root / ctrl.JOURNAL).write_text(json.dumps(self.record))
        registry = self.state / "repositories.json"
        original = b'{"version":1,"repositories":[]}\n'
        registry.write_bytes(original)
        (self.state / "agentd.lock").write_text("offline")
        for leaf in ("claims", "corrupt-claims", "result-spool"):
            (self.state / leaf).mkdir()
        with (mock.patch.object(ctrl.Path, "home", return_value=self.home),
              mock.patch.object(ctrl, "__file__",
                                str(self.source / "scripts/macos_repository_maintenance.py")),
              mock.patch.object(ctrl, "_state_paths",
                                return_value=(self.state, self.plist)),
              mock.patch.object(ctrl, "_identify_running_source",
                                return_value=(self.source, "a" * 40)),
              mock.patch.object(ctrl, "_quiescent"),
              mock.patch.object(ctrl, "_new_session",
                                return_value=(self.root, 777)),
              mock.patch.object(ctrl, "_await_drain", return_value=888),
              mock.patch.object(ctrl, "_release") as release,
              mock.patch.object(ctrl, "_launch", return_value=mock.Mock(returncode=0)) as launch,
              mock.patch.object(ctrl, "_wait_absent"),
              mock.patch.object(ctrl, "_resume") as resume,
              mock.patch.object(ctrl, "_retire") as retire):
            ctrl.maintain(operation="probe")
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(launch.call_args.args[0], "bootout")
            resume.assert_called_once()
            retire.assert_called_once()
            self.assertEqual(release.call_count, 2)
        self.assertEqual(registry.read_bytes(), original)
        self.assertFalse((self.state / ctrl.FROZEN).exists())
        self.assertEqual(json.loads((self.root / ctrl.JOURNAL).read_text())["state"],
                         "accepted")

    def test_preflight_admission_failure_never_boots_out_running_service(self):
        (self.root / ctrl.JOURNAL).write_text(json.dumps(self.record))
        with (mock.patch.object(ctrl.Path, "home", return_value=self.home),
              mock.patch.object(ctrl, "__file__",
                                str(self.source / "scripts/macos_repository_maintenance.py")),
              mock.patch.object(ctrl, "_state_paths",
                                return_value=(self.state, self.plist)),
              mock.patch.object(ctrl, "_identify_running_source",
                                return_value=(self.source, "a" * 40)),
              mock.patch.object(ctrl, "_quiescent"),
              mock.patch.object(ctrl, "_new_session",
                                return_value=(self.root, 777)),
              mock.patch.object(ctrl, "_await_drain",
                                side_effect=ctrl.MaintenanceBlocked("admission busy")),
              mock.patch.object(ctrl, "_generation", return_value="running"),
              mock.patch.object(ctrl, "_release") as release,
              mock.patch.object(ctrl, "_rollback") as rollback,
              mock.patch.object(ctrl, "_launch") as launch):
            with self.assertRaisesRegex(ctrl.MaintenanceBlocked, "admission busy"):
                ctrl.maintain(operation="probe")
            release.assert_called_once_with(777)
            rollback.assert_not_called()
            launch.assert_not_called()
        self.assertEqual(json.loads((self.root / ctrl.JOURNAL).read_text())["state"],
                         "rolled_back")

    def test_foreign_freeze_appearing_after_admission_refuses_bootout(self):
        (self.root / ctrl.JOURNAL).write_text(json.dumps(self.record))
        marker = self.state / ctrl.FROZEN
        marker.write_text("someone-else")
        with (mock.patch.object(ctrl.Path, "home", return_value=self.home),
              mock.patch.object(ctrl, "__file__",
                                str(self.source / "scripts/macos_repository_maintenance.py")),
              mock.patch.object(ctrl, "_state_paths",
                                return_value=(self.state, self.plist)),
              mock.patch.object(ctrl, "_identify_running_source",
                                return_value=(self.source, "a" * 40)),
              mock.patch.object(ctrl, "_quiescent"),
              mock.patch.object(ctrl, "_new_session",
                                return_value=(self.root, 777)),
              mock.patch.object(ctrl, "_await_drain", return_value=888),
              mock.patch.object(ctrl, "_release"),
              mock.patch.object(ctrl, "_rollback") as rollback,
              mock.patch.object(ctrl, "_generation", return_value="running"),
              mock.patch.object(ctrl, "_launch") as launch):
            with self.assertRaisesRegex(ctrl.MaintenanceBlocked, "foreign pre-shutdown"):
                ctrl.maintain(operation="probe")
            rollback.assert_not_called()
            launch.assert_not_called()
        self.assertEqual(marker.read_text(), "someone-else")

    def test_freeze_write_once_refuses_foreign_marker(self):
        marker = self.state / ctrl.FROZEN
        marker.write_text("owner-other")
        with self.assertRaises(FileExistsError):
            ctrl._freeze_drain(self.state, self.record)
        self.assertEqual(marker.read_text(), "owner-other")

    def test_drain_waits_for_existing_shared_workers_and_active_repo_lease(self):
        (self.state / ctrl.FROZEN).write_bytes(ctrl._owned_freeze(self.record))
        from local_agent.supervisor import macos_release_admission
        lease_file = self.state / "macos-release-admission.lock"
        lease_file.touch()
        timeline = []
        acquired = []
        checks = {"count": 0}
        def attempt(_path):
            checks["count"] += 1
            if checks["count"] == 1:
                raise BlockingIOError("active worker shared admission")
            acquired.append(321)
            return 321
        def checked(_state, _source, _sha):
            timeline.append("quiescent")
            if len(timeline) == 1:
                raise ctrl.MaintenanceBlocked("repository execution lease is still busy")
        with (mock.patch.object(macos_release_admission, "_active_config", return_value=True),
              mock.patch.object(ctrl, "_lock", side_effect=attempt),
              mock.patch.object(ctrl, "_quiescent", side_effect=checked),
              mock.patch.object(ctrl, "_generation", return_value="running"),
              mock.patch.object(ctrl, "_release") as release,
              mock.patch.object(ctrl.time, "monotonic",
                                side_effect=[0, 1, 2, 3, 4]),
              mock.patch.object(ctrl.time, "sleep") as sleep):
            result = ctrl._await_drain(self.state, self.source, "a" * 40, self.record,
                                       timeout_seconds=20)
        self.assertEqual(result, 321)
        self.assertEqual(checks["count"], 2)
        self.assertEqual(timeline, ["quiescent", "quiescent"])
        self.assertEqual(sleep.call_count, 2)
        release.assert_not_called()

    def test_timeout_releases_admission_and_does_not_bootout(self):
        (self.state / ctrl.FROZEN).write_bytes(ctrl._owned_freeze(self.record))
        from local_agent.supervisor import macos_release_admission
        with (mock.patch.object(macos_release_admission, "_active_config", return_value=True),
              mock.patch.object(ctrl, "_lock", return_value=321),
              mock.patch.object(ctrl, "_quiescent",
                                side_effect=ctrl.MaintenanceBlocked("lease busy")),
              mock.patch.object(ctrl, "_generation", return_value="running"),
              mock.patch.object(ctrl, "_release") as release,
              mock.patch.object(ctrl.time, "monotonic", side_effect=[0, 1, 2, 3]),
              mock.patch.object(ctrl.time, "sleep")):
            with self.assertRaisesRegex(ctrl.MaintenanceBlocked, "safe task drain timed out"):
                ctrl._await_drain(self.state, self.source, "a" * 40, self.record,
                                  timeout_seconds=2)
        release.assert_called_once_with(321)

    def test_foreign_same_shape_marker_cannot_be_thawed(self):
        marker = self.state / ctrl.FROZEN
        foreign = dict(self.record, session_id="session-" + "b" * 24)
        marker.write_bytes(ctrl._owned_freeze(foreign))
        with mock.patch.object(ctrl, "_generation", return_value="running"):
            with self.assertRaisesRegex(ctrl.MaintenanceBlocked, "foreign pre-shutdown"):
                ctrl._thaw_before_shutdown(self.root, self.record, self.source, self.state)
        self.assertEqual(marker.read_bytes(), ctrl._owned_freeze(foreign))

    def test_drain_marker_is_session_unique_and_protected(self):
        self.assertNotEqual(ctrl._owned_freeze(self.record),
                            ctrl._owned_freeze(dict(self.record,
                                                    session_id="session-" + "c" * 24)))
        with self.assertRaisesRegex(ctrl.MaintenanceBlocked, "invalid maintenance"):
            ctrl._owned_freeze({"session_id": "wrong"})

    def test_thaw_crashed_drain_preserves_owner_files_and_registry(self):
        marker = self.state / ctrl.FROZEN
        marker.write_bytes(ctrl._owned_freeze(self.record))
        private = self.home / "owner-sensitive-worktree"
        private.write_text("untouched")
        with mock.patch.object(ctrl, "_generation", return_value="running"):
            ctrl._thaw_before_shutdown(self.root, self.record, self.source, self.state)
        self.assertFalse(marker.exists())
        self.assertEqual(private.read_text(), "untouched")
        self.assertEqual(json.loads((self.root / ctrl.JOURNAL).read_text())["state"],
                         "rolled_back")

    def test_inert_staging_never_boots_out_original_daemon(self):
        launch_agents = self.home / "Library" / "LaunchAgents"
        launch_agents.mkdir(parents=True)
        state = self.home / "Library/Application Support/local-agent"
        state.mkdir(parents=True)
        original = b'{"version":1, "repositories":[]}\n'
        (state / "repositories.json").write_bytes(original)
        plist = launch_agents / (ctrl.LABEL + ".plist")
        plist.write_bytes(b"original-plist")
        with mock.patch.object(ctrl, "_launch", return_value=mock.Mock(returncode=0)) as launch:
            root, fd = ctrl._new_session(self.home, state, plist, self.source, "a" * 40)
            try:
                self.assertEqual((root / "registry-backup.json").read_bytes(), original)
                self.assertEqual((root / "service.plist").read_bytes(), b"original-plist")
                self.assertEqual(json.loads((root / ctrl.JOURNAL).read_text())["state"],
                                 "prepared")
                watch = plistlib.loads((root / "watch.plist").read_bytes())
                self.assertTrue(watch["RunAtLoad"])
                self.assertEqual(watch["StartInterval"], 15)
                self.assertTrue((launch_agents / (ctrl.WATCH_LABEL + ".plist")).exists())
                self.assertEqual(launch.call_count, 1)
                self.assertEqual(launch.call_args.args[0], "bootstrap")
            finally:
                ctrl._release(fd)
        self.assertEqual(plist.read_bytes(), b"original-plist")


if __name__ == "__main__":
    unittest.main()
