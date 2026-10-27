"""Isolated real-Git producer -> pinned operator -> host worker -> result reader.

The only mocks replace the genuine GitHub origin check with an ephemeral bare
repository, the install checkout overlap check and the ambient emergency state.
Real Git refs, SHA pins, grants, durable ledger, executor and Git publication run.
No network credentials, production host enrollment or project registry required.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import shlex
import subprocess
import time
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import local_agent.daemon.service as agentd
from local_agent.cli import diagnostics
from local_agent.foundation.process import ExecutionLeaseBusy
from local_agent.supervisor import host_remote_client
import local_agent.operator.remote as operator_remote
import local_agent.repository.worker as repository_worker
from local_agent.repository.host_routing import HostIdentity
from local_agent.runtime.task_contract import task_digest
from local_agent.supervisor import host_remote_worker as remote_worker
from local_agent.supervisor import host_remote_intake as remote_intake
from local_agent.supervisor.host_control import HostControlSource
from local_agent.supervisor.macos_release_admission import CONFIG as MAC_ADMISSION_CONFIG
from local_agent.supervisor.resources import (
    MachineResourceBusy, machine_resource_lease, resource_lock_dir,
)


class HostRemoteRealGitEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(prefix="host-real-git-e2e-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.remote = self.root / "private-origin.git"
        self.producer = self.root / "producer"
        self.operator = self.root / "operator"
        self.checkout = self.root / "consumer"
        self.installed = self.root / "installed"
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        # Enroll only this disposable fixture in native Mac release admission.
        # Keep the inherited production REQUIRED flag intact and fail closed elsewhere.
        admission = self.state / MAC_ADMISSION_CONFIG
        admission.write_text(json.dumps({"version": 1, "enabled": True}) + "\n")
        admission.chmod(0o600)
        self.work = self.root / "ordinary-work"
        self.work.mkdir()
        self.identity = HostIdentity(
            "e2e-host", "12345678-1234-4234-8234-123456789abc",
        )
        self.host = HostControlSource(
            self.identity.host_id, self.identity.installation_id, self.checkout,
        )
        self._git(self.root, "init", "-q", "--bare", str(self.remote))
        for name, branch in (("producer", "agent-control"), ("operator", "operator-control")):
            root = getattr(self, name)
            self._git(self.root, "init", "-q", "-b", branch, str(root))
            self._git(root, "config", "user.name", "Local test")
            self._git(root, "config", "user.email", "local@example.invalid")
            self._git(root, "remote", "add", "origin", str(self.remote))
        self.task_id = "remote-e2e-one"
        self.task = self._task(self.task_id)
        self._publish_task(self.task)
        self._git(self.root, "clone", "-q", "-b", "agent-control",
                  str(self.remote), str(self.checkout))
        self._git(self.root, "clone", "-q", "-b", "agent-control",
                  str(self.remote), str(self.installed))
        self._git(self.checkout, "config", "user.name", "Local consumer")
        self._git(self.checkout, "config", "user.email", "consumer@example.invalid")

        identity = self.state / "identity.json"
        identity.write_text(json.dumps(self.identity.as_record()))
        identity.chmod(0o600)
        self._private_json("supervisor-control.json", {
            "version": 1, "enabled": True, "host_id": self.identity.host_id,
            "identity_file": str(identity), "control_checkout": str(self.checkout),
        })
        self._private_json("host-task-intake.json", {
            "version": 1, "enabled": True, "host_id": self.identity.host_id,
            "installation_id": self.identity.installation_id,
            "identity_file": str(identity),
        })
        self._publish_grants([self.task])

    @staticmethod
    def _git(cwd: Path, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, text=True, capture_output=True,
            check=True, timeout=20,
        )
        return proc.stdout.strip()

    def _private_json(self, name: str, record: dict) -> None:
        path = self.state / name
        path.write_text(json.dumps(record))
        path.chmod(0o600)

    def _task(self, task_id: str) -> dict:
        return {
            "version": 1, "id": task_id,
            "target_host_id": self.identity.host_id,
            "target_installation_id": self.identity.installation_id,
            "cwd": str(self.work),
            "commands": ["printf x >> effect-count.txt"],
            "resources": [], "command_timeout": 20, "task_timeout": 90,
        }

    def _publish_task(self, task: dict, *, staged: bool = False) -> None:
        path = self.producer / self.host.namespace / "tasks" / f"{task['id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if staged:
            self.assertEqual(json.loads(path.read_text()), task)
        else:
            path.write_text(json.dumps(task, sort_keys=True))
        self._git(self.producer, "add", ".agent")
        self._git(self.producer, "commit", "-q", "-m", "producer task")
        # The worker may have committed the previous result since the
        # producer's last push. Rebase before publication, without force or
        # rewriting any consumer result.
        if self._git(
            self.producer, "ls-remote", "--heads", "origin", "refs/heads/agent-control",
        ):
            self._git(self.producer, "pull", "-q", "--rebase", "origin", "agent-control")
        self._git(self.producer, "push", "-q", "origin", "agent-control")

    def _publish_grants(self, tasks: list[dict]) -> str:
        root = self.operator / ".agent" / "operator"
        root.mkdir(parents=True, exist_ok=True)
        hosts = {"version": 1, "hosts": [{
            **self.identity.as_record(), "enabled": True, "repositories": [],
        }]}
        grants = {"version": 1, "grants": [{
            "host_id": self.identity.host_id,
            "installation_id": self.identity.installation_id,
            "task_id": t["id"], "task_digest": task_digest(t),
        } for t in tasks]}
        (root / "hosts.json").write_text(json.dumps(hosts))
        (root / "host-commands.json").write_text(json.dumps(grants))
        self._git(self.operator, "add", ".agent/operator")
        self._git(self.operator, "commit", "-q", "-m", "independent operator authorization")
        self._git(self.operator, "push", "-q", "origin", "operator-control")
        sha = self._git(self.operator, "rev-parse", "HEAD")
        pin = self.state / "operator-control.sha"
        if not pin.exists():
            pin.write_text(sha + "\n")
            pin.chmod(0o600)
        return sha

    def _execute(self, task_id: str) -> str:
        # Real Git origin provenance is checked in normal code. An ephemeral
        # local bare Git fixture deliberately has a different origin URL.
        with (
            mock.patch.object(operator_remote, "_require_trusted_host_registry_origin"),
            mock.patch.object(remote_intake, "validate_host_control_checkout"),
            mock.patch.object(remote_worker, "validate_host_control_checkout"),
            mock.patch.object(remote_worker.operator_local, "is_disabled", return_value=False),
            mock.patch.object(repository_worker, "repository_lease_dir",
                              return_value=self.state / "locks" / "repositories"),
            mock.patch.object(remote_worker.supervisor_control, "bind_supervisor_control"),
            mock.patch.object(agentd.core, "CONTROL", self.checkout),
            mock.patch.object(agentd.core, "CONTROL_BRANCH", "agent-control"),
        ):
            return remote_worker.run_remote_host_task_once(
                task_id, state_dir=self.state, operator_checkout=self.installed,
                production_checkout=self.installed,
            )

    def test_committed_unpushed_result_retries_after_conflict_without_reexecution(self) -> None:
        with mock.patch.object(agentd.storage, "publish_control_commit",
                               side_effect=RuntimeError("push failed after local commit")):
            with self.assertRaisesRegex(RuntimeError, "push failed"):
                self._execute(self.task_id)
        result_path = self.state / "host-tasks/results" / f"{self.task_id}.json"
        durable = result_path.read_bytes()
        local_result = self.checkout / self.host.namespace / "task-results" / result_path.name
        self.assertTrue(local_result.is_file())
        self.assertEqual((self.work / "effect-count.txt").read_text(), "x")
        # Another producer advances the remote while this result exists only
        # in the consumer's commit. A new worker must recover publication.
        self._publish_task(self._task("rival-task"))
        self.assertEqual(remote_worker.pending_host_task_ids(self.host, self.state)[0],
                         self.task_id)
        with mock.patch.object(remote_worker, "preview_authorized_remote_host_task",
                               side_effect=AssertionError("must reuse durable result")):
            self.assertEqual(self._execute(self.task_id), "published")
        self.assertEqual(result_path.read_bytes(), durable)
        self.assertEqual((self.work / "effect-count.txt").read_text(), "x")
        remote_result = json.loads(self._git(
            self.remote, "show", f"agent-control:{self.host.namespace}/task-results/{result_path.name}",
        ))
        self.assertEqual(remote_result["result"], json.loads(durable))
        self.assertNotIn(self.task_id, remote_worker.pending_host_task_ids(self.host, self.state))

    def test_lost_push_ack_does_not_requeue_a_remotely_published_result(self) -> None:
        publish = agentd.storage.publish_control_commit

        def lose_ack(*args, **kwargs):
            publish(*args, **kwargs)
            raise RuntimeError("push acknowledgement lost")

        with mock.patch.object(agentd.storage, "publish_control_commit", side_effect=lose_ack):
            with self.assertRaisesRegex(RuntimeError, "acknowledgement lost"):
                self._execute(self.task_id)
        self.assertNotIn(self.task_id, remote_worker.pending_host_task_ids(self.host, self.state))
        self.assertEqual((self.work / "effect-count.txt").read_text(), "x")
        remote_result = json.loads(self._git(
            self.remote, "show", f"agent-control:{self.host.namespace}/task-results/{self.task_id}.json",
        ))
        self.assertEqual(remote_result["status"], "done")

    def test_six_historical_timeout_shapes_publish_rejections_without_claims(self) -> None:
        config = self.state / "host-task-intake.json"
        self._private_json(config.name, {
            **json.loads(config.read_text()), "authorization": "github_writers",
        })
        for number, limits in enumerate((
            {"idle_timeout": 3650}, {"idle_timeout": 3650},
            {"command_timeout": 75, "task_timeout": 125},
            {"command_timeout": 180, "task_timeout": 220},
            {"command_timeout": 125, "task_timeout": 175},
            {"command_timeout": 130, "task_timeout": 185},
        )):
            with self.subTest(limits=limits):
                task_id = f"invalid-timeout-{number}"
                task = {**self._task(task_id), **limits}
                self._publish_task(task)
                self.assertEqual(self._execute(task_id), "published")
                base = self.state / "host-tasks"
                result_path = base / "results" / f"{task_id}.json"
                result_bytes = result_path.read_bytes()
                result = json.loads(result_bytes)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["failure_reason"], "invalid_task_file")
                self.assertEqual(result["task_digest"], task_digest(task))
                self.assertIsNone(result["started_at"])
                self.assertEqual(result["commands"], [])
                self.assertFalse((base / "claims" / f"{task_id}.json").exists())
                self.assertFalse((self.work / "effect-count.txt").exists())
                published = self.checkout / self.host.namespace / "task-results" / result_path.name
                self.assertEqual(json.loads(published.read_text())["result"], result)
                self.assertNotIn(task_id, remote_worker.pending_host_task_ids(self.host, self.state))
                old_head = self._git(self.checkout, "rev-parse", "HEAD")
                with mock.patch.object(remote_worker, "preview_authorized_remote_host_task",
                                       side_effect=AssertionError("revalidation after rejection")):
                    self.assertEqual(self._execute(task_id), "published")
                self.assertEqual(self._git(self.checkout, "rev-parse", "HEAD"), old_head)
                self.assertEqual(result_path.read_bytes(), result_bytes)

    def test_missing_checkout_local_git_author_blocks_effect_until_provisioned(self) -> None:
        # An inherited Git identity is NOT enough for the dedicated host
        # control writer; failure must precede task approval and OS effects.
        self._git(self.checkout, "config", "--local", "--unset", "user.email")
        effect = self.work / "effect-count.txt"
        with self.assertRaisesRegex(ValueError, "local Git user.email"):
            self._execute(self.task_id)
        self.assertFalse(effect.exists())
        self.assertFalse((self.state / "host-tasks" / "claims" / f"{self.task_id}.json").exists())
        self.assertFalse((self.state / "host-tasks" / "remote-approvals" / f"{self.task_id}.json").exists())

        self._git(self.checkout, "config", "--local", "user.email", "consumer@example.invalid")
        self.assertEqual(self._execute(self.task_id), "published")
        self.assertEqual(effect.read_text(), "x")
        self.assertEqual(self._execute(self.task_id), "published")
        self.assertEqual(effect.read_text(), "x")

    def _advance_control_writer(self) -> str:
        (self.producer / "concurrent-marker").write_text("preserved\n")
        self._git(self.producer, "add", "concurrent-marker")
        self._git(self.producer, "commit", "-qm", "independent remote writer")
        self._git(self.producer, "push", "-q", "origin", "agent-control")
        return self._git(self.producer, "rev-parse", "HEAD")

    def test_worker_fast_forwards_clean_stale_checkout_before_preview(self) -> None:
        before = self._git(self.checkout, "rev-parse", "HEAD")
        advanced = self._advance_control_writer()
        self.assertNotEqual(before, advanced)
        self.assertEqual(self._execute(self.task_id), "published")
        self.assertEqual((self.checkout / "concurrent-marker").read_text(), "preserved\n")
        self.assertEqual((self.work / "effect-count.txt").read_text(), "x")
        self.assertEqual(self._execute(self.task_id), "published")
        self.assertEqual((self.work / "effect-count.txt").read_text(), "x")

    def test_remote_writer_during_preview_defers_before_claim_then_recovers(self) -> None:
        real_git = remote_intake._git
        observations = 0

        def advance_during_preview(checkout, *args, **kwargs):
            nonlocal observations
            if args[0] == "ls-remote":
                observations += 1
                if observations == 2:
                    self._advance_control_writer()
            return real_git(checkout, *args, **kwargs)

        with mock.patch.object(remote_intake, "_git", side_effect=advance_during_preview):
            with self.assertRaises(remote_intake.HostControlSnapshotChanged):
                self._execute(self.task_id)
        self.assertFalse((self.state / "host-tasks").exists())
        self.assertFalse((self.work / "effect-count.txt").exists())
        self.assertEqual(self._execute(self.task_id), "published")
        self.assertEqual((self.work / "effect-count.txt").read_text(), "x")

    def test_resource_deferral_survives_unrelated_remote_commit(self) -> None:
        with mock.patch.object(remote_worker.host_tasks, "machine_resource_lease",
                               side_effect=MachineResourceBusy("fixture")):
            self.assertEqual(self._execute(self.task_id), "deferred")
        receipt_path = self.state / "host-tasks" / "remote-approvals" / f"{self.task_id}.json"
        original_receipt = receipt_path.read_bytes()
        self._advance_control_writer()
        self.assertEqual(self._execute(self.task_id), "published")
        self.assertEqual(receipt_path.read_bytes(), original_receipt)
        self.assertEqual((self.work / "effect-count.txt").read_text(), "x")

    def test_refresh_preserves_dirty_work_and_index_without_claim(self) -> None:
        tracked = self.checkout / self.host.namespace / "tasks" / f"{self.task_id}.json"
        original = tracked.read_bytes()
        for kind in ("untracked", "unstaged", "staged"):
            with self.subTest(kind=kind):
                if kind == "untracked":
                    dirty = self.checkout / "owner-notes"
                else:
                    dirty = tracked
                dirty.write_bytes(b"preserve these bytes\n")
                if kind == "staged":
                    self._git(self.checkout, "add", str(tracked.relative_to(self.checkout)))
                head = self._git(self.checkout, "rev-parse", "HEAD")
                index = self._git(self.checkout, "diff", "--cached")
                status = self._git(self.checkout, "status", "--porcelain")
                with self.assertRaisesRegex(ValueError, "local changes"):
                    self._execute(self.task_id)
                self.assertEqual(self._git(self.checkout, "rev-parse", "HEAD"), head)
                self.assertEqual(self._git(self.checkout, "diff", "--cached"), index)
                self.assertEqual(self._git(self.checkout, "status", "--porcelain"), status)
                self.assertEqual(dirty.read_bytes(), b"preserve these bytes\n")
                self.assertFalse((self.state / "host-tasks").exists())
                if kind == "untracked":
                    dirty.unlink()
                else:
                    if kind == "staged":
                        self._git(self.checkout, "restore", "--staged", "--", str(tracked.relative_to(self.checkout)))
                    tracked.write_bytes(original)
        self.assertFalse((self.work / "effect-count.txt").exists())

    def test_refresh_preserves_unpublished_commit_and_interrupted_rebase(self) -> None:
        marker = self.checkout / ".git" / "rebase-merge"
        marker.mkdir()
        with self.assertRaisesRegex(RuntimeError, "unfinished Git"):
            self._execute(self.task_id)
        self.assertTrue(marker.is_dir())
        marker.rmdir()
        (self.checkout / "local-only").write_text("unpublished\n")
        self._git(self.checkout, "add", "local-only")
        self._git(self.checkout, "commit", "-qm", "unpublished fixture commit")
        head = self._git(self.checkout, "rev-parse", "HEAD")
        self._advance_control_writer()
        with self.assertRaises(ValueError):
            self._execute(self.task_id)
        self.assertEqual(self._git(self.checkout, "rev-parse", "HEAD"), head)
        self.assertEqual((self.checkout / "local-only").read_text(), "unpublished\n")
        self.assertFalse((self.work / "effect-count.txt").exists())
        self.assertFalse((self.state / "host-tasks").exists())

    def test_two_independent_workers_contend_then_publish_one_effect(self) -> None:
        code = '''
import sys
from pathlib import Path
from unittest import mock
import local_agent.daemon.service as agentd
import local_agent.operator.remote as operator_remote
import local_agent.repository.worker as repository_worker
from local_agent.supervisor import host_remote_intake, host_remote_worker

state, checkout, installed, task_id = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
preview = host_remote_worker.preview_authorized_remote_host_task
def hold_preview(*args, **kwargs):
    print("READY", flush=True)
    assert sys.stdin.readline().strip() == "CONTINUE"
    return preview(*args, **kwargs)
with (
    mock.patch.object(operator_remote, "_require_trusted_host_registry_origin"),
    mock.patch.object(host_remote_intake, "validate_host_control_checkout"),
    mock.patch.object(host_remote_worker, "validate_host_control_checkout"),
    mock.patch.object(host_remote_worker.operator_local, "is_disabled", return_value=False),
    mock.patch.object(repository_worker, "repository_lease_dir", return_value=state / "locks" / "repositories"),
    mock.patch.object(host_remote_worker.supervisor_control, "bind_supervisor_control"),
    mock.patch.object(agentd.core, "CONTROL", checkout),
    mock.patch.object(agentd.core, "CONTROL_BRANCH", "agent-control"),
    mock.patch.object(host_remote_worker, "preview_authorized_remote_host_task", side_effect=hold_preview),
):
    assert host_remote_worker.run_remote_host_task_once(task_id, state_dir=state,
        operator_checkout=installed, production_checkout=installed) == "published"
'''
        env = {k: v for k, v in os.environ.items() if k not in (
            "LOCAL_AGENT_LEASE_FDS", "LOCAL_AGENT_LEASE_KEYS_DIGEST",
            "LOCAL_AGENT_RESOURCE_LEASE_FDS",
        )}
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", code, str(self.state), str(self.checkout),
             str(self.installed), self.task_id],
            env=env, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            import select

            self.assertTrue(select.select([proc.stdout], [], [], 15)[0], "worker startup timed out")
            line = proc.stdout.readline().strip()
            if line != "READY":
                output, _ = proc.communicate(timeout=5)
                self.fail(line + "\n" + output)
            with self.assertRaises(ExecutionLeaseBusy):
                self._execute(self.task_id)
            self.assertFalse((self.state / "host-tasks").exists())
            self.assertFalse((self.work / "effect-count.txt").exists())
            output, _ = proc.communicate("CONTINUE\n", timeout=25)
            self.assertEqual(proc.returncode, 0, output)
            self.assertEqual(self._execute(self.task_id), "published")
            self.assertEqual((self.work / "effect-count.txt").read_text(), "x")
            commits = self._git(self.checkout, "log", "--format=%s").splitlines()
            self.assertEqual(commits.count(f"Host task result: {self.task_id}"), 1)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate(timeout=5)

    def test_owner_github_one_local_choice_executes_without_per_task_grants(self) -> None:
        record = json.loads((self.state / "host-task-intake.json").read_text())
        record["authorization"] = "github_writers"
        self._private_json("host-task-intake.json", record)
        # A fresh host-only task is intentionally not in operator grants.
        task = self._task("owner-github-command")
        self._publish_task(task)
        self._git(self.checkout, "pull", "-q", "--ff-only")
        (self.state / "operator-control.sha").unlink()
        self.assertEqual(self._execute(task["id"]), "published")
        self.assertEqual((self.work / "effect-count.txt").read_text(), "x")
        receipt = json.loads((self.state / "host-tasks" / "remote-approvals" /
                              f"{task['id']}.json").read_text())
        self.assertEqual(receipt["operator_sha"], "github_writers")
        self.assertEqual(receipt["host_id"], self.identity.host_id)
        self.assertEqual(receipt["installation_id"], self.identity.installation_id)
        self.assertEqual(self._execute(task["id"]), "published")
        self.assertEqual((self.work / "effect-count.txt").read_text(), "x")

    def test_independent_operator_grant_real_git_once_and_revocation(self) -> None:
        self.assertEqual(
            remote_worker.pending_host_task_ids(self.host, self.state),
            (self.task_id,),
        )
        self.assertEqual(self._execute(self.task_id), "published")
        effect = self.work / "effect-count.txt"
        self.assertEqual(effect.read_text(), "x")
        result_path = f"{self.host.namespace}/task-results/{self.task_id}.json"
        payload = json.loads(self._git(
            self.root, "--git-dir", str(self.remote),
            "show", f"refs/heads/agent-control:{result_path}",
        ))
        self.assertEqual(payload["target_host_id"], self.identity.host_id)
        self.assertEqual(payload["target_installation_id"], self.identity.installation_id)
        self.assertEqual(payload["task_digest"], task_digest(self.task))
        self.assertEqual(payload["result"]["status"], "done")
        self.assertEqual(self._execute(self.task_id), "published")
        self.assertEqual(effect.read_text(), "x")
        self.assertFalse((self.checkout / ".agent" / "tasks").exists())

        # A producer can publish another task but cannot extend operator grants.
        other = self._task("remote-e2e-unapproved")
        self._publish_task(other)
        self._git(self.checkout, "pull", "-q", "--ff-only")
        with self.assertRaisesRegex(ValueError, "not approved"):
            self._execute(other["id"])
        self.assertEqual(effect.read_text(), "x")

        # Revoking the independent operator ref with an unchanged local pin
        # fails closed even for a previously approved but not-yet-run task.
        eligible = self._task("remote-e2e-revoked")
        self._publish_grants([self.task, eligible])
        self._publish_task(eligible)
        self._git(self.checkout, "pull", "-q", "--ff-only")
        with self.assertRaises((ValueError, RuntimeError)):
            self._execute(eligible["id"])
        self.assertEqual(effect.read_text(), "x")


    def test_cli_stage_existing_worker_independent_git_result_readback(self) -> None:
        """CLI producer -> real Git -> exact grant -> executor -> fresh reader."""
        task = self._task("remote-e2e-cli")
        with contextlib.redirect_stdout(io.StringIO()) as buffer:
            args = diagnostics.build_parser().parse_args([
                "host-stage", "--host-id", self.identity.host_id,
                "--installation-id", self.identity.installation_id,
                "--output-root", str(self.producer),
                str(self.root / "task-draft.json"),
            ])
            draft = self.root / "task-draft.json"
            draft.write_text(json.dumps(task))
            self.assertEqual(args.func(args), 0)
        staged = json.loads(buffer.getvalue())
        self.assertEqual(staged["status"], "staged_only")
        self.assertFalse(staged["published"])
        self.assertEqual(staged["task_digest"], task_digest(task))
        with self.assertRaises(FileExistsError):
            host_remote_client.stage_host_command(
                task, host_id=self.identity.host_id,
                installation_id=self.identity.installation_id,
                output_root=self.producer,
            )
        with mock.patch.object(host_remote_client, "_require_control_origin"):
            arguments = diagnostics.build_parser().parse_args([
                "host-publish", task["id"],
                "--host-id", self.identity.host_id,
                "--installation-id", self.identity.installation_id,
                "--producer-checkout", str(self.producer),
                "--expected-control-sha", self._git(self.producer, "rev-parse", "HEAD"),
                "--expected-task-digest", task_digest(task),
            ])
            with contextlib.redirect_stdout(io.StringIO()) as published_output:
                self.assertEqual(arguments.func(arguments), 0)
        publication = json.loads(published_output.getvalue())
        self.assertEqual(publication["status"], "published")
        self.assertEqual(
            publication["control_sha"],
            self._git(self.root, "--git-dir", str(self.remote),
                      "rev-parse", "refs/heads/agent-control"),
        )
        pinned = self._publish_grants([self.task, task])
        # Fixture-only independent approval: not a real operator-control pin.
        (self.state / "operator-control.sha").write_text(pinned + "\n")
        self._git(self.checkout, "pull", "-q", "--ff-only")
        self.assertEqual(self._execute(task["id"]), "published")
        self.assertEqual((self.work / "effect-count.txt").read_text(), "x")

        independent = self.root / "independent-reader"
        self._git(self.root, "clone", "-q", "-b", "agent-control",
                  str(self.remote), str(independent))
        result_sha = self._git(independent, "rev-parse", "HEAD")

        def readback(*, digest: str, installation: str, sha: str) -> tuple[int, dict]:
            arguments = diagnostics.build_parser().parse_args([
                "host-result", task["id"],
                "--host-id", self.identity.host_id,
                "--installation-id", installation,
                "--control-checkout", str(independent),
                "--expected-control-sha", sha,
                "--expected-task-digest", digest,
            ])
            with contextlib.redirect_stdout(io.StringIO()) as out:
                with mock.patch.object(host_remote_client, "_require_control_origin"):
                    code = arguments.func(arguments)
            return code, json.loads(out.getvalue())

        code, read = readback(
            digest=task_digest(task),
            installation=self.identity.installation_id,
            sha=result_sha,
        )
        self.assertEqual(code, 0, read)
        self.assertEqual(read["status"], "verified_snapshot")
        self.assertEqual(read["result"]["status"], "done")
        self.assertEqual(read["result"]["target_installation_id"],
                         self.identity.installation_id)
        for invalid in (
            {"digest": "a" * 64, "installation": self.identity.installation_id,
             "sha": result_sha},
            {"digest": task_digest(task),
             "installation": "12345678-1234-4234-8234-123456789abd",
             "sha": result_sha},
            {"digest": task_digest(task),
             "installation": self.identity.installation_id, "sha": "f" * 40},
        ):
            code, denied = readback(**invalid)
            self.assertEqual(code, 2)
            self.assertEqual(denied["status"], "unverified")
        self.assertEqual(self._execute(task["id"]), "published")
        self.assertEqual((self.work / "effect-count.txt").read_text(), "x")


    def test_host_publisher_rejects_dirty_index_wrong_digest_and_stale_remote(self) -> None:
        task = self._task("remote-e2e-publish-negative")
        host_remote_client.stage_host_command(
            task, host_id=self.identity.host_id,
            installation_id=self.identity.installation_id, output_root=self.producer,
        )
        head = self._git(self.producer, "rev-parse", "HEAD")
        extra = self.producer / "unrelated-file"
        extra.write_text("unrelated")
        with mock.patch.object(host_remote_client, "_require_control_origin"):
            with self.assertRaisesRegex(ValueError, "unrelated"):
                host_remote_client.publish_staged_host_command(
                    HostControlSource(self.identity.host_id, self.identity.installation_id,
                                      self.producer),
                    task_id=task["id"], expected_control_sha=head,
                    expected_task_digest=task_digest(task),
                )
            extra.unlink()
            with self.assertRaisesRegex(ValueError, "digest"):
                host_remote_client.publish_staged_host_command(
                    HostControlSource(self.identity.host_id, self.identity.installation_id,
                                      self.producer),
                    task_id=task["id"], expected_control_sha=head,
                    expected_task_digest="a" * 64,
                )
            # Independent actor advances remote. Publisher never pulls or rebases.
            racer = self.root / "racer"
            self._git(self.root, "clone", "-q", "-b", "agent-control",
                      str(self.remote), str(racer))
            self._git(racer, "config", "user.name", "Local racer")
            self._git(racer, "config", "user.email", "racer@example.invalid")
            (racer / "unrelated.txt").write_text("new upstream commit")
            self._git(racer, "add", "unrelated.txt")
            self._git(racer, "commit", "-qm", "upstream race")
            self._git(racer, "push", "-q", "origin", "agent-control")
            with self.assertRaisesRegex(ValueError, "current remote HEAD"):
                host_remote_client.publish_staged_host_command(
                    HostControlSource(self.identity.host_id, self.identity.installation_id,
                                      self.producer),
                    task_id=task["id"], expected_control_sha=head,
                    expected_task_digest=task_digest(task),
                )
        self.assertFalse((self.work / "effect-count.txt").exists())
        self.assertFalse((self.producer / ".git" / "MERGE_HEAD").exists())
        published = self._git(
            self.root, "--git-dir", str(self.remote), "ls-tree", "-r",
            "--name-only", "refs/heads/agent-control",
        )
        self.assertNotIn(task["id"] + ".json", published)

    def test_host_publisher_rejects_non_fast_forward_race_without_force(self) -> None:
        task = self._task("remote-e2e-raced-push")
        source = HostControlSource(self.identity.host_id, self.identity.installation_id,
                                   self.producer)
        host_remote_client.stage_host_command(
            task, host_id=self.identity.host_id,
            installation_id=self.identity.installation_id, output_root=self.producer,
        )
        head = self._git(self.producer, "rev-parse", "HEAD")
        racer = self.root / "race-writer"
        self._git(self.root, "clone", "-q", "-b", "agent-control",
                  str(self.remote), str(racer))
        self._git(racer, "config", "user.name", "Local racer")
        self._git(racer, "config", "user.email", "racer@example.invalid")
        (racer / "race-marker").write_text("preserved")
        self._git(racer, "add", "race-marker")
        self._git(racer, "commit", "-qm", "race ahead")
        original = host_remote_client._git_write
        raced = False

        def publication_race(checkout: Path, *argv: str) -> str:
            nonlocal raced
            if argv and argv[0] == "push" and not raced:
                raced = True
                self._git(racer, "push", "-q", "origin", "agent-control")
            return original(checkout, *argv)

        with (
            mock.patch.object(host_remote_client, "_require_control_origin"),
            mock.patch.object(host_remote_client, "_git_write",
                              side_effect=publication_race),
        ):
            with self.assertRaisesRegex(RuntimeError, "uncertain"):
                host_remote_client.publish_staged_host_command(
                    source, task_id=task["id"], expected_control_sha=head,
                    expected_task_digest=task_digest(task),
                )
        self.assertTrue(raced)
        self.assertEqual(
            self._git(self.root, "--git-dir", str(self.remote),
                      "show", "refs/heads/agent-control:race-marker"),
            "preserved",
        )
        published = self._git(
            self.root, "--git-dir", str(self.remote), "ls-tree", "-r",
            "--name-only", "refs/heads/agent-control",
        )
        self.assertNotIn(task["id"] + ".json", published)
        self.assertFalse((self.work / "effect-count.txt").exists())


    def _crash_worker_child_at_result_publication(
        self, task_id: str, *, after_push: bool,
    ) -> None:
        """SIGKILL a separate worker, never the test runner or live daemon."""
        code = """
