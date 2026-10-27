"""Host command runner tests with no project registry, remote Git or live effects."""
from __future__ import annotations

import fcntl
import json
import os
import shlex
import sys
import threading
import time
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.repository.host_routing import HostIdentity
from local_agent.supervisor import host_tasks
from local_agent.supervisor.macos_release_admission import (
    CONFIG as MAC_ADMISSION_CONFIG,
    REQUIRED_ENV as MAC_ADMISSION_REQUIRED_ENV,
    MacReleaseAdmissionBlocked,
)
from local_agent.supervisor.resources import MachineResourceBusy


class StandaloneHostTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "private-state"
        self.state.mkdir(mode=0o700)
        # Production's inherited Mac admission requirement remains enabled.
        # Enroll this disposable state root only; never change the live gate.
        self.admission = self.state / MAC_ADMISSION_CONFIG
        self.admission.write_text(json.dumps({"version": 1, "enabled": True}) + "\n")
        self.admission.chmod(0o600)
        self.base = self.state / "host-tasks"
        self.base.mkdir(mode=0o700)
        self.requests = self.base / "requests"
        self.requests.mkdir(mode=0o700)
        self.cwd = self.root / "ordinary-local-folder"
        self.cwd.mkdir()
        self.source = HostIdentity(
            "test-mac", "b0e94f93-478f-4fc6-abed-159d3702362f",
        )
        self.identity_file = self.state / "identity.json"
        self.identity_file.write_text(json.dumps(self.source.as_record()), encoding="utf-8")
        self.identity_file.chmod(0o600)
        self.p3 = mock.patch.object(host_tasks.operator_local, "is_disabled", return_value=False)
        self.p3.start()
        self.addCleanup(self.p3.stop)

    def task(self, *, task_id: str = "host-example", command: str = "pwd", **extra: object) -> dict:
        obj = {
            "version": 1, "id": task_id,
            "target_host_id": self.source.host_id,
            "target_installation_id": self.source.installation_id,
            "cwd": str(self.cwd),
            "commands": [command],
            "command_timeout": 25, "task_timeout": 120,
        }
        obj.update(extra)
        return obj

    def queue(self, task: dict) -> None:
        path = self.requests / (task["id"] + ".json")
        path.write_text(json.dumps(task), encoding="utf-8")
        path.chmod(0o600)

    def execute(self, task_id: str = "host-example") -> dict | None:
        return host_tasks.run_local_host_task(
            task_id, state_dir=self.state,
            identity_file=self.identity_file,
        )

    def test_release_admission_fixture_is_private(self) -> None:
        self.assertEqual(self.admission.parent, self.state)
        self.assertEqual(stat.S_IMODE(self.admission.stat().st_mode), 0o600)
        self.assertEqual(
            json.loads(self.admission.read_text()),
            {"version": 1, "enabled": True},
        )

    def test_required_admission_missing_fails_closed_before_claim(self) -> None:
        self.queue(self.task(command="touch should-not-exist"))
        self.admission.unlink()
        with mock.patch.dict(os.environ, {MAC_ADMISSION_REQUIRED_ENV: "1"}):
            # Check actual macOS admission policy even on a Linux test runner.
            with mock.patch("local_agent.supervisor.macos_release_admission.sys.platform", "darwin"):
                with self.assertRaisesRegex(
                    MacReleaseAdmissionBlocked, "not enrolled"
                ):
                    self.execute()
        self.assertFalse((self.cwd / "should-not-exist").exists())
        self.assertFalse((self.base / "claims" / "host-example.json").exists())

    def test_normal_command_with_no_repository_or_git_checkout(self) -> None:
        self.queue(self.task(command="pwd"))
        result = self.execute()
        assert result is not None
        self.assertEqual(result["status"], "done", result)
        self.assertEqual(result["target_installation_id"], self.source.installation_id)
        self.assertIn(str(self.cwd), result["commands"][0]["output"])
        self.assertNotIn("work_branch", result)
        self.assertNotIn("git_status", result)
        self.assertTrue((self.base / "claims" / "host-example.json").exists())
        result_path = self.base / "results" / "host-example.json"
        self.assertEqual(json.loads(result_path.read_text())["status"], "done")
        self.assertEqual(stat.S_IMODE(result_path.stat().st_mode), 0o600)

    def test_normal_shell_can_write_files_outside_git_without_repo_binding(self) -> None:
        self.queue(self.task(command="printf host-success > local-output.txt"))
        result = self.execute()
        self.assertEqual(result["status"], "done")
        self.assertEqual((self.cwd / "local-output.txt").read_text(), "host-success")

    def test_completed_request_does_not_run_twice(self) -> None:
        self.queue(self.task(command="printf x >> counter.txt"))
        self.assertEqual(self.execute()["status"], "done")
        self.assertIsNone(self.execute())
        self.assertEqual((self.cwd / "counter.txt").read_text(), "x")

    def test_host_ops_nested_command_uses_host_leases_and_never_replays(self) -> None:
        source = str(Path(__file__).resolve().parents[1])  # checkout root, import bridge
        program = (
            "import os,sys; from pathlib import Path; "
            f"sys.path.insert(0,{source!r}); "
            "from local_agent.host_ops.core.execution import ProcessRunner; "
            "assert not os.environ.get('LOCAL_AGENT_LEASE_FDS'); "
            "fds=[int(n) for n in os.environ['LOCAL_AGENT_RESOURCE_LEASE_FDS'].split(',')]; "
            "result=ProcessRunner().run([sys.executable,'-c',"
            "'import os; print(os.getpgrp())']); "
            "assert result.ok, result; "
            "assert result.stdout.strip()==str(os.getpgrp()); "
            "[os.fstat(fd) for fd in fds]; "
            "Path('hostops-once.txt').write_text('verified')"
        )
        self.queue(self.task(command=shlex.join([sys.executable, "-c", program])))
        result = self.execute()
        self.assertEqual(result["status"], "done", result)
        self.assertEqual((self.cwd / "hostops-once.txt").read_text(), "verified")
        self.assertIsNone(self.execute())

    def test_changed_request_cannot_reuse_claimed_id(self) -> None:
        self.queue(self.task(command="printf original > once.txt"))
        self.execute()
        self.queue(self.task(command="printf changed > once.txt"))
        self.assertIsNone(self.execute())
        self.assertEqual((self.cwd / "once.txt").read_text(), "original")

    def test_wrong_installation_or_project_fields_are_rejected_before_effect(self) -> None:
        for field in [
            {"target_installation_id": "00000000-0000-4000-8000-000000000001"},
            {"agent_binding": "00000000-0000-4000-8000-000000000001"},
            {"work_branch": "main"},
            {"repository_id": "example-project"},
            {"target_repository_id": "local-agent"},
            {"cwd": "../relative"},
        ]:
            with self.subTest(field=field):
                self.queue(self.task(command="touch should-not-exist", **field))
                with self.assertRaises(ValueError):
                    self.execute()
                self.assertFalse((self.cwd / "should-not-exist").exists())
                self.assertFalse((self.base / "claims" / "host-example.json").exists())

    def test_no_implicit_execution_of_git_control_files(self) -> None:
        self.queue(self.task(command="echo local"))
        file = self.requests / "host-example.json"
        file.unlink()
        file.symlink_to(self.root / "not-present")
        with self.assertRaises(ValueError):
            self.execute()
        self.assertFalse((self.base / "claims" / "host-example.json").exists())

    def test_unprivileged_public_task_is_rejected_before_claim(self) -> None:
        self.queue(self.task())
        (self.requests / "host-example.json").chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private"):
            self.execute()
        self.assertFalse((self.base / "claims" / "host-example.json").exists())

    def test_missing_or_disabled_local_operator_gate_blocks_effect(self) -> None:
        self.queue(self.task(command="touch forbidden"))
        with mock.patch.object(host_tasks.operator_local, "is_disabled", return_value=True):
            with self.assertRaisesRegex(ValueError, "disabled"):
                self.execute()
        self.assertFalse((self.cwd / "forbidden").exists())
        self.identity_file.unlink()
        with self.assertRaisesRegex(ValueError, "identity"):
            self.execute()

    def test_interrupted_claim_is_not_replayed_after_crash(self) -> None:
        self.queue(self.task(command="touch never"))
        claim = self.base / "claims" / "host-example.json"
        claim.parent.mkdir(mode=0o700)
        host_tasks._durable_exclusive_claim(claim, {
            "id": "host-example", "task_digest": "abcdef", "status": "claimed",
        })
        self.assertEqual(host_tasks.recover_interrupted_local_tasks(self.state), 1)
        result = json.loads((self.base / "results" / "host-example.json").read_text())
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["failure_reason"], "interrupted_execution")
        self.assertEqual(host_tasks.recover_interrupted_local_tasks(self.state), 0)
        self.assertIsNone(self.execute())
        self.assertFalse((self.cwd / "never").exists())

    def test_recovery_refuses_to_mark_live_task_interrupted(self) -> None:
        self.base.mkdir(exist_ok=True)
        (self.base / "claims").mkdir(mode=0o700, exist_ok=True)
        (self.base / "runner.lock").touch(mode=0o600)
        with (self.base / "runner.lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "active"):
                host_tasks.recover_interrupted_local_tasks(self.state)

    def test_failed_command_reports_terminal_result_and_no_replay(self) -> None:
        self.queue(self.task(command="exit 17"))
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["commands"][0]["exit_code"], 17)
        self.assertIsNone(self.execute())

    def test_real_named_resource_is_propagated_without_machine_wide_default(self) -> None:
        self.queue(self.task(command="echo works", resources=["usb:test-device"]))
        with mock.patch.object(host_tasks, "machine_resource_lease", wraps=host_tasks.machine_resource_lease) as lease:
            outcome = self.execute()
        self.assertEqual(outcome["status"], "done")
        self.assertEqual(lease.call_args.args[0]["resources"], ["usb:test-device"])
        self.assertNotIn("machine", lease.call_args.args[0]["resources"])

    def test_dedupe_revision_blocks_different_task_ids(self) -> None:
        self.queue(self.task(command="printf first > marker.txt",
                             dedupe_key="user-maintenance", dedupe_revision=1))
        self.assertEqual(self.execute()["status"], "done")
        self.queue(self.task(task_id="second-task",
                             command="printf second > marker.txt",
                             dedupe_key="user-maintenance", dedupe_revision=1))
        with self.assertRaisesRegex(ValueError, "dedupe revision"):
            self.execute("second-task")
        self.assertEqual((self.cwd / "marker.txt").read_text(), "first")
        self.assertFalse((self.base / "claims" / "second-task.json").exists())

    def test_busy_external_device_does_not_consume_claim(self) -> None:
        self.queue(self.task(command="printf x > untouched.txt",
                             resources=["usb:test-device"]))
        with mock.patch.object(
            host_tasks, "machine_resource_lease",
            side_effect=MachineResourceBusy("usb:test-device"),
        ):
            with self.assertRaises(MachineResourceBusy):
                self.execute()
        self.assertFalse((self.base / "claims" / "host-example.json").exists())
        self.assertFalse((self.cwd / "untouched.txt").exists())

    def test_operator_emergency_disable_terminates_active_host_command(self) -> None:
        self.queue(self.task(
            command="sleep 10; printf unexpected > after-disable.txt",
            command_timeout=20, task_timeout=100,
        ))
        disabled = threading.Event()
        def enable_disable() -> None:
            time.sleep(0.8)
            disabled.set()
        toggler = threading.Thread(target=enable_disable, daemon=True)
        toggler.start()
        with mock.patch.object(
            host_tasks.operator_local, "is_disabled",
            side_effect=disabled.is_set,
        ):
            result = self.execute()
        toggler.join(timeout=3)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["failure_reason"], "operator_disabled", result)
        self.assertFalse((self.cwd / "after-disable.txt").exists())

    def test_local_operator_commands_do_not_inherit_project_codex_blacklist(self) -> None:
        from local_agent.runtime.task_contract import validate_task

        request = self.task(command="codex --help")
        normalized, workspace = host_tasks.validate_local_host_task(request, self.source)
        self.assertEqual(workspace, self.cwd)
        self.assertEqual(normalized["commands"], ["codex --help"])
        with self.assertRaisesRegex(ValueError, "local Codex"):
            validate_task(normalized)

    def test_invalid_json_and_duplicate_keys_never_claimed(self) -> None:
        req = self.requests / "host-example.json"
        req.write_text('{"id":"host-example","id":"alternate"}')
        req.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.execute()
        self.assertFalse((self.base / "claims" / "host-example.json").exists())


if __name__ == "__main__":
    unittest.main()
