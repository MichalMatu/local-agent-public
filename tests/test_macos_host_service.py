"""Independent service-channel regression tests; no real launchd operations."""
from __future__ import annotations

import base64
import fcntl
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts import macos_host_self_update as updater
from scripts import macos_host_service as service


class IndependentHostServiceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="independent-service-")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.state = self.home / "Library/Application Support/local-agent"
        self.root = self.home / updater.UPDATER_ROOT
        self.root.mkdir(parents=True, mode=0o700)
        self.state.chmod(0o700)
        self.identity = {"host_id": "macbook-test", "installation_id": "11111111-2222-4333-8444-555555555555"}
        updater._atomic(self.state / "host-identity.json", self.identity)
        updater._atomic(self.state / "supervisor-control.json", {
            "enabled": True, "host_id": "macbook-test", "repository": "owner/repository",
        })
        updater._atomic(self.state / service.ACCEPTED, {"service_actions_owner": "external_updater"})
        updater._atomic(self.root / "service-activation.json", {"snapshot": "0" * 40, "historical_digest": None})
        self.request = {"version": 1, "id": "diagnose-01", "action": "host_diagnostics",
                        "target_host_id": self.identity["host_id"],
                        "target_installation_id": self.identity["installation_id"]}
        self.namespace = ".agent/hosts/macbook-test/supervisor/" + self.identity["installation_id"]
        self.patches = [
            mock.patch.object(service, "_mailbox", return_value=self.root / "service.git"),
            mock.patch.object(service, "_fetch", return_value="a" * 40),
            mock.patch.object(service, "_blob", side_effect=lambda _cache, _sha, path:
                              self.request if path.endswith("/control.json") else None),
            mock.patch.object(service, "observation", return_value={"running_revision": "b" * 40}),
            mock.patch.object(service, "_publish"),
        ]
        self.mocks = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)

    def test_diagnostics_do_not_need_supervisor_project_checkout_or_task_scheduler(self):
        with mock.patch.object(service, "recover", side_effect=AssertionError("not a restart")):
            result = service.poll(self.home)
        self.assertEqual(result["result"], "completed")
        payload = self.mocks[-1].call_args.args[1]
        self.assertEqual(set(payload), {self.namespace + "/results/diagnose-01.json",
                                       self.namespace + "/acks/diagnose-01.json"})

    def test_failed_publication_reuses_saved_result_without_repeating_operation(self):
        self.mocks[-1].side_effect = [RuntimeError("offline"), None]
        with self.assertRaisesRegex(RuntimeError, "offline"):
            service.poll(self.home)
        self.assertEqual(service.poll(self.home)["result"], "completed")
        self.assertEqual(self.mocks[-2].call_count, 1)

    def test_wrong_installation_and_id_mutation_never_execute_an_operation(self):
        self.request["target_installation_id"] = "66666666-2222-4333-8444-555555555555"
        with self.assertRaisesRegex(ValueError, "exact-installation"):
            service.poll(self.home)
        self.mocks[-2].assert_not_called()
        self.request["target_installation_id"] = self.identity["installation_id"]
        service.poll(self.home)
        self.request["action"] = "restart"
        with mock.patch.object(service, "recover") as restart:
            with self.assertRaisesRegex(ValueError, "immutable"):
                service.poll(self.home)
        restart.assert_not_called()

    def test_historical_restart_ack_is_never_replayed(self):
        self.request["action"] = "restart"
        self.mocks[2].side_effect = lambda _c, _s, path: (
            self.request if path.endswith("/control.json") else
            {"id": self.request["id"], "action": "restart"})
        with mock.patch.object(service, "recover") as restart:
            result = service.poll(self.home)
        self.assertEqual(result["status"], "service_request_already_acknowledged")
        restart.assert_not_called()

    def test_claimed_restart_with_uncertain_effect_is_not_replayed(self):
        self.request["action"] = "restart"
        with mock.patch.object(service, "recover", side_effect=RuntimeError("interrupted")) as restart:
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                service.poll(self.home)
            self.assertEqual(service.poll(self.home)["result"], "unknown")
        self.assertEqual(restart.call_count, 1)

    def test_busy_restart_can_retry_because_it_performed_no_effect(self):
        self.request["action"] = "restart"
        with mock.patch.object(service, "recover", side_effect=[
            {"status": "waiting_for_active_leases", "changed": False},
            {"status": "accepted_service_recovered", "changed": True},
        ]) as restart:
            self.assertEqual(service.poll(self.home)["result"], "waiting_for_active_leases")
            self.assertEqual(service.poll(self.home)["result"], "accepted_service_recovered")
        self.assertEqual(restart.call_count, 2)

    def test_self_update_is_queued_while_supervisor_is_unavailable(self):
        self.request.update(action="self_update", candidate_sha="c" * 40)
        result = service.poll(self.home)
        self.assertEqual(result["result"], "queued")
        record = service._read(self.state / updater.INTENTS / "diagnose-01.json")
        self.assertEqual(record["candidate_sha"], "c" * 40)
        self.assertEqual(record["target_installation_id"], self.identity["installation_id"])
        self.mocks[-2].assert_not_called()

    def test_disabled_host_still_diagnoses_but_never_queues_an_update(self):
        updater._atomic(self.state / "disabled.json", {"disabled": True})
        self.request.update(action="self_update", candidate_sha="c" * 40)
        self.assertEqual(service.poll(self.home)["result"], "disabled")
        self.assertFalse((self.state / updater.INTENTS).exists())

    def test_activation_fences_missing_ack_historical_restart(self):
        (self.root / "service-activation.json").unlink()
        self.request["action"] = "restart"
        with mock.patch.object(service, "recover") as restart:
            self.assertEqual(service.poll(self.home)["result"], "historical_fenced")
        restart.assert_not_called()


class AcceptedServiceRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="accepted-service-")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.state = self.home / "Library/Application Support/local-agent"
        self.root = self.home / updater.UPDATER_ROOT
        self.root.mkdir(parents=True, mode=0o700)
        self.state.chmod(0o700)
        (self.state / "locks/repositories").mkdir(parents=True)
        for name in [service.rollout.LOCK, "installation.lock", "agentd.lock", "locks/repositories/one.lock"]:
            (self.state / name).touch(mode=0o600)
        self.identity = {"host_id": "macbook-test", "installation_id": "11111111-2222-4333-8444-555555555555"}
        updater._atomic(self.state / "host-identity.json", self.identity)
        updater._atomic(self.state / "repositories.json", {"version": 1, "repositories": []})
        self.plist = self.home / "Library/LaunchAgents/com.michal.local-agent.plist"
        self.plist.parent.mkdir(parents=True)
        self.raw = plistlib.dumps({"Label": service.rollout.LABEL,
                                  "WorkingDirectory": str(self.home / "release"),
                                  "ProgramArguments": ["python", str(self.home / "release/agent_entrypoint.py"),
                                                       "--registry", "registry", "--max-workers", "4"]})
        self.previous = plistlib.dumps({"ProgramArguments": ["python", str(self.home / "old/agent_entrypoint.py")]})
        self.plist.write_bytes(self.previous)
        self.git = mock.patch.object(updater, "_git", side_effect=lambda _p, *args:
                                     "a" * 40 if args[0] == "rev-parse" else
                                     updater.CANONICAL_SOURCE if args[0] == "remote" else "")
        self.git.start()
        self.addCleanup(self.git.stop)
        service.remember_accepted(self.home, "a" * 40, self.raw)
        self.observed = {"disabled": False, "running_revision": "b" * 40,
                         "definition_matches": False, "revision_matches": False,
                         "heartbeat_fresh": True, "generation_matches": False}
        self.observe_patch = mock.patch.object(service, "observation", return_value=self.observed)
        self.observe_patch.start()
        self.addCleanup(self.observe_patch.stop)
        leases = mock.patch.object(service, "_registered_leases", return_value=[])
        leases.start()
        self.addCleanup(leases.stop)

    def test_restore_accepted_target_preserves_claims_and_pending_result_spool(self):
        ledger = self.state / "durable-result.json"
        ledger.write_bytes(b"immutable execution evidence")
        with mock.patch.object(service, "_switch") as switch:
            result = service.recover(self.home)
        self.assertEqual(result["status"], "accepted_service_recovered")
        self.assertEqual(switch.call_args.args[1:3], (self.raw, "a" * 40))
        self.assertEqual(ledger.read_bytes(), b"immutable execution evidence")
        self.assertFalse((self.state / service.rollout.FROZEN).exists())

    def test_active_descendant_lease_blocks_recovery_without_touching_launchd(self):
        with (self.state / "locks/repositories/one.lock").open("r+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(service, "_switch") as switch:
                result = service.recover(self.home)
        self.assertEqual(result["status"], "waiting_for_active_leases")
        switch.assert_not_called()

    def test_failed_candidate_rolls_back_and_automatic_retry_is_bounded(self):
        with mock.patch.object(service, "_switch", side_effect=[RuntimeError("unhealthy"), None]) as switch:
            with self.assertRaisesRegex(RuntimeError, "unhealthy"):
                service.recover(self.home)
            self.assertEqual(service.recover(self.home)["status"], "repair_failed_requires_new_request")
        self.assertEqual(switch.call_count, 2)
        self.assertEqual(switch.call_args.args[1], self.previous)

    def test_crashed_switch_rolls_back_instead_of_replaying_restart(self):
        updater._atomic(self.state / service.rollout.FROZEN, {"service_recovery": "a" * 40})
        service._write(self.root / service.TRANSACTION, {
            "state": "switching", "revision": "a" * 40,
            "previous": base64.b64encode(self.previous).decode(),
            "previous_revision": "b" * 40,
        })
        with mock.patch.object(service, "_switch") as switch:
            result = service.recover(self.home)
        self.assertEqual(result["status"], "interrupted_service_recovery_rolled_back")
        self.assertEqual(switch.call_args.args[1], self.previous)
        self.assertFalse((self.state / service.rollout.FROZEN).exists())

    def test_disable_and_foreign_maintenance_never_get_cleared(self):
        self.observed["disabled"] = True
        with mock.patch.object(service, "_switch") as switch:
            self.assertEqual(service.recover(self.home)["status"], "disabled")
            self.observed["disabled"] = False
            updater._atomic(self.state / service.rollout.FROZEN, {"foreign": True})
            self.assertEqual(service.recover(self.home)["status"], "maintenance_in_progress")
        switch.assert_not_called()
        self.assertEqual(service._read(self.state / service.rollout.FROZEN), {"foreign": True})

    def test_failed_rollback_preserves_admission_and_does_not_loop(self):
        with mock.patch.object(service, "_switch", side_effect=RuntimeError("unhealthy")) as switch:
            with self.assertRaisesRegex(RuntimeError, "unhealthy"):
                service.recover(self.home)
            self.assertEqual(service.recover(self.home)["status"], "rollback_failed_requires_new_request")
        self.assertEqual(switch.call_count, 2)
        self.assertTrue((self.state / service.rollout.FROZEN).is_file())

    def test_pending_installation_blocks_restart_without_clearing_its_journal(self):
        pending = self.state / "installation-pending.json"
        updater._atomic(pending, {"status": "interrupted"})
        with mock.patch.object(service, "_switch") as switch:
            self.assertEqual(service.recover(self.home)["status"], "installation_recovery_required")
        switch.assert_not_called()
        self.assertTrue(pending.is_file())


class ServiceGitPublicationTests(unittest.TestCase):
    def test_real_bare_git_preserves_remote_tasks_without_any_rebase(self):
        with tempfile.TemporaryDirectory(prefix="service-git-") as tmp:
            root = Path(tmp)
            remote, producer, cache = root / "remote.git", root / "producer", root / "service.git"
            def git(*args, cwd=root):
                return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True,
                                      check=True, timeout=12).stdout.strip()
            git("init", "--bare", "--initial-branch=agent-control", str(remote))
            git("clone", str(remote), str(producer))
            git("config", "user.name", "Test", cwd=producer)
            git("config", "user.email", "test@example.invalid", cwd=producer)
            tasks = producer / ".agent/tasks"
            tasks.mkdir(parents=True)
            (tasks / "untouched.json").write_text('{"id":"untouched"}\n')
            git("add", ".agent/tasks/untouched.json", cwd=producer)
            git("commit", "-m", "Task", cwd=producer)
            git("push", "origin", "agent-control", cwd=producer)
            git("clone", "--bare", str(remote), str(cache))
            service._publish(cache, {".agent/service/result.json": {"status": "completed"}})
            commit = service._fetch(cache)
            self.assertEqual(service._blob(cache, commit, ".agent/tasks/untouched.json"), {"id": "untouched"})
            self.assertEqual(service._blob(cache, commit, ".agent/service/result.json"), {"status": "completed"})
            self.assertFalse((cache / "rebase-merge").exists())
            self.assertEqual(list(root.glob("service-index-*")), [])


