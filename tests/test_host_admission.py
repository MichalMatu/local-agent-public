"""Offline host task preflight and legacy-worker no-execution regression tests."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import local_agent.repository.worker as serial_worker
import local_agent.supervisor.worker as parallel_worker
from local_agent.repository.context import RepositoryContext
from local_agent.repository.host_admission import (
    HostAdmissionBlocked,
    has_host_target,
    preflight_host_targeted_task,
)
from local_agent.repository.host_routing import HostIdentity, validate_host_enrollments
from local_agent.runtime.task_contract import task_digest

BINDING = "b0e94f93-478f-4fc6-abed-159d3702362f"
INSTALLATION = "12345678-1234-4234-8234-123456789abc"


class HostAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.repo = RepositoryContext(
            repository_id="local-agent", repository="example-owner/local-agent",
            control=root / "control", work=root / "work", checkpoints=root / "checkpoints",
            agent_binding=BINDING,
        )
        self.identity = HostIdentity("worker-linux", INSTALLATION)
        self.enrollment = validate_host_enrollments([{
            "version": 1, "host_id": "worker-linux", "installation_id": INSTALLATION,
            "enabled": True,
            "repositories": [{"repository_id": "local-agent", "agent_binding": BINDING}],
        }])[0]
        self.task = {
            "id": "offline-target", "mode": "commands", "resources": [],
            "commands": ["printf 'not-executed'"], "agent_binding": BINDING,
            "host_mailbox_version": 1, "target_host_id": "worker-linux",
            "target_installation_id": INSTALLATION,
            "target_repository_id": "local-agent",
        }

    def fixture_args(self, task=None, **overrides):
        item = task if task is not None else self.task
        return dict(
            task=item, repository=self.repo, source="host_mailbox", enabled=True,
            local_identity=self.identity, enrollment=self.enrollment,
            expected_task_id=item["id"], expected_task_digest=task_digest(item),
        ) | overrides

    def assert_blocked(self, reason, **kwargs):
        with self.assertRaises(HostAdmissionBlocked) as failure:
            preflight_host_targeted_task(**kwargs)
        self.assertEqual(failure.exception.reason, reason)

    def test_non_targeted_legacy_task_is_unchanged(self):
        plain = {"id": "normal", "agent_binding": BINDING, "commands": ["true"]}
        before = json.dumps(plain, sort_keys=True)
        self.assertFalse(has_host_target(plain))
        preflight_host_targeted_task(plain, self.repo)
        self.assertEqual(json.dumps(plain, sort_keys=True), before)

    def test_disabled_gate_and_legacy_queue_never_promote_target(self):
        before = task_digest(self.task)
        self.assert_blocked("host_target_in_legacy_queue", task=self.task, repository=self.repo)
        self.assert_blocked("host_target_in_legacy_queue", **self.fixture_args(source="legacy_queue"))
        self.assert_blocked("host_targeted_intake_disabled", **self.fixture_args(enabled=False))
        self.assert_blocked("host_target_in_legacy_queue", task={"id": "x", "target_host_id": "worker-linux"}, repository=self.repo)
        self.assertEqual(task_digest(self.task), before)

    def test_offline_preflight_checks_identity_and_never_grants_execution(self):
        self.assert_blocked("distributed_fence_unavailable", **self.fixture_args())
        self.assert_blocked("operator_emergency_stop", **self.fixture_args(operator_disabled=True))
        for state in ("UNKNOWN", "claimed", "running", "failed", "done", "interrupted"):
            with self.subTest(state=state):
                self.assert_blocked("host_task_not_replayable", **self.fixture_args(lifecycle_state=state))
        for change in ({"target_host_id": "worker-other"},
                       {"target_installation_id": "12345678-1234-4234-8234-123456789abd"},
                       {"target_repository_id": "other-repo"},
                       {"agent_binding": "12345678-1234-4234-8234-123456789abd"}):
            item = self.task | change
            with self.subTest(change=change):
                self.assert_blocked("host_binding_or_enrollment_invalid", **self.fixture_args(item))
        disabled = self.enrollment.__class__(self.identity, False, self.enrollment.repositories)
        self.assert_blocked("host_binding_or_enrollment_invalid", **self.fixture_args(enrollment=disabled))
        self.assert_blocked("host_binding_or_enrollment_invalid", **self.fixture_args(enrollment=None))

    def test_partial_version_and_immutable_task_identity_rejected(self):
        partial = {key: value for key, value in self.task.items() if key != "target_host_id"}
        self.assert_blocked("host_target_incomplete", **self.fixture_args(partial))
        for version in (True, 2, "1", None):
            item = self.task | {"host_mailbox_version": version}
            with self.subTest(version=version):
                self.assert_blocked("host_mailbox_version_invalid", **self.fixture_args(item))
        self.assert_blocked("host_task_identity_changed", **self.fixture_args(expected_task_id="reused-id"))
        self.assert_blocked("host_task_digest_changed", **self.fixture_args(expected_task_digest="0" * 64))

    def test_disposable_bare_git_bundle_preserves_pinned_payload(self):
        """Fixture is a local disposable Git transport, not operator-controlled GitHub."""
        root = self.repo.control.parent
        bare = root / "bare.git"
        work = root / "staging"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True, timeout=5)
        subprocess.run(["git", "init", "-q", "-b", "agent-control", str(work)], check=True, timeout=5)
        def git(*args):
            return subprocess.run(["git", *args], cwd=work, capture_output=True,
                                  text=True, check=True, timeout=5).stdout.strip()
        git("config", "user.name", "Sandbox fixture")
        git("config", "user.email", "test@example.invalid")
        git("remote", "add", "origin", str(bare))
        target = work / ".agent/hosts/worker-linux/tasks/offline-target.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(self.task), encoding="utf-8")
        git("add", ".agent")
        git("commit", "-q", "-m", "Fixture only")
        git("push", "-q", "origin", "agent-control")
        sha = git("rev-parse", "HEAD")
        pinned = json.loads(git("show", f"{sha}:.agent/hosts/worker-linux/tasks/offline-target.json"))
        self.assertEqual(pinned, self.task)
        self.assert_blocked("distributed_fence_unavailable", **self.fixture_args(pinned))
        self.assertFalse((work / ".agent/hosts/worker-linux/results").exists())
        self.assertFalse((work / ".agent/hosts/worker-linux/claims").exists())
        git("commit", "-q", "--allow-empty", "-m", "Move fixture ref")
        self.assertNotEqual(git("rev-parse", "HEAD"), sha)
        self.assertEqual(json.loads(git("show", f"{sha}:.agent/hosts/worker-linux/tasks/offline-target.json")), pinned)

    def test_parallel_worker_rejects_before_dedupe_claim_and_execution(self):
        with mock.patch.object(parallel_worker.serial_worker, "bind_repository"), \
             mock.patch.object(parallel_worker.serial_worker, "validate_repository_checkouts"), \
             mock.patch.object(parallel_worker.serial_worker, "sync_control_quietly"), \
             mock.patch.object(parallel_worker.agent_operator, "is_disabled", return_value=False), \
             mock.patch.object(parallel_worker, "_repository_binding_ready", return_value=True), \
             mock.patch.object(parallel_worker.agentd, "recover_stale_claims"), \
             mock.patch.object(parallel_worker.agentd, "recover_invalid_task_files"), \
             mock.patch.object(parallel_worker.serial_worker, "handle_repository_control"), \
             mock.patch.object(parallel_worker.agentd, "pending_tasks", return_value=[(Path("host.json"), self.task)]), \
             mock.patch.object(parallel_worker.serial_worker, "publish_repository_status"), \
             mock.patch.object(parallel_worker.serial_worker, "reject_host_task") as reject, \
             mock.patch.object(parallel_worker.task_dedupe, "record_admission") as admit, \
             mock.patch.object(parallel_worker.agentd, "execute_task") as execute:
            self.assertFalse(parallel_worker.poll_repository_once(self.repo))
        self.assertEqual(reject.call_args.args[2], "host_target_in_legacy_queue")
        admit.assert_not_called()
        execute.assert_not_called()

    def test_serial_worker_rejects_before_claim_and_execution(self):
        with mock.patch.object(serial_worker, "bind_repository"), \
             mock.patch.object(serial_worker, "validate_repository_checkouts"), \
             mock.patch.object(serial_worker, "sync_control_quietly"), \
             mock.patch.object(serial_worker.agent_operator, "is_disabled", return_value=False), \
             mock.patch.object(serial_worker, "repository_binding_ready", return_value=True), \
             mock.patch.object(serial_worker.agentd, "recover_stale_claims"), \
             mock.patch.object(serial_worker.agentd, "recover_invalid_task_files"), \
             mock.patch.object(serial_worker, "handle_repository_control"), \
             mock.patch.object(serial_worker.agentd, "pending_tasks", return_value=[(Path("host.json"), self.task)]), \
             mock.patch.object(serial_worker, "reject_host_task") as reject, \
             mock.patch.object(serial_worker.agentd, "execute_task") as execute:
            self.assertTrue(serial_worker.poll_repository_once(self.repo))
        self.assertEqual(reject.call_args.args[2], "host_target_in_legacy_queue")
        execute.assert_not_called()

    def test_rejection_publishes_existing_immutable_digest_and_terminal_result(self):
        with mock.patch.object(serial_worker.core, "publish_result") as result, \
             mock.patch.object(serial_worker.agentd, "publish_run_state") as run:
            serial_worker.reject_host_task(self.repo, self.task, "host_target_in_legacy_queue")
        payload = result.call_args.args[1]
        self.assertEqual(payload["task_digest"], task_digest(self.task))
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["failure_reason"], "host_target_in_legacy_queue")
        self.assertIsNone(payload["started_at"])
        self.assertEqual(run.call_args.args[1]["event"], "task_rejected")


if __name__ == "__main__":
    unittest.main()
