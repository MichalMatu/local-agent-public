"""No-live-effects host fact staging and pinned operator admission tests."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from local_agent.cli import diagnostics
from local_agent.host_ops.capabilities.local.host.models import HostProfile
from local_agent.host_ops.capabilities.local.tools.models import ToolInspectionResult
from local_agent.repository.host_facts import (
    build_staged_host_facts, initialize_local_host_identity,
    load_local_host_identity, write_staged_host_facts,
)
from local_agent.repository.host_routing import HostIdentity, validate_host_enrollments

INSTALLATION = "550e8400-e29b-41d4-a716-446655440000"
OTHER_INSTALLATION = "550e8400-e29b-41d4-a716-446655440001"
BINDING = "b0e94f93-478f-4fc6-abed-159d3702362f"
OPERATOR_SHA = "a" * 40
SOURCE_SHA = "b" * 40


class HostFactStagingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.identity = HostIdentity("worker-linux", INSTALLATION)
        self.identity_file = self.root / "host-identity.json"
        self.identity_file.write_text(json.dumps(self.identity.as_record()))
        self.identity_file.chmod(0o600)
        self.enrolled = validate_host_enrollments([{
            "version": 1, "host_id": "worker-linux", "installation_id": INSTALLATION,
            "enabled": True, "repositories": [
                {"repository_id": "local-agent", "agent_binding": BINDING},
            ],
        }])
        self.profile = HostProfile(
            hostname="very-private-host.local", system="Linux", release="6.12.1",
            architecture="x86_64", logical_cpu_count=8,
            memory_total_bytes=8_000_000_000, root_total_bytes=100_000_000,
            root_free_bytes=50_000_000, gpu_devices=None,
        )
        self.tools = [ToolInspectionResult(
            name="git", present=True, resolved_path="/home/private/bin/git",
            version="git version 2.48.0", version_exit_code=0,
        ), ToolInspectionResult(
            name="docker", present=False, resolved_path=None,
            version=None, version_exit_code=None,
        )]

    def facts(self, **changes):
        args = dict(identity=self.identity, enrollments=self.enrolled,
                    operator_sha=OPERATOR_SHA, source_revision=SOURCE_SHA,
                    collected_at="2026-10-12T20:00:00+00:00",
                    profile=self.profile, tools=self.tools)
        args.update(changes)
        return build_staged_host_facts(**args)

    def test_init_identity_is_exclusive_stable_and_not_enrolled(self):
        path = self.root / "new-installation.json"
        result = initialize_local_host_identity(path, "worker-linux")
        self.assertEqual(result.host_id, "worker-linux")
        self.assertNotEqual(result.installation_id, INSTALLATION)
        self.assertEqual(load_local_host_identity(path), result)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            initialize_local_host_identity(path, "worker-linux")
        self.assertEqual(load_local_host_identity(path), result)
        with self.assertRaises(ValueError):
            initialize_local_host_identity(self.root / "Bad-id.json", "Bad-id")
        (self.root / "repo/.git").mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "Git working tree"):
            initialize_local_host_identity(self.root / "repo/identity.json", "worker-linux")
        alias = self.root / "file-alias"
        alias.symlink_to(path)
        with self.assertRaisesRegex(ValueError, "symlink"):
            initialize_local_host_identity(alias, "worker-linux")

    def test_init_identity_cli_cannot_claim_enrollment(self):
        args = Namespace(host_id="worker-linux", output_file=self.root / "new-cli.json")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(diagnostics.command_init_host_identity(args), 0)
        obj = json.loads(stdout.getvalue())
        self.assertEqual(obj["status"], "created_not_enrolled")
        self.assertEqual(obj["execution_authority"], "not_asserted")
        self.assertFalse(obj["published"])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(diagnostics.command_init_host_identity(args), 2)

    def test_identity_file_accepts_only_canonical_bounded_record(self):
        self.assertEqual(load_local_host_identity(self.identity_file), self.identity)
        invalid = [
            '{"version":1,"host_id":"worker-linux","host_id":"worker-mac","installation_id":"' + INSTALLATION + '"}',
            '{"version":1,"host_id":"worker-linux","installation_id":"' + INSTALLATION + '","secret":"oops"}',
            '{"version":1,"host_id":"Worker-Linux","installation_id":"' + INSTALLATION + '"}',
            '{"version":1,"host_id":"worker-linux","installation_id":"broken"}',
            '{"version":true,"host_id":"worker-linux","installation_id":"' + INSTALLATION + '"}',
            ' ' * 4097, 'broken',
        ]
        for raw in invalid:
            with self.subTest(raw=raw[:60]), self.assertRaises(ValueError):
                self.identity_file.write_text(raw)
                load_local_host_identity(self.identity_file)
        self.identity_file.write_text(json.dumps(self.identity.as_record()))
        alias = self.root / "identity-alias.json"
        alias.symlink_to(self.identity_file)
        with self.assertRaisesRegex(ValueError, "non-symlink"):
            load_local_host_identity(alias)
        parent_alias = self.root / "identity-parent-alias"
        parent_alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "non-symlink"):
            load_local_host_identity(parent_alias / self.identity_file.name)
        if __import__("os").name == "posix":
            self.identity_file.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "group or others"):
                load_local_host_identity(self.identity_file)
            self.identity_file.chmod(0o600)
        self.assertEqual(load_local_host_identity(self.identity_file), self.identity)

    def test_staged_manifest_redacts_machine_paths_and_never_claims_permission(self):
        facts = self.facts()
        self.assertEqual(set(facts), {"identity.json", "system.json", "capabilities.json"})
        dumped = json.dumps(facts)
        self.assertNotIn("private", dumped)
        self.assertNotIn("/home", dumped)
        self.assertEqual(facts["system.json"]["architecture"], "x86_64")
        self.assertEqual(facts["system.json"]["installation_id"], INSTALLATION)
        self.assertEqual(facts["capabilities.json"]["tools"][0]["name"], "docker")
        self.assertFalse(facts["capabilities.json"]["tools"][0]["present"])
        for record in facts.values():
            self.assertEqual(record["publication_authority"], "not_asserted")
            self.assertEqual(record["record_type"], "diagnostic_preview")
            self.assertEqual(record["operator_registry_sha"], OPERATOR_SHA)

    def test_disabled_revoked_or_wrong_installation_denied_before_writes(self):
        wrong = HostIdentity("worker-linux", OTHER_INSTALLATION)
        disabled = validate_host_enrollments([{
            "version": 1, "host_id": "worker-linux", "installation_id": INSTALLATION,
            "enabled": False, "repositories": [],
        }])
        for changes in ({"identity": wrong}, {"enrollments": disabled},
                        {"enrollments": ()}, {"operator_sha": "main"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.facts(**changes)

    def test_staged_files_writes_once_and_no_status_file(self):
        root = self.root / "new-staging"
        paths = write_staged_host_facts(root, self.identity, self.facts())
        self.assertEqual([x.name for x in paths],
                         ["identity.json", "system.json", "capabilities.json"])
        self.assertEqual([x.parent for x in paths], [root / ".agent/hosts/worker-linux"] * 3)
        for path in paths:
            self.assertTrue(path.is_file())
            self.assertEqual(json.loads(path.read_text())["host_id"], "worker-linux")
        self.assertFalse((paths[0].parent / "status.json").exists())
        with self.assertRaises(FileExistsError):
            write_staged_host_facts(root, self.identity, self.facts())
        self.assertEqual(len(list(paths[0].parent.iterdir())), 3)

    def test_refuse_malicious_target_paths_and_overlaps(self):
        facts = self.facts()
        protected = self.root / "active/control"
        protected.mkdir(parents=True)
        for root in (protected, protected / "new", self.root / "active"):
            with self.subTest(root=root), self.assertRaisesRegex(ValueError, "protected"):
                write_staged_host_facts(root, self.identity, facts, protected_paths=(protected,))
        (self.root / "repo/.git").mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "Git working tree"):
            write_staged_host_facts(self.root / "repo/export", self.identity, facts)
        (self.root / "alias").symlink_to(self.root / "repo", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            write_staged_host_facts(self.root / "alias/export", self.identity, facts)
        with self.assertRaisesRegex(ValueError, "absolute"):
            write_staged_host_facts(Path("relative"), self.identity, facts)

    def test_cannot_stage_unauthorized_content_and_no_directory_is_created(self):
        facts = self.facts()
        invalid = [facts | {"status.json": {"state": "idle"}},
                   facts | {"identity.json": facts["identity.json"] | {"publication_authority": "granted"}},
                   facts | {"system.json": facts["system.json"] | {"host_id": "worker-mac"}}]
        for idx, candidate in enumerate(invalid):
            root = self.root / f"invalid-{idx}"
            with self.assertRaises(ValueError):
                write_staged_host_facts(root, self.identity, candidate)
            self.assertFalse(root.exists())

    def test_partial_filesystem_error_rolls_back_new_root(self):
        root = self.root / "failure"
        with mock.patch("local_agent.repository.host_facts.os.fsync", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                write_staged_host_facts(root, self.identity, self.facts())
        self.assertFalse(root.exists())

    def test_cli_validates_pinned_operator_ref_and_does_not_publish(self):
        args = Namespace(host_id="worker-linux", identity_file=self.identity_file,
                         expected_operator_sha=OPERATOR_SHA, source_revision=SOURCE_SHA,
                         output_root=self.root / "cli", registry=None, tools=["git"])
        result = io.StringIO()
        with (mock.patch.object(diagnostics, "is_disabled", return_value=False),
              mock.patch.object(diagnostics, "fetch_operator_host_registry", return_value=(OPERATOR_SHA, self.enrolled)),
              mock.patch.object(diagnostics, "HostProfiler") as profiler,
              mock.patch.object(diagnostics, "ToolInspector") as inspector,
              mock.patch.object(diagnostics, "load_repository_registry", return_value=[]),
              redirect_stdout(result)):
            profiler.return_value.inspect.return_value = self.profile
            inspector.return_value.inspect.return_value = self.tools
            code = diagnostics.command_stage_host_facts(args)
        self.assertEqual(code, 0)
        output = json.loads(result.getvalue())
        self.assertEqual(output["status"], "staged_only")
        self.assertFalse(output["published"])
        self.assertFalse(output["heartbeat_published"])
        self.assertEqual(len(output["files"]), 3)
        self.assertFalse((args.output_root / ".git").exists())

    def test_cli_wrong_sha_or_global_disabled_fail_closed(self):
        args = Namespace(host_id="worker-linux", identity_file=self.identity_file,
                         expected_operator_sha="c" * 40, source_revision=SOURCE_SHA,
                         output_root=self.root / "none", registry=None, tools=["git"])
        for disabled in (False, True):
            with (mock.patch.object(diagnostics, "is_disabled", return_value=disabled),
                  mock.patch.object(diagnostics, "fetch_operator_host_registry", return_value=(OPERATOR_SHA, self.enrolled)),
                  redirect_stdout(io.StringIO())):
                self.assertEqual(diagnostics.command_stage_host_facts(args), 2)
                self.assertFalse(args.output_root.exists())


if __name__ == "__main__":
    unittest.main()
