"""Offline Git transport / durable publication / no-replay contract tests."""
from __future__ import annotations

# ruff: noqa: E402 -- local source import setup in standalone test
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from linux_agent.agent import Agent
from linux_agent.git_transport import GitError


def git(*args, cwd=None):
    return subprocess.run(["git", *(["-C", str(cwd)] if cwd else []), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


class LinuxGitPilotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bare = self.root / "remote.git"
        self.writer = self.root / "writer"
        git("init", "--bare", str(self.bare))
        git("clone", str(self.bare), str(self.writer))
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@local.invalid",
            "commit", "--allow-empty", "-m", "init", cwd=self.writer)
        git("branch", "-M", "agent-control", cwd=self.writer)
        git("push", "-u", "origin", "agent-control", cwd=self.writer)
        self.config = {"version": 1, "host_id": "debian-n5105",
                       "installation_id": "123e4567-e89b-12d3-a456-426614174000",
                       "repository": str(self.bare), "branch": "agent-control",
                       "enabled": True,
                       "host_intake": {"enabled": True, "authorization": "github_writers"}}
        self.state = self.root / "state"
        self.agent = Agent(self.config, self.state)

    def send(self, task_id="linux-pilot-1", commands=None, output_policy="summary"):
        path = self.writer / f".agent/hosts/debian-n5105/supervisor/{self.config['installation_id']}/tasks/{task_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        task = {"version": 1, "id": task_id, "target_host_id": "debian-n5105",
                "target_installation_id": self.config["installation_id"],
                "cwd": str(self.root), "commands": commands or ["printf done"],
                "resources": [], "command_timeout": 3, "idle_timeout": 3,
                "task_timeout": 65, "memory_limit_mb": 256,
                "output_policy": output_policy}
        path.write_text(json.dumps(task))
        git("add", str(path.relative_to(self.writer)), cwd=self.writer)
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@local.invalid",
            "commit", "-m", "enqueue " + task_id, cwd=self.writer)
        git("push", cwd=self.writer)
        return task

    def test_unsupported_output_policy_is_failed_before_executor_and_never_replayed(self):
        marker = self.root / "forbidden-output-policy"
        self.send(commands=[f"touch {marker}"], output_policy="full")
        with mock.patch.object(self.agent, "_execute") as execute:
            self.assertEqual(self.agent.once()["status"], "failed")
            self.assertEqual(self.agent.once()["status"], "idle")
        execute.assert_not_called()
        result = json.loads(self.agent._spool_path("linux-pilot-1").read_bytes())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["result"]["reason"], "invalid_task_file")
        self.assertFalse(marker.exists())

    def test_dangling_stop_symlink_blocks_git_task_without_claim(self):
        marker = self.root / "forbidden.txt"
        self.send(commands=[f"touch {marker}"])
        self.state.mkdir(mode=0o700)
        (self.state / "EMERGENCY_STOP").symlink_to(self.root / "missing-stop-target")
        self.assertEqual(self.agent.once()["status"], "disabled")
        self.assertFalse(marker.exists())
        self.assertFalse(self.agent._claim_path("linux-pilot-1").exists())

    def test_daemon_lock_symlink_is_refused_without_touching_target(self):
        self.state.mkdir(mode=0o700)
        target = self.root / "unrelated"
        target.write_text("preserve me")
        (self.state / "agent.lock").symlink_to(target)
        with self.assertRaises((ValueError, OSError)):
            self.agent.once()
        self.assertEqual(target.read_text(), "preserve me")

    def test_daemon_lock_fifo_and_writable_or_linked_file_are_refused(self):
        self.state.mkdir(mode=0o700)
        lock = self.state / "agent.lock"
        os.mkfifo(lock, 0o600)
        with self.assertRaisesRegex(ValueError, "unsafe daemon lock"):
            self.agent.once()
        lock.unlink()
        lock.write_text("")
        lock.chmod(0o620)
        with self.assertRaisesRegex(ValueError, "unsafe daemon lock"):
            self.agent.once()
        lock.chmod(0o600)
        os.link(lock, self.root / "other-lock")
        with self.assertRaisesRegex(ValueError, "unsafe daemon lock"):
            self.agent.once()

    def test_stop_inspection_error_fails_closed(self):
        with mock.patch.object(Path, "lstat", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                self.agent.disabled()

    def test_published_spools_need_one_remote_fetch_per_pass(self):
        """N retained results must not trigger N network fetches each poll."""
        from unittest import mock

        for n in range(3):
            if n:
                git("pull", "--ff-only", cwd=self.writer)
            self.send(task_id=f"batch-publish-{n}", commands=["printf done"])
            self.assertEqual(self.agent.once()["status"], "done")

        spools = sorted((self.state / "results").glob("*.json"))
        self.assertEqual(len(spools), 3)
        before = [p.read_bytes() for p in spools]
        with mock.patch.object(
            self.agent.transport, "refresh", wraps=self.agent.transport.refresh
        ) as refresh:
            self.assertEqual(self.agent.publish_pending(), 3)
            self.assertEqual(refresh.call_count, 1)
        self.assertEqual([p.read_bytes() for p in spools], before)

    def test_idle_cycle_uses_one_refresh_and_does_not_reinspect_claimed(self):
        self.send(task_id="existing")
        self.assertEqual(self.agent.once()["status"], "done")
        self.assertEqual(self.agent.once()["status"], "idle")
        with (mock.patch.object(self.agent.transport, "refresh",
                                wraps=self.agent.transport.refresh) as refresh,
              mock.patch.object(self.agent.transport, "_blob",
                                wraps=self.agent.transport._blob) as blobs):
            self.assertEqual(self.agent.once()["status"], "idle")
        self.assertEqual(refresh.call_count, 1)
        self.assertFalse(any("/tasks/existing.json" in c.args[0]
                             for c in blobs.call_args_list))

    def test_single_pending_task_recheck_avoids_full_rescan(self):
        self.send(task_id="fresh")
        with mock.patch.object(
            self.agent.transport, "task_candidates",
            wraps=self.agent.transport.task_candidates
        ) as scan:
            self.assertEqual(self.agent.once()["status"], "done")
        self.assertEqual(scan.call_count, 1)

    def test_task_completion_reuses_preclaim_refresh_without_weakening_publish(self):
        self.send(task_id="fastpath-fresh-publish", commands=["printf safe"])
        with mock.patch.object(
            self.agent.transport, "refresh", wraps=self.agent.transport.refresh
        ) as refresh:
            out = self.agent.once()
        self.assertEqual(out["status"], "done")
        # One intake fetch, one preclaim verification, one independent
        # prepush refresh and one postpush confirmation; no fifth fetch
        # solely for the newly-created durable spool.
        self.assertEqual(refresh.call_count, 4)
        git("pull", "--ff-only", cwd=self.writer)
        path = self.writer / (
            f".agent/hosts/debian-n5105/supervisor/"
            f"{self.config['installation_id']}/task-results/fastpath-fresh-publish.json"
        )
        self.assertEqual(json.loads(path.read_text())["status"], "done")
        self.assertEqual(self.agent.once()["status"], "idle")

    def test_adaptive_poll_tiers_are_bounded(self):
        self.assertEqual(Agent.adaptive_delay(8, None, 100), 8)
        self.assertEqual(Agent.adaptive_delay(8, 99, 100), 2)
        self.assertEqual(Agent.adaptive_delay(8, 60, 100), 5)
        self.assertEqual(Agent.adaptive_delay(8, 0, 1000), 8)
        self.assertEqual(Agent.adaptive_delay(2, 60, 100), 2)

    def test_serve_drains_completed_backlog_without_sleep_or_outage_spin(self):
        seen = []
        statuses = iter([
            {"status": "done"}, {"status": "failed"},
            {"status": "idle"}, {"status": "done"},
        ])
        def pause(seconds):
            seen.append(seconds)
            if len(seen) == 4:
                self.agent.request_stop()
            return False
        with (
            mock.patch.object(self.agent, "_once_locked",
                              side_effect=lambda: next(statuses)),
            mock.patch.object(self.agent.stop_requested, "wait", side_effect=pause),
        ):
            self.agent.serve(poll_seconds=8, adaptive=True)
        self.assertEqual(seen[:2], [0.0, 0.0])
        self.assertGreater(seen[2], 0)
        self.assertEqual(seen[3], 0.0)

    def test_serve_git_outage_still_backs_off(self):
        seen = []
        def pause(seconds):
            seen.append(seconds)
            self.agent.request_stop()
            return False
        with (
            mock.patch.object(self.agent, "_once_locked", side_effect=GitError("offline")),
            mock.patch.object(self.agent.stop_requested, "wait", side_effect=pause),
        ):
            self.agent.serve(poll_seconds=8, adaptive=True)
        self.assertEqual(seen, [8])

    def test_git_roundtrip_and_no_replay(self):
        marker = self.root / "marker"
        self.send(commands=[f"printf x >> '{marker}'"])
        first = self.agent.once()
        self.assertEqual(first["status"], "done")
        self.assertEqual(self.agent.once()["status"], "idle")
        self.assertEqual(marker.read_text(), "x")
        git("pull", "--ff-only", cwd=self.writer)
        result = self.writer / f".agent/hosts/debian-n5105/supervisor/{self.config['installation_id']}/task-results/linux-pilot-1.json"
        self.assertEqual(json.loads(result.read_text())["status"], "done")

    def test_stale_claim_unknown_not_reexecuted(self):
        marker = self.root / "marker"
        self.send(commands=[f"printf x >> '{marker}'"])
        self.state.mkdir(mode=0o700, parents=True)
        self.agent.claim("linux-pilot-1", "1234")
        result = self.agent.once()
        self.assertEqual(result["status"], "idle")
        self.assertFalse(marker.exists())
        git("pull", "--ff-only", cwd=self.writer)
        path = self.writer / f".agent/hosts/debian-n5105/supervisor/{self.config['installation_id']}/task-results/linux-pilot-1.json"
        self.assertEqual(json.loads(path.read_text())["status"], "unknown")

    def test_unsupported_mutation_rejected_before_effects(self):
        marker = self.root / "marker"
        self.send(commands=[f"printf x >> '{marker}'"])
        path = self.writer / f".agent/hosts/debian-n5105/supervisor/{self.config['installation_id']}/tasks/linux-pilot-1.json"
        t = json.loads(path.read_text())
        t["writes"] = [{"path":"x","content":"y"}]
        path.write_text(json.dumps(t))
        git("add", str(path.relative_to(self.writer)), cwd=self.writer)
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@local.invalid",
            "commit", "-m", "invalid task", cwd=self.writer)
        git("push", cwd=self.writer)
        out = self.agent.once()
        self.assertEqual(out["reason"], "invalid_task_file")
        self.assertFalse(marker.exists())

    def test_unimplemented_stages_rejected_before_effects(self):
        marker = self.root / "unsafe-stage"
        self.send(commands=["printf allowed"])
        path = self.writer / f".agent/hosts/debian-n5105/supervisor/{self.config['installation_id']}/tasks/linux-pilot-1.json"
        task = json.loads(path.read_text())
        task["verify_commands"] = [f"touch '{marker}'"]
        path.write_text(json.dumps(task))
        git("add", str(path.relative_to(self.writer)), cwd=self.writer)
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@local.invalid",
            "commit", "-m", "unimplemented verification", cwd=self.writer)
        git("push", cwd=self.writer)
        out = self.agent.once()
        self.assertEqual(out["reason"], "invalid_task_file")
        self.assertFalse(marker.exists())

    def test_named_resource_conflict_fails_before_claim_and_retries(self):
        from linux_agent.resources import machine_resource_lease, resource_lock_dir
        marker = self.root / "resource-effects"
        self.send(commands=[f"touch '{marker}'"])
        path = self.writer / f".agent/hosts/debian-n5105/supervisor/{self.config['installation_id']}/tasks/linux-pilot-1.json"
        task = json.loads(path.read_text())
        task["resources"] = ["usb"]
        path.write_text(json.dumps(task))
        git("add", str(path.relative_to(self.writer)), cwd=self.writer)
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@local.invalid",
            "commit", "-m", "resource task", cwd=self.writer)
        git("push", cwd=self.writer)
        self.state.mkdir(mode=0o700, parents=True)
        with machine_resource_lease(task, lock_dir=resource_lock_dir(self.state), command_env={}):
            status = self.agent.once()
            self.assertEqual(status["status"], "resource_busy")
            self.assertFalse(marker.exists())
            self.assertFalse(self.agent._claim_path(task["id"]).exists())
        self.assertEqual(self.agent.once()["status"], "done")
        self.assertTrue(marker.exists())

    def test_machine_lease_excludes_named_resource_task(self):
        from linux_agent.resources import machine_resource_lease, resource_lock_dir
        self.send()
        self.state.mkdir(mode=0o700, parents=True)
        with machine_resource_lease(
            {"resources": ["machine"]},
            lock_dir=resource_lock_dir(self.state), command_env={}
        ):
            self.assertEqual(self.agent.once()["status"], "resource_busy")
            self.assertFalse(self.agent._claim_path("linux-pilot-1").exists())
        self.assertEqual(self.agent.once()["status"], "done")

    @unittest.skipUnless(sys.platform.startswith('linux'),
                         'requires native Linux process and Git outage semantics')
    def test_real_worker_sigkill_recovers_orphan_without_replay(self):
        """Kill actual Python runner while a child holds the inherited task lease."""
        import time
        from linux_agent.execution_recovery import lease_is_busy, recover_task_lease
        from linux_agent.foundation.process import execution_lease_path
        marker = self.root / "sigkill-executed"
        started = self.root / "sigkill-started"
        self.send(commands=[f"printf x >> '{marker}'; touch '{started}'; sleep 25"])
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        lease = execution_lease_path(
            self.state / "locks" / "host-execution", "host-task:linux-pilot-1"
        )
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        code = (
            "from pathlib import Path; from linux_agent.agent import Agent; "
            f"config={self.config!r}; state=Path({str(self.state)!r}); "
            "Agent(config,state).once()"
        )
        runner = subprocess.Popen(
            [sys.executable, "-c", code], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 12
            while not started.exists() and time.monotonic() < deadline:
                if runner.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertTrue(started.exists(), "child command never started")
            runner.kill()
            runner.wait(timeout=5)
            deadline = time.monotonic() + 3
            while not lease_is_busy(lease) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(lease_is_busy(lease), "orphan lock did not survive SIGKILL")
            self.assertEqual(self.agent.once()["status"], "idle")
            self.assertFalse(lease_is_busy(lease))
            self.assertEqual(marker.read_text(), "x")
            git("pull", "--ff-only", cwd=self.writer)
            result_path = self.writer / (
                ".agent/hosts/debian-n5105/supervisor/"
                f"{self.config['installation_id']}/task-results/linux-pilot-1.json"
            )
            self.assertEqual(json.loads(result_path.read_text())["status"], "unknown")
            self.assertEqual(self.agent.once()["status"], "idle")
            self.assertEqual(marker.read_text(), "x")
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.wait(timeout=5)
            if lease_is_busy(lease):
                recover_task_lease(lease)

    def test_corrupt_claim_never_fabricates_unknown_or_replays(self):
        marker = self.root / "claim-corruption-effect"
        self.send(commands=[f"touch '{marker}'"])
        self.state.mkdir(mode=0o700, parents=True)
        self.agent.claim("linux-pilot-1", "1234")
        claim = self.agent._claim_path("linux-pilot-1")
        valid = json.loads(claim.read_text())
        cases = {
            "duplicate_digest": (
                '{"version":1,"id":"linux-pilot-1","status":'
                '"execution_may_have_started","task_digest":"1234",'
                '"task_digest":"replaced"}'
            ),
            "altered_status": json.dumps({**valid, "status": "done"}),
            "altered_version": json.dumps({**valid, "version": 2}),
            "extra_field": json.dumps({**valid, "unexpected": True}),
            "non_object": "[]",
            "truncated": '{"version":',
            "oversized": " " * 5000,
        }
        for label, invalid in cases.items():
            with self.subTest(case=label):
                claim.write_text(invalid)
                with self.assertRaisesRegex(ValueError, "corrupt claim"):
                    Agent(self.config, self.state).once()
                self.assertFalse(self.agent._spool_path("linux-pilot-1").exists())
                self.assertFalse(marker.exists())
        claim.write_text(json.dumps(valid))
        self.assertEqual(Agent(self.config, self.state).once()["status"], "idle")
        self.assertFalse(marker.exists())
        git("pull", "--ff-only", cwd=self.writer)
        remote = self.writer / (
            ".agent/hosts/debian-n5105/supervisor/"
            f"{self.config['installation_id']}/task-results/linux-pilot-1.json"
        )
        self.assertEqual(json.loads(remote.read_text())["status"], "unknown")

    def test_remote_result_collision_keeps_original_spool_and_no_replay(self):
        marker = self.root / "conflicting-remote-effect"
        self.send(commands=[f"printf x >> '{marker}'"])
        original_publish = self.agent.transport.publish
        self.agent.transport.publish = lambda *_: (_ for _ in ()).throw(
            GitError("simulated offline publication")
        )
        try:
            with self.assertRaises(GitError):
                self.agent.once()
        finally:
            self.agent.transport.publish = original_publish
        spool = self.agent._spool_path("linux-pilot-1")
        original_bytes = spool.read_bytes()
        self.assertEqual(marker.read_text(), "x")
        remote = self.writer / (
            ".agent/hosts/debian-n5105/supervisor/"
            f"{self.config['installation_id']}/task-results/linux-pilot-1.json"
        )
        remote.parent.mkdir(parents=True, exist_ok=True)
        remote.write_text('{"status":"conflict"}\\n')
        git("add", str(remote.relative_to(self.writer)), cwd=self.writer)
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@local.invalid",
            "commit", "-m", "independent conflicting result", cwd=self.writer)
        git("push", cwd=self.writer)
        with self.assertRaisesRegex(GitError, "immutable remote result conflict"):
            Agent(self.config, self.state).once()
        self.assertEqual(spool.read_bytes(), original_bytes)
        self.assertTrue(self.agent._claim_path("linux-pilot-1").exists())
        self.assertEqual(marker.read_text(), "x")
        self.assertEqual(remote.read_text(), '{"status":"conflict"}\\n')

    @unittest.skipUnless(sys.platform.startswith('linux'),
                         'requires native Linux process and Git outage semantics')
    def test_serve_sigterm_cancels_inflight_to_unknown(self):
        import signal
        import time
        started = self.root / "service-started"
        marker = self.root / "service-effect"
        self.send(commands=[f"printf x >> '{marker}'; touch '{started}'; sleep 20"])
        config = self.root / "private-config.json"
        config.write_text(json.dumps(self.config))
        config.chmod(0o600)
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        runner = subprocess.Popen(
            [sys.executable, "-m", "linux_agent", "--config", str(config),
             "--state", str(self.state), "serve"],
            env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 10
            while not started.exists() and time.monotonic() < deadline:
                if runner.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertTrue(started.exists(), "service child did not start")
            runner.send_signal(signal.SIGTERM)
            self.assertEqual(runner.wait(timeout=10), 0)
            self.assertEqual(marker.read_text(), "x")
            git("pull", "--ff-only", cwd=self.writer)
            result = self.writer / (
                ".agent/hosts/debian-n5105/supervisor/"
                f"{self.config['installation_id']}/task-results/linux-pilot-1.json"
            )
            self.assertEqual(json.loads(result.read_text())["status"], "unknown")
            self.assertEqual(self.agent.once()["status"], "idle")
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.wait(timeout=5)
            from linux_agent.execution_recovery import recover_task_lease, lease_is_busy
            from linux_agent.foundation.process import execution_lease_path
            lock = execution_lease_path(
                self.state / "locks" / "host-execution", "host-task:linux-pilot-1"
            )
            if lease_is_busy(lock):
                recover_task_lease(lock)

    @unittest.skipUnless(sys.platform.startswith('linux'),
                         'requires native Linux process and Git outage semantics')
    def test_separate_serve_process_survives_real_local_git_outage(self):
        """No systemd: real Python serve process, missing bare Git, same PID recovers."""
        import signal
        import time

        marker = self.root / "external-process-effect"
        missing_remote = self.root / "remote-unavailable.git"
        self.send(commands=[
            f"printf x >> '{marker}'; mv '{self.bare}' '{missing_remote}'"
        ])
        config = self.root / "private-serve-config.json"
        config.write_text(json.dumps(self.config))
        config.chmod(0o600)
        script = (
            "import json,signal,sys; from pathlib import Path; "
            "from linux_agent.agent import Agent; "
            "agent=Agent(json.loads(Path(sys.argv[1]).read_text()),"
            "Path(sys.argv[2])); "
            "signal.signal(signal.SIGTERM,lambda *_:agent.request_stop()); "
            "agent.serve(poll_seconds=0.2)"
        )
        runner = subprocess.Popen(
            [sys.executable, "-c", script, str(config), str(self.state)],
            env=dict(os.environ, PYTHONPATH=str(ROOT)),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        spool = self.state / "results/linux-pilot-1.json"
        remote_result = self.writer / (
            ".agent/hosts/debian-n5105/supervisor/"
            f"{self.config['installation_id']}/task-results/linux-pilot-1.json"
        )
        try:
            deadline = time.monotonic() + 12
            while (not missing_remote.exists() or not spool.is_file()) and time.monotonic() < deadline:
                if runner.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertTrue(missing_remote.is_dir(), "isolated Git outage did not start")
            self.assertTrue(spool.is_file(), "durable result not persisted")
            self.assertIsNone(runner.poll(), "serve process died during Git outage")
            self.assertEqual(marker.read_text(), "x")
            original_bytes = spool.read_bytes()
            missing_remote.rename(self.bare)
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if runner.poll() is not None:
                    break
                git("fetch", "origin", "agent-control", cwd=self.writer)
                remote_blob = git("show", "FETCH_HEAD:" + str(remote_result.relative_to(self.writer)),
                                  cwd=self.writer) if "task-results/linux-pilot-1.json" in git(
                                      "ls-tree", "-r", "--name-only", "FETCH_HEAD",
                                      cwd=self.writer) else None
                if remote_blob is not None:
                    break
                time.sleep(0.2)
            self.assertIsNone(runner.poll(), "serve process died after Git recovery")
            self.assertIsNotNone(remote_blob, "remote Git result not published after reconnection")
            self.assertEqual(json.loads(remote_blob)["status"], "done")
            self.assertEqual(spool.read_bytes(), original_bytes)
            self.assertEqual(marker.read_text(), "x")
            runner.send_signal(signal.SIGTERM)
            self.assertEqual(runner.wait(timeout=5), 0)
            self.assertEqual(marker.read_text(), "x")
        finally:
            if missing_remote.exists() and not self.bare.exists():
                missing_remote.rename(self.bare)
            if runner.poll() is None:
                runner.send_signal(signal.SIGTERM)
                try:
                    runner.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    runner.kill()
                    runner.wait(timeout=4)

    def test_serve_retries_git_outage_without_daemon_restart_or_task_replay(self):
        import threading
        import time
        marker = self.root / "outage-once"
        self.send(commands=[f"printf x >> '{marker}'"])
        original = self.agent.transport.refresh
        seen = threading.Event()
        restored = threading.Event()
        failures = []

        def outage():
            seen.set()
            if not restored.is_set():
                raise GitError("simulated transient offline transport")
            return original()

        def run():
            try:
                self.agent.serve(poll_seconds=0.2)
            except BaseException as exc:
                failures.append(exc)

        self.agent.transport.refresh = outage
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            self.assertTrue(seen.wait(4), "scheduler did not attempt Git refresh")
            self.assertTrue(worker.is_alive(), "Git outage killed the scheduler")
            self.assertFalse(marker.exists(), "task must not run before Git recovers")
            restored.set()
            deadline = time.monotonic() + 8
            while not (self.state / "results/linux-pilot-1.json").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue((self.state / "results/linux-pilot-1.json").exists())
            self.assertEqual(marker.read_text(), "x")
        finally:
            self.agent.request_stop()
            worker.join(timeout=4)
            self.agent.transport.refresh = original
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        # Restart must be a fresh Agent; stop_requested is intentionally sticky.
        self.assertEqual(Agent(self.config, self.state).once()["status"], "idle")
        self.assertEqual(marker.read_text(), "x")

    def test_stop_between_stages_fences_next_command_before_spawn(self):
        """Deterministically observe STOP after admission, before next spawn."""
        import threading
        from linux_agent.runtime.executor import RuntimeExecutor

        marker = self.root / "stop-must-not-execute"
        task = self.send(commands=[f"touch '{marker}'"])
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        observed = threading.Event()
        real_process_task = RuntimeExecutor.process_task

        def after_stop(executor, pending):
            (self.state / "EMERGENCY_STOP").write_text("disabled\\n")
            self.assertTrue(observed.wait(3), "STOP watcher did not run")
            return real_process_task(executor, pending)

        with (
            mock.patch.object(
                RuntimeExecutor, "terminate_active_command", autospec=True,
                side_effect=lambda _executor: observed.set(),
            ),
            mock.patch.object(
                RuntimeExecutor, "process_task", autospec=True,
                side_effect=after_stop,
            ),
        ):
            result = self.agent._execute(task)
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(marker.exists(), "STOP must fence unstarted command")
        self.assertTrue(result["commands"][0].get("not_started"))

    def test_emergency_stop(self):
        self.send()
        self.state.mkdir(mode=0o700, parents=True)
        (self.state / "EMERGENCY_STOP").write_text("disabled\n")
        self.assertEqual(self.agent.once()["status"], "disabled")

    def test_private_state_and_claim_permissions(self):
        self.send()
        self.agent.once()
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.state / "claims" / "linux-pilot-1.json").stat().st_mode & 0o777, 0o600)

    def test_insecure_state_rejected(self):
        self.state.mkdir(mode=0o755)
        self.state.chmod(0o755)
        with self.assertRaises(ValueError):
            self.agent.once()

    def test_rewritten_remote_tracking_branch_republishes_without_replay(self):
        marker = self.root / "rewrite-marker"
        self.send(commands=[f"printf x >> '{marker}'"])
        remote_before = git("rev-parse", "HEAD", cwd=self.writer)
        self.assertEqual(self.agent.once()["status"], "done")
        self.assertEqual(marker.read_text(), "x")
        # Model an authorized transport history rewrite with the task intact,
        # but the previously published result omitted.
        git("reset", "--hard", remote_before, cwd=self.writer)
        git("push", "--force", "origin", "HEAD:refs/heads/agent-control", cwd=self.writer)
        self.assertEqual(self.agent.once()["status"], "idle")
        self.assertEqual(marker.read_text(), "x")
        git("pull", "--ff-only", cwd=self.writer)
        result = self.writer / (
            ".agent/hosts/debian-n5105/supervisor/"
            f"{self.config['installation_id']}/task-results/linux-pilot-1.json"
        )
        self.assertEqual(json.loads(result.read_text())["status"], "done")

    def test_lost_push_ack_confirms_original_result_without_second_effect(self):
        """A successful remote push with a lost ACK needs readback, not replay."""
        marker = self.root / "ack-effect"
        self.send(commands=[f"printf x >> '{marker}'"])
        transport = self.agent.transport
        original_git = transport.git
        pushes = []

        def lost_ack(*args, **kwargs):
            result = original_git(*args, **kwargs)
            if args and args[0] == "push":
                pushes.append(args)
                if len(pushes) == 1:
                    raise GitError("simulated ACK lost after completed push")
            return result

        transport.git = lost_ack
        try:
            self.assertEqual(self.agent.once()["status"], "done")
        finally:
            transport.git = original_git
        self.assertEqual(len(pushes), 1)
        self.assertEqual(marker.read_text(), "x")
        self.assertEqual(Agent(self.config, self.state).once()["status"], "idle")
        self.assertEqual(marker.read_text(), "x")
        git("pull", "--ff-only", cwd=self.writer)
        remote = self.writer / (
            ".agent/hosts/debian-n5105/supervisor/"
            f"{self.config['installation_id']}/task-results/linux-pilot-1.json"
        )
        local = self.state / "results/linux-pilot-1.json"
        self.assertEqual(remote.read_bytes(), local.read_bytes())

    def test_corrupt_durable_spool_never_published_or_reexecuted(self):
        marker = self.root / "corrupt-spool-effect"
        self.send(commands=[f"printf x >> '{marker}'"])
        transport = self.agent.transport
        original = transport.publish
        transport.publish = lambda *_: (_ for _ in ()).throw(GitError("offline"))
        try:
            with self.assertRaises(GitError):
                self.agent.once()
        finally:
            transport.publish = original
        spool = self.state / "results/linux-pilot-1.json"
        spool.write_text('{"truncated":')
        with self.assertRaisesRegex(ValueError, "invalid durable result"):
            Agent(self.config, self.state).once()
        self.assertEqual(marker.read_text(), "x")
        git("pull", "--ff-only", cwd=self.writer)
        remote = self.writer / (
            ".agent/hosts/debian-n5105/supervisor/"
            f"{self.config['installation_id']}/task-results/linux-pilot-1.json"
        )
        self.assertFalse(remote.exists())
        self.assertTrue(self.agent._claim_path("linux-pilot-1").exists())

    def test_spooled_result_target_mismatch_refused_before_publish(self):
        self.send()
        transport = self.agent.transport
        original = transport.publish
        transport.publish = lambda *_: (_ for _ in ()).throw(GitError("offline"))
        try:
            with self.assertRaises(GitError):
                self.agent.once()
        finally:
            transport.publish = original
        spool = self.state / "results/linux-pilot-1.json"
        value = json.loads(spool.read_text())
        value["target_installation_id"] = str(uuid.uuid4())
        spool.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "invalid durable result"):
            Agent(self.config, self.state).once()
        git("pull", "--ff-only", cwd=self.writer)
        self.assertFalse((self.writer / (
            ".agent/hosts/debian-n5105/supervisor/"
            f"{self.config['installation_id']}/task-results/linux-pilot-1.json"
        )).exists())

    def test_symlink_spooled_result_refused_before_publish(self):
        self.send()
        transport = self.agent.transport
        original = transport.publish
        transport.publish = lambda *_: (_ for _ in ()).throw(GitError("offline"))
        try:
            with self.assertRaises(GitError):
                self.agent.once()
        finally:
            transport.publish = original
        spool = self.state / "results/linux-pilot-1.json"
        outside = self.root / "unrelated.json"
        spool.rename(outside)
        spool.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "invalid durable result"):
            Agent(self.config, self.state).once()

    def test_dangling_spool_symlink_never_replaced_by_unknown(self):
        marker = self.root / "dangling-symlink-effect"
        self.send(commands=[f"touch '{marker}'"])
        self.state.mkdir(mode=0o700, parents=True)
        self.agent.claim("linux-pilot-1", "a" * 64)
        results = self.state / "results"
        results.mkdir(mode=0o700)
        spool = results / "linux-pilot-1.json"
        outside = self.root / "missing-outside.json"
        spool.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "invalid durable result spool"):
            Agent(self.config, self.state).once()
        self.assertTrue(spool.is_symlink())
        self.assertFalse(outside.exists())
        self.assertFalse(marker.exists())

    def test_nested_durable_identity_mismatch_never_published(self):
        marker = self.root / "nested-spool-effect"
        self.send(commands=[f"printf x >> '{marker}'"])
        transport = self.agent.transport
        original = transport.publish
        transport.publish = lambda *_: (_ for _ in ()).throw(GitError("offline"))
        try:
            with self.assertRaises(GitError):
                self.agent.once()
        finally:
            transport.publish = original
        spool = self.state / "results/linux-pilot-1.json"
        original_data = json.loads(spool.read_text())
        for field, bad_value in (
            ("task_digest", "bad-digest"),
            ("target_host_id", "wrong-host"),
            ("target_installation_id", "wrong-installation"),
        ):
            with self.subTest(field=field):
                changed = json.loads(json.dumps(original_data))
                changed["result"][field] = bad_value
                spool.write_text(json.dumps(changed))
                with self.assertRaisesRegex(ValueError, "claim/envelope mismatch"):
                    Agent(self.config, self.state).once()
        self.assertEqual(marker.read_text(), "x")
        git("pull", "--ff-only", cwd=self.writer)
        remote = self.writer / (
            ".agent/hosts/debian-n5105/supervisor/"
            f"{self.config['installation_id']}/task-results/linux-pilot-1.json"
        )
        self.assertFalse(remote.exists())

    def test_serve_outage_then_emergency_publishes_without_new_execution(self):
        """In-process service loop: offline result survives an emergency stop."""
        import threading
        import time

        first = self.root / "service-outage-first"
        second = self.root / "service-outage-second"
        self.send(commands=[f"printf x >> '{first}'"])
        attempted = threading.Event()
        restored = threading.Event()
        published = threading.Event()
        failures = []
        transport = self.agent.transport
        original = transport.publish

        def flaky_publish(task_id, data):
            attempted.set()
            if not restored.is_set():
                raise GitError("simulated publish outage")
            original(task_id, data)
            published.set()

        def run():
            try:
                self.agent.serve(poll_seconds=0.2)
            except BaseException as exc:
                failures.append(exc)

        transport.publish = flaky_publish
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            self.assertTrue(attempted.wait(8), "service did not attempt publication")
            self.assertEqual(first.read_text(), "x")
            spool = self.state / "results/linux-pilot-1.json"
            self.assertTrue(spool.is_file())
            (self.state / "EMERGENCY_STOP").write_text("disabled\\n")
            self.send(task_id="linux-pilot-second",
                      commands=[f"touch '{second}'"])
            restored.set()
            self.assertTrue(published.wait(8), "durable result not republished")
            time.sleep(0.3)
            self.assertFalse(second.exists(), "emergency stop admitted new task")
            self.assertFalse(self.agent._claim_path("linux-pilot-second").exists())
        finally:
            self.agent.request_stop()
            worker.join(timeout=5)
            transport.publish = original

        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(first.read_text(), "x")
        git("pull", "--ff-only", cwd=self.writer)
        result = self.writer / (
            ".agent/hosts/debian-n5105/supervisor/"
            f"{self.config['installation_id']}/task-results/linux-pilot-1.json"
        )
        self.assertEqual(result.read_bytes(), spool.read_bytes())
        self.assertEqual(Agent(self.config, self.state).once()["status"], "disabled")
        self.assertFalse(second.exists())

    def test_publication_outage_no_replay(self):
        marker = self.root / "marker"
        self.send(commands=[f"printf x >> '{marker}'"])
        original = self.agent.transport.publish
        def offline(_task_id, _data):
            raise GitError("simulated Git outage")
        self.agent.transport.publish = offline
        with self.assertRaises(GitError):
            self.agent.once()
        self.assertEqual(marker.read_text(), "x")
        self.assertTrue((self.state / "results" / "linux-pilot-1.json").exists())
        self.agent.transport.publish = original
        self.assertEqual(self.agent.once()["status"], "idle")
        self.assertEqual(marker.read_text(), "x")

    def test_emergency_watch_is_not_reused_from_previous_task(self):
        import threading
        import time
        task = self.send(commands=["printf first"])
        self.agent._execute(task)
        self.assertFalse(self.agent.disabled())
        next_task = dict(task, id="linux-pilot-second", commands=["sleep 3"])
        def stop():
            time.sleep(0.35)
            self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
            (self.state / "EMERGENCY_STOP").write_text("disabled")
        watcher = threading.Thread(target=stop, daemon=True)
        watcher.start()
        result = self.agent._execute(next_task)
        watcher.join(timeout=2)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "emergency_stop_effects_uncertain")

    def test_durable_result_publishes_with_emergency_intake_disabled(self):
        self.send()
        original = self.agent.transport.publish
        def offline(_task_id, _data):
            raise GitError("simulated Git outage")
        self.agent.transport.publish = offline
        with self.assertRaises(GitError):
            self.agent.once()
        self.agent.transport.publish = original
        (self.state / "EMERGENCY_STOP").write_text("disabled")
        self.assertEqual(self.agent.once()["status"], "disabled")
        git("pull", "--ff-only", cwd=self.writer)
        path = self.writer / f".agent/hosts/debian-n5105/supervisor/{self.config['installation_id']}/task-results/linux-pilot-1.json"
        self.assertEqual(json.loads(path.read_text())["status"], "done")

    def test_duplicate_wire_json_fails_before_effects(self):
        marker = self.root / "duplicate-json-effect"
        self.send(commands=[f"touch '{marker}'"])
        path = self.writer / f".agent/hosts/debian-n5105/supervisor/{self.config['installation_id']}/tasks/linux-pilot-1.json"
        data = path.read_text()
        path.write_text(data[:-1] + ', "version": 1}')
        git("add", str(path.relative_to(self.writer)), cwd=self.writer)
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@local.invalid",
            "commit", "-m", "ambiguous fields", cwd=self.writer)
        git("push", cwd=self.writer)
        self.assertEqual(self.agent.once()["status"], "idle")
        self.assertFalse(marker.exists())

    def test_overlong_git_task_filename_cannot_poison_durable_claims(self):
        """A malformed Git inbox name must not block valid tasks or future polls."""
        invalid_id = "a" * 210  # valid Git basename, exceeds durable 200-char ID
        self.send(task_id=invalid_id, commands=["printf ignored"])
        marker = self.root / "valid-after-malformed-task"
        git("pull", "--ff-only", cwd=self.writer)
        self.send(task_id="z-valid-after-malformed", commands=[
            f"printf x >> '{marker}'"
        ])
        self.assertEqual(self.agent.once()["status"], "done")
        self.assertEqual(marker.read_text(), "x")
        self.assertFalse(self.agent._claim_path(invalid_id).exists())
        self.assertEqual(Agent(self.config, self.state).once()["status"], "idle")
        self.assertEqual(marker.read_text(), "x")

    def test_wrong_installation_ignored(self):
        task = self.send()
        path = self.writer / f".agent/hosts/debian-n5105/supervisor/{self.config['installation_id']}/tasks/linux-pilot-1.json"
        task["target_installation_id"] = str(uuid.uuid4())
        path.write_text(json.dumps(task))
        git("add", str(path.relative_to(self.writer)), cwd=self.writer)
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@local.invalid",
            "commit", "-m", "wrong target", cwd=self.writer)
        git("push", cwd=self.writer)
        self.assertEqual(self.agent.once()["status"], "idle")
        self.assertFalse((self.state / "claims" / "linux-pilot-1.json").exists())

    def test_result_conflict_fails_closed(self):
        self.send()
        self.agent.transport.refresh()
        self.agent.transport.publish("linux-pilot-1", b'{"status":"done"}\n')
        with self.assertRaises(GitError):
            self.agent.transport.publish("linux-pilot-1", b'{"status":"unknown"}\n')


if __name__ == "__main__":
    unittest.main()
