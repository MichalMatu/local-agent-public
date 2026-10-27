"""Exact operator authorization for Git transport without executing host commands."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.repository.host_routing import HostIdentity, HostEnrollment
from local_agent.runtime.task_contract import task_digest
from local_agent.supervisor.host_control import HostControlSource
from local_agent.supervisor import host_remote_intake as intake
from local_agent.supervisor.host_remote_grants import (
    HostCommandGrant, parse_host_command_grants, require_exact_host_command_grant,
)
from local_agent.supervisor.host_remote_intake import (
    HostControlSnapshotChanged, preview_authorized_remote_host_task,
)


HOST_ID = "worker-mac"
INSTALL = "12345678-1234-4234-8234-123456789abc"
OPERATOR_SHA = "a" * 40


class HostRemoteGrantParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = HostIdentity(HOST_ID, INSTALL)
        self.grant = {
            "host_id": HOST_ID, "installation_id": INSTALL,
            "task_id": "example", "task_digest": "b" * 64,
        }

    def parse(self, grants: list[dict]) -> tuple[HostCommandGrant, ...]:
        return parse_host_command_grants(json.dumps({
            "version": 1, "grants": grants,
        }).encode("utf-8"))

    def test_exact_digest_and_installation_required(self) -> None:
        grants = self.parse([self.grant])
        require_exact_host_command_grant(
            identity=self.identity, task_id="example",
            task_digest="b" * 64, grants=grants,
        )
        for changes in [
            {"task_id": "other"}, {"task_digest": "c" * 64},
            {"identity": HostIdentity("another-host", INSTALL)},
            {"identity": HostIdentity(HOST_ID, "12345678-1234-4234-8234-123456789abd")},
        ]:
            kwargs = {"identity": self.identity, "task_id": "example",
                      "task_digest": "b" * 64, "grants": grants}
            kwargs.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                require_exact_host_command_grant(**kwargs)

    def test_empty_manifest_denies_every_command(self) -> None:
        with self.assertRaisesRegex(ValueError, "not approved"):
            require_exact_host_command_grant(
                identity=self.identity, task_id="example",
                task_digest="b" * 64, grants=self.parse([]),
            )

    def test_duplicate_task_grants_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.parse([self.grant, self.grant])

    def test_unknown_fields_missing_identity_and_invalid_digest_rejected(self) -> None:
        for changes in [
            {"note": "please run"}, {"task_digest": "NOT-A-SHA"},
            {"installation_id": None}, {"task_id": "../escape"},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.parse([{**self.grant, **changes}])

    def test_duplicate_json_keys_and_invalid_manifest_version_rejected(self) -> None:
        for raw in [
            b'{"version":1,"version":1,"grants":[]}',
            b'{"version":true,"grants":[]}',
            b'{"version":1,"grants":[],"ignored":false}',
            b'{"version":1,"grants":NaN}',
            b'[]',
        ]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_host_command_grants(raw)


class HostRemoteSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(prefix="host-grant-fixture-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.checkout = self.root / "control"
        self.checkout.mkdir()
        self.remote = self.root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        self.git("init", "-q", "-b", "agent-control")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("remote", "add", "origin", str(self.remote))
        self.identity = HostIdentity(HOST_ID, INSTALL)
        self.identity_path = self.root / "host-identity.json"
        self.identity_path.write_text(json.dumps(self.identity.as_record()))
        self.identity_path.chmod(0o600)
        self.pin = self.root / "operator-control.sha"
        self.pin.write_text(OPERATOR_SHA + "\n")
        self.pin.chmod(0o600)
        self.source = HostControlSource(HOST_ID, INSTALL, self.checkout)
        self.task = {
            "version": 1, "id": "example", "target_host_id": HOST_ID,
            "target_installation_id": INSTALL,
            "cwd": str(self.root), "commands": ["pwd"],
            "resources": [], "command_timeout": 15, "task_timeout": 100,
        }
        self.task_path = self.checkout / self.source.namespace / "tasks" / "example.json"
        self.task_path.parent.mkdir(parents=True)
        self.publish()
        self.grant = HostCommandGrant(self.identity, "example", task_digest(self.task))
        self.enrollment = HostEnrollment(identity=self.identity, enabled=True, repositories=())

    def git(self, *args: str) -> str:
        proc = subprocess.run(["git", *args], cwd=self.checkout, capture_output=True,
                              check=True, text=True, timeout=10)
        return proc.stdout.strip()

    def publish(self) -> None:
        self.task_path.write_text(json.dumps(self.task, sort_keys=True))
        self.git("add", ".agent")
        self.git("commit", "-q", "-m", "fixture")
        self.git("push", "-q", "origin", "agent-control")

    def preview(self, *, grants=None, operator_sha=OPERATOR_SHA, enrollments=None,
                authorization="operator_grants"):
        with (
            mock.patch(
                "local_agent.supervisor.host_remote_intake.validate_host_control_checkout",
            ),
            mock.patch(
                "local_agent.supervisor.host_remote_intake.fetch_operator_host_registry",
                return_value=(operator_sha, (self.enrollment,) if enrollments is None else enrollments),
            ),
            mock.patch(
                "local_agent.supervisor.host_remote_intake.fetch_operator_host_command_grants",
                return_value=(operator_sha, (self.grant,) if grants is None else grants),
            ),
            mock.patch(
                "local_agent.supervisor.host_remote_intake._remote_ref",
                return_value=operator_sha,
            ),
        ):
            return preview_authorized_remote_host_task(
                self.source, task_id="example", identity_file=self.identity_path,
                production_checkout=self.root / "installed",
                operator_checkout=self.root / "installed",
                authorization=authorization,
            )

    def test_repo_free_exactly_approved_command_can_be_previewed(self) -> None:
        result = self.preview()
        self.assertEqual(result.control_sha, self.git("rev-parse", "HEAD"))
        self.assertEqual(result.task_digest, task_digest(self.task))
        self.assertEqual(result.operator_sha, OPERATOR_SHA)
        self.assertEqual(result.task["commands"], ["pwd"])
        self.assertNotIn("agent_binding", result.task)
        self.assertNotIn("repository_id", result.task)
        self.assertFalse((self.root / "host-tasks").exists())

    def test_owner_github_authority_uses_existing_target_snapshot_not_grants(self) -> None:
        self.pin.unlink()  # GitHub-owner mode does not require a second operator pin.
        with mock.patch(
            "local_agent.supervisor.host_remote_intake.load_local_operator_pin",
            side_effect=AssertionError("unexpected operator pin read"),
        ):
            preview = self.preview(grants=(), enrollments=(),
                                   authorization="github_writers")
        self.assertEqual(preview.operator_sha, "github_writers")
        self.assertEqual(preview.task_digest, task_digest(self.task))
        self.assertEqual(preview.control_sha, self.git("rev-parse", "HEAD"))

    def test_owner_github_still_rejects_mistargeted_or_stale_tasks(self) -> None:
        self.task["target_host_id"] = "another-host"
        self.publish()
        with self.assertRaisesRegex(ValueError, "host"):
            self.preview(grants=(), authorization="github_writers")
        self.task["target_host_id"] = HOST_ID
        self.publish()
        self.git("commit", "-q", "--allow-empty", "-m", "new remote snapshot")
        self.git("push", "-q", "origin", "agent-control")
        self.git("reset", "--hard", "HEAD~1")
        with self.assertRaisesRegex(HostControlSnapshotChanged, "not current"):
            self.preview(grants=(), authorization="github_writers")

    def test_unknown_auth_model_cannot_be_selected(self) -> None:
        with self.assertRaisesRegex(ValueError, "authorization model"):
            self.preview(authorization="no_protection")

    def test_invalid_timeout_is_terminal_only_after_exact_authorization(self) -> None:
        self.task["idle_timeout"] = 3650
        self.publish()
        with self.assertRaisesRegex(ValueError, "not approved"):
            self.preview()
        preview = self.preview(grants=(), authorization="github_writers")
        self.assertIn("idle_timeout", preview.validation_error)
        self.assertEqual(preview.task_digest, task_digest(self.task))
        self.assertFalse((self.root / "host-tasks").exists())
        self.grant = HostCommandGrant(self.identity, "example", task_digest(self.task))
        self.assertEqual(self.preview().validation_error, preview.validation_error)

    def test_invalid_timeout_does_not_bypass_snapshot_recheck(self) -> None:
        self.task["idle_timeout"] = 3650
        self.publish()
        real_git = intake._git
        probes = 0

        def advance_remote(checkout, *args):
            nonlocal probes
            value = real_git(checkout, *args)
            if args[0] == "ls-remote":
                probes += 1
                if probes == 2:
                    return "f" * 40 + "\trefs/heads/agent-control"
            return value

        with mock.patch.object(intake, "_git", side_effect=advance_remote):
            with self.assertRaises(HostControlSnapshotChanged):
                self.preview(grants=(), authorization="github_writers")

    def test_no_enrollment_or_grant_must_fail_closed(self) -> None:
        for kwargs in [
            {"enrollments": ()}, {"grants": ()},
            {"operator_sha": "c" * 40},
            {"grants": (HostCommandGrant(self.identity, "example", "f" * 64),)},
        ]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.preview(**kwargs)

    def test_changed_remote_task_content_invalidates_grant(self) -> None:
        self.task["commands"] = ["touch marker-should-not-exist"]
        self.publish()
        with self.assertRaisesRegex(ValueError, "not approved"):
            self.preview()
        self.assertFalse((self.root / "marker-should-not-exist").exists())

    def test_remote_head_advanced_before_local_checkout_blocks(self) -> None:
        self.git("commit", "-q", "--allow-empty", "-m", "changed")
        self.git("push", "-q", "origin", "agent-control")
        self.git("reset", "--hard", "HEAD~1")
        with self.assertRaisesRegex(HostControlSnapshotChanged, "not current"):
            self.preview()

    def test_invalid_project_binding_and_wrong_host_rejected(self) -> None:
        for changed in [
            {"agent_binding": INSTALL}, {"work_branch": "main"},
            {"target_host_id": "another-host"},
        ]:
            with self.subTest(changed=changed):
                self.task.update(changed)
                self.publish()
                with self.assertRaises(ValueError):
                    self.preview()

    def test_host_command_preview_has_no_arbitrary_application_blacklist(self) -> None:
        # Read-only preview fixture; does not run the named application.
        self.task["commands"] = ["codex --help"]
        self.publish()
        preview = self.preview(grants=(), authorization="github_writers")
        self.assertEqual(preview.task["commands"], ["codex --help"])
        self.assertEqual(preview.task["target_installation_id"], INSTALL)

    def test_missing_local_pin_disallows_host_execution(self) -> None:
        self.pin.unlink()
        with self.assertRaises(OSError):
            self.preview()


if __name__ == "__main__":
    unittest.main()
