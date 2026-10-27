"""Real local Git fixture tests for read-only, pinned host task preview."""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from local_agent.cli import diagnostics
from local_agent.repository.binding import resolve_execution_target
from local_agent.repository.host_mailbox import prepare_host_mailbox_task, write_host_mailbox_bundle
from local_agent.repository.host_mailbox_reader import preview_host_mailbox_snapshot
from local_agent.repository.host_routing import validate_host_enrollments
from local_agent.runtime.task_contract import task_digest

BINDING = "b0e94f93-478f-4fc6-abed-159d3702362f"
INSTALLATION = "12345678-1234-4234-8234-123456789abc"


class HostMailboxReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.work = self.root / "control"
        self.work.mkdir()
        self.remote = self.root / "remote.git"
        self.git("init", "-q", "-b", "agent-control")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        self.git("remote", "add", "origin", str(self.remote))
        (self.work / ".agent").mkdir()
        (self.work / ".agent/binding.json").write_text(json.dumps({
            "version": 1, "repository_id": "local-agent", "repository": "owner/local-agent",
            "agent_binding": BINDING,
        }))
        self.catalog = self.root / "catalog.json"
        self.catalog.write_text(json.dumps({"version": 1, "agents": [
            {"id": "local-agent", "repository": "owner/local-agent", "agent_binding": BINDING}
        ]}))
        self.target = resolve_execution_target("local-agent", path=self.catalog)
        self.enrolled = validate_host_enrollments([{
            "version": 1, "host_id": "worker-linux", "installation_id": INSTALLATION,
            "enabled": True, "repositories": [{"repository_id": "local-agent", "agent_binding": BINDING}],
        }])[0]
        self.draft = {"id": "task-one", "mode": "commands", "resources": [],
                      "commands": ["printf 'hello'\n"], "command_timeout": 30, "task_timeout": 120}
        task, e, t = prepare_host_mailbox_task(
            self.draft, host_id="worker-linux", repository="local-agent",
            enrollments=[self.enrolled], catalog_path=self.catalog,
        )
        self.task = task
        staging = self.root / "staging"
        write_host_mailbox_bundle(staging, task, enrollment=e, target=t)
        for source in (staging / ".agent").rglob("*"):
            if source.is_file():
                destination = self.work / source.relative_to(staging)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read_bytes())
        self.commit()

    def git(self, *args: str) -> str:
        r = subprocess.run(["git", *args], cwd=self.work, capture_output=True,
                           check=True, text=True, timeout=5)
        return r.stdout.strip()

    def commit(self):
        self.git("add", ".agent")
        self.git("commit", "-q", "-m", "fixture")
        self.git("push", "-q", "-u", "origin", "agent-control")
        self.sha = self.git("rev-parse", "HEAD")

    def preview(self, **changes):
        args = {"host_id": "worker-linux", "task_id": "task-one",
                "expected_control_sha": self.sha, "enrollment": self.enrolled,
                "target": self.target}
        args.update(changes)
        with mock.patch("local_agent.repository.host_mailbox_reader._trusted_origin"):
            return preview_host_mailbox_snapshot(self.work, **args)

    def test_exact_git_payload_and_digest_are_validated_without_execution(self):
        result = self.preview()
        self.assertEqual(result["status"], "validated_preview")
        self.assertEqual(result["source_sha"], self.sha)
        self.assertEqual(result["payload_file_count"], 1)
        self.assertEqual(result["task_digest"], task_digest(self.task))
        self.assertFalse(result["executed"])
        self.assertFalse(result["claimed"])
        self.assertNotIn("printf", json.dumps(result))
        self.assertFalse((self.work / ".agent/hosts/worker-linux/results").exists())

    def test_remote_moved_and_uncached_commit_fail_closed(self):
        before = self.sha
        self.git("commit", "-q", "--allow-empty", "-m", "new-head")
        self.git("push", "-q", "origin", "agent-control")
        with self.assertRaisesRegex(ValueError, "differs"):
            self.preview(expected_control_sha=before)
        with self.assertRaisesRegex(ValueError, "differs"):
            self.preview(expected_control_sha="a" * 40)

    def test_invalid_target_is_rejected(self):
        for overrides in ({"host_id": "other-host"}, {"task_id": "../unsafe"},
                          {"expected_control_sha": "main"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.preview(**overrides)

    def test_manifest_wrong_target_or_binding_fails_closed(self):
        manifest = self.work / ".agent/hosts/worker-linux/tasks/task-one.json"
        for changes in ({"target_installation_id": "12345678-1234-4234-8234-123456789abd"},
                        {"agent_binding": "12345678-1234-4234-8234-123456789abd"},
                        {"host_mailbox_version": 9}):
            with self.subTest(changes=changes):
                payload = json.loads(manifest.read_text())
                payload.update(changes)
                manifest.write_text(json.dumps(payload))
                self.commit()
                with self.assertRaises(ValueError):
                    self.preview()

    def test_symlink_payload_and_missing_file_fail_closed(self):
        sidecar = self.work / ".agent/hosts/worker-linux/tasks/task-one.payload/commands/001.sh"
        sidecar.unlink()
        sidecar.symlink_to("/etc/passwd")
        self.commit()
        with self.assertRaisesRegex(ValueError, "regular Git blob"):
            self.preview()
        sidecar.unlink()
        self.git("add", "-u")
        self.git("commit", "-q", "-m", "remove")
        self.git("push", "-q", "origin", "agent-control")
        self.sha = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(ValueError, "missing"):
            self.preview()

    def test_wrong_origin_is_not_accepted(self):
        with self.assertRaisesRegex(ValueError, "origin"):
            preview_host_mailbox_snapshot(self.work, host_id="worker-linux",
                task_id="task-one", expected_control_sha=self.sha,
                enrollment=self.enrolled, target=self.target)

    def test_uncached_remote_head_and_missing_task_fail_closed(self):
        old_sha = self.sha
        other = self.root / "external"
        subprocess.run(["git", "clone", "-q", "-b", "agent-control", str(self.remote), str(other)], check=True)
        subprocess.run(["git", "-C", str(other), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(other), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(other), "commit", "-q", "--allow-empty", "-m", "new-remote"], check=True)
        subprocess.run(["git", "-C", str(other), "push", "-q", "origin", "agent-control"], check=True)
        sha = subprocess.run(["git", "-C", str(other), "rev-parse", "HEAD"],
                             check=True, capture_output=True, text=True).stdout.strip()
        with self.assertRaisesRegex(ValueError, "not cached"):
            self.preview(expected_control_sha=sha)
        with self.assertRaisesRegex(ValueError, "differs"):
            self.preview(expected_control_sha=old_sha)

    def test_duplicate_manifest_keys_are_rejected(self):
        path = self.work / ".agent/hosts/worker-linux/tasks/task-one.json"
        original = path.read_text()
        path.write_text(original.rstrip()[:-1] + ', "id": "task-one"}\n')
        self.commit()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.preview()

    def test_successful_cli_reports_only_metadata(self):
        args = diagnostics.build_parser().parse_args([
            "preview-host-mailbox", "task-one", "--repository", "local-agent",
            "--host-id", "worker-linux", "--expected-control-sha", self.sha,
            "--expected-operator-sha", "b" * 40, "--catalog", str(self.catalog),
        ])
        row = SimpleNamespace(repository_id="local-agent", repository="owner/local-agent",
                              agent_binding=BINDING, control=self.work)
        sink = io.StringIO()
        with (mock.patch.object(diagnostics, "is_disabled", return_value=False),
              mock.patch.object(diagnostics, "fetch_operator_host_registry",
                                return_value=("b" * 40, (self.enrolled,))),
              mock.patch.object(diagnostics, "load_repository_registry", return_value=[row]),
              mock.patch.object(diagnostics, "validate_repository_control_binding"),
              mock.patch("local_agent.repository.host_mailbox_reader._trusted_origin"),
              redirect_stdout(sink)):
            self.assertEqual(args.func(args), 0)
        report = json.loads(sink.getvalue())
        self.assertEqual(report["status"], "validated_preview")
        self.assertEqual(report["operator_registry_sha"], "b" * 40)
        self.assertEqual(report["payload_file_count"], 1)
        self.assertNotIn("printf", sink.getvalue())
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_control_binding_mismatch_fails_closed(self):
        binding = self.work / ".agent/binding.json"
        j = json.loads(binding.read_text())
        j["agent_binding"] = INSTALLATION
        binding.write_text(json.dumps(j))
        self.commit()
        with self.assertRaisesRegex(ValueError, "control binding"):
            self.preview()

    def test_cli_on_error_does_not_leak_source_details(self):
        args = diagnostics.build_parser().parse_args([
            "preview-host-mailbox", "task-one", "--repository", "local-agent",
            "--host-id", "worker-linux", "--expected-control-sha", self.sha,
            "--expected-operator-sha", "b" * 40,
        ])
        out = io.StringIO()
        with (mock.patch.object(diagnostics, "is_disabled", return_value=False),
              mock.patch.object(diagnostics, "fetch_operator_host_registry",
                                side_effect=RuntimeError("private credential")),
              redirect_stdout(out)):
            self.assertEqual(args.func(args), 2)
        self.assertEqual(json.loads(out.getvalue())["status"], "unverified")
        self.assertNotIn("private credential", out.getvalue())


if __name__ == "__main__":
    unittest.main()
