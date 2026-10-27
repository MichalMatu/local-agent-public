"""Real offline Git host+project integration, with one explicit Linux supervisor."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from linux_agent.agent import Agent
from linux_agent.git_transport import GitError
from linux_agent.repository.publisher import ProjectPublishError
from linux_agent.resources import machine_resource_lease, resource_lock_dir

HOST = "fixture-linux"
INSTALLATION = "9dfe2220-65ae-4624-b6db-8d9649fb2f8b"
BINDING = "b0e94f93-478f-4fc6-abed-159d3702362f"


def git(*args, cwd=None):
    out = subprocess.run(["git", *(["-C", str(cwd)] if cwd else []), *args],
                         capture_output=True, text=True, check=True, timeout=20,
                         env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    return out.stdout.strip()


class ProjectTurnTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temp)
        self.state = self.root / "state"
        self.work = self.root / "project-work"
        self.work.mkdir()
        self.origin = self.root / "project.git"
        self.host_origin = self.root / "host.git"
        self.producer = self.root / "producer"
        self.host_producer = self.root / "host-producer"
        for remote, producer in ((self.origin, self.producer),
                                 (self.host_origin, self.host_producer)):
            git("init", "--bare", "-q", str(remote))
            git("init", "-q", "-b", "agent-control", str(producer))
            git("config", "user.name", "Fixture", cwd=producer)
            git("config", "user.email", "fixture@local.invalid", cwd=producer)
            git("remote", "add", "origin", str(remote), cwd=producer)
            (producer / "README.md").write_text("initial\n")
            git("add", ".", cwd=producer)
            git("commit", "-q", "-m", "initial", cwd=producer)
            git("push", "-q", "-u", "origin", "agent-control", cwd=producer)
        self.identity = self.root / "host-identity.json"
        self.identity.write_text(json.dumps({
            "version": 1, "host_id": HOST, "installation_id": INSTALLATION}))
        self.identity.chmod(0o600)
        self.registry = self.root / "registry.json"
        self.registry.write_text(json.dumps({
            "version": 1,
            "repositories": [{
                "id": "example", "repository": "fixture/example",
                "agent_binding": BINDING, "host_id": HOST,
                "host_mailbox_enabled": True, "host_execution_enabled": True,
                "host_identity_file": str(self.identity),
                "control_dir": str(self.root / "owner-control"),
                "work_dir": str(self.work),
                "checkpoints_dir": str(self.root / "checkpoints"),
            }],
        }))
        self.config = {
            "version": 1, "host_id": HOST, "installation_id": INSTALLATION,
            "repository": str(self.host_origin), "branch": "agent-control",
            "enabled": True,
            "host_intake": {"enabled": True, "authorization": "github_writers"},
            "project_turn": {
                "enabled": True, "registry_path": str(self.registry),
                "repository_id": "example", "origin": str(self.origin),
                "max_memory_mb": 1024,
            },
        }

    def new_agent(self, *, enabled=True):
        config = dict(self.config)
        if not enabled:
            config.pop("project_turn")
        return Agent(config, self.state)

    def project_task(self, **changes):
        return {
            "version": 1, "id": "job", "repository_id": "example",
            "agent_binding": BINDING, "target_host_id": HOST,
            "target_installation_id": INSTALLATION, "mode": "commands",
            "commands": ["printf x >> project-ran.txt"], "resources": [],
            "memory_limit_mb": 256,
        } | changes

    def push(self, task=None):
        git("fetch", "-q", "origin", "agent-control", cwd=self.producer)
        git("merge", "--ff-only", "origin/agent-control", cwd=self.producer)
        task = self.project_task() if task is None else task
        path = self.producer / ".agent" / "hosts" / HOST / "tasks" / (task["id"] + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(task))
        git("add", ".", cwd=self.producer)
        git("commit", "-q", "-m", "project task", cwd=self.producer)
        git("push", "-q", "origin", "agent-control", cwd=self.producer)

    def remote_project_result(self, task_id="job"):
        path = f".agent/hosts/{HOST}/results/{task_id}.json"
        rev = git("--git-dir", str(self.origin), "rev-parse",
                  "refs/heads/agent-control")
        try:
            return json.loads(git("--git-dir", str(self.origin),
                                  "show", f"{rev}:{path}"))
        except subprocess.CalledProcessError:
            return None

    def test_malformed_git_basename_does_not_block_project_turn(self):
        """Real Git project turn skips unaddressable names without claiming them."""
        bad_id = "a" * 210
        bad = (self.producer / ".agent" / "hosts" / HOST /
               "tasks" / (bad_id + ".json"))
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text(json.dumps(self.project_task(id=bad_id)))
        git("add", ".", cwd=self.producer)
        git("commit", "-q", "-m", "invalid path fixture", cwd=self.producer)
        git("push", "-q", "origin", "agent-control", cwd=self.producer)
        self.push()
        agent = self.new_agent()
        self.assertEqual(agent.once()["status"], "done")
        self.assertEqual(self.remote_project_result()["status"], "done")
        self.assertEqual((self.work / "project-ran.txt").read_text(), "x")
        self.assertEqual(agent.once()["status"], "idle")
        self.assertEqual((self.work / "project-ran.txt").read_text(), "x")
        self.assertFalse((agent.project_turn.journal.root / "claims" /
                          (bad_id + ".json")).exists())

    def test_registry_alone_never_activates_a_project(self):
        self.push()
        agent = self.new_agent(enabled=False)
        self.assertEqual(agent.once()["status"], "idle")
        self.assertFalse((self.work / "project-ran.txt").exists())
        self.assertFalse((self.state / "projects").exists())

    def test_explicit_project_turn_publishes_on_project_not_host(self):
        self.push()
        agent = self.new_agent()
        first = agent.once()
        self.assertEqual(first["status"], "done")
        result = self.remote_project_result()
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["repository_id"], "example")
        self.assertEqual((self.work / "project-ran.txt").read_text(), "x")
        self.assertEqual(agent.once()["status"], "idle")
        self.assertEqual((self.work / "project-ran.txt").read_text(), "x")
        self.assertFalse((self.state / "results").exists())
        self.assertNotIn(".agent/hosts/", git("--git-dir", str(self.host_origin),
                         "ls-tree", "-r", "--name-only", "agent-control"))
        self.assertFalse((self.root / "owner-control").exists())

    def test_project_stop_blocks_claim_and_host_continues(self):
        self.push()
        agent = self.new_agent()
        agent.project_turn.gate.request_stop()
        self.assertEqual(agent.once()["status"], "project_stopped")
        self.assertFalse((self.work / "project-ran.txt").exists())
        self.assertFalse(agent.project_turn.journal._claim_path("job").exists())

    def test_remote_change_at_final_preclaim_fails_without_execution(self):
        self.push()
        agent = self.new_agent()
        old = agent.project_turn.intake.revalidate

        def change_and_revalidate(candidate):
            self.push(self.project_task(commands=["printf other >> project-ran.txt"]))
            return old(candidate)

        agent.project_turn.intake.revalidate = change_and_revalidate
        self.assertEqual(agent.once()["status"], "task_changed_before_claim")
        self.assertFalse(agent.project_turn.journal._claim_path("job").exists())
        self.assertFalse((self.work / "project-ran.txt").exists())

    def test_over_budget_task_is_rejected_without_execution(self):
        self.push(self.project_task(memory_limit_mb=2048))
        agent = self.new_agent()
        self.assertEqual(agent.once()["status"], "failed")
        result = self.remote_project_result()["result"]
        self.assertEqual(result["reason"], "invalid_task_file")
        self.assertIn("memory cap", result["error"])
        self.assertFalse((self.work / "project-ran.txt").exists())
        self.identity.write_text(json.dumps({
            "version": 1, "host_id": "wrong", "installation_id": INSTALLATION}))
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            self.new_agent()

    def test_live_stop_terminates_bounded_command_and_spools_unknown(self):
        self.push(self.project_task(commands=[
            "touch started.txt; sleep 20; touch should-not-exist.txt"]))
        agent = self.new_agent()
        errors = []
        outcome = []

        def turn():
            try:
                outcome.append(agent.once())
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=turn)
        thread.start()
        until = time.monotonic() + 8
        while not (self.work / "started.txt").exists() and time.monotonic() < until:
            time.sleep(0.05)
        self.assertTrue((self.work / "started.txt").exists())
        agent.project_turn.gate.request_stop()
        thread.join(timeout=8)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(outcome[0]["status"], "unknown")
        self.assertFalse((self.work / "should-not-exist.txt").exists())
        self.assertEqual(self.remote_project_result()["status"], "unknown")

    def test_host_git_outage_does_not_block_independent_project_queue(self):
        self.push()
        agent = self.new_agent()
        lost = self.root / "lost-host.git"
        self.host_origin.rename(lost)
        try:
            self.assertEqual(agent.once()["status"], "done")
        finally:
            lost.rename(self.host_origin)
        self.assertEqual(self.remote_project_result()["status"], "done")
        self.assertEqual((self.work / "project-ran.txt").read_text(), "x")
        self.assertEqual(agent.once()["status"], "idle")

    def test_host_only_git_outage_behavior_remains_unchanged(self):
        agent = self.new_agent(enabled=False)
        lost = self.root / "lost-host.git"
        self.host_origin.rename(lost)
        try:
            with self.assertRaises(GitError):
                agent.once()
        finally:
            lost.rename(self.host_origin)
        self.assertFalse((self.state / "projects").exists())

    def test_git_intake_outage_does_not_claim_and_can_retry(self):
        self.push()
        agent = self.new_agent()
        unavailable = self.root / "temporarily-unavailable.git"
        self.origin.rename(unavailable)
        try:
            with self.assertRaises(GitError):
                agent.once()
            self.assertFalse(agent.project_turn.journal._claim_path("job").exists())
        finally:
            unavailable.rename(self.origin)
        self.assertEqual(agent.once()["status"], "done")
        self.assertEqual((self.work / "project-ran.txt").read_text(), "x")

    def test_publisher_outage_retries_same_durable_spool_without_replay(self):
        self.push()
        agent = self.new_agent()
        real_git = agent.project_turn.publisher._git

        def lost_push(*args, **kwargs):
            if args and args[0] == "push":
                raise ProjectPublishError("project Git push failed")
            return real_git(*args, **kwargs)

        agent.project_turn.publisher._git = lost_push
        with self.assertRaises(GitError):
            agent.once()
        self.assertEqual((self.work / "project-ran.txt").read_text(), "x")
        self.assertEqual(len(agent.project_turn.journal.verified_results()), 1)
        self.assertIsNone(self.remote_project_result())
        agent.project_turn.publisher._git = real_git
        self.assertEqual(agent.once()["status"], "idle")
        self.assertEqual((self.work / "project-ran.txt").read_text(), "x")
        self.assertEqual(self.remote_project_result()["status"], "done")

    def test_host_named_resource_collision_blocks_project_before_claim(self):
        self.push(self.project_task(resources=["camera"]))
        agent = self.new_agent()
        # This standalone resource fixture runs before Agent.once().
        # Create the owner's private state root first; mkdir(parents=True)
        # inside machine_resource_lease otherwise inherits umask (e.g. 022)
        # and correctly fails the daemon's strict 0700 state admission.
        self.state.mkdir(mode=0o700)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        with machine_resource_lease(
            {"resources": ["camera"]},
            lock_dir=resource_lock_dir(self.state),
            command_env=os.environ,
        ):
            self.assertEqual(agent.once()["status"], "resource_busy")
        self.assertFalse(agent.project_turn.journal._claim_path("job").exists())
        self.assertEqual(agent.once()["status"], "done")
        self.assertEqual(self.remote_project_result()["status"], "done")

    def test_invalid_project_tasks_are_terminal_without_crashing_or_execution(self):
        cases = (
            self.project_task(mode="tool"),
            self.project_task(work_branch="main", allow_write=False),
            self.project_task(steps=[{"command": "true"}]),
            self.project_task(resources=["camera", "camera"]),
            self.project_task(resources="invalid"),
            self.project_task(commands=[""]),
        )
        agent = self.new_agent()
        for index, task in enumerate(cases):
            task["id"] = f"invalid-{index}"
            with self.subTest(task=task):
                self.push(task)
                self.assertEqual(agent.once()["status"], "failed")
                result = self.remote_project_result(task["id"])
                self.assertEqual(result["result"]["reason"], "invalid_task_file")
                self.assertEqual(agent.once()["status"], "idle")
                self.assertFalse((self.work / "project-ran.txt").exists())
        self.push()
        self.assertEqual(agent.once()["status"], "done")
        self.assertEqual((self.work / "project-ran.txt").read_text(), "x")
        self.assertEqual(agent.once()["status"], "idle")

    def test_wrong_project_targets_fail_before_claim(self):
        agent = self.new_agent()
        for task in (self.project_task(target_host_id="wrong"),
                     self.project_task(target_installation_id="wrong")):
            with self.subTest(task=task):
                self.push(task)
                with self.assertRaises(ValueError):
                    agent.once()
                self.assertFalse(agent.project_turn.journal._claim_path("job").exists())
                self.assertFalse((self.work / "project-ran.txt").exists())

    def test_invalid_task_changed_at_recheck_is_not_claimed(self):
        self.push(self.project_task(allow_write=False))
        agent = self.new_agent()
        old = agent.project_turn.intake.revalidate

        def replace(candidate):
            self.push(self.project_task())
            old(candidate)

        agent.project_turn.intake.revalidate = replace
        self.assertEqual(agent.once()["status"], "task_changed_before_claim")
        self.assertFalse(agent.project_turn.journal._claim_path("job").exists())
        self.assertFalse((self.work / "project-ran.txt").exists())

    def test_rejection_publication_outage_retries_same_result_without_execution(self):
        self.push(self.project_task(resources="invalid"))
        agent = self.new_agent()
        real_git = agent.project_turn.publisher._git

        def lost_push(*args, **kwargs):
            if args and args[0] == "push":
                raise ProjectPublishError("project Git push failed")
            return real_git(*args, **kwargs)

        agent.project_turn.publisher._git = lost_push
        with self.assertRaises(GitError):
            agent.once()
        before = agent.project_turn.journal.verified_results()
        self.assertEqual(len(before), 1)
        self.assertFalse((self.work / "project-ran.txt").exists())
        agent.project_turn.publisher._git = real_git
        self.assertEqual(agent.once()["status"], "idle")
        self.assertEqual(agent.project_turn.journal.verified_results(), before)
        self.assertEqual(self.remote_project_result()["result"]["reason"], "invalid_task_file")
        self.assertFalse((self.work / "project-ran.txt").exists())

    def test_project_stop_also_prevents_invalid_task_claim(self):
        self.push(self.project_task(allow_write=False))
        agent = self.new_agent()
        agent.project_turn.gate.request_stop()
        self.assertEqual(agent.once()["status"], "project_stopped")
        self.assertFalse(agent.project_turn.journal._claim_path("job").exists())
        self.assertFalse((self.work / "project-ran.txt").exists())

    def test_registry_changed_after_start_fails_before_claim(self):
        self.push()
        agent = self.new_agent()
        record = json.loads(self.registry.read_text())
        record["repositories"][0]["host_execution_enabled"] = False
        self.registry.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            agent.once()
        self.assertFalse(agent.project_turn.journal._claim_path("job").exists())
        self.assertFalse((self.work / "project-ran.txt").exists())

    def test_local_identity_changed_after_start_fails_before_claim(self):
        self.push()
        agent = self.new_agent()
        self.identity.write_text(json.dumps({
            "version": 1, "host_id": HOST, "installation_id": "changed"}))
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            agent.once()
        self.assertFalse(agent.project_turn.journal._claim_path("job").exists())

    def test_low_memory_config_is_enforced_without_raising_task_defaults(self):
        self.push(self.project_task(memory_limit_mb=128))
        self.config["project_turn"]["max_memory_mb"] = 256
        agent = self.new_agent()
        self.assertEqual(agent.once()["status"], "done")
        self.assertEqual(self.remote_project_result()["status"], "done")
        self.assertEqual((self.work / "project-ran.txt").read_text(), "x")

    @unittest.skipUnless(sys.platform.startswith("linux") and hasattr(os, "pidfd_open"),
                         "requires native Linux process fencing")
    def test_supervisor_sigkill_fences_orphan_and_never_replays(self):
        self.push(self.project_task(commands=[
            "echo x >> project-ran.txt; touch started.txt; sleep 30; "
            "echo y >> project-ran.txt"]))
        config_file = self.root / "host-config.json"
        config_file.write_text(json.dumps(self.config))
        code = ("import json,sys,pathlib;"
                "from linux_agent.agent import Agent;"
                "p=pathlib.Path(sys.argv[1]);"
                "Agent(json.loads(p.read_text()), pathlib.Path(sys.argv[2])).once()")
        process = subprocess.Popen([sys.executable, "-B", "-c", code,
                                    str(config_file), str(self.state)],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL,
                                   env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                   start_new_session=True)
        try:
            until = time.monotonic() + 8
            while not (self.work / "started.txt").exists() and time.monotonic() < until:
                time.sleep(0.05)
            self.assertTrue((self.work / "started.txt").exists())
            process.kill()
            process.wait(timeout=4)
            agent = self.new_agent()
            self.assertEqual(agent.once()["status"], "idle")
            self.assertEqual((self.work / "project-ran.txt").read_text().strip(), "x")
            self.assertEqual(self.remote_project_result()["status"], "unknown")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
