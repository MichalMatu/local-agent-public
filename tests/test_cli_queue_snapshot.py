"""Offline Git control task/result inspection with no task admission or mutation."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.cli.diagnostics import build_parser
from local_agent.cli.queue import command_queue
from local_agent.runtime.queue_snapshot import observe_local_task_queue
from tests.test_multirepo_integration import create_repository_fixture, git, write_registry


class LocalQueueSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="la-queue-snapshot-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.item = create_repository_fixture(self.root, "queue-project")
        self.control = Path(self.item["control"])
        self.registry = write_registry(self.root, (self.item,))
        self.task_file = self.control / ".agent/tasks/shared-task-id.json"
        self.result_file = self.control / ".agent/results/shared-task-id.json"

    def inspect(self, repo_id: str = "queue-project") -> dict[str, object]:
        return observe_local_task_queue(
            registry_file=self.registry, repository_id=repo_id,
        )

    def commit(self, path: Path) -> None:
        git(["add", str(path.relative_to(self.control))], cwd=self.control)
        git(["commit", "-m", "Fixture: update control manifest"], cwd=self.control)

    def result(self, *, task_id: str = "shared-task-id", status: str = "done") -> None:
        target = self.control / ".agent/results" / f"{task_id}.json"
        target.write_text(
            json.dumps({"id": task_id, "status": status}), encoding="utf-8",
        )
        self.commit(target)

    def test_unresolved_manifest_is_not_admission_or_a_verified_remote_queue(self) -> None:
        before = git(["status", "--porcelain"], cwd=self.control)
        result = self.inspect()
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["counts"], {
            "awaiting_result": 1, "done": 0, "failed": 0, "unverified": 0,
        })
        self.assertFalse(result["remote_freshness_verified"])
        self.assertFalse(result["local_claims_observed"])
        self.assertEqual(result["execution_authority"], "not_asserted")
        self.assertEqual(result["replay_authority"], "not_asserted")
        self.assertEqual(result["control_commit"], git(["rev-parse", "HEAD"], cwd=self.control).strip())
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertNotIn(str(self.item["agent_binding"]), json.dumps(result))
        self.assertEqual(git(["status", "--porcelain"], cwd=self.control), before)

    def test_done_and_failed_terminal_result_records(self) -> None:
        self.result(status="done")
        report = self.inspect()
        self.assertEqual(report["counts"]["done"], 1)
        self.assertEqual(report["counts"]["awaiting_result"], 0)
        self.result(status="failed")
        report = self.inspect()
        self.assertEqual(report["counts"]["failed"], 1)
        self.assertEqual(report["counts"]["done"], 0)

    def test_uncommitted_result_is_not_pretended_published(self) -> None:
        self.result_file.write_text('{"id":"shared-task-id","status":"done"}', encoding="utf-8")
        self.assertEqual(self.inspect()["counts"]["awaiting_result"], 1)
        self.assertTrue(self.result_file.exists())

    def test_malformed_manifest_or_mismatched_binding_is_unverified(self) -> None:
        manifest = json.loads(self.task_file.read_text(encoding="utf-8"))
        manifest["agent_binding"] = "00000000-0000-0000-0000-000000000000"
        self.task_file.write_text(json.dumps(manifest), encoding="utf-8")
        self.commit(self.task_file)
        result = self.inspect()
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["counts"]["unverified"], 1)
        self.assertEqual(result["counts"]["awaiting_result"], 0)

    def test_invalid_result_and_orphan_result_are_unverified(self) -> None:
        self.result_file.write_text('{"id":"different","status":"done"}', encoding="utf-8")
        self.commit(self.result_file)
        self.assertEqual(self.inspect()["counts"]["unverified"], 1)
        target = self.control / ".agent/results/orphan.json"
        target.write_text('{"id":"orphan","status":"done"}', encoding="utf-8")
        self.commit(target)
        self.assertEqual(self.inspect()["counts"]["unverified"], 2)

    def test_git_symlink_task_record_fails_closed(self) -> None:
        self.task_file.unlink()
        self.task_file.symlink_to(self.control / ".agent/binding.json")
        self.commit(self.task_file)
        response = self.inspect()
        self.assertEqual(response["status"], "unverified")
        self.assertIsNone(response["counts"]["awaiting_result"])
        self.assertIn("local_control_snapshot_unverified", response["warnings"])

    def test_mismatched_registry_control_identity_is_unverified(self) -> None:
        payload = json.loads(self.registry.read_text(encoding="utf-8"))
        payload["repositories"][0]["agent_binding"] = "00000000-0000-0000-0000-000000000000"
        self.registry.write_text(json.dumps(payload), encoding="utf-8")
        report = self.inspect()
        self.assertEqual(report["status"], "unverified")
        self.assertIsNone(report["control_commit"])
        self.assertIsNone(report["counts"]["done"])

    def test_wrong_checkout_branch_or_missing_registry_is_unverified(self) -> None:
        git(["checkout", "--detach", "HEAD"], cwd=self.control)
        self.assertEqual(self.inspect()["status"], "unverified")
        self.registry.unlink()
        self.assertEqual(self.inspect()["status"], "unverified")

    def test_zero_project_installation_is_supported_without_fabrication(self) -> None:
        self.registry.write_text('{"version":1,"repositories":[]}', encoding="utf-8")
        self.assertEqual(self.inspect()["status"], "unverified")
        self.assertEqual(self.inspect()["counts"]["awaiting_result"], None)

    def test_cli_is_read_only_adapter_without_fetch_or_execution(self) -> None:
        args = build_parser().parse_args([
            "queue", "--repository-id", "queue-project",
            "--registry-file", str(self.registry),
        ])
        with mock.patch("local_agent.runtime.queue_snapshot.subprocess.run",
                        wraps=__import__("subprocess").run) as git_call:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(command_queue(args), 0)
            for call in git_call.call_args_list:
                command = call.args[0]
                self.assertNotIn("fetch", command)
                self.assertNotIn("pull", command)
                self.assertNotIn("checkout", command)
                self.assertNotIn("push", command)
        self.assertEqual(json.loads(out.getvalue())["counts"]["awaiting_result"], 1)
        self.result(status="done")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(command_queue(args), 0)
        self.assertEqual(json.loads(out.getvalue())["counts"]["done"], 1)
        self.registry.unlink()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(command_queue(args), 2)
        self.assertEqual(json.loads(out.getvalue())["status"], "unverified")


if __name__ == "__main__":
    unittest.main()
