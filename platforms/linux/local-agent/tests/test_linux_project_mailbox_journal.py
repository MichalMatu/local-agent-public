"""Offline project Git snapshot + durable journal tests; never run a project task."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from linux_agent.repository.journal import ProjectJournal
from linux_agent.repository.mailbox import ProjectMailbox
from linux_agent.repository.projects import load_project_registry

BINDING = "b0e94f93-478f-4fc6-abed-159d3702362f"
HOST = "pi-zero2w"
INSTALLATION = "9dfe2220-65ae-4624-b6db-8d9649fb2f8b"


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *(["-C", str(cwd)] if cwd else []), *args],
        capture_output=True, text=True, check=True, timeout=15,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return result.stdout.strip()


class ProjectMailboxJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temp)
        self.remote = self.root / "remote.git"
        self.producer = self.root / "producer"
        self.control = self.root / "control"
        self.state = self.root / "private"
        self.registry = self.root / "registry.json"
        git("init", "--bare", "-q", str(self.remote))
        git("init", "-q", "-b", "agent-control", str(self.producer))
        git("config", "user.name", "Local Test", cwd=self.producer)
        git("config", "user.email", "local@example.invalid", cwd=self.producer)
        git("remote", "add", "origin", str(self.remote), cwd=self.producer)
        (self.producer / "README.md").write_text("offline control\n")
        git("add", ".", cwd=self.producer)
        git("commit", "-q", "-m", "init", cwd=self.producer)
        git("push", "-q", "-u", "origin", "agent-control", cwd=self.producer)
        git("clone", "-q", "-b", "agent-control", str(self.remote), str(self.control))
        self.registry.write_text(json.dumps({"version": 1, "repositories": [{
            "id": "example-project", "repository": "example-owner/example-project",
            "agent_binding": BINDING, "host_id": HOST,
            "host_mailbox_enabled": True, "host_execution_enabled": True,
            "host_identity_file": str(self.root / "identity.json"),
            "control_dir": str(self.control),
            "work_dir": str(self.root / "work"),
            "checkpoints_dir": str(self.root / "checkpoints"),
        }]}))
        self.project, = load_project_registry(
            self.registry, home=self.root, expected_host_id=HOST)
        self.mailbox = ProjectMailbox(
            self.project, host_id=HOST, installation_id=INSTALLATION,
            expected_origin=str(self.remote))
        self.journal = ProjectJournal(
            self.state, self.project, host_id=HOST,
            installation_id=INSTALLATION)

    def push(self, task: dict, *, task_id: str = "job1", results: bool = False) -> None:
        subdir = "results" if results else "tasks"
        path = self.producer / ".agent" / "hosts" / HOST / subdir / (task_id + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(task))
        git("add", ".", cwd=self.producer)
        git("commit", "-q", "-m", "mailbox data", cwd=self.producer)
        git("push", "-q", "origin", "agent-control", cwd=self.producer)
        git("fetch", "-q", "origin", "agent-control", cwd=self.control)

    def task(self, **extra) -> dict:
        return {"version": 1, "id": "job1", "agent_binding": BINDING,
                "mode": "commands", "commands": ["echo fixture"], **extra}

    def test_empty_snapshot_returns_no_tasks_and_does_not_write(self):
        before = git("status", "--porcelain", cwd=self.control)
        self.assertEqual(self.mailbox.pending_tasks(), [])
        self.assertEqual(self.mailbox.verify_snapshot(),
                         git("rev-parse", "refs/remotes/origin/agent-control", cwd=self.control))
        self.assertEqual(git("status", "--porcelain", cwd=self.control), before)
        self.assertFalse(self.state.exists())

    def test_b2_host_project_task_is_read_without_claim(self):
        self.push(self.task())
        candidates = self.mailbox.pending_tasks()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].task_id, "job1")
        self.assertEqual(candidates[0].digest, hashlib.sha256(candidates[0].raw).hexdigest())
        self.assertEqual(self.mailbox.pending_tasks(claimed={"job1"}), [])
        self.assertFalse(self.state.exists())

    def test_malformed_project_filename_does_not_starve_valid_git_task(self):
        """An unaddressable inbox filename cannot block a valid later task."""
        self.push(self.task(), task_id="a" * 210)
        self.push(self.task())
        candidates = self.mailbox.pending_tasks()
        self.assertEqual([candidate.task_id for candidate in candidates], ["job1"])
        self.assertFalse(self.state.exists())
        # A second poll sees the same immutable task and no spurious claim.
        self.assertEqual([candidate.task_id for candidate
                          in self.mailbox.pending_tasks()], ["job1"])

    def test_non_utf8_git_basename_does_not_starve_valid_project_task(self):
        self.push(self.task())
        # Git trees permit byte names that the platform worktree cannot create.
        # Put the malformed name in the index only; discovery reads frozen Git.
        blob = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"], cwd=self.producer,
            input=b"{}", capture_output=True, check=True, timeout=10,
        ).stdout.decode("ascii").strip()
        invalid = f".agent/hosts/{HOST}/tasks/a-" + "\udcff" + ".json"
        git("update-index", "--add", "--cacheinfo", "100644", blob, invalid,
            cwd=self.producer)
        git("commit", "-q", "-m", "unaddressable Git basename", cwd=self.producer)
        git("push", "-q", "origin", "agent-control", cwd=self.producer)
        git("fetch", "-q", "origin", "agent-control", cwd=self.control)
        before = git("status", "--porcelain", cwd=self.control)
        for _ in range(2):
            self.assertEqual([c.task_id for c in self.mailbox.pending_tasks()], ["job1"])
            self.assertFalse(self.state.exists())
        self.assertEqual(self.mailbox.pending_tasks(claimed={"job1"}), [])
        self.assertEqual(git("status", "--porcelain", cwd=self.control), before)

    def test_valid_project_basename_with_non_utf8_payload_still_fails_closed(self):
        self.push(self.task())
        path = self.producer / ".agent" / "hosts" / HOST / "tasks/job1.json"
        path.write_bytes(b"\xff")
        git("add", ".", cwd=self.producer)
        git("commit", "-q", "-m", "invalid JSON encoding", cwd=self.producer)
        git("push", "-q", "origin", "agent-control", cwd=self.producer)
        git("fetch", "-q", "origin", "agent-control", cwd=self.control)
        with self.assertRaisesRegex(ValueError, "invalid project task JSON"):
            self.mailbox.pending_tasks()
        self.assertFalse(self.state.exists())

    def test_distinct_project_results_hide_completed_remote_tasks(self):
        self.push(self.task())
        self.push({"id": "job1", "status": "done"}, results=True)
        self.assertEqual(self.mailbox.pending_tasks(), [])
        self.assertIsNotNone(self.mailbox.remote_result("job1"))

    def test_wrong_binding_and_target_are_rejected_before_claim(self):
        for item in ({"agent_binding": "21010ac9-7eaa-47f2-9c58-180ac0a0819c"},
                     {"repository_id": "other"},
                     {"target_host_id": "other"},
                     {"target_installation_id": "other"}):
            with self.subTest(item=item):
                temp = {"version": 1, "id": "job1", "agent_binding": BINDING} | item
                self.push(temp)
                with self.assertRaises(ValueError):
                    self.mailbox.pending_tasks()
                self.assertFalse(self.state.exists())

    def test_exact_origin_and_snapshot_are_mandatory(self):
        wrong = ProjectMailbox(
            self.project, host_id=HOST, installation_id=INSTALLATION,
            expected_origin="other-origin")
        with self.assertRaisesRegex(ValueError, "origin"):
            wrong.verify_snapshot()
        self.control.rename(self.root / "control-old")
        self.control.symlink_to(self.root / "control-old", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "checkout"):
            self.mailbox.verify_snapshot()

    def test_snapshot_is_frozen_when_remote_ref_advances_mid_scan(self):
        self.push(self.task())
        old_snapshot = self.mailbox.verify_snapshot()
        original = self.mailbox._git
        changed = False

        def moved_ref(*args):
            nonlocal changed
            data = original(*args)
            if (not changed and args[:3] == ("ls-tree", "-r", "-z")):
                changed = True
                self.push({"id": "job1", "status": "done"}, results=True)
            return data

        self.mailbox._git = moved_ref
        tasks = self.mailbox.pending_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].snapshot_sha, old_snapshot)
        self.assertNotEqual(old_snapshot, self.mailbox.verify_snapshot())
        self.assertEqual(self.mailbox.pending_tasks(), [])

    def test_nested_project_result_cannot_contradict_claim(self):
        digest = hashlib.sha256(b"task").hexdigest()
        self.journal.claim("job1", digest)
        for changed in (
            {"task_digest": hashlib.sha256(b"other").hexdigest()},
            {"agent_binding": "21010ac9-7eaa-47f2-9c58-180ac0a0819c"},
            {"target_installation_id": "other"},
        ):
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(ValueError, "nested"):
                    self.journal.record_result(
                        "job1", digest, {"status": "done", **changed})
        self.assertEqual(self.journal.verified_results(), [])

    def test_legacy_project_git_namespace_is_separate_from_host_mailbox(self):
        self.push(self.task())
        task_path = f".agent/hosts/{HOST}/tasks/job1.json"
        self.assertIsNotNone(self.mailbox._blob(task_path))
        self.assertIsNone(self.mailbox._blob(
            f".agent/hosts/{HOST}/supervisor/{INSTALLATION}/tasks/job1.json"))

    def test_claim_is_durable_and_duplicate_execution_is_fenced(self):
        digest = hashlib.sha256(b"original task").hexdigest()
        self.journal.claim("job1", digest)
        claim = self.journal._claim_path("job1")
        self.assertTrue(claim.exists())
        self.assertEqual(claim.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.journal.root.stat().st_mode & 0o077)
        with self.assertRaises(FileExistsError):
            self.journal.claim("job1", digest)
        result = self.journal.record_result(
            "job1", digest, {"status": "done", "output": "ok"})
        self.assertEqual(self.journal.record_result(
            "job1", digest, {"status": "done", "output": "ok"}), result)
        self.assertEqual(self.journal.verified_results(), [("job1", result)])
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.journal.record_result("job1", digest, {"status": "failed"})

    def test_wrong_digest_and_identity_are_rejected(self):
        digest = hashlib.sha256(b"task").hexdigest()
        self.journal.claim("job1", digest)
        with self.assertRaisesRegex(ValueError, "digest"):
            self.journal.record_result("job1", hashlib.sha256(b"other").hexdigest(),
                                       {"status": "done"})
        claim = self.journal._claim_path("job1")
        changed = json.loads(claim.read_text())
        changed["repository_id"] = "wrong"
        claim.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, "identity"):
            self.journal.record_result("job1", digest, {"status": "done"})

    def test_crash_recovery_requires_fence_and_never_reexecutes(self):
        digest = hashlib.sha256(b"task").hexdigest()
        self.journal.claim("job1", digest)
        with self.assertRaisesRegex(ValueError, "lease fence"):
            self.journal.recover_unknown()
        self.assertEqual(self.journal.verified_results(), [])
        with self.assertRaisesRegex(RuntimeError, "still running"):
            self.journal.recover_unknown(
                fence=lambda _id: (_ for _ in ()).throw(RuntimeError("still running")))
        self.assertEqual(self.journal.verified_results(), [])
        observed = []
        self.assertEqual(self.journal.recover_unknown(fence=observed.append), 1)
        self.assertEqual(observed, ["job1"])
        result = self.journal.verified_results()[0][1]
        self.assertEqual(json.loads(result)["status"], "unknown")
        self.assertEqual(self.journal.recover_unknown(fence=observed.append), 0)

    def test_spool_symlinks_corruption_and_private_dir_are_rejected(self):
        digest = hashlib.sha256(b"task").hexdigest()
        self.journal.claim("job1", digest)
        result = self.journal._result_path("job1")
        result.parent.mkdir(mode=0o700)
        result.symlink_to(self.root / "unrelated")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.journal.record_result("job1", digest, {"status": "unknown"})
        result.unlink()
        blob = self.journal.record_result("job1", digest, {"status": "unknown"})
        self.assertTrue(blob)
        result.write_text('{"version":1,"version":1}')
        with self.assertRaisesRegex(ValueError, "corrupt"):
            self.journal.verified_results()
        result.unlink()
        (self.journal.root / "claims").chmod(0o755)
        with self.assertRaisesRegex(ValueError, "owner-private"):
            self.journal._read_claim("job1")
        (self.journal.root / "claims").chmod(0o700)


if __name__ == "__main__":
    unittest.main()