import contextlib
import os
import signal
import sys
from pathlib import Path
from unittest import mock

import local_agent.daemon.service as agentd
import local_agent.operator.remote as operator_remote
import local_agent.repository.worker as repository_worker
from local_agent.supervisor import host_remote_intake, host_remote_worker
from local_agent.supervisor.host_tasks import _load_private_request

state, checkout, installed, task_id, work, after_push = (
    Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]),
    sys.argv[4], Path(sys.argv[5]), sys.argv[6] == "1",
)
real_publish = agentd.publish_control_json

def kill_after_durable_result(*args, **kwargs):
    result = _load_private_request(
        state / "host-tasks" / "results" / f"{task_id}.json"
    )
    assert result["id"] == task_id and result["status"] == "done"
    assert (work / "effect-count.txt").read_text() == "x"
    if after_push:
        real_publish(*args, **kwargs)
    os.kill(os.getpid(), signal.SIGKILL)
    raise AssertionError("SIGKILL unexpectedly returned")

with (
    mock.patch.object(operator_remote, "_require_trusted_host_registry_origin"),
    mock.patch.object(host_remote_intake, "validate_host_control_checkout"),
    mock.patch.object(host_remote_worker, "validate_host_control_checkout"),
    mock.patch.object(host_remote_worker.operator_local, "is_disabled", return_value=False),
    mock.patch.object(repository_worker, "repository_lease_dir",
                      return_value=state / "locks" / "repositories"),
    mock.patch.object(host_remote_worker.supervisor_control, "bind_supervisor_control"),
    mock.patch.object(agentd.core, "CONTROL", checkout),
    mock.patch.object(agentd.core, "CONTROL_BRANCH", "agent-control"),
    mock.patch.object(agentd, "publish_control_json",
                      side_effect=kill_after_durable_result),
    mock.patch("local_agent.runtime.telemetry._safe_command", return_value=None),
):
    host_remote_worker.run_remote_host_task_once(
        task_id, state_dir=state, operator_checkout=installed,
        production_checkout=installed,
    )
