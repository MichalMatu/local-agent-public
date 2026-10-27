"""Worker fixtures: authenticated remote intake and durable no-replay publication."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from local_agent.foundation.process import ExecutionLeaseBusy
from local_agent.repository.host_routing import HostIdentity
from local_agent.runtime.task_contract import task_digest
from local_agent.supervisor.host_control import HostControlSource
from local_agent.supervisor.host_remote_intake import RemoteHostTaskPreview
from local_agent.supervisor.host_remote_intake import HostControlSnapshotChanged
from local_agent.supervisor import host_remote_worker as worker
from local_agent.supervisor.resources import MachineResourceBusy
from local_agent.supervisor.macos_release_admission import CONFIG as MAC_ADMISSION_CONFIG

HOST_ID = "host-mac"
INSTALL = "12345678-1234-4234-8234-123456789abc"


class RemoteHostWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(prefix="remote-host-worker-test-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        # Native Mac fixture admission, never a bypass of production release guard.
        admission = self.state / MAC_ADMISSION_CONFIG
        admission.write_text(json.dumps({"version": 1, "enabled": True}) + "\n")
        admission.chmod(0o600)
        self.checkout = self.root / "git-control"
        self.checkout.mkdir()
        self.source = HostControlSource(HOST_ID, INSTALL, self.checkout)
        self.identity = HostIdentity(HOST_ID, INSTALL)
        self.identity_file = self.state / "identity.json"
        self.identity_file.write_text(json.dumps(self.identity.as_record()))
        self.identity_file.chmod(0o600)
        self.config = self.state / "host-task-intake.json"
        self.config.write_text(json.dumps({
            "version": 1, "enabled": True, "host_id": HOST_ID,
            "installation_id": INSTALL, "identity_file": str(self.identity_file),
        }))
        self.config.chmod(0o600)
        self.task = {
            "version": 1, "id": "sample", "target_host_id": HOST_ID,
            "target_installation_id": INSTALL, "cwd": str(self.root),
            "commands": ["printf first >> counter.txt"],
            "resources": [], "command_timeout": 20, "task_timeout": 100,
        }
        self.preview = RemoteHostTaskPreview(
            task=self.task, task_id="sample", task_digest=task_digest(self.task),
            control_sha="a" * 40, operator_sha="b" * 40,
        )
        self.patches = [
            mock.patch.object(worker, "load_host_control_source", return_value=self.source),
            mock.patch.object(worker, "preview_authorized_remote_host_task",
                              return_value=self.preview),
            mock.patch.object(worker.operator_local, "is_disabled", return_value=False),
            mock.patch.object(worker.supervisor_control, "bind_supervisor_control"),
            mock.patch.object(worker.agentd, "publish_control_json"),
            mock.patch.object(worker, "host_supervisor_lease",
                              return_value=contextlib.nullcontext()),
            mock.patch.object(worker, "validate_host_control_checkout"),
            # These tests exercise remote admission, command execution, and
            # durable result publication—not host metric probes. Do not start
            # asynchronous macOS top/ps telemetry children from this fixture:
            # they can outlive the short command and contaminate the next
            # process-lifecycle test's live-process registry.
            mock.patch("local_agent.runtime.telemetry._safe_command", return_value=None),
            mock.patch.object(worker, "refresh_host_control_checkout"),
            mock.patch.object(worker, "require_host_publication_identity"),
            mock.patch.object(worker, "_published_host_result_names", return_value=frozenset()),
        ]
        self.mocks = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)

    def run_one(self) -> str:
        return worker.run_remote_host_task_once(
            "sample", state_dir=self.state, operator_checkout=self.root,
            production_checkout=self.root / "installed",
        )

    def test_discovery_skips_foreign_installations_without_claiming(self) -> None:
        inbox = self.checkout / self.source.namespace / "tasks"
        inbox.mkdir(parents=True)
        local = dict(self.task)
        (inbox / "sample.json").write_text(json.dumps(local))
        foreign = {**local, "id": "foreign",
                   "target_installation_id": "87654321-4321-4321-8321-cba987654321"}
        (inbox / "foreign.json").write_text(json.dumps(foreign))
        (inbox / "malformed.json").write_text("not-json")
        (inbox / "duplicate.json").write_text(
            '{"target_host_id":"other","target_host_id":"host-mac",'
            '"target_installation_id":"other"}'
        )
        self.assertEqual(
            worker.pending_host_task_ids(self.source, self.state),
            ("duplicate", "malformed", "sample"),
        )
        # The scheduler never creates a claim or terminal receipt for
        # another installation, and malformed tasks still reach validation.
        self.assertFalse((self.state / "host-tasks" / "claims").exists())
        foreign["target_installation_id"] = INSTALL
        (inbox / "foreign.json").write_text(json.dumps(foreign))
        self.assertEqual(
            worker.pending_host_task_ids(self.source, self.state),
            ("duplicate", "foreign", "malformed", "sample"),
        )

    def test_discovery_keeps_invalid_targets_for_validation(self) -> None:
        inbox = self.checkout / self.source.namespace / "tasks"
        inbox.mkdir(parents=True)
        for index, target in enumerate(("", "not-a-uuid", 42, None)):
            task = {**self.task, "target_installation_id": target}
            (inbox / f"invalid-{index}.json").write_text(json.dumps(task))
        self.assertEqual(worker.pending_host_task_ids(self.source, self.state),
                         tuple(f"invalid-{index}" for index in range(4)))

    def test_deep_json_does_not_abort_discovery_of_later_task(self) -> None:
        inbox = self.checkout / self.source.namespace / "tasks"
        inbox.mkdir(parents=True)
        (inbox / "deep.json").write_text("[" * 2000 + "0" + "]" * 2000)
        (inbox / "sample.json").write_text(json.dumps(self.task))
        self.assertEqual(worker.pending_host_task_ids(self.source, self.state),
                         ("deep", "sample"))

    def test_worker_exit_distinguishes_contention_from_real_errors(self) -> None:
        argv = ["worker", "--id", "sample", "--state-dir", str(self.state),
                "--operator-checkout", str(self.root),
                "--production-checkout", str(self.root / "installed"), "--execute"]
        for error, expected in (
            (ExecutionLeaseBusy("host-control"), worker.HOST_WORKER_CONTROL_DEFERRED),
            (HostControlSnapshotChanged("advanced"), worker.HOST_WORKER_CONTROL_DEFERRED),
            (ValueError("wrong installation"), 2),
            (PermissionError("denied"), 2),
            (RuntimeError("publication failed"), 2),
        ):
            with (
                self.subTest(error=type(error).__name__),
                mock.patch("sys.argv", argv),
                mock.patch.object(worker, "run_remote_host_task_once", side_effect=error),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(worker.main(), expected)

    def test_control_lease_busy_before_preview_never_stages_or_claims(self) -> None:
        self.mocks[5].side_effect = ExecutionLeaseBusy("host-control")
        with self.assertRaises(ExecutionLeaseBusy):
            self.run_one()
        self.mocks[1].assert_not_called()
        self.assertFalse((self.state / "host-tasks").exists())
        self.assertFalse((self.root / "counter.txt").exists())

    def test_publication_lease_busy_keeps_result_and_never_reexecutes(self) -> None:
        self.mocks[5].side_effect = [contextlib.nullcontext(), ExecutionLeaseBusy("host-control")]
        with self.assertRaises(ExecutionLeaseBusy):
            self.run_one()
        result_path = self.state / "host-tasks" / "results" / "sample.json"
        result = result_path.read_bytes()
        self.assertEqual((self.root / "counter.txt").read_text(), "first")
        self.mocks[5].side_effect = None
        self.assertEqual(self.run_one(), "published")
        self.assertEqual(result_path.read_bytes(), result)
        self.assertEqual((self.root / "counter.txt").read_text(), "first")
        self.mocks[1].assert_called_once()

    def test_explicit_private_intake_only(self) -> None:
        self.config.unlink()
        self.assertIsNone(worker.load_remote_intake_config(self.state, self.source))
        with self.assertRaisesRegex(ValueError, "not locally enabled"):
            self.run_one()
        self.assertFalse((self.root / "counter.txt").exists())

    def test_owner_github_mode_is_explicit_and_forwarded_to_worker(self) -> None:
        value = json.loads(self.config.read_text())
        value["authorization"] = "github_writers"
        self.config.write_text(json.dumps(value))
        self.assertEqual(worker.load_remote_intake_config(self.state, self.source).authorization,
                         "github_writers")
        self.assertEqual(self.run_one(), "published")
        self.assertEqual(self.mocks[1].call_args.kwargs["authorization"], "github_writers")
        self.assertEqual((self.root / "counter.txt").read_text(), "first")

    def test_unknown_authorization_model_does_not_execute(self) -> None:
        value = json.loads(self.config.read_text())
        for choice in ("new_root_model", None, False, 1):
            with self.subTest(choice=choice):
                value["authorization"] = choice
                self.config.write_text(json.dumps(value))
                with self.assertRaisesRegex(ValueError, "authorization"):
                    self.run_one()
        self.assertFalse((self.root / "counter.txt").exists())

    def test_wrong_installation_and_public_config_refused(self) -> None:
        self.config.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private"):
            self.run_one()
        self.config.chmod(0o600)
        cfg = json.loads(self.config.read_text())
        cfg["installation_id"] = "12345678-1234-4234-8234-123456789abd"
        self.config.write_text(json.dumps(cfg))
        with self.assertRaisesRegex(ValueError, "different installation"):
            self.run_one()

    def test_missing_control_git_author_never_admits_command(self) -> None:
        self.mocks[9].side_effect = ValueError(
            "dedicated host control checkout requires local Git user.email"
        )
        with self.assertRaisesRegex(ValueError, "local Git user.email"):
            self.run_one()
        self.mocks[1].assert_not_called()
        self.assertFalse((self.root / "counter.txt").exists())
        self.assertFalse((self.state / "host-tasks" / "claims" / "sample.json").exists())
        self.assertFalse((self.state / "host-tasks" / "remote-approvals" / "sample.json").exists())

    def test_task_executed_once_then_published_from_durable_result(self) -> None:
        self.assertEqual(self.run_one(), "published")
        self.assertEqual((self.root / "counter.txt").read_text(), "first")
        self.assertEqual(self.run_one(), "published")
        self.assertEqual((self.root / "counter.txt").read_text(), "first")
        self.assertEqual(self.mocks[4].call_count, 2)
        payload = self.mocks[4].call_args.args[1]
        self.assertEqual(payload["task_digest"], self.preview.task_digest)
        self.assertEqual(payload["target_host_id"], HOST_ID)
        self.assertIn("/task-results/sample.json", self.mocks[4].call_args.args[0])

    def test_network_failure_retries_result_without_reexecuting_command(self) -> None:
        self.mocks[4].side_effect = [OSError("network down"), None]
        with self.assertRaisesRegex(OSError, "network down"):
            self.run_one()
        self.assertEqual((self.root / "counter.txt").read_text(), "first")
        self.assertEqual(self.run_one(), "published")
        self.assertEqual((self.root / "counter.txt").read_text(), "first")

    def test_invalid_request_retries_only_durable_result_publication(self) -> None:
        self.preview = replace(self.preview, validation_error="ValueError: invalid timeout")
        self.mocks[1].return_value = self.preview
        self.mocks[4].side_effect = [OSError("network down"), None]
        with mock.patch.object(worker.host_tasks, "run_local_host_task") as execute:
            with self.assertRaisesRegex(OSError, "network down"):
                self.run_one()
            result_path = self.state / "host-tasks" / "results" / "sample.json"
            durable = result_path.read_bytes()
            self.mocks[1].side_effect = AssertionError("must not validate again")
            self.assertEqual(self.run_one(), "published")
            execute.assert_not_called()
        self.assertEqual(result_path.read_bytes(), durable)
        self.assertEqual(self.mocks[4].call_args.args[1]["result"]["failure_reason"],
                         "invalid_task_file")
        self.assertFalse((self.state / "host-tasks" / "claims" / "sample.json").exists())
        self.assertFalse((self.root / "counter.txt").exists())

    def test_result_without_remote_authorization_receipt_is_not_published(self) -> None:
        results = self.state / "host-tasks" / "results"
        results.mkdir(parents=True, mode=0o700)
        result = results / "sample.json"
        result.write_text(json.dumps({
            "id": "sample", "status": "done", "task_digest": task_digest(self.task),
        }))
        result.chmod(0o600)
        with self.assertRaises(FileNotFoundError):
            self.run_one()
        self.mocks[4].assert_not_called()

    def test_mismatched_previous_remote_grant_cannot_be_reused(self) -> None:
        base = self.state / "host-tasks"
        base.mkdir(mode=0o700)
        approvals = base / "remote-approvals"
        approvals.mkdir(mode=0o700)
        f = approvals / "sample.json"
        f.write_text(json.dumps({
            "id": "sample", "host_id": HOST_ID, "installation_id": INSTALL,
            "task_digest": "c" * 64, "control_sha": "a" * 40,
            "operator_sha": "b" * 40,
        }))
        f.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "different bytes"):
            self.run_one()
        self.assertFalse((self.root / "counter.txt").exists())

    def test_busy_device_is_deferred_before_claim_and_execution(self) -> None:
        self.task["resources"] = ["usb:test-device"]
        self.preview = RemoteHostTaskPreview(
            task=self.task, task_id="sample", task_digest=task_digest(self.task),
            control_sha="a" * 40, operator_sha="b" * 40,
        )
        self.mocks[1].return_value = self.preview
        with mock.patch.object(
            worker.host_tasks, "machine_resource_lease",
            side_effect=MachineResourceBusy("usb:test-device"),
        ):
            self.assertEqual(self.run_one(), "deferred")
        self.assertFalse((self.root / "counter.txt").exists())
        self.assertFalse((self.state / "host-tasks" / "claims" / "sample.json").exists())

    def test_abandoned_approved_claim_recovers_unknown_without_git_inbox(self) -> None:
        base = self.state / "host-tasks"
        worker._record_approved_remote_task(self.preview, self.source, self.state)
        worker._stage_private_remote_task(self.preview, self.state)
        claims = base / "claims"
        claims.mkdir(mode=0o700)
        worker.host_tasks._durable_exclusive_claim(claims / "sample.json", {
            "id": "sample", "task_digest": self.preview.task_digest,
        })
        self.assertEqual(worker.pending_host_task_ids(self.source, self.state), ("sample",))
        self.assertEqual(self.run_one(), "published")
        result = json.loads((base / "results" / "sample.json").read_text())
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["replayed"])
        self.assertFalse((self.root / "counter.txt").exists())
        self.mocks[1].assert_not_called()

    def test_result_republication_does_not_require_git_inbox(self) -> None:
        base = self.state / "host-tasks"
        worker._record_approved_remote_task(self.preview, self.source, self.state)
        results = base / "results"
        results.mkdir(mode=0o700)
        result_file = results / "sample.json"
        result_file.write_text(json.dumps({
            "id": "sample", "status": "unknown", "task_digest": self.preview.task_digest,
        }))
        result_file.chmod(0o600)
        self.assertEqual(worker.pending_host_task_ids(self.source, self.state), ("sample",))
        self.assertEqual(self.run_one(), "published")
        self.mocks[1].assert_not_called()

    def test_result_leaf_symlink_cannot_redirect_publication(self) -> None:
        directory = self.checkout / self.source.namespace / worker.HOST_REMOTE_RESULT_DIR
        directory.mkdir(parents=True)
        outside = self.root / "another-host-result.json"
        outside.write_text("untouched")
        (directory / "sample.json").symlink_to(outside)
        inbox = self.checkout / self.source.namespace / "tasks"
        inbox.mkdir()
        (inbox / "sample.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "symlink"):
            worker.pending_host_task_ids(self.source, self.state)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.run_one()
        self.assertEqual(outside.read_text(), "untouched")
        self.assertFalse((self.root / "counter.txt").exists())
        self.mocks[4].assert_not_called()

    def test_unapproved_intent_never_runs_or_stages(self) -> None:
        self.mocks[1].side_effect = ValueError("operator approval absent")
        with self.assertRaisesRegex(ValueError, "approval absent"):
            self.run_one()
        self.assertFalse((self.root / "counter.txt").exists())
        self.assertFalse((self.state / "host-tasks" / "requests" / "sample.json").exists())

    def test_host_inbox_symlink_ancestors_are_not_traversed(self) -> None:
        namespace_root = self.checkout / ".agent"
        namespace_root.mkdir()
        (namespace_root / "hosts").symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            worker.pending_host_task_ids(self.source, self.state)

    def test_local_results_not_mistaken_for_remote_results(self) -> None:
        inbox = self.checkout / self.source.namespace / "tasks"
        inbox.mkdir(parents=True)
        (inbox / "sample.json").write_text("{}")
        remote = self.checkout / self.source.namespace / worker.HOST_REMOTE_RESULT_DIR
        remote.mkdir()
        (remote / "sample.json").write_text("{}")
        self.mocks[10].return_value = frozenset({"sample.json"})
        local_results = self.state / "host-tasks" / "results"
        local_results.mkdir(parents=True, mode=0o700)
        (local_results / "unrelated.json").write_text("{}")
        self.assertEqual(worker.pending_host_task_ids(self.source, self.state), ())
        (remote / "sample.json").unlink()
        self.mocks[10].return_value = frozenset()
        self.assertEqual(worker.pending_host_task_ids(self.source, self.state), ("sample",))


if __name__ == "__main__":
    unittest.main()