class IndependentReleasePreparationTests(unittest.TestCase):
    def test_diagnostics_continue_during_bounded_release_preparation(self):
        with tempfile.TemporaryDirectory(prefix="service-preparation-") as tmp:
            home = Path(tmp)
            root = home / updater.UPDATER_ROOT
            scripts = root / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "__init__.py").touch()
            (scripts / "macos_host_self_update.py").write_text(
                'import time\ntime.sleep(0.4)\nprint(\'{"status":"verified_stub"}\')\n')
            ticks = []
            with mock.patch.object(updater, "SERVICE_POLL_SECONDS", 0.01):
                result = updater._run_update_turn(home, lambda: ticks.append(True))
            self.assertEqual(result["status"], "verified_stub")
            self.assertTrue(ticks)

    def test_preparation_timeout_reaps_its_process_group(self):
        with tempfile.TemporaryDirectory(prefix="service-preparation-timeout-") as tmp:
            home = Path(tmp)
            root = home / updater.UPDATER_ROOT
            scripts = root / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "__init__.py").touch()
            (scripts / "macos_host_self_update.py").write_text('import time\ntime.sleep(30)\n')
            children = []
            real = subprocess.Popen
            def spawn(*args, **kwargs):
                child = real(*args, **kwargs)
                children.append(child)
                return child
            with (mock.patch.object(updater, "UPDATE_TURN_TIMEOUT_SECONDS", 0.01),
                  mock.patch.object(updater.subprocess, "Popen", side_effect=spawn)):
                with self.assertRaises(TimeoutError):
                    updater._run_update_turn(home, lambda: None)
            self.assertIsNotNone(children[0].poll())