raise AssertionError("worker completed without publication kill")
"""
        process = subprocess.run(
            [
                sys.executable, "-c", code,
                str(self.state), str(self.checkout), str(self.installed),
                task_id, str(self.work), str(int(after_push)),
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=os.environ.copy(),
            capture_output=True, text=True, timeout=100,
        )
        self.assertEqual(
            process.returncode, -signal.SIGKILL,
            f"worker did not reach SIGKILL publication boundary: "
            f"{process.returncode}\n{process.stdout}\n{process.stderr}",
        )

    def test_sigkill_after_durable_result_before_git_push_recovers_once(self) -> None:
        task = self._task("remote-e2e-killed-before-push")
        pin = self._publish_grants([self.task, task])
        (self.state / "operator-control.sha").write_text(pin + "\n")
        self._publish_task(task)
        self._git(self.checkout, "pull", "-q", "--ff-only")
        self._crash_worker_child_at_result_publication(task["id"], after_push=False)

        result_path = (
            self.state / "host-tasks" / "results" / f"{task['id']}.json"
        )
        self.assertEqual(json.loads(result_path.read_text())["status"], "done")
        self.assertEqual((self.work / "effect-count.txt").read_text(), "x")
        relative = f"{self.host.namespace}/task-results/{task['id']}.json"
        original_remote_sha = self._git(
            self.root, "--git-dir", str(self.remote), "rev-parse",
            "refs/heads/agent-control",
        )
        self.assertNotIn(
            relative,
            self._git(self.root, "--git-dir", str(self.remote),
                      "ls-tree", "-r", "--name-only",
                      "refs/heads/agent-control"),
        )

        self.assertEqual(self._execute(task["id"]), "published")
        self.assertEqual((self.work / "effect-count.txt").read_text(), "x")
        self.assertNotEqual(
            self._git(self.root, "--git-dir", str(self.remote), "rev-parse",
                      "refs/heads/agent-control"), original_remote_sha,
        )
        record = json.loads(self._git(
            self.root, "--git-dir", str(self.remote), "show",
            f"refs/heads/agent-control:{relative}",
        ))
        self.assertEqual(record["status"], "done")
        self.assertEqual(record["task_digest"], task_digest(task))
        self.assertEqual(record["target_installation_id"], self.identity.installation_id)

    def test_sigkill_after_git_push_before_ack_keeps_exactly_once(self) -> None:
        task = self._task("remote-e2e-killed-after-push")
        pin = self._publish_grants([self.task, task])
        (self.state / "operator-control.sha").write_text(pin + "\n")
        self._publish_task(task)
        self._git(self.checkout, "pull", "-q", "--ff-only")
        self._crash_worker_child_at_result_publication(task["id"], after_push=True)

        relative = f"{self.host.namespace}/task-results/{task['id']}.json"
        pushed = self._git(
            self.root, "--git-dir", str(self.remote), "rev-parse",
            "refs/heads/agent-control",
        )
        record = json.loads(self._git(
            self.root, "--git-dir", str(self.remote), "show",
            f"refs/heads/agent-control:{relative}",
        ))
        self.assertEqual(record["status"], "done")
        self.assertEqual(record["task_digest"], task_digest(task))
        self.assertEqual((self.work / "effect-count.txt").read_text(), "x")
        self.assertEqual(self._execute(task["id"]), "published")
        self.assertEqual((self.work / "effect-count.txt").read_text(), "x")
        self.assertEqual(
            self._git(self.root, "--git-dir", str(self.remote), "rev-parse",
                      "refs/heads/agent-control"), pushed,
            "ack recovery must not create another remote commit",
        )

    def test_ack_loss_recovery_preserves_concurrent_git_writer(self) -> None:
        """After result push/ACK loss, a new remote commit is never overwritten."""
        task = self._task("remote-e2e-ack-race")
        pin = self._publish_grants([self.task, task])
        (self.state / "operator-control.sha").write_text(pin + "\n")
        self._publish_task(task)
        self._git(self.checkout, "pull", "-q", "--ff-only")
        self._crash_worker_child_at_result_publication(task["id"], after_push=True)
        result_relative = f"{self.host.namespace}/task-results/{task['id']}.json"
        initial = self._git(self.root, "--git-dir", str(self.remote),
                            "rev-parse", "refs/heads/agent-control")
        racer = self.root / "concurrent-after-ack-writer"
        self._git(self.root, "clone", "-q", "-b", "agent-control",
                  str(self.remote), str(racer))
        self._git(racer, "config", "user.name", "Concurrent fixture")
        self._git(racer, "config", "user.email", "fixture@example.invalid")
        (racer / "unrelated-concurrent-receipt").write_text("preserved\n")
        self._git(racer, "add", "unrelated-concurrent-receipt")
        self._git(racer, "commit", "-qm", "unrelated concurrent result")
        self._git(racer, "push", "-q", "origin", "agent-control")
        raced_head = self._git(racer, "rev-parse", "HEAD")
        self.assertNotEqual(initial, raced_head)

        self.assertEqual(self._execute(task["id"]), "published")
        self.assertEqual((self.work / "effect-count.txt").read_text(), "x")
        self.assertEqual(
            self._git(self.root, "--git-dir", str(self.remote),
                      "rev-parse", "refs/heads/agent-control"),
            raced_head, "recovery must not add a redundant result commit",
        )
        self.assertEqual(
            self._git(self.root, "--git-dir", str(self.remote),
                      "show", "refs/heads/agent-control:unrelated-concurrent-receipt"),
            "preserved",
        )
        published = json.loads(self._git(
            self.root, "--git-dir", str(self.remote),
            "show", f"refs/heads/agent-control:{result_relative}",
        ))
        self.assertEqual(published["status"], "done")
        self.assertEqual(published["task_digest"], task_digest(task))

    def test_recovery_push_time_conflict_rebases_result_once(self) -> None:
        """Concurrent remote push between pull/push must preserve both commits."""
        task = self._task("remote-e2e-recovery-push-race")
        pin = self._publish_grants([self.task, task])
        (self.state / "operator-control.sha").write_text(pin + "\n")
        self._publish_task(task)
        self._git(self.checkout, "pull", "-q", "--ff-only")
        self._crash_worker_child_at_result_publication(task["id"], after_push=False)
        self.assertEqual((self.work / "effect-count.txt").read_text(), "x")
        remote_before = self._git(self.root, "--git-dir", str(self.remote),
                                  "rev-parse", "refs/heads/agent-control")
        racer = self.root / "concurrent-during-result-push"
        self._git(self.root, "clone", "-q", "-b", "agent-control",
                  str(self.remote), str(racer))
        self._git(racer, "config", "user.name", "Concurrent fixture")
        self._git(racer, "config", "user.email", "fixture@example.invalid")
        (racer / "concurrent-marker").write_text("preserved\n")
        self._git(racer, "add", "concurrent-marker")
        self._git(racer, "commit", "-qm", "concurrent fixture push")
        racer_sha = self._git(racer, "rev-parse", "HEAD")
        real_process = agentd.core.process
        injected = False

        def push_race(args, *positional, **kwargs):
            nonlocal injected
            if args[:2] == ["git", "push"] and not injected:
                injected = True
                self._git(racer, "push", "-q", "origin", "agent-control")
            return real_process(args, *positional, **kwargs)

        with mock.patch.object(agentd.core, "process", side_effect=push_race):
            self.assertEqual(self._execute(task["id"]), "published")
        self.assertTrue(injected, "expected a real competing remote Git push")
        self.assertEqual((self.work / "effect-count.txt").read_text(), "x")
        remote_after = self._git(self.root, "--git-dir", str(self.remote),
                                 "rev-parse", "refs/heads/agent-control")
        self.assertNotEqual(remote_after, remote_before)
        self.assertEqual(
            self._git(self.root, "--git-dir", str(self.remote),
                      "merge-base", remote_after, racer_sha),
            racer_sha, "remote competitor must be an ancestor of the final head",
        )
        self.assertEqual(
            self._git(self.root, "--git-dir", str(self.remote),
                      "show", "refs/heads/agent-control:concurrent-marker"),
            "preserved",
        )
        commits = self._git(self.root, "--git-dir", str(self.remote),
                            "log", "--format=%s",
                            f"{racer_sha}..refs/heads/agent-control").splitlines()
        self.assertEqual(commits, [f"Host task result: {task['id']}"],
                         "recovery should publish exactly one terminal result commit")
        result_relative = f"{self.host.namespace}/task-results/{task['id']}.json"
        terminal = json.loads(self._git(
            self.root, "--git-dir", str(self.remote),
            "show", f"refs/heads/agent-control:{result_relative}",
        ))
        self.assertEqual(terminal["status"], "done")
        self.assertEqual(terminal["task_digest"], task_digest(task))
        self.assertEqual(self._execute(task["id"]), "published")
        self.assertEqual(
            self._git(self.root, "--git-dir", str(self.remote),
                      "rev-parse", "refs/heads/agent-control"), remote_after,
        )
        self.assertEqual((self.work / "effect-count.txt").read_text(), "x")

    def test_actual_git_remote_outage_after_durable_result_recovers_without_replay(self) -> None:
        """Temporarily remove only the disposable bare remote at publication."""
        task = self._task("remote-e2e-real-offline-publish")
        pin = self._publish_grants([self.task, task])
        (self.state / "operator-control.sha").write_text(pin + "\n")
        self._publish_task(task)
        self._git(self.checkout, "pull", "-q", "--ff-only")
        offline = self.root / "bare-git-temporarily-offline"
        real_publish = agentd.publish_control_json
        dropped = False

        def unavailable_remote(*args, **kwargs):
            nonlocal dropped
            if not dropped:
                self.remote.rename(offline)
                dropped = True
            return real_publish(*args, **kwargs)

        try:
            with mock.patch.object(agentd, "publish_control_json",
                                   side_effect=unavailable_remote):
                with self.assertRaisesRegex(RuntimeError, "git|fatal|repository"):
                    self._execute(task["id"])
        finally:
            if offline.exists():
                offline.rename(self.remote)
        self.assertTrue(dropped)
        self.assertEqual((self.work / "effect-count.txt").read_text(), "x")
        result_path = (
            self.state / "host-tasks" / "results" / f"{task['id']}.json"
        )
        saved = json.loads(result_path.read_text())
        self.assertEqual(saved["status"], "done")
        self.assertEqual(saved["task_digest"], task_digest(task))
        relative = f"{self.host.namespace}/task-results/{task['id']}.json"
        self.assertNotIn(
            relative,
            self._git(self.root, "--git-dir", str(self.remote),
                      "ls-tree", "-r", "--name-only", "refs/heads/agent-control"),
        )
        # A subsequent independent worker invocation can publish the
        # already-fsynced result once when the Git remote comes back.
        self.assertEqual(self._execute(task["id"]), "published")
        published_head = self._git(
            self.root, "--git-dir", str(self.remote),
            "rev-parse", "refs/heads/agent-control",
        )
        record = json.loads(self._git(
            self.root, "--git-dir", str(self.remote),
            "show", f"refs/heads/agent-control:{relative}",
        ))
        self.assertEqual(record["status"], "done")
        self.assertEqual(record["task_digest"], task_digest(task))
        self.assertEqual((self.work / "effect-count.txt").read_text(), "x")
        self.assertEqual(self._execute(task["id"]), "published")
        self.assertEqual(
            self._git(self.root, "--git-dir", str(self.remote),
                      "rev-parse", "refs/heads/agent-control"),
            published_head, "online retry must not create a second Git commit",
        )

    def test_publication_outage_republishes_durable_result_without_reexecution(self) -> None:
        task = self._task("remote-e2e-offline-result")
        pin = self._publish_grants([self.task, task])
        # Fixture-only pin advance: production operator pins remain untouched.
        (self.state / "operator-control.sha").write_text(pin + "\n")
        self._publish_task(task)
        self._git(self.checkout, "pull", "-q", "--ff-only")
        real_publish = agentd.publish_control_json
        attempts = 0

        def fail_once(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("simulated result push outage")
            return real_publish(*args, **kwargs)

        with mock.patch.object(agentd, "publish_control_json", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "push outage"):
                self._execute(task["id"])
            self.assertEqual((self.work / "effect-count.txt").read_text(), "x")
            self.assertEqual(self._execute(task["id"]), "published")
        self.assertEqual(attempts, 2)
        self.assertEqual((self.work / "effect-count.txt").read_text(), "x")
        reader = self.root / "outage-result-reader"
        self._git(self.root, "clone", "-q", "-b", "agent-control",
                  str(self.remote), str(reader))
        with mock.patch.object(host_remote_client, "_require_control_origin"):
            observed = host_remote_client.read_host_command_result(
                HostControlSource(
                    self.identity.host_id, self.identity.installation_id, reader,
                ),
                task_id=task["id"], expected_task_digest=task_digest(task),
                expected_control_sha=self._git(reader, "rev-parse", "HEAD"),
            )
        self.assertEqual(observed["result"]["status"], "done")

    def test_sigkill_entire_worker_during_running_command_recovers_unknown_once(self) -> None:
        """Kill a real worker while its shell is active, then recover its claim."""
        task = self._task("remote-e2e-worker-killed-while-running")
        task["resources"] = ["board:orphan-worker-qa"]
        command = (
            "from pathlib import Path; import os,time; "
            "Path('effect-count.txt').open('a').write('x'); "
            "Path('command-pgid.txt').write_text(str(os.getpgrp())); "
            "time.sleep(60)"
        )
        task["commands"] = [shlex.quote(sys.executable) + " -c " + shlex.quote(command)]
        pin = self._publish_grants([self.task, task])
        (self.state / "operator-control.sha").write_text(pin + "\n")
        self._publish_task(task)
        self._git(self.checkout, "pull", "-q", "--ff-only")

        worker_code = """
