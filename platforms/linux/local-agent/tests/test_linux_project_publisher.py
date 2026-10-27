"""Offline local bare-Git result publisher tests: no project command execution."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from linux_agent.repository.journal import ProjectJournal
from linux_agent.repository.projects import load_project_registry
from linux_agent.repository.publisher import ProjectPublishError, ProjectResultPublisher

HOST = "pi-zero2w"
INSTALL = "9dfe2220-65ae-4624-b6db-8d9649fb2f8b"
BINDING = "b0e94f93-478f-4fc6-abed-159d3702362f"


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *(["-C", str(cwd)] if cwd else []), *args],
        capture_output=True, text=True, check=True, timeout=20,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return result.stdout.strip()


class ProjectResultPublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temp)
        self.remote = self.root / "remote.git"
        self.producer = self.root / "producer"
        self.control = self.root / "user-control"
        git("init", "--bare", "-q", str(self.remote))
        git("init", "-q", "-b", "agent-control", str(self.producer))
        git("config", "user.name", "Test", cwd=self.producer)
        git("config", "user.email", "test@invalid.local", cwd=self.producer)
        git("remote", "add", "origin", str(self.remote), cwd=self.producer)
        (self.producer / "README.md").write_text("user project\n")
        git("add", ".", cwd=self.producer)
        git("commit", "-q", "-m", "initial project", cwd=self.producer)
        git("push", "-q", "-u", "origin", "agent-control", cwd=self.producer)
        git("clone", "-q", "-b", "agent-control", str(self.remote), str(self.control))
        self.registry = self.root / "registry.json"
        self.registry.write_text(json.dumps({"version": 1, "repositories": [{
            "id": "example-project", "repository": "example-owner/example-project",
            "agent_binding": BINDING, "host_id": HOST,
            "host_mailbox_enabled": True, "host_execution_enabled": True,
            "host_identity_file": str(self.root / "identity"),
            "control_dir": str(self.control),
            "work_dir": str(self.root / "user-work"),
            "checkpoints_dir": str(self.root / "checkpoints"),
        }]}))
        self.project, = load_project_registry(
            self.registry, home=self.root, expected_host_id=HOST)
        self.journal = ProjectJournal(
            self.root / "state", self.project,
            host_id=HOST, installation_id=INSTALL)
        self.publisher = ProjectResultPublisher(self.journal, origin=str(self.remote))
        self.digest = hashlib.sha256(b"immutable-task-bytes").hexdigest()

    def spool(self, task_id="job1", status="done") -> bytes:
        self.journal.claim(task_id, self.digest)
        return self.journal.record_result(
            task_id, self.digest, {"status": status, "output": "offline-test"})

    def remote_result(self, task_id="job1") -> dict:
        git("fetch", "-q", "origin", "agent-control", cwd=self.producer)
        sha = git("rev-parse", "refs/remotes/origin/agent-control", cwd=self.producer)
        path = f".agent/hosts/{HOST}/results/{task_id}.json"
        return json.loads(git("show", sha + ":" + path, cwd=self.producer))

    def test_durable_spool_only_publishes_exact_bytes_and_never_touches_user_control(self):
        data = self.spool()
        before = git("rev-parse", "HEAD", cwd=self.control)
        self.assertEqual(self.publisher.publish_pending(), 1)
        self.assertEqual(self.remote_result()["task_digest"], self.digest)
        self.assertEqual(self.publisher._remote_result(
            "job1", self.publisher._refresh()), data)
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.control), before)
        self.assertEqual(git("status", "--porcelain", cwd=self.control), "")
        self.assertFalse((self.control / ".agent").exists())

    def test_second_publication_is_idempotent_and_no_extra_git_commit(self):
        self.spool()
        self.publisher.publish_pending()
        first = git("ls-remote", str(self.remote), "refs/heads/agent-control")
        self.publisher.publish_pending()
        self.assertEqual(first, git("ls-remote", str(self.remote), "refs/heads/agent-control"))

    def test_ambiguous_push_which_reached_remote_is_confirmed_by_readback(self):
        self.spool()
        original = self.publisher._git
        simulated = []

        def lost_ack(*args, **kwargs):
            if args and args[0] == "push" and not simulated:
                simulated.append(1)
                original(*args, **kwargs)  # Git accepted push, ACK is lost
                raise ProjectPublishError("lost ACK after successful push")
            return original(*args, **kwargs)

        self.publisher._git = lost_ack
        self.assertEqual(self.publisher.publish_pending(), 1)
        self.assertEqual(simulated, [1])
        self.assertEqual(self.remote_result()["status"], "done")

    def test_remote_conflict_fails_without_overwriting_durable_result(self):
        data = self.spool()
        conflict = self.producer / ".agent" / "hosts" / HOST / "results/job1.json"
        conflict.parent.mkdir(parents=True)
        conflict.write_text('{"status":"other"}')
        git("add", ".", cwd=self.producer)
        git("commit", "-q", "-m", "foreign result", cwd=self.producer)
        git("push", "-q", "origin", "agent-control", cwd=self.producer)
        with self.assertRaisesRegex(ProjectPublishError, "conflict"):
            self.publisher.publish_pending()
        self.assertEqual(self.journal.verified_results(), [("job1", data)])
        self.assertEqual(self.remote_result()["status"], "other")

    def test_git_push_outage_retains_spool_and_recovery_does_not_reexecute(self):
        data = self.spool()
        original = self.publisher._git

        def offline(*args, **kwargs):
            if args and args[0] == "push":
                raise ProjectPublishError("test offline")
            return original(*args, **kwargs)

        self.publisher._git = offline
        with self.assertRaisesRegex(ProjectPublishError, "unconfirmed"):
            self.publisher.publish_pending()
        self.assertEqual(self.journal.verified_results(), [("job1", data)])
        self.publisher._git = original
        self.assertEqual(self.publisher.publish_pending(), 1)
        self.assertEqual(self.remote_result()["status"], "done")

    def test_concurrent_unrelated_remote_commit_is_preserved(self):
        self.spool()
        original = self.publisher._git
        concurrent = []

        def remote_moves_before_push(*args, **kwargs):
            if args and args[0] == "push" and not concurrent:
                concurrent.append(True)
                (self.producer / "other-writer.txt").write_text("keep me" + chr(10))
                git("add", ".", cwd=self.producer)
                git("commit", "-q", "-m", "unrelated concurrent writer",
                    cwd=self.producer)
                git("push", "-q", "origin", "agent-control", cwd=self.producer)
            return original(*args, **kwargs)

        self.publisher._git = remote_moves_before_push
        self.assertEqual(self.publisher.publish_pending(), 1)
        self.assertEqual(concurrent, [True])
        self.assertEqual(self.remote_result()["status"], "done")
        sha = git("rev-parse", "refs/remotes/origin/agent-control",
                  cwd=self.producer)
        self.assertEqual(git("show", sha + ":other-writer.txt",
                             cwd=self.producer), "keep me")

    def test_git_fetch_outage_preserves_spool_and_recovers(self):
        data = self.spool()
        original = self.publisher._git

        def fetch_offline(*args, **kwargs):
            if args and args[0] == "fetch":
                raise ProjectPublishError("network unavailable during fetch")
            return original(*args, **kwargs)

        self.publisher._git = fetch_offline
        with self.assertRaisesRegex(ProjectPublishError, "network unavailable"):
            self.publisher.publish_pending()
        self.assertEqual(self.journal.verified_results(), [("job1", data)])
        self.publisher._git = original
        self.assertEqual(self.publisher.publish_pending(), 1)
        self.assertEqual(self.remote_result()["status"], "done")

    def test_unverified_bytes_cannot_be_published(self):
        data = self.spool()
        with self.assertRaisesRegex(ValueError, "not verified"):
            self.publisher.publish_result("job1", data + b"changed")
        self.assertFalse(self.publisher.checkout.exists())

    def test_marker_origin_or_checkout_tampering_fails_closed(self):
        self.spool()
        self.publisher.initialize()
        marker = json.loads(self.publisher.marker.read_text())
        marker["origin"] = "/another/private/repository"
        self.publisher.marker.write_text(json.dumps(marker))
        with self.assertRaisesRegex(ValueError, "marker"):
            self.publisher.publish_pending()
        self.assertFalse((self.control / ".agent").exists())


if __name__ == "__main__":
    unittest.main()
