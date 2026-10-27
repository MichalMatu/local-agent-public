"""Host fact publication preflight: disposable Git fixtures; no remote enrollment."""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from local_agent.cli import diagnostics
from local_agent.host_ops.capabilities.local.host.models import HostProfile
from local_agent.repository import host_publication_preview as publication
from local_agent.repository.host_facts import build_staged_host_facts, write_staged_host_facts
from local_agent.repository.host_routing import HostIdentity, validate_host_enrollments

HOST_ID = "worker-linux"
INSTALLATION = "550e8400-e29b-41d4-a716-446655440000"
OTHER_INSTALLATION = "550e8400-e29b-41d4-a716-446655440001"
OPERATOR_SHA = "a" * 40
SOURCE_SHA = "b" * 40
BINDING = "b0e94f93-478f-4fc6-abed-159d3702362f"


class HostPublicationPreviewTests(unittest.TestCase):
    def git(self, repo: Path, *args: str) -> str:
        return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL, text=True).strip()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.identity = HostIdentity(HOST_ID, INSTALLATION)
        self.enrollments = validate_host_enrollments([{
            "version": 1, "host_id": HOST_ID, "installation_id": INSTALLATION,
            "enabled": True, "repositories": [{"repository_id": "local-agent", "agent_binding": BINDING}],
        }])
        self.staging = self.root / "staging"
        profile = HostProfile(
            hostname="private.internal", system="Linux", release="6.12",
            architecture="x86_64", logical_cpu_count=4, memory_total_bytes=1024,
            root_total_bytes=2048, root_free_bytes=1024, gpu_devices=None,
        )
        self.records = build_staged_host_facts(
            self.identity, enrollments=self.enrollments, operator_sha=OPERATOR_SHA,
            source_revision=SOURCE_SHA, collected_at="2026-10-12T20:00:00+00:00",
            profile=profile, tools=[],
        )
        write_staged_host_facts(self.staging, self.identity, self.records)
        self.host_path = self.staging / ".agent" / "hosts" / HOST_ID
        self.bare = self.root / "disposable.git"
        subprocess.run(["git", "init", "--bare", "--initial-branch=agent-control", str(self.bare)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        self.writer = self.root / "writer"
        subprocess.run(["git", "init", "--initial-branch=agent-control", str(self.writer)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        self.git(self.writer, "config", "user.name", "Offline Test")
        self.git(self.writer, "config", "user.email", "offline@example.invalid")
        (self.writer / "README").write_text("local fixture\n")
        self.git(self.writer, "add", "README")
        self.git(self.writer, "commit", "-m", "initial")
        self.git(self.writer, "remote", "add", "origin", str(self.bare))
        self.git(self.writer, "push", "origin", "HEAD:refs/heads/agent-control")
        self.checkout = self.root / "control"
        subprocess.run(["git", "clone", str(self.bare), str(self.checkout)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        self.head = self.git(self.writer, "rev-parse", "HEAD")

    def preview(self, **changes):
        kw = dict(identity=self.identity, enrollments=self.enrollments,
                  repository_id="local-agent", agent_binding=BINDING,
                  expected_operator_sha=OPERATOR_SHA, expected_control_sha=self.head)
        kw.update(changes)
        return publication.preview_host_facts_publication(self.checkout, self.staging, **kw)

    def rewrite(self, name: str, record: dict):
        (self.host_path / name).write_text(json.dumps(record) + "\n")

    def advance(self, name: str = "OTHER"):
        (self.writer / name).write_text("writer mutation\n")
        self.git(self.writer, "add", name)
        self.git(self.writer, "commit", "-m", "other writer")
        self.git(self.writer, "push", "origin", "HEAD:refs/heads/agent-control")

    def test_success_is_only_a_cas_preview_with_three_digest_paths(self):
        result = self.preview()
        self.assertEqual(result["status"], "publication_preview_only")
        self.assertTrue(result["requires_write_time_cas"])
        self.assertFalse(result["published"])
        self.assertFalse(result["heartbeat_published"])
        self.assertEqual(result["execution_authority"], "not_asserted")
        self.assertEqual(len(result["file_sha256"]), 3)
        self.assertEqual(result["repository_id"], "local-agent")
        self.assertEqual(result["agent_binding"], BINDING)
        self.assertNotIn("status.json", " ".join(result["file_sha256"]))
        self.assertEqual(self.git(self.writer, "rev-parse", "HEAD"), self.head)
        self.assertFalse((self.checkout / ".agent").exists())

    def test_revoked_mismatched_identity_or_wrong_pins_fail_before_git(self):
        revoked = validate_host_enrollments([{
            "version": 1, "host_id": HOST_ID, "installation_id": INSTALLATION,
            "enabled": False, "repositories": [],
        }])
        for changes in (
            {"enrollments": ()}, {"enrollments": revoked},
            {"identity": HostIdentity(HOST_ID, OTHER_INSTALLATION)},
            {"expected_operator_sha": "main"}, {"expected_control_sha": "HEAD"},
        ):
            with self.subTest(changes=changes), mock.patch.object(publication, "_git") as git:
                with self.assertRaises(ValueError):
                    self.preview(**changes)
                git.assert_not_called()

    def test_repository_enrollment_is_exact_and_cannot_cross_project(self):
        for changes in (
            {"repository_id": "other-repo"},
            {"repository_id": "local-agent", "agent_binding": OTHER_INSTALLATION},
            {"repository_id": "local-agent/../bad"},
            {"agent_binding": "not-a-binding"},
        ):
            with self.subTest(changes=changes), mock.patch.object(publication, "_git") as git:
                with self.assertRaises(ValueError):
                    self.preview(**changes)
                git.assert_not_called()

    def test_path_and_manifest_tampering_denied(self):
        for name, record in (
            ("identity.json", self.records["identity.json"] | {"grant": "all"}),
            ("system.json", self.records["system.json"] | {"secret": "private"}),
            ("capabilities.json", self.records["capabilities.json"] | {"execution_permission": "automatic"}),
            ("system.json", self.records["system.json"] | {"operator_registry_sha": "c" * 40}),
            ("system.json", self.records["system.json"] | {"collected_at": "wrong timestamp"}),
        ):
            with self.subTest(name=name, record=record):
                saved = (self.host_path / name).read_bytes()
                self.rewrite(name, record)
                try:
                    with self.assertRaises(ValueError):
                        self.preview()
                finally:
                    (self.host_path / name).write_bytes(saved)
        file = self.host_path / "identity.json"
        saved = file.read_bytes()
        file.write_text('{"host_id":"x","host_id":"y"}')
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.preview()
        file.write_bytes(saved)
        file.unlink()
        file.symlink_to(self.root / "outside")
        with self.assertRaises(ValueError):
            self.preview()
        file.unlink()
        file.write_bytes(saved)
        (self.host_path / "status.json").write_text('{}')
        with self.assertRaisesRegex(ValueError, "unexpected"):
            self.preview()
        (self.host_path / "status.json").unlink()
        file.write_bytes(b"x" * (16 * 1024 + 1))
        with self.assertRaises(ValueError):
            self.preview()

    def test_control_ref_stale_or_second_writer_ref_change(self):
        self.advance()
        with self.assertRaisesRegex(ValueError, "CAS conflict"):
            self.preview()
        self.git(self.checkout, "fetch", "origin", "agent-control")
        head = self.git(self.writer, "rev-parse", "HEAD")
        real_git = publication._git
        count = 0

        def raced(*args, **kwargs):
            nonlocal count
            result = real_git(*args, **kwargs)
            if "ls-remote" in args:
                count += 1
                if count == 1:
                    self.advance("RACE")
            return result

        with mock.patch.object(publication, "_git", side_effect=raced):
            with self.assertRaisesRegex(ValueError, "during publication preview"):
                self.preview(expected_control_sha=head)

    def test_existing_host_snapshot_blocks_overwrite(self):
        path = self.writer / ".agent" / "hosts" / HOST_ID
        path.mkdir(parents=True)
        (path / "status.json").write_text('{}')
        self.git(self.writer, "add", ".agent/hosts")
        self.git(self.writer, "commit", "-m", "prior host record")
        self.git(self.writer, "push", "origin", "HEAD:refs/heads/agent-control")
        self.git(self.checkout, "fetch", "origin", "agent-control")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.preview(expected_control_sha=self.git(self.writer, "rev-parse", "HEAD"))

    def test_interrupted_stage_and_non_repository_ref_are_unverified(self):
        (self.host_path / "capabilities.json").unlink()
        with self.assertRaisesRegex(ValueError, "missing or unexpected"):
            self.preview()
        with self.assertRaises(ValueError):
            publication.preview_host_facts_publication(
                self.root / "absent", self.staging, identity=self.identity,
                enrollments=self.enrollments, repository_id="local-agent",
                agent_binding=BINDING, expected_operator_sha=OPERATOR_SHA,
                expected_control_sha=self.head,
            )

    def test_cli_read_only_preview_with_canonical_binding_and_pinned_operator(self):
        args = Namespace(
            identity_file=self.root / "identity.json", host_id=HOST_ID,
            expected_operator_sha=OPERATOR_SHA, expected_control_sha=self.head,
            staging_root=self.staging, repository="local-agent", catalog=None,
            registry=None,
        )
        target = SimpleNamespace(repository_id="local-agent", repository="example-owner/local-agent",
                                 agent_binding=BINDING, control=self.checkout)
        output = io.StringIO()
        with mock.patch.object(diagnostics, "is_disabled", return_value=False), \
             mock.patch.object(diagnostics, "load_local_host_identity", return_value=self.identity), \
             mock.patch.object(diagnostics, "fetch_operator_host_registry", return_value=(OPERATOR_SHA, self.enrollments)), \
             mock.patch.object(diagnostics, "resolve_execution_target", return_value=target), \
             mock.patch.object(diagnostics, "load_repository_registry", return_value=[target]), \
             mock.patch.object(diagnostics, "validate_repository_control_binding"), \
             mock.patch.object(diagnostics, "_trusted_origin"), \
             redirect_stdout(output):
            self.assertEqual(diagnostics.command_preview_host_facts_publication(args), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["expected_control_sha"], self.head)
        self.assertFalse(result["published"])
        self.assertTrue(result["requires_write_time_cas"])
        self.assertEqual(self.git(self.writer, "rev-parse", "HEAD"), self.head)

    def test_cli_disabled_and_missing_operator_grant_fail_closed(self):
        args = Namespace(
            identity_file=self.root / "identity.json", host_id=HOST_ID,
            expected_operator_sha=OPERATOR_SHA, expected_control_sha=self.head,
            staging_root=self.staging, repository="local-agent", catalog=None,
            registry=None,
        )
        for disabled in (True, False):
            with mock.patch.object(diagnostics, "is_disabled", return_value=disabled), \
                 mock.patch.object(diagnostics, "load_local_host_identity", return_value=self.identity), \
                 mock.patch.object(diagnostics, "fetch_operator_host_registry", side_effect=ValueError("absent")), \
                 redirect_stdout(io.StringIO()) as capture:
                self.assertEqual(diagnostics.command_preview_host_facts_publication(args), 2)
                self.assertFalse(json.loads(capture.getvalue())["published"])


if __name__ == "__main__":
    unittest.main()