class IndependentControllerRefreshTests(unittest.TestCase):
    def test_dangling_disable_marker_remains_authoritative_for_service_diagnostics(self):
        with tempfile.TemporaryDirectory(prefix="service-disabled-marker-") as tmp:
            home = Path(tmp)
            state = home / "Library/Application Support/local-agent"
            state.mkdir(parents=True, mode=0o700)
            updater._atomic(state / "status.json", {"state": "disabled"})
            (state / "disabled.json").symlink_to("missing-disable-target")
            path = home / "Library/LaunchAgents/com.michal.local-agent.plist"
            path.parent.mkdir(parents=True)
            path.write_bytes(plistlib.dumps({"WorkingDirectory": "/accepted"}))
            self.assertTrue(service.observation(home)["disabled"])

    def test_failed_updater_bootstrap_restores_exact_code_and_registration(self):
        with tempfile.TemporaryDirectory(prefix="service-refresh-") as tmp:
            home = Path(tmp)
            state = home / "Library/Application Support/local-agent"
            root = home / updater.UPDATER_ROOT
            package = root / "scripts"
            package.mkdir(parents=True, mode=0o700)
            root.chmod(0o700)
            (state / "archives").mkdir()
            (root / "watch.lock").touch(mode=0o600)
            saved = {}
            for name in ["macos_host_self_update.py", "macos_host_service.py", "macos_cli_polling_rollout.py"]:
                saved[name] = ("# Original " + name + "\n").encode()
                path = package / name
                path.write_bytes(saved[name])
                path.chmod(0o600)
            watcher = home / "Library/LaunchAgents" / (updater.UPDATER_LABEL + ".plist")
            watcher.parent.mkdir(parents=True)
            raw = plistlib.dumps({"Label": updater.UPDATER_LABEL, "WorkingDirectory": str(root)})
            watcher.write_bytes(raw)
            def git(_source, *args):
                return "a" * 40 if args[0] == "rev-parse" else ""
            observed = {key: True for key in ["definition_matches", "revision_matches", "heartbeat_fresh", "generation_matches"]}
            with (
                mock.patch.object(updater, "_git", side_effect=git),
                mock.patch.object(service, "adopt_accepted_release"),
                mock.patch.object(service, "observation", return_value=observed),
                mock.patch.object(updater, "_reload_updater", side_effect=[RuntimeError("bootstrap"), None]),
            ):
                with self.assertRaisesRegex(RuntimeError, "bootstrap"):
                    updater.refresh(home)
            for name, expected in saved.items():
                self.assertEqual((package / name).read_bytes(), expected)
            self.assertEqual(watcher.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
