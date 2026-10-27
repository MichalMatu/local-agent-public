"""Private host producer provisioning and source Git/worker/result vertical slice."""
from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from local_agent.cli import diagnostics
from local_agent.operator import remote as operator_remote
from local_agent.runtime.task_contract import task_digest
from local_agent.supervisor import host_private_producer as private
from local_agent.supervisor import host_remote_client


class PrivateProducerSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        # Use the existing real Git + exact-grant + existing-worker fixture;
        # import lazily to avoid discovering its other tests twice.
        from tests.test_host_remote_git_e2e import HostRemoteRealGitEndToEndTests
        self.fixture = HostRemoteRealGitEndToEndTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture
        self.identity_file = self.f.state / "identity.json"
        self.private_root = self.f.root / "private-producer"
        self.authorized = self.f._task("private-setup-authorized")
        pin = self.f._publish_grants([self.f.task, self.authorized])
        (self.f.state / "operator-control.sha").write_text(pin + "\n")

    def _setup(self, *, root: Path | None = None, origin: str | None = None) -> dict:
        with (
            mock.patch.object(private, "_validate_origin"),
            mock.patch.object(private, "_require_control_origin"),
            mock.patch.object(private, "_validate_origin"),
            mock.patch.object(operator_remote, "_require_trusted_host_registry_origin"),
        ):
            return private.setup_private_host_producer(
                identity_file=self.identity_file, operator_checkout=self.f.operator,
                private_root=root or self.private_root, origin_url=origin or str(self.f.remote),
                expected_control_sha=self.f._git(
                    self.f.root, "--git-dir", str(self.f.remote),
                    "rev-parse", "refs/heads/agent-control",
                ),
                git_author_name="Private producer test",
                git_author_email="producer@example.invalid",
            )

    def _cli(self, argv: list[str]) -> tuple[int, dict]:
        args = diagnostics.build_parser().parse_args(argv)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = args.func(args)
        return code, json.loads(output.getvalue())

    def test_setup_stage_publish_worker_durable_independent_readback(self) -> None:
        setup = self._setup()
        self.assertEqual(setup["status"], "configured")
        self.assertEqual(setup["execution_authority"], "exact_digest_grant_required")
        self.assertFalse(setup["published"])
        profile = Path(setup["profile"])
        checkout = profile.parent / "control"
        self.assertTrue(checkout.is_dir())
        self.assertFalse((checkout / ".agent" / "tasks").exists())
        draft = self.f.root / "new-task-draft.json"
        draft.write_text(json.dumps(self.authorized))
        head = self.f._git(checkout, "rev-parse", "HEAD")
        with (
            mock.patch.object(private, "_require_control_origin"),
            mock.patch.object(private, "_validate_origin"),
            mock.patch.object(operator_remote, "_require_trusted_host_registry_origin"),
            mock.patch.object(host_remote_client, "_require_control_origin"),
        ):
            code, staged = self._cli([
                "host-stage", str(draft), "--profile", str(profile),
            ])
            self.assertEqual(code, 0, staged)
            self.assertEqual(staged["status"], "staged_only")
            self.assertFalse(staged["published"])
            code, published = self._cli([
                "host-publish", self.authorized["id"], "--profile", str(profile),
                "--expected-control-sha", head,
                "--expected-task-digest", task_digest(self.authorized),
            ])
            self.assertEqual(code, 0, published)
            self.assertEqual(published["status"], "published")
        self.f._git(self.f.checkout, "pull", "-q", "--ff-only")
        self.assertEqual(self.f._execute(self.authorized["id"]), "published")
        self.assertEqual((self.f.work / "effect-count.txt").read_text(), "x")
        self.assertEqual(self.f._execute(self.authorized["id"]), "published")
        self.assertEqual((self.f.work / "effect-count.txt").read_text(), "x")
        reader = self.f.root / "independent-private-reader"
        self.f._git(self.f.root, "clone", "-q", "-b", "agent-control",
                    str(self.f.remote), str(reader))
        current = self.f._git(reader, "rev-parse", "HEAD")
        with (
            mock.patch.object(private, "_require_control_origin"),
            mock.patch.object(private, "_validate_origin"),
            mock.patch.object(host_remote_client, "_require_control_origin"),
        ):
            code, result = self._cli([
                "host-result", self.authorized["id"], "--profile", str(profile),
                "--control-checkout", str(reader),
                "--expected-control-sha", current,
                "--expected-task-digest", task_digest(self.authorized),
            ])
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "verified_snapshot")
        self.assertEqual(result["result"]["status"], "done")
        self.assertEqual(result["result"]["task_digest"], task_digest(self.authorized))
        self.assertFalse((checkout / ".agent" / "hosts" /
                          self.f.identity.host_id / "supervisor" /
                          self.f.identity.installation_id /
                          "task-results" / (self.authorized["id"] + ".json")).exists())

    def test_profile_requires_exact_digest_grant_before_publication(self) -> None:
        setup = self._setup()
        profile = Path(setup["profile"])
        checkout = profile.parent / "control"
        task = self.f._task("private-setup-unsigned")
        draft = self.f.root / "unsigned-draft.json"
        draft.write_text(json.dumps(task))
        with (
            mock.patch.object(private, "_require_control_origin"),
            mock.patch.object(private, "_validate_origin"),
            mock.patch.object(operator_remote, "_require_trusted_host_registry_origin"),
        ):
            code, staged = self._cli([
                "host-stage", str(draft), "--profile", str(profile),
            ])
            self.assertEqual(code, 0, staged)
            code, denied = self._cli([
                "host-publish", task["id"], "--profile", str(profile),
                "--expected-control-sha", self.f._git(checkout, "rev-parse", "HEAD"),
                "--expected-task-digest", task_digest(task),
            ])
        self.assertEqual(code, 2, denied)
        self.assertEqual(denied["status"], "unverified")
        self.assertEqual(self.f._git(checkout, "status", "--porcelain").count("??"), 1)
        self.assertFalse((self.f.work / "effect-count.txt").exists())

    def test_setup_refuses_stale_pin_and_does_not_create_checkout(self) -> None:
        (self.f.state / "operator-control.sha").write_text("a" * 40 + "\n")
        with self.assertRaisesRegex(ValueError, "operator authority"):
            self._setup()
        self.assertFalse(self.private_root.exists())

    def test_setup_refuses_existing_directory_and_unsafe_parent(self) -> None:
        self.private_root.mkdir()
        with self.assertRaises(FileExistsError):
            self._setup()
        unsafe = self.f.root / "unsafe"
        unsafe.mkdir(mode=0o777)
        unsafe.chmod(0o777)
        with self.assertRaisesRegex(ValueError, "0700"):
            self._setup(root=unsafe / "producer")
        self.assertFalse((unsafe / "producer").exists())

    def test_profile_refuses_changed_operator_pin(self) -> None:
        profile = Path(self._setup()["profile"])
        (self.f.state / "operator-control.sha").write_text("b" * 40 + "\n")
        with (
            mock.patch.object(private, "_require_control_origin"),
            mock.patch.object(private, "_validate_origin"),
            mock.patch.object(operator_remote, "_require_trusted_host_registry_origin"),
        ):
            code, denied = self._cli([
                "host-publish", self.authorized["id"], "--profile", str(profile),
                "--expected-control-sha", self.f._git(profile.parent / "control",
                                                     "rev-parse", "HEAD"),
                "--expected-task-digest", task_digest(self.authorized),
            ])
        self.assertEqual(code, 2, denied)
        self.assertFalse((self.f.work / "effect-count.txt").exists())
        self.assertEqual(denied["status"], "unverified")

    def test_setup_refuses_dirty_operator_checkout_without_created_root(self) -> None:
        (self.f.operator / "unrelated.txt").write_text("unsaved owner work")
        with self.assertRaisesRegex(ValueError, "unrelated local changes"):
            self._setup()
        self.assertFalse(self.private_root.exists())

    def test_profile_rejects_explicit_other_installation(self) -> None:
        profile = Path(self._setup()["profile"])
        draft = self.f.root / "misaddressed-task.json"
        draft.write_text(json.dumps(self.authorized))
        with (
            mock.patch.object(private, "_require_control_origin"),
            mock.patch.object(private, "_validate_origin"),
            mock.patch.object(operator_remote, "_require_trusted_host_registry_origin"),
        ):
            code, denied = self._cli([
                "host-stage", str(draft), "--profile", str(profile),
                "--installation-id", "12345678-1234-4234-8234-123456789abd",
            ])
        self.assertEqual(code, 2, denied)
        self.assertFalse((profile.parent / "control" / self.f.host.namespace /
                          "tasks" / (self.authorized["id"] + ".json")).exists())

    def test_real_origin_allowlist_accepts_exact_control_repository(self) -> None:
        for origin in (
            "https://github.com/example-owner/local-agent",
            "https://github.com/example-owner/local-agent.git",
            "git@github.com:example-owner/local-agent.git",
            "ssh://git@github.com/example-owner/local-agent.git",
        ):
            with self.subTest(origin=origin):
                private._validate_origin(origin)

    def test_real_origin_allowlist_rejects_embedded_credentials(self) -> None:
        for origin in ("https://token@github.com/example-owner/local-agent.git",
                       "file:///tmp/local-agent.git", "git@elsewhere:local-agent"):
            with self.assertRaisesRegex(ValueError, "origin differs from selected GitHub repository"):
                private._validate_origin(origin)
        self.assertFalse(self.private_root.exists())


if __name__ == "__main__":
    unittest.main()