import sys
from pathlib import Path
from unittest import mock

import local_agent.daemon.service as agentd
import local_agent.operator.remote as operator_remote
import local_agent.repository.worker as repository_worker
from local_agent.supervisor import host_remote_intake, host_remote_worker

state, checkout, installed, task_id = (
    Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4],
)
with (
    mock.patch.object(operator_remote, "_require_trusted_host_registry_origin"),
    mock.patch.object(host_remote_intake, "validate_host_control_checkout"),
    mock.patch.object(host_remote_worker, "validate_host_control_checkout"),
    mock.patch.object(host_remote_worker.operator_local, "is_disabled", return_value=False),
    mock.patch.object(repository_worker, "repository_lease_dir",
                      return_value=state / "locks" / "repositories"),
    mock.patch.object(host_remote_worker.supervisor_control, "bind_supervisor_control"),
    mock.patch.object(agentd.core, "CONTROL", checkout),
    mock.patch.object(agentd.core, "CONTROL_BRANCH", "agent-control"),
    mock.patch("local_agent.runtime.telemetry._safe_command", return_value=None),
):
    host_remote_worker.run_remote_host_task_once(
        task_id, state_dir=state, operator_checkout=installed,
        production_checkout=installed,
    )
"""
        env = dict(os.environ)
        for name in ("LOCAL_AGENT_LEASE_FDS", "LOCAL_AGENT_RESOURCE_LEASE_FDS"):
            env.pop(name, None)
        worker = subprocess.Popen(
            [sys.executable, "-c", worker_code, str(self.state),
             str(self.checkout), str(self.installed), task["id"]],
            cwd=Path(__file__).resolve().parents[1], env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        command_group = None
        try:
            marker = self.work / "command-pgid.txt"
            claim = self.state / "host-tasks" / "claims" / f"{task['id']}.json"
            private_result = self.state / "host-tasks" / "results" / f"{task['id']}.json"
            deadline = time.monotonic() + 25.0
            while time.monotonic() < deadline:
                if marker.is_file() and claim.is_file():
                    break
                if worker.poll() is not None:
                    self.fail(f"disposable worker terminated early: {worker.returncode}")
                time.sleep(0.05)
            self.assertTrue(marker.exists(), "command never started")
            self.assertTrue(claim.exists(), "claim was not durably established")
            command_group = int(marker.read_text())
            self.assertNotEqual(command_group, os.getpgrp())
            self.assertFalse(private_result.exists())
            self.assertEqual((self.work / "effect-count.txt").read_text(), "x")

            os.kill(worker.pid, signal.SIGKILL)
            self.assertEqual(worker.wait(timeout=8), -signal.SIGKILL)

            # Despite the dead parent worker, its still-running command
            # inherited the resource lease FDs. Other work MUST defer until
            # the abandoned command exits; claiming UNKNOWN alone must never
            # release a live device to a second executor.
            with self.assertRaisesRegex(MachineResourceBusy, "board:orphan-worker-qa"):
                with machine_resource_lease(
                    task, lock_dir=resource_lock_dir(self.state),
                    command_env={}, process_env={},
                ):
                    self.fail("orphan command lost its machine resource fence")

            # A fresh worker recovers the prior durable claim without
            # running even one byte of the interrupted command again.
            self.assertEqual(self._execute(task["id"]), "published")
            self.assertEqual((self.work / "effect-count.txt").read_text(), "x")
            result = json.loads(private_result.read_text())
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(result["failure_reason"], "interrupted_execution")
            self.assertIs(result["replayed"], False)
            self.assertEqual(result["task_digest"], task_digest(task))
            self.assertEqual(self._execute(task["id"]), "published")
            self.assertEqual((self.work / "effect-count.txt").read_text(), "x")
            relative = f"{self.host.namespace}/task-results/{task['id']}.json"
            remote = json.loads(self._git(
                self.root, "--git-dir", str(self.remote),
                "show", f"refs/heads/agent-control:{relative}",
            ))
            self.assertEqual(remote["status"], "unknown")
            self.assertEqual(remote["task_digest"], task_digest(task))
        finally:
            # Executor commands run in a separate process group. Do not
            # leave the sleeping, deliberately interrupted child behind.
            if command_group is not None and command_group != os.getpgrp():
                try:
                    os.killpg(command_group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if worker.poll() is None:
                os.killpg(worker.pid, signal.SIGKILL)
            worker.wait(timeout=10)

    def test_abandoned_claim_publishes_unknown_without_running_task_again(self) -> None:
        task = self._task("remote-e2e-interrupted")
        pin = self._publish_grants([self.task, task])
        # Fixture-only pin advance: production operator pins remain untouched.
        (self.state / "operator-control.sha").write_text(pin + "\n")
        self._publish_task(task)
        self._git(self.checkout, "pull", "-q", "--ff-only")

        def simulated_interruption(*_args, **_kwargs):
            base = self.state / "host-tasks" / "claims"
            base.mkdir(parents=True, mode=0o700, exist_ok=True)
            remote_worker.host_tasks._durable_exclusive_claim(
                base / f"{task['id']}.json",
                {"id": task["id"], "task_digest": task_digest(task)},
            )
            raise RuntimeError("simulated crash after durable claim")

        with mock.patch.object(
            remote_worker.host_tasks, "run_local_host_task",
            side_effect=simulated_interruption,
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                self._execute(task["id"])
        self.assertFalse((self.work / "effect-count.txt").exists())
        self.assertEqual(self._execute(task["id"]), "published")
        self.assertFalse((self.work / "effect-count.txt").exists())
        reader = self.root / "unknown-result-reader"
        self._git(self.root, "clone", "-q", "-b", "agent-control",
                  str(self.remote), str(reader))
        with mock.patch.object(host_remote_client, "_require_control_origin"):
            observed = host_remote_client.read_host_command_result(
                HostControlSource(
                    self.identity.host_id, self.identity.installation_id, reader,
                ),
                task_id=task["id"], expected_task_digest=task_digest(task),
                expected_control_sha=self._git(reader, "rev-parse", "HEAD"),
            )
        self.assertEqual(observed["result"]["status"], "unknown")
        self.assertFalse(observed["result"]["replayed"])
        self.assertEqual(self._execute(task["id"]), "published")
        self.assertFalse((self.work / "effect-count.txt").exists())



if __name__ == "__main__":
    unittest.main()
