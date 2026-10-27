"""Offline disposable project intake revalidation against a real bare Git remote."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from linux_agent.repository.admission import ProjectAdmissionGate, ProjectStopRequested
from linux_agent.repository.intake import ProjectGitIntake, ProjectTaskStale
from linux_agent.repository.journal import ProjectJournal
from linux_agent.repository.projects import load_project_registry

HOST = "pi-zero2w"
INSTALLATION = "9dfe2220-65ae-4624-b6db-8d9649fb2f8b"
BINDING = "b0e94f93-478f-4fc6-abed-159d3702362f"


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *(["-C", str(cwd)] if cwd else []), *args],
        capture_output=True, text=True, check=True, timeout=20,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return result.stdout.strip()


class ProjectGitIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.tmp)
        self.origin = self.root / "origin.git"
        self.producer = self.root / "producer"
        self.owner_control = self.root / "owner-control"
        self.owner_work = self.root / "owner-work"
        self.owner_work.mkdir()
        git("init", "--bare", "-q", str(self.origin))
        git("init", "-q", "-b", "agent-control", str(self.producer))
        git("config", "user.name", "Fixture", cwd=self.producer)
        git("config", "user.email", "fixture@local.invalid", cwd=self.producer)
        git("remote", "add", "origin", str(self.origin), cwd=self.producer)
        (self.producer / "README.md").write_text("initial\n")
        git("add", ".", cwd=self.producer)
        git("commit", "-q", "-m", "initial", cwd=self.producer)
        git("push", "-q", "-u", "origin", "agent-control", cwd=self.producer)
        git("clone", "-q", "-b", "agent-control", str(self.origin),
            str(self.owner_control))
        self.registry = self.root / "registry.json"
        self.registry.write_text(json.dumps({"version": 1, "repositories": [{
            "id": "fixture", "repository": "local/fixture",
            "agent_binding": BINDING, "host_id": HOST,
            "host_mailbox_enabled": True, "host_execution_enabled": True,
            "host_identity_file": str(self.root / "identity"),
            "control_dir": str(self.owner_control),
            "work_dir": str(self.owner_work),
            "checkpoints_dir": str(self.root / "checkpoints"),
        }]}))
        (project,) = load_project_registry(
            self.registry, home=self.root, expected_host_id=HOST)
        self.journal = ProjectJournal(
            self.root / "state", project, host_id=HOST,
            installation_id=INSTALLATION)
        self.intake = ProjectGitIntake(self.journal, origin=str(self.origin))
        self.gate = ProjectAdmissionGate(self.journal)

    def write_remote(self, task_id="job", *, result=False, **changes):
        path = (self.producer / ".agent" / "hosts" / HOST /
                ("results" if result else "tasks") / (task_id + ".json"))
        path.parent.mkdir(parents=True, exist_ok=True)
        task = {
            "version": 1, "id": task_id, "repository_id": "fixture",
            "agent_binding": BINDING, "target_host_id": HOST,
            "target_installation_id": INSTALLATION,
            "mode": "commands", "commands": ["printf fixture"],
            "resources": [],
        } | changes
        path.write_text(json.dumps(task, sort_keys=True))
        git("add", ".", cwd=self.producer)
        git("commit", "-q", "-m", "update", cwd=self.producer)
        git("push", "-q", "origin", "agent-control", cwd=self.producer)

    def test_exact_fresh_pending_task_admitted_without_touching_owner(self):
        self.write_remote()
        before = git("status", "--porcelain", cwd=self.owner_control)
        stale_ref = git("rev-parse", "refs/remotes/origin/agent-control",
                        cwd=self.owner_control)
        candidate, = self.intake.pending_tasks()
        with self.gate.admit(candidate, preclaim_check=self.intake.revalidate):
            self.assertTrue(self.journal._claim_path("job").is_file())
        self.assertEqual(before, git("status", "--porcelain", cwd=self.owner_control))
        self.assertEqual(stale_ref, git("rev-parse",
                          "refs/remotes/origin/agent-control", cwd=self.owner_control))
        self.assertFalse(any(self.owner_work.iterdir()))

    def test_changed_remote_task_denied_before_claim(self):
        self.write_remote()
        candidate, = self.intake.pending_tasks()
        self.write_remote(commands=["printf changed"])
        with self.assertRaisesRegex(ProjectTaskStale, "changed"):
            with self.gate.admit(candidate, preclaim_check=self.intake.revalidate):
                pass
        self.assertFalse(self.journal._claim_path("job").exists())

    def test_remote_result_supersedes_candidate(self):
        self.write_remote()
        candidate, = self.intake.pending_tasks()
        self.write_remote(result=True, status="done")
        with self.assertRaisesRegex(ProjectTaskStale, "no longer pending"):
            with self.gate.admit(candidate, preclaim_check=self.intake.revalidate):
                pass
        self.assertFalse(self.journal._claim_path("job").exists())

    def test_remote_unavailable_does_not_claim(self):
        self.write_remote()
        candidate, = self.intake.pending_tasks()
        self.origin.rename(self.root / "lost.git")
        with self.assertRaisesRegex(ValueError, "Git unavailable"):
            with self.gate.admit(candidate, preclaim_check=self.intake.revalidate):
                pass
        self.assertFalse(self.journal._claim_path("job").exists())

    def test_stop_before_fresh_recheck_denies_without_claim(self):
        self.write_remote()
        candidate, = self.intake.pending_tasks()
        self.gate.request_stop()
        with self.assertRaises(ProjectStopRequested):
            with self.gate.admit(candidate, preclaim_check=self.intake.revalidate):
                pass
        self.assertFalse(self.journal._claim_path("job").exists())

    def test_checkout_and_origin_marker_tamper_fails_closed(self):
        self.write_remote()
        self.intake.pending_tasks()
        marker = json.loads(self.intake.marker.read_text())
        marker["origin"] = "untrusted"
        self.intake.marker.write_text(json.dumps(marker))
        with self.assertRaisesRegex(ValueError, "ownership mismatch"):
            self.intake.pending_tasks()
        self.assertFalse(self.journal._claim_path("job").exists())

    def test_wrong_target_is_never_a_candidate(self):
        self.write_remote(target_installation_id="wrong")
        with self.assertRaisesRegex(ValueError, "installation mismatch"):
            self.intake.pending_tasks()
        self.assertFalse(self.journal._claim_path("job").exists())


if __name__ == "__main__":
    unittest.main()
