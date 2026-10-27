"""Source-level single-submit flow; no Mac host worker or real credentials."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.supervisor import host_task_flow as flow
from local_agent.supervisor.host_control import HostControlSource
from local_agent.runtime.task_contract import task_digest


class SubmitHostCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="la-host-submit-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.profile = self.root / "producer-profile.json"
        self.checkout = self.root / "producer"
        self.checkout.mkdir()
        self.source = HostControlSource(
            "target-mac", "12345678-1234-4234-8234-123456789abc",
            self.checkout,
        )
        self.task = {
            "version": 1, "id": "one",
            "target_host_id": self.source.host_id,
            "target_installation_id": self.source.installation_id,
            "cwd": str(self.root), "commands": ["pwd"], "resources": [],
            "command_timeout": 20, "task_timeout": 100,
        }

    def run_flow(self, *, local: str = "a" * 40, remote: str = "a" * 40,
                 status: str = ""):
        def git(_checkout, *args):
            if args[0] == "status":
                return status
            if args[0] == "rev-parse":
                return local
            if args[0] == "ls-remote":
                return remote + "\trefs/heads/agent-control"
            raise AssertionError(args)
        published = {
            "status": "published", "task_id": "one",
            "task_digest": task_digest(self.task), "published": True,
            "control_sha": "c" * 40,
        }
        with (
            mock.patch.object(flow, "load_private_host_producer", return_value=self.source),
            mock.patch.object(flow, "_git", side_effect=git),
            mock.patch.object(flow, "sync_private_host_producer") as sync,
            mock.patch.object(flow, "stage_host_command",
                              return_value={"task_digest": task_digest(self.task)}) as stage,
            mock.patch.object(flow, "publish_staged_host_command",
                              return_value=published) as publish,
        ):
            output = flow.submit_host_command(self.profile, self.task)
            return output, sync.call_args_list, stage.call_args_list, publish.call_args_list

    def test_normal_submission_needs_no_manual_shas(self) -> None:
        out, sync, stage, publish = self.run_flow()
        self.assertEqual(out["status"], "published")
        self.assertFalse(out["synced_prior_results"])
        self.assertEqual(len(sync), 0)
        self.assertEqual(len(stage), 1)
        self.assertEqual(publish[0].kwargs["expected_control_sha"], "a" * 40)
        self.assertEqual(publish[0].kwargs["expected_task_digest"], task_digest(self.task))

    def test_clean_remote_result_advance_auto_fast_forwards(self) -> None:
        out, sync, stage, publish = self.run_flow(remote="b" * 40)
        self.assertTrue(out["synced_prior_results"])
        self.assertEqual(sync[0].kwargs["expected_local_sha"], "a" * 40)
        self.assertEqual(sync[0].kwargs["expected_remote_sha"], "b" * 40)
        self.assertEqual(publish[0].kwargs["expected_control_sha"], "b" * 40)

    def test_dirty_checkout_never_syncs_or_stages(self) -> None:
        with (
            mock.patch.object(flow, "load_private_host_producer", return_value=self.source),
            mock.patch.object(flow, "_git", return_value="?? pending.json\n"),
            mock.patch.object(flow, "sync_private_host_producer") as sync,
            mock.patch.object(flow, "stage_host_command") as stage,
        ):
            with self.assertRaisesRegex(ValueError, "not clean"):
                flow.submit_host_command(self.profile, self.task)
            sync.assert_not_called()
            stage.assert_not_called()

    def test_wrong_installation_rejected_before_git_or_effects(self) -> None:
        self.task["target_installation_id"] = "12345678-1234-4234-8234-123456789abd"
        with (
            mock.patch.object(flow, "load_private_host_producer", return_value=self.source),
            mock.patch.object(flow, "_git") as git,
            mock.patch.object(flow, "stage_host_command") as stage,
        ):
            with self.assertRaises(ValueError):
                flow.submit_host_command(self.profile, self.task)
            git.assert_not_called()
            stage.assert_not_called()

    def test_uncertain_publish_not_retried(self) -> None:
        def git(_checkout, *args):
            return "a" * 40 if args[0] == "rev-parse" else (
                "a" * 40 + "\trefs/heads/agent-control" if args[0] == "ls-remote" else ""
            )
        with (
            mock.patch.object(flow, "load_private_host_producer", return_value=self.source),
            mock.patch.object(flow, "_git", side_effect=git),
            mock.patch.object(flow, "stage_host_command",
                              return_value={"task_digest": task_digest(self.task)}),
            mock.patch.object(flow, "publish_staged_host_command",
                              side_effect=RuntimeError("uncertain push")) as publish,
        ):
            with self.assertRaisesRegex(RuntimeError, "uncertain"):
                flow.submit_host_command(self.profile, self.task)
            publish.assert_called_once()

    def test_status_reports_pending_without_replaying_host_task(self) -> None:
        with (
            mock.patch.object(flow, "load_private_host_producer",
                              return_value=self.source),
            mock.patch.object(flow, "_fresh_private_head",
                              return_value=("b" * 40, True)),
            mock.patch.object(flow, "_blob_from_snapshot",
                              return_value=json.dumps(self.task).encode()),
            mock.patch.object(flow, "read_host_command_result") as read,
        ):
            result = flow.current_host_command_result(self.profile, "one")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["task_digest"], task_digest(self.task))
        read.assert_not_called()

    def test_status_reads_terminal_result_only_from_matching_git_snapshot(self) -> None:
        result_dir = self.checkout / self.source.namespace / "task-results"
        result_dir.mkdir(parents=True)
        (result_dir / "one.json").write_text("{}")
        expected = {"status": "verified_snapshot",
                    "result": {"status": "done", "id": "one"}}
        with (
            mock.patch.object(flow, "load_private_host_producer",
                              return_value=self.source),
            mock.patch.object(flow, "_fresh_private_head",
                              return_value=("b" * 40, True)),
            mock.patch.object(flow, "_blob_from_snapshot",
                              return_value=json.dumps(self.task).encode()),
            mock.patch.object(flow, "read_host_command_result",
                              return_value=expected) as read,
        ):
            result = flow.current_host_command_result(self.profile, "one")
        self.assertEqual(result, expected)
        self.assertEqual(read.call_args.kwargs["expected_control_sha"], "b" * 40)
        self.assertEqual(read.call_args.kwargs["expected_task_digest"],
                         task_digest(self.task))

    def test_status_wrong_installation_rejected_before_result_read(self) -> None:
        invalid = {**self.task,
                   "target_installation_id": "12345678-1234-4234-8234-123456789abd"}
        with (
            mock.patch.object(flow, "load_private_host_producer",
                              return_value=self.source),
            mock.patch.object(flow, "_fresh_private_head",
                              return_value=("b" * 40, False)),
            mock.patch.object(flow, "_blob_from_snapshot",
                              return_value=json.dumps(invalid).encode()),
            mock.patch.object(flow, "read_host_command_result") as read,
        ):
            with self.assertRaises(ValueError):
                flow.current_host_command_result(self.profile, "one")
            read.assert_not_called()

    def test_cli_registers_simple_submit(self) -> None:
        from local_agent.cli.diagnostics import build_parser

        options = build_parser().parse_args([
            "host-submit", str(self.profile), "--profile", str(self.profile),
        ])
        self.assertEqual(options.profile, self.profile)
        self.assertEqual(options.path, str(self.profile))
        status = build_parser().parse_args([
            "host-status", "one", "--profile", str(self.profile),
        ])
        self.assertEqual(status.task_id, "one")
        self.assertEqual(status.profile, self.profile)


if __name__ == "__main__":
    unittest.main()
