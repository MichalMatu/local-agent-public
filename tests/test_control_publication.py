from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from local_agent.foundation import core, storage
from local_agent.daemon import service as agentd


class ControlPublicationTests(unittest.TestCase):
    def git(self, path: Path, *args: str) -> str:
        return subprocess.run(
            ['git', *args], cwd=path, check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
        ).stdout.strip()

    def test_competing_host_push_is_rebased_without_losing_either_result_or_legacy_task(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            remote, pi, mac = (root / name for name in ('remote.git', 'pi', 'mac'))
            remote.mkdir()
            self.git(remote, 'init', '--bare', '--initial-branch=agent-control')
            self.git(root, 'clone', str(remote), str(pi))
            for repo in (pi,):
                self.git(repo, 'config', 'user.name', 'Publication Test')
                self.git(repo, 'config', 'user.email', 'publication@example.invalid')
            legacy = '.agent/tasks/mac-original.json'
            (pi / legacy).parent.mkdir(parents=True)
            (pi / legacy).write_text('{"id":"mac-original"}\n')
            self.git(pi, 'add', '--', legacy)
            self.git(pi, 'commit', '-m', 'Seed legacy task')
            self.git(pi, 'push', 'origin', 'agent-control')
            self.git(root, 'clone', str(remote), str(mac))
            self.git(mac, 'config', 'user.name', 'Other Host')
            self.git(mac, 'config', 'user.email', 'other@example.invalid')
            legacy_blob = self.git(remote, 'rev-parse', f'HEAD:{legacy}')
            pi_result = '.agent/hosts/pi-zero2w/results/task-pi.json'
            mac_result = '.agent/hosts/macbook/results/task-mac.json'
            original_retry = storage.run_git_with_network_retry
            pushes = 0

            def with_competing_push(module, args, cwd, **kwargs):
                nonlocal pushes
                if args[1] == 'push':
                    pushes += 1
                    if pushes == 1:
                        (mac / mac_result).parent.mkdir(parents=True)
                        (mac / mac_result).write_text('{"id":"task-mac","status":"done"}\n')
                        self.git(mac, 'add', '--', mac_result)
                        self.git(mac, 'commit', '-m', 'Other host result')
                        self.git(mac, 'push', 'origin', 'agent-control')
                return original_retry(module, args, cwd, **kwargs)

            with mock.patch.object(core, 'CONTROL', pi), mock.patch.object(core, 'CONTROL_BRANCH', 'agent-control'), mock.patch.object(storage, 'run_git_with_network_retry', side_effect=with_competing_push):
                core.publish_control_json(pi_result, {'id':'task-pi', 'status':'done'}, commit_message='Pi result')
            self.assertEqual(pushes, 2)
            self.assertEqual(json.loads(self.git(remote, 'show', f'HEAD:{pi_result}'))['id'], 'task-pi')
            self.assertEqual(json.loads(self.git(remote, 'show', f'HEAD:{mac_result}'))['id'], 'task-mac')
            self.assertEqual(self.git(remote, 'rev-parse', f'HEAD:{legacy}'), legacy_blob)
            self.assertEqual(self.git(pi, 'status', '--porcelain'), '')

    def test_durable_result_reconciles_two_consecutive_real_git_writer_races(self) -> None:
        """Remote result delivery should not back off for minutes after two short races."""
        with tempfile.TemporaryDirectory(prefix="host-result-contention-") as folder:
            root = Path(folder)
            remote, host, competitor = (root / name for name in ("remote.git", "host", "writer"))
            remote.mkdir()
            self.git(remote, "init", "--bare", "--initial-branch=agent-control")
            self.git(root, "clone", str(remote), str(host))
            self.git(host, "config", "user.name", "Host Result")
            self.git(host, "config", "user.email", "host@example.invalid")
            seed = host / ".agent" / "tasks" / "request.json"
            seed.parent.mkdir(parents=True)
            seed.write_text('{"id":"request"}\n')
            self.git(host, "add", "--", ".agent/tasks/request.json")
            self.git(host, "commit", "-m", "Seed host request")
            self.git(host, "push", "origin", "agent-control")
            self.git(root, "clone", str(remote), str(competitor))
            self.git(competitor, "config", "user.name", "Competing Writer")
            self.git(competitor, "config", "user.email", "writer@example.invalid")

            result_path = ".agent/hosts/macbook/supervisor/12345678-1234-4234-8234-123456789abc/task-results/result-one.json"
            payload = {"version": 1, "id": "result-one", "status": "done", "effect_count": 1}
            original_retry = storage.run_git_with_network_retry
            pushes = 0

            def inject_two_concurrent_writers(module, args, cwd, **kwargs):
                nonlocal pushes
                if args[1] == "push":
                    pushes += 1
                    if pushes <= 2:
                        self.git(competitor, "pull", "--rebase", "origin", "agent-control")
                        remote_relative = f".agent/status/competing-{pushes}.json"
                        target = competitor / remote_relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(json.dumps({"writer": pushes}) + "\n")
                        self.git(competitor, "add", "--", remote_relative)
                        self.git(competitor, "commit", "-m", f"Writer status {pushes}")
                        self.git(competitor, "push", "origin", "agent-control")
                return original_retry(module, args, cwd, **kwargs)

            with (
                mock.patch.object(core, "CONTROL", host),
                mock.patch.object(core, "CONTROL_BRANCH", "agent-control"),
                mock.patch.object(storage, "run_git_with_network_retry",
                                  side_effect=inject_two_concurrent_writers),
            ):
                agentd.publish_control_json(
                    result_path, payload, commit_message="Host task result: result-one",
                    ensure_remote=True,
                )
            self.assertEqual(pushes, 3)
            self.assertEqual(json.loads(self.git(remote, "show", f"HEAD:{result_path}")), payload)
            for number in (1, 2):
                self.assertEqual(
                    json.loads(self.git(remote, "show", f"HEAD:.agent/status/competing-{number}.json")),
                    {"writer": number},
                )
            self.assertEqual(
                self.git(host, "log", "--format=%s", "--", result_path).splitlines(),
                ["Host task result: result-one"],
            )
            self.assertEqual(self.git(host, "status", "--porcelain"), "")

    def test_git_compare_and_swap_push_race_is_reconciled_in_turn(self) -> None:
        module = SimpleNamespace(CONTROL=Path('/unused'), CONTROL_BRANCH='agent-control')
        changed = (
            "! [remote rejected] agent-control -> agent-control "
            "(cannot lock ref 'refs/heads/agent-control': is at "
            + "b" * 40 + " but expected " + "a" * 40 + ")"
        )
        validator = mock.Mock()
        with mock.patch.object(storage, "run_git_with_network_retry", side_effect=[
            {'exit_code': 0, 'output': ''},
            {'exit_code': 1, 'output': changed},
            {'exit_code': 0, 'output': ''},
            {'exit_code': 0, 'output': ''},
        ]) as retry:
            self.assertTrue(storage.publish_control_commit(
                module, attempts=2, post_pull_validate=validator,
            ))
        self.assertEqual(retry.call_count, 4)
        self.assertEqual(validator.call_count, 2)

    def test_remote_pre_receive_rejection_is_not_a_competing_writer(self) -> None:
        module = SimpleNamespace(CONTROL=Path('/unused'), CONTROL_BRANCH='agent-control')
        for output in (
            "! [remote rejected] agent-control -> agent-control (pre-receive hook declined)",
            "! [remote rejected] agent-control -> agent-control (protected branch hook)",
            "remote: error: unable to write pack file",
            "! [rejected] agent-control -> agent-control (permission denied)",
        ):
            with self.subTest(output=output), mock.patch.object(
                storage, "run_git_with_network_retry", side_effect=[
                    {'exit_code': 0, 'output': ''}, {'exit_code': 1, 'output': output},
                ],
            ) as retry:
                with self.assertRaises(RuntimeError):
                    storage.publish_control_commit(module, attempts=3)
                self.assertEqual(retry.call_count, 2)

    def test_permanent_failure_is_not_retried_as_a_publication_race(self) -> None:
        module = SimpleNamespace(CONTROL=Path('/unused'), CONTROL_BRANCH='agent-control')
        with mock.patch.object(storage, 'run_git_with_network_retry', side_effect=[
            {'exit_code':0, 'output':''},
            {'exit_code':128, 'output':'Permission denied (publickey)'},
        ]) as retry:
            with self.assertRaisesRegex(RuntimeError, 'Permission denied'):
                storage.publish_control_commit(module)
        self.assertEqual(retry.call_count, 2)

    def test_race_retries_and_authorization_rechecks_are_bounded(self) -> None:
        module = SimpleNamespace(CONTROL=Path('/unused'), CONTROL_BRANCH='agent-control')
        validator = mock.Mock()
        with mock.patch.object(storage, 'run_git_with_network_retry', side_effect=[
            {'exit_code':0, 'output':''}, {'exit_code':1, 'output':'[rejected] fetch first'},
            {'exit_code':0, 'output':''}, {'exit_code':1, 'output':'[rejected] fetch first'},
        ]) as retry:
            with self.assertRaises(RuntimeError):
                storage.publish_control_commit(module, post_pull_validate=validator)
        self.assertEqual(retry.call_count, 4)
        self.assertEqual(validator.call_count, 2)


if __name__ == '__main__':
    unittest.main()
