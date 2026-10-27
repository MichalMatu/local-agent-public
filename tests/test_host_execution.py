"""Opt-in per-host execution: offline-only operator and disposable task fixtures."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import local_agent.daemon.service as agentd
import local_agent.foundation.core as core
import local_agent.repository.worker as serial
import local_agent.supervisor.worker as parallel
from local_agent.repository import host_execution
from local_agent.repository.context import RepositoryContext, repository_from_dict
from local_agent.repository.host_admission import HostAdmissionBlocked
from local_agent.repository.host_facts import initialize_local_host_identity
from local_agent.repository.host_routing import validate_host_enrollments
from local_agent.supervisor.macos_release_admission import (
    CONFIG as MAC_ADMISSION_CONFIG,
    REQUIRED_ENV as MAC_ADMISSION_REQUIRED_ENV,
    MacReleaseAdmissionBlocked,
    macos_task_admission,
)
from local_agent.runtime.task_contract import task_digest

BINDING = "b0e94f93-478f-4fc6-abed-159d3702362f"
SHA = "a" * 40


class HostExecutionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.root = root
        self.defaults = {module: {name: getattr(module, name) for name in names}
                         for module, names in ((agentd, (
                             "STATE_DIR", "REMOTE_TASKS_DIR", "REMOTE_RESULTS_DIR",
                             "REMOTE_RUNS_DIR", "CLAIMS_DIR", "CORRUPT_CLAIMS_DIR",
                             "LOCAL_STATUS_PATH", "LOCAL_RUNS_DIR", "RESULT_SPOOL_DIR")),
                             (core, ("CONTROL", "WORK", "CHECKPOINTS", "CONTROL_BRANCH", "RESULTS_DIR")))}
        self.addCleanup(self.restore)
        agentd.STATE_DIR = root / "state"
        agentd.STATE_DIR.mkdir(mode=0o700)
        # The real Mac service enforces admission for every task turn.
        # This disposable test state needs its own private enrollment;
        # never relax the inherited production REQUIRED environment flag.
        self.admission_file = agentd.STATE_DIR / MAC_ADMISSION_CONFIG
        self.admission_file.write_text(
            json.dumps({"version": 1, "enabled": True}) + "\n",
            encoding="utf-8",
        )
        self.admission_file.chmod(0o600)
        self.identity_file = root / "identity.json"
        self.identity = initialize_local_host_identity(self.identity_file, "worker-linux")
        self.operator_pin = root / "operator-control.sha"
        self.operator_pin.write_text(SHA + "\n", encoding="ascii")
        self.operator_pin.chmod(0o600)
        self.repo = RepositoryContext(
            repository_id="local-agent", repository="example-owner/local-agent",
            control=root / "control", work=root / "work", checkpoints=root / "checkpoints",
            agent_binding=BINDING, host_id="worker-linux", host_mailbox_enabled=True,
            host_execution_enabled=True, host_identity_file=self.identity_file,
        )
        self.repo.control.mkdir()
        self.repo.work.mkdir()
        self.task = {
            "id": "offline-01", "mode": "commands", "resources": [],
            "commands": ["printf 'offline-test'"], "agent_binding": BINDING,
            "host_mailbox_version": 1, "target_host_id": "worker-linux",
            "target_installation_id": self.identity.installation_id,
            "target_repository_id": "local-agent",
        }
        self.path = self.repo.control / ".agent/hosts/worker-linux/tasks/offline-01.json"
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(self.task), encoding="utf-8")
        self.enrollment = validate_host_enrollments([{
            "version": 1, "host_id": "worker-linux", "installation_id": self.identity.installation_id,
            "enabled": True, "repositories": [{"repository_id": "local-agent", "agent_binding": BINDING}],
        }])
        serial.bind_repository(self.repo)

    def restore(self):
        for module, fields in self.defaults.items():
            for name, value in fields.items():
                setattr(module, name, value)

    def trust(self, enrollments=None):
        return (mock.patch.object(host_execution, "fetch_operator_host_registry",
                                  return_value=(SHA, self.enrollment if enrollments is None else enrollments)),
                mock.patch.object(host_execution, "_remote_ref", return_value=SHA))

    def test_native_mac_fixture_admission_stays_fail_closed(self):
        self.assertEqual(self.admission_file.parent, agentd.STATE_DIR)
        self.assertEqual(stat.S_IMODE(self.admission_file.stat().st_mode), 0o600)
        self.assertEqual(
            json.loads(self.admission_file.read_text(encoding="utf-8")),
            {"version": 1, "enabled": True},
        )
        with mock.patch.dict(os.environ, {MAC_ADMISSION_REQUIRED_ENV: "1"}), \
             mock.patch("local_agent.supervisor.macos_release_admission.sys.platform", "darwin"):
            with macos_task_admission(agentd.STATE_DIR):
                pass
            self.admission_file.unlink()
            with self.assertRaisesRegex(MacReleaseAdmissionBlocked, "not enrolled"):
                with macos_task_admission(agentd.STATE_DIR):
                    self.fail("unregistered fixture must not admit task effects")

    def test_feature_gate_and_local_registry_requires_identity(self):
        base = {"id": "local-agent", "repository": "example-owner/local-agent",
                "agent_binding": BINDING, "host_id": "worker-linux", "host_mailbox_enabled": True}
        self.assertFalse(repository_from_dict(base, home=self.root).host_execution_enabled)
        for extra in ({"host_execution_enabled": True},
                      {"host_execution_enabled": True, "host_identity_file": "relative.json"},
                      {"host_execution_enabled": "true", "host_identity_file": str(self.identity_file)},
                      {"host_execution_enabled": True, "host_mailbox_enabled": False,
                       "host_identity_file": str(self.identity_file)}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                repository_from_dict(base | extra, home=self.root)
        configured = repository_from_dict(base | {"host_execution_enabled": True,
                                                      "host_identity_file": str(self.identity_file)}, home=self.root)
        self.assertTrue(configured.host_execution_enabled)

    def test_positive_preclaim_validated_from_fresh_operator_snapshot(self):
        p1, p2 = self.trust()
        with p1 as fetch, p2 as ref:
            host_execution.require_host_execution(self.repo, self.task, self.path)
        fetch.assert_called_once()
        ref.assert_called_once()
        self.assertEqual(agentd.pending_tasks()[0][1]["id"], "offline-01")

    def test_native_canonical_task_path_does_not_accept_user_aliases(self):
        p1, p2 = self.trust()
        with p1, p2:
            host_execution.require_host_execution(
                self.repo, self.task, self.path.resolve()
            )
        alias = self.root / "fake-control-alias"
        alias.symlink_to(self.repo.control, target_is_directory=True)
        with self.assertRaises(HostAdmissionBlocked) as failure:
            host_execution.require_host_execution(
                self.repo, self.task,
                alias / ".agent/hosts/worker-linux/tasks/offline-01.json",
            )
        self.assertEqual(failure.exception.reason, "host_task_identity_changed")

    def test_disabled_gate_pauses_all_mailboxes_before_operator_transport(self):
        for repo, task in (
            (self.repo, self.task),
            (self.repo, {"id": "untargeted", "commands": ["false"]}),
            (replace(self.repo, host_mailbox_enabled=False, host_execution_enabled=False),
             {"id": "legacy", "commands": ["false"]}),
        ):
            with self.subTest(host_mode=repo.host_mailbox_enabled, task=task["id"]), \
                 mock.patch.object(host_execution, "fetch_operator_host_registry") as fetch:
                with self.assertRaises(host_execution.HostAdmissionUnavailable):
                    host_execution.check_host_task_admission(
                        repo, task, self.path, operator_disabled=True,
                    )
                fetch.assert_not_called()

    def test_disable_during_task_selection_prevents_admission_in_both_workers(self):
        from contextlib import nullcontext
        for variant in (serial, parallel):
            disabled = False

            def pending_after_disable():
                nonlocal disabled
                disabled = True
                return [(self.path, self.task)]

            p1, p2 = self.trust()
            with self.subTest(variant=variant.__name__), p1 as fetch, p2, \
                 mock.patch.object(serial, "validate_repository_checkouts"), \
                 mock.patch.object(serial, "sync_control_quietly"), \
                 mock.patch.object(serial, "repository_binding_ready", return_value=True), \
                 mock.patch.object(parallel, "_repository_binding_ready", return_value=True), \
                 mock.patch.object(serial.agent_operator, "is_disabled", side_effect=lambda: disabled), \
                 mock.patch.object(serial, "handle_repository_control"), \
                 mock.patch.object(serial, "publish_repository_status"), \
                 mock.patch.object(serial, "ActiveRepositoryControlWatcher", return_value=nullcontext()), \
                 mock.patch.object(agentd, "recover_stale_claims"), \
                 mock.patch.object(agentd, "recover_invalid_task_files"), \
                 mock.patch.object(agentd, "pending_tasks", side_effect=pending_after_disable), \
                 mock.patch.object(parallel, "_coalesce_pending_tasks", side_effect=lambda _repo, tasks: tasks), \
                 mock.patch.object(parallel, "machine_resource_lease", return_value=nullcontext(())), \
                 mock.patch.object(parallel.task_dedupe, "record_admission") as admission, \
                 mock.patch.object(agentd, "execute_task", return_value="published") as execute:
                self.assertFalse(variant.poll_repository_once(self.repo))
                execute.assert_not_called()
                admission.assert_not_called()
                fetch.assert_not_called()
                self.assertTrue(self.path.exists())
                self.assertFalse(agentd.task_claim_path(self.task["id"]).exists())

    def test_unpinned_operator_never_reaches_git_or_claim(self):
        self.operator_pin.unlink()
        with mock.patch.object(host_execution, "fetch_operator_host_registry") as fetch:
            with self.assertRaises(host_execution.HostAdmissionUnavailable):
                host_execution.require_host_execution(self.repo, self.task, self.path)
        fetch.assert_not_called()
        self.assertFalse(agentd.task_claim_path(self.task["id"]).exists())

    def test_operator_pin_rejects_unauthorized_ref_change_and_tampering(self):
        self.operator_pin.write_text("b" * 40 + "\n", encoding="ascii")
        p1, p2 = self.trust()
        with p1, p2, self.assertRaises(host_execution.HostAdmissionUnavailable):
            host_execution.require_host_execution(self.repo, self.task, self.path)
        self.operator_pin.write_text(SHA + "\n", encoding="ascii")
        self.operator_pin.chmod(0o644)
        with mock.patch.object(host_execution, "fetch_operator_host_registry") as fetch:
            with self.assertRaises(host_execution.HostAdmissionUnavailable):
                host_execution.require_host_execution(self.repo, self.task, self.path)
        fetch.assert_not_called()
        self.operator_pin.chmod(0o600)
        for malformed in ("not-a-sha", "c" * 41, "c" * 40 + "\n\n", "A" * 40):
            with self.subTest(value=malformed):
                self.operator_pin.write_text(malformed, encoding="ascii")
                with self.assertRaises(host_execution.HostAdmissionUnavailable):
                    host_execution.require_host_execution(self.repo, self.task, self.path)

    def test_operator_pin_rejects_symlink_without_redispatch(self):
        original = self.root / "operator-trust-source.sha"
        original.write_text(SHA + "\n", encoding="ascii")
        original.chmod(0o600)
        self.operator_pin.unlink()
        self.operator_pin.symlink_to(original)
        with mock.patch.object(host_execution, "fetch_operator_host_registry") as fetch:
            with self.assertRaises(host_execution.HostAdmissionUnavailable):
                host_execution.check_host_task_admission(self.repo, self.task, self.path)
        fetch.assert_not_called()

    def test_shared_guard_keeps_legacy_and_host_admission_separate(self):
        legacy = replace(
            self.repo, host_id=None, host_mailbox_enabled=False,
            host_execution_enabled=False, host_identity_file=None,
        )
        plain = {k: v for k, v in self.task.items() if not k.startswith("target_")
                 and k != "host_mailbox_version"}
        host_execution.check_host_task_admission(legacy, plain, self.path)
        with self.assertRaisesRegex(HostAdmissionBlocked, "host_target_in_legacy_queue"):
            host_execution.check_host_task_admission(legacy, self.task, self.path)

        disabled = replace(self.repo, host_execution_enabled=False)
        with self.assertRaisesRegex(HostAdmissionBlocked, "host_targeted_intake_disabled"):
            host_execution.check_host_task_admission(disabled, self.task, self.path)
        with self.assertRaisesRegex(HostAdmissionBlocked, "host_target_missing"):
            host_execution.check_host_task_admission(self.repo, plain, self.path)

    def test_shared_guard_waits_without_an_operator_snapshot(self):
        with mock.patch.object(host_execution, "fetch_operator_host_registry", side_effect=OSError("offline")):
            with self.assertRaises(host_execution.HostAdmissionUnavailable):
                host_execution.check_host_task_admission(self.repo, self.task, self.path)
        p1, p2 = self.trust()
        with p1, p2:
            host_execution.check_host_task_admission(self.repo, self.task, self.path)

    def test_denied_or_unavailable_operator_preserves_unclaimed_work(self):
        for enrollments in ((), (replace(self.enrollment[0], enabled=False),)):
            p1, p2 = self.trust(enrollments)
            with self.subTest(enrollments=enrollments), p1, p2, self.assertRaises(host_execution.HostAdmissionUnavailable):
                host_execution.require_host_execution(self.repo, self.task, self.path)
        wrong_grant = (replace(self.enrollment[0], repositories=()),)
        p1, p2 = self.trust(wrong_grant)
        with p1, p2, self.assertRaises(host_execution.HostAdmissionUnavailable):
            host_execution.require_host_execution(self.repo, self.task, self.path)
        p1, p2 = self.trust()
        with p1, p2, mock.patch.object(host_execution, "_remote_ref", return_value="b" * 40):
            with self.assertRaises(host_execution.HostAdmissionUnavailable):
                host_execution.require_host_execution(self.repo, self.task, self.path)
        with mock.patch.object(host_execution, "fetch_operator_host_registry", side_effect=OSError("offline")):
            with self.assertRaises(host_execution.HostAdmissionUnavailable):
                host_execution.require_host_execution(self.repo, self.task, self.path)
        self.assertFalse(agentd.task_claim_path(self.task["id"]).exists())
        self.assertFalse((self.repo.control / ".agent/hosts/worker-linux/results/offline-01.json").exists())

    def test_changed_manifest_wrong_host_and_symlink_fail_before_effects(self):
        p1, p2 = self.trust()
        with p1, p2:
            self.path.write_text(json.dumps(self.task | {"commands": ["false"]}), encoding="utf-8")
            with self.assertRaises(HostAdmissionBlocked) as failure:
                host_execution.require_host_execution(self.repo, self.task, self.path)
            self.assertEqual(failure.exception.reason, "host_task_digest_changed")
            self.path.write_text(json.dumps(self.task), encoding="utf-8")
            with self.assertRaises(HostAdmissionBlocked):
                host_execution.require_host_execution(self.repo, self.task | {"target_host_id": "worker-other"}, self.path)
            alias = self.path.parent / "linked.json"
            alias.symlink_to(self.path)
            with self.assertRaises(HostAdmissionBlocked):
                host_execution.require_host_execution(self.repo, self.task | {"id": "linked"}, alias)

    def test_serial_worker_executes_one_bounded_fixture_and_publishes_host_result(self):
        results = []
        p1, p2 = self.trust()
        with p1, p2, mock.patch.object(serial, "validate_repository_checkouts"), \
             mock.patch.object(serial, "sync_control_quietly"), \
             mock.patch.object(serial, "repository_binding_ready", return_value=True), \
             mock.patch.object(serial.agent_operator, "is_disabled", return_value=False), \
             mock.patch.object(serial, "handle_repository_control"), \
             mock.patch.object(serial, "publish_repository_status"), \
             mock.patch.object(agentd, "recover_stale_claims"), \
             mock.patch.object(agentd, "recover_invalid_task_files"), \
             mock.patch.object(agentd, "publish_daemon_status"), \
             mock.patch.object(agentd, "publish_run_state"), \
             mock.patch.object(core, "publish_result", side_effect=lambda task_id, result: results.append((task_id, result))), \
             mock.patch.object(agentd.runtime, "process_task", return_value={
                 "id": "offline-01", "status": "done", "task_digest": task_digest(self.task),
                 "finished_at": agentd.now_iso(),
             }) as effect:
            self.assertTrue(serial.poll_repository_once(self.repo))
        effect.assert_called_once()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], "offline-01")
        self.assertEqual(results[0][1]["status"], "done")
        self.assertEqual(agentd.REMOTE_RESULTS_DIR, ".agent/hosts/worker-linux/results")

    def test_worker_waits_instead_of_terminally_rejecting_unverified_enrollment(self):
        result = []
        with mock.patch.object(host_execution, "fetch_operator_host_registry", side_effect=OSError("offline")), \
             mock.patch.object(serial, "validate_repository_checkouts"), \
             mock.patch.object(serial, "sync_control_quietly"), \
             mock.patch.object(serial, "repository_binding_ready", return_value=True), \
             mock.patch.object(serial.agent_operator, "is_disabled", return_value=False), \
             mock.patch.object(serial, "handle_repository_control"), \
             mock.patch.object(serial, "publish_repository_status"), \
             mock.patch.object(agentd, "recover_stale_claims"), \
             mock.patch.object(agentd, "recover_invalid_task_files"), \
             mock.patch.object(core, "publish_result", side_effect=result.append), \
             mock.patch.object(agentd, "execute_task") as effect:
            self.assertFalse(serial.poll_repository_once(self.repo))
        effect.assert_not_called()
        self.assertEqual(result, [])
        self.assertFalse(agentd.task_claim_path(self.task["id"]).exists())

    def test_parallel_worker_runs_single_verified_task_and_no_legacy_fallback(self):
        from contextlib import nullcontext
        p1, p2 = self.trust()
        with p1, p2, mock.patch.object(serial, "validate_repository_checkouts"), \
             mock.patch.object(serial, "sync_control_quietly"), \
             mock.patch.object(parallel, "_repository_binding_ready", return_value=True), \
             mock.patch.object(parallel.agent_operator, "is_disabled", return_value=False), \
             mock.patch.object(serial, "handle_repository_control"), \
             mock.patch.object(serial, "publish_repository_status"), \
             mock.patch.object(agentd, "recover_stale_claims"), \
             mock.patch.object(agentd, "recover_invalid_task_files"), \
             mock.patch.object(parallel, "machine_resource_lease", return_value=nullcontext(())), \
             mock.patch.object(parallel.task_dedupe, "record_admission"), \
             mock.patch.object(agentd, "execute_task", return_value="published") as execute:
            self.assertTrue(parallel.poll_repository_once(self.repo))
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[0]["id"], "offline-01")
        self.assertEqual(agentd.REMOTE_RESULTS_DIR, ".agent/hosts/worker-linux/results")

    def test_parallel_worker_offline_registry_does_not_record_admission_or_execute(self):
        with mock.patch.object(host_execution, "fetch_operator_host_registry", side_effect=OSError("offline")), \
             mock.patch.object(serial, "validate_repository_checkouts"), \
             mock.patch.object(serial, "sync_control_quietly"), \
             mock.patch.object(parallel, "_repository_binding_ready", return_value=True), \
             mock.patch.object(parallel.agent_operator, "is_disabled", return_value=False), \
             mock.patch.object(serial, "handle_repository_control"), \
             mock.patch.object(serial, "publish_repository_status") as status, \
             mock.patch.object(agentd, "recover_stale_claims"), \
             mock.patch.object(agentd, "recover_invalid_task_files"), \
             mock.patch.object(parallel.task_dedupe, "record_admission") as record, \
             mock.patch.object(agentd, "execute_task") as execute:
            self.assertFalse(parallel.poll_repository_once(self.repo))
        execute.assert_not_called()
        record.assert_not_called()
        self.assertTrue(any(c.args[1] == "host_admission_waiting" for c in status.call_args_list))

    def test_parallel_filter_prevents_unverified_tasks_from_effects_and_dedupe(self):
        p1, p2 = self.trust()
        with p1, p2:
            self.assertEqual([v[1]["id"] for v in parallel._filter_pending_task_bindings(
                self.repo, agentd.pending_tasks())], ["offline-01"])
        with mock.patch.object(host_execution, "fetch_operator_host_registry", side_effect=RuntimeError("offline")), \
             mock.patch.object(serial, "reject_host_task") as reject:
            with self.assertRaises(host_execution.HostAdmissionUnavailable):
                parallel._filter_pending_task_bindings(self.repo, agentd.pending_tasks())
        reject.assert_not_called()
        self.assertFalse(agentd.task_claim_path(self.task["id"]).exists())


if __name__ == "__main__":
    unittest.main()
