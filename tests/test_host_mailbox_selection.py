"""Source-only mailbox isolation tests; no host execution or live GitHub writes."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import local_agent.daemon.service as agentd
import local_agent.foundation.core as core
import local_agent.repository.worker as serial
import local_agent.supervisor.worker as parallel
from local_agent.supervisor.macos_release_admission import (
    CONFIG as MAC_ADMISSION_CONFIG,
    REQUIRED_ENV as MAC_ADMISSION_REQUIRED_ENV,
    MacReleaseAdmissionBlocked,
    macos_task_admission,
)
from local_agent.repository.context import RepositoryContext, repository_config_digest, repository_from_dict

BINDING = "b0e94f93-478f-4fc6-abed-159d3702362f"
INSTALLATION = "12345678-1234-4234-8234-123456789abc"


class HostMailboxSelectionTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.defaults = {module: {name: getattr(module, name) for name in names}
                         for module, names in ((agentd, (
                             "STATE_DIR", "REMOTE_TASKS_DIR", "REMOTE_RESULTS_DIR",
                             "REMOTE_RUNS_DIR", "REMOTE_DAEMON_STATUS",
                             "REMOTE_CONTROL_REQUEST", "REMOTE_CONTROL_ACK_DIR", "CLAIMS_DIR", "CORRUPT_CLAIMS_DIR",
                             "LOCAL_STATUS_PATH", "LOCAL_RUNS_DIR", "RESULT_SPOOL_DIR")),
                             (core, ("CONTROL", "WORK", "CHECKPOINTS", "CONTROL_BRANCH", "RESULTS_DIR")))}
        self.addCleanup(self.restore)
        agentd.STATE_DIR = self.root / "local-state"
        agentd.STATE_DIR.mkdir(mode=0o700)
        # Enroll only this disposable test state. Inherited production
        # LOCAL_AGENT_MACOS_ADMISSION_REQUIRED stays unchanged.
        self.admission_file = agentd.STATE_DIR / MAC_ADMISSION_CONFIG
        self.admission_file.write_text(
            json.dumps({"version": 1, "enabled": True}) + "\n",
            encoding="utf-8",
        )
        self.admission_file.chmod(0o600)
        self.repo = RepositoryContext(
            repository_id="local-agent", repository="example-owner/local-agent",
            control=self.root / "control", work=self.root / "work",
            checkpoints=self.root / "checkpoints", agent_binding=BINDING,
            host_id="worker-a", host_mailbox_enabled=True,
        )
        self.repo.control.mkdir(parents=True)

    def restore(self):
        for module, fields in self.defaults.items():
            for name, value in fields.items():
                setattr(module, name, value)

    def task(self, task_id: str, *, host_id: str = "worker-a", target: bool = True):
        data = {"id": task_id, "mode": "commands", "resources": [],
                "commands": ["printf 'never-executed'"], "agent_binding": BINDING}
        if target:
            data.update(host_mailbox_version=1, target_host_id=host_id,
                        target_installation_id=INSTALLATION, target_repository_id="local-agent")
        return data

    def put(self, relative: str, task: dict):
        path = self.repo.control / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(task), encoding="utf-8")
        return path

    def test_disposable_mac_admission_is_private_and_missing_config_fails_closed(self):
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
                    self.fail("missing enrollment must block task effects")

    def test_registry_opt_in_is_explicit_and_in_worker_digest(self):
        base = {"id": "local-agent", "repository": "example-owner/local-agent", "agent_binding": BINDING}
        legacy = repository_from_dict(base, home=self.root)
        host = repository_from_dict(base | {"host_id": "worker-a", "host_mailbox_enabled": True}, home=self.root)
        self.assertFalse(legacy.host_mailbox_enabled)
        self.assertEqual(host.host_id, "worker-a")
        self.assertNotEqual(repository_config_digest(legacy), repository_config_digest(host))
        for setting in ({"host_mailbox_enabled": True}, {"host_id": "../other", "host_mailbox_enabled": True},
                        {"host_id": "worker-a", "host_mailbox_enabled": "true"}):
            with self.subTest(setting=setting), self.assertRaises(ValueError):
                repository_from_dict(base | setting, home=self.root)

    def test_mailbox_selected_only_from_local_registry_and_state_isolated(self):
        self.put(".agent/tasks/legacy.json", self.task("legacy", target=False))
        self.put(".agent/hosts/worker-a/tasks/a.json", self.task("a"))
        self.put(".agent/hosts/worker-b/tasks/b.json", self.task("b", host_id="worker-b"))
        serial.bind_repository(self.repo)
        self.assertEqual([task["id"] for _, task in agentd.pending_tasks()], ["a"])
        self.assertEqual(agentd.REMOTE_RESULTS_DIR, ".agent/hosts/worker-a/results")
        self.assertEqual(agentd.REMOTE_DAEMON_STATUS, ".agent/hosts/worker-a/status/daemon.json")
        self.assertEqual(agentd.REMOTE_CONTROL_REQUEST, ".agent/hosts/worker-a/daemon/control.json")
        self.assertEqual(agentd.REMOTE_CONTROL_ACK_DIR, ".agent/hosts/worker-a/daemon/acks")
        self.assertEqual(core.RESULTS_DIR, agentd.REMOTE_RESULTS_DIR)
        self.assertIn("hosts/worker-a", str(agentd.CLAIMS_DIR))
        with mock.patch.object(core, "publish_control_json") as publish:
            core.publish_result("a", {"id": "a", "status": "done"})
        self.assertEqual(publish.call_args.args[0], Path(".agent/hosts/worker-a/results/a.json"))
        serial.bind_repository(replace(self.repo, host_id="worker-b"))
        self.assertEqual([task["id"] for _, task in agentd.pending_tasks()], ["b"])
        self.assertNotEqual(agentd.CLAIMS_DIR, serial.repository_state_dir(self.repo) / "claims")
        serial.bind_repository(replace(self.repo, host_id=None, host_mailbox_enabled=False))
        self.assertEqual([task["id"] for _, task in agentd.pending_tasks()], ["legacy"])
        self.assertEqual(agentd.REMOTE_RESULTS_DIR, ".agent/results")
        self.assertEqual(agentd.REMOTE_DAEMON_STATUS, ".agent/status/daemon.json")
        self.assertEqual(agentd.REMOTE_CONTROL_REQUEST, ".agent/daemon/control.json")
        self.assertEqual(agentd.REMOTE_CONTROL_ACK_DIR, ".agent/daemon/acks")

    def test_host_rejections_do_not_execute_or_publish_into_legacy(self):
        self.put(".agent/hosts/worker-a/tasks/a.json", self.task("a"))
        self.put(".agent/hosts/worker-a/tasks/untargeted.json", self.task("untargeted", target=False))
        captured = []
        with mock.patch.object(serial, "validate_repository_checkouts"), \
             mock.patch.object(serial, "sync_control_quietly"), \
             mock.patch.object(serial, "repository_binding_ready", return_value=True), \
             mock.patch.object(serial.agent_operator, "is_disabled", return_value=False), \
             mock.patch.object(agentd, "recover_stale_claims"), \
             mock.patch.object(agentd, "recover_invalid_task_files"), \
             mock.patch.object(serial, "handle_repository_control"), \
             mock.patch.object(serial, "publish_repository_status"), \
             mock.patch.object(agentd, "publish_run_state"), \
             mock.patch.object(core, "publish_result", side_effect=lambda task_id, result: captured.append((task_id, result))), \
             mock.patch.object(agentd, "execute_task") as execute:
            self.assertTrue(serial.poll_repository_once(self.repo))
        execute.assert_not_called()
        self.assertEqual(captured[0][0], "a")
        self.assertEqual(captured[0][1]["failure_reason"], "host_targeted_intake_disabled")
        self.assertFalse((self.repo.control / ".agent/results/a.json").exists())
        # A missing target in the selected mailbox is also a terminal pre-claim refusal.
        (self.repo.control / ".agent/hosts/worker-a/tasks/a.json").unlink()
        captured.clear()
        with mock.patch.object(serial, "validate_repository_checkouts"), \
             mock.patch.object(serial, "sync_control_quietly"), \
             mock.patch.object(serial, "repository_binding_ready", return_value=True), \
             mock.patch.object(serial.agent_operator, "is_disabled", return_value=False), \
             mock.patch.object(agentd, "recover_stale_claims"), \
             mock.patch.object(agentd, "recover_invalid_task_files"), \
             mock.patch.object(serial, "handle_repository_control"), \
             mock.patch.object(serial, "publish_repository_status"), \
             mock.patch.object(agentd, "publish_run_state"), \
             mock.patch.object(core, "publish_result", side_effect=lambda task_id, result: captured.append((task_id, result))), \
             mock.patch.object(agentd, "execute_task") as execute:
            self.assertTrue(serial.poll_repository_once(self.repo))
        execute.assert_not_called()
        self.assertEqual(captured[0][1]["failure_reason"], "host_target_missing")

    def test_parallel_worker_never_executes_host_task_before_enrollment(self):
        self.put(".agent/hosts/worker-a/tasks/a.json", self.task("a"))
        with mock.patch.object(serial, "validate_repository_checkouts"), \
             mock.patch.object(serial, "sync_control_quietly"), \
             mock.patch.object(parallel, "_repository_binding_ready", return_value=True), \
             mock.patch.object(parallel.agent_operator, "is_disabled", return_value=False), \
             mock.patch.object(agentd, "recover_stale_claims"), \
             mock.patch.object(agentd, "recover_invalid_task_files"), \
             mock.patch.object(serial, "handle_repository_control"), \
             mock.patch.object(serial, "publish_repository_status"), \
             mock.patch.object(serial, "reject_host_task") as reject, \
             mock.patch.object(agentd, "execute_task") as execute:
            self.assertFalse(parallel.poll_repository_once(self.repo))
        execute.assert_not_called()
        self.assertEqual(reject.call_args.args[2], "host_targeted_intake_disabled")

    def test_symlink_to_other_host_mailbox_is_rejected(self):
        other = self.repo.control / ".agent/hosts/worker-b/tasks"
        other.mkdir(parents=True)
        (other / "b.json").write_text(json.dumps(self.task("b", host_id="worker-b")), encoding="utf-8")
        alias = self.repo.control / ".agent/hosts/worker-a/tasks"
        alias.parent.mkdir(parents=True)
        alias.symlink_to(other, target_is_directory=True)
        serial.bind_repository(self.repo)
        with self.assertRaisesRegex(ValueError, "symlink"):
            agentd.pending_tasks()
        alias.unlink()
        alias.symlink_to(self.repo.control / ".agent/hosts/worker-b", target_is_directory=True)
        with mock.patch.object(core, "publish_control_json"):
            with self.assertRaisesRegex(ValueError, "symlink"):
                agentd.safe_control_directory(agentd.REMOTE_TASKS_DIR)

    def test_two_disposable_git_clones_publish_isolated_results(self):
        """Two checkout fixtures use one bare control remote; neither runs commands."""
        bare = self.root / "offline-bare.git"
        seed = self.root / "offline-seed"
        def git(*arguments, cwd=None):
            return subprocess.run(
                ["git", *arguments], cwd=cwd, capture_output=True, text=True,
                check=True, timeout=10,
            ).stdout.strip()

        git("init", "--bare", "-q", str(bare))
        git("init", "-q", "-b", "agent-control", str(seed))
        git("config", "user.name", "Disposable Test", cwd=seed)
        git("config", "user.email", "fixture@example.invalid", cwd=seed)
        git("remote", "add", "origin", str(bare), cwd=seed)
        for host in ("worker-a", "worker-b"):
            task = self.task(f"task-{host}", host_id=host)
            path = seed / f".agent/hosts/{host}/tasks/{task['id']}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(task), encoding="utf-8")
        git("add", ".agent", cwd=seed)
        git("commit", "-q", "-m", "Disposable task fixture", cwd=seed)
        git("push", "-q", "origin", "agent-control", cwd=seed)
        instances = []
        for host in ("worker-a", "worker-b"):
            clone = self.root / f"clone-{host}"
            git("clone", "-q", "--branch", "agent-control", str(bare), str(clone))
            git("config", "user.name", "Disposable Test", cwd=clone)
            git("config", "user.email", "fixture@example.invalid", cwd=clone)
            instances.append((host, clone))

        for host, clone in instances:
            local = replace(self.repo, host_id=host, control=clone)
            serial.bind_repository(local)
            pending = agentd.pending_tasks()
            self.assertEqual([task["id"] for _, task in pending], [f"task-{host}"])
            result = {"id": f"task-{host}", "status": "failed",
                      "failure_reason": "host_targeted_intake_disabled",
                      "task_digest": agentd.task_digest(pending[0][1])}
            core.publish_result(result["id"], result)

        verifier = self.root / "verify-clone"
        git("clone", "-q", "--branch", "agent-control", str(bare), str(verifier))
        for host in ("worker-a", "worker-b"):
            result = json.loads((verifier / f".agent/hosts/{host}/results/task-{host}.json").read_text())
            self.assertEqual(result["failure_reason"], "host_targeted_intake_disabled")
        self.assertFalse((verifier / ".agent/results").exists())
        serial.bind_repository(replace(self.repo, host_id="worker-a", control=verifier))
        self.assertEqual(agentd.pending_tasks(), [])

    def test_claim_recovery_is_host_scoped_and_never_replays(self):
        self.put(".agent/hosts/worker-a/tasks/a.json", self.task("a"))
        self.put(".agent/hosts/worker-b/tasks/b.json", self.task("b", host_id="worker-b"))
        serial.bind_repository(self.repo)
        self.assertIsNotNone(agentd.claim_task(self.task("a")))
        with mock.patch.object(core, "publish_result", side_effect=lambda task_id, result: self.put(
            f"{agentd.REMOTE_RESULTS_DIR}/{task_id}.json", result)):
            agentd.recover_stale_claims()
        self.assertTrue((self.repo.control / ".agent/hosts/worker-a/results/a.json").exists())
        self.assertFalse((self.repo.control / ".agent/hosts/worker-b/results/b.json").exists())
        self.assertEqual(agentd.pending_tasks(), [])
        serial.bind_repository(replace(self.repo, host_id="worker-b"))
        self.assertEqual([task["id"] for _, task in agentd.pending_tasks()], ["b"])


if __name__ == "__main__":
    unittest.main()
