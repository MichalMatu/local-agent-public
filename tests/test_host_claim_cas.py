"""Negative and race tests for the disposable host claim Git CAS fixture."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.repository import host_claim_cas as cas
from local_agent.repository.host_routing import HostIdentity, validate_host_enrollments
from local_agent.runtime.task_contract import task_digest

IDENTITY = HostIdentity("worker-linux", "550e8400-e29b-41d4-a716-446655440000")
BINDING = "b0e94f93-478f-4fc6-abed-159d3702362f"
OPERATOR = "a" * 40


class HostClaimFixtureTests(unittest.TestCase):
    def git(self, cwd, *args):
        return subprocess.check_output(["git", "-C", str(cwd), *args], text=True,
                                       stderr=subprocess.DEVNULL).strip()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / ".local-agent-disposable-cas-fixture").write_text("offline-fixture-only\n")
        self.bare = self.root / "remote.git"
        self.seed = self.root / "seed"
        subprocess.run(["git", "init", "--bare", "-q", "--initial-branch=agent-control", str(self.bare)], check=True)
        subprocess.run(["git", "init", "-q", "--initial-branch=agent-control", str(self.seed)], check=True)
        self.git(self.seed, "config", "user.name", "Disposable fixture")
        self.git(self.seed, "config", "user.email", "fixture@example.invalid")
        self.enrollment = validate_host_enrollments([{
            "version": 1, "host_id": IDENTITY.host_id,
            "installation_id": IDENTITY.installation_id, "enabled": True,
            "repositories": [{"repository_id": "local-agent", "agent_binding": BINDING}],
        }])[0]
        self.task = {
            "id": "offline-target", "mode": "commands", "commands": ["printf safe"],
            "resources": [], "agent_binding": BINDING,
            "host_mailbox_version": 1,
            "target_host_id": IDENTITY.host_id,
            "target_installation_id": IDENTITY.installation_id,
            "target_repository_id": "local-agent",
        }
        self.manifest = self.seed / ".agent/hosts/worker-linux/tasks/offline-target.json"
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text(json.dumps(self.task))
        self.git(self.seed, "add", ".agent")
        self.git(self.seed, "commit", "-q", "-m", "seed disposable host task")
        self.git(self.seed, "remote", "add", "origin", str(self.bare))
        self.git(self.seed, "push", "-q", "origin", "HEAD:refs/heads/agent-control")
        self.first = self.git(self.seed, "rev-parse", "HEAD")

    def run_claim(self, **change):
        values = dict(expected_control_sha=self.first, expected_operator_sha=OPERATOR,
                      expected_task_digest=task_digest(self.task), task_id=self.task["id"],
                      local_identity=IDENTITY, enrollment=self.enrollment,
                      repository_id="local-agent", agent_binding=BINDING)
        return cas.simulate_host_claim_cas(self.root, self.bare, **(values | change))

    def test_single_commit_and_exact_shared_claim_path(self):
        result = self.run_claim()
        self.assertEqual(result["status"], "offline_fixture_only")
        self.assertFalse(result["claimed_in_production"])
        self.assertFalse(result["executed"])
        self.assertEqual(result["execution_authority"], "not_asserted")
        self.assertEqual(result["changed_path"], ".agent/claims/host-targeted/offline-target.json")
        self.assertEqual(self.git(self.bare, "rev-parse", "refs/heads/agent-control"), result["fixture_commit_sha"])
        self.assertEqual(self.git(self.bare, "diff-tree", "--no-commit-id", "--name-only", "-r", result["fixture_commit_sha"]), result["changed_path"])
        with self.assertRaisesRegex(ValueError, "expected control SHA changed"):
            self.run_claim()
        with self.assertRaisesRegex(ValueError, "never replay"):
            self.run_claim(expected_control_sha=result["fixture_commit_sha"])

    def test_identity_emergency_revocation_and_unknown_never_change_ref(self):
        for change in (
            {"operator_disabled": True}, {"lifecycle_state": "UNKNOWN"},
            {"lifecycle_state": "running"}, {"lifecycle_state": "completed"},
            {"expected_task_digest": "0" * 40}, {"expected_task_digest": "0" * 64},
            {"expected_operator_sha": "not-a-sha"}, {"agent_binding": "bad-binding"},
            {"repository_id": "../other"}, {"task_id": "../other"},
            {"local_identity": HostIdentity("other-host", IDENTITY.installation_id)},
            {"enrollment": self.enrollment.__class__(IDENTITY, False, self.enrollment.repositories)},
        ):
            with self.subTest(change=list(change)), self.assertRaises(ValueError):
                self.run_claim(**change)
            self.assertEqual(self.git(self.bare, "rev-parse", "HEAD"), self.first)

    def test_snapshot_task_tamper_and_changed_expected_digest(self):
        self.task["commands"] = ["printf changed"]
        with self.assertRaisesRegex(ValueError, "immutable task digest"):
            self.run_claim()
        self.manifest.write_text(json.dumps(self.task))
        self.git(self.seed, "add", ".agent")
        self.git(self.seed, "commit", "-q", "-m", "changed mailbox")
        self.git(self.seed, "push", "-q", "origin", "HEAD:refs/heads/agent-control")
        with self.assertRaisesRegex(ValueError, "expected control SHA changed"):
            self.run_claim()

    def test_competing_writer_between_snapshot_and_push(self):
        before = cas._git
        ran = False

        def race(cwd, *args, **kwargs):
            nonlocal ran
            if args[:2] == ("rev-parse", "--verify") and cwd == self.bare.resolve() and ran is False:
                # First probe is before clone; inject at second probe.
                ran = "first_probe"
            elif args[:2] == ("rev-parse", "--verify") and cwd == self.bare.resolve() and ran == "first_probe":
                self.git(self.seed, "commit", "--allow-empty", "-q", "-m", "competing worker")
                self.git(self.seed, "push", "-q", "origin", "HEAD:refs/heads/agent-control")
                ran = "injected"
            return before(cwd, *args, **kwargs)

        with mock.patch.object(cas, "_git", side_effect=race):
            with self.assertRaisesRegex(ValueError, "concurrent writer advanced"):
                self.run_claim()
        self.assertEqual(ran, "injected")
        self.assertFalse((self.seed / ".agent/claims").exists())

    def test_ambiguous_push_is_terminal_unknown_even_if_ref_advanced(self):
        before = cas._git
        pushed = 0

        def lose_ack(cwd, *args, **kwargs):
            nonlocal pushed
            if args and args[0] == "push":
                pushed += 1
                before(cwd, *args, **kwargs)
                raise ValueError("transport dropped after push")
            return before(cwd, *args, **kwargs)

        with mock.patch.object(cas, "_git", side_effect=lose_ack):
            with self.assertRaisesRegex(ValueError, "UNKNOWN; never auto-retry"):
                self.run_claim()
        self.assertEqual(pushed, 1)
        self.assertNotEqual(self.git(self.bare, "rev-parse", "HEAD"), self.first)

    def test_push_time_conflict_refuses_without_overwriting_competitor(self):
        before = cas._git
        calls = 0

        def race_during_push(cwd, *args, **kwargs):
            nonlocal calls
            if args and args[0] == "push":
                calls += 1
                self.git(self.seed, "commit", "--allow-empty", "-q", "-m", "push-time writer")
                self.git(self.seed, "push", "-q", "origin", "HEAD:refs/heads/agent-control")
            return before(cwd, *args, **kwargs)

        with mock.patch.object(cas, "_git", side_effect=race_during_push):
            with self.assertRaisesRegex(ValueError, "UNKNOWN; never auto-retry"):
                self.run_claim()
        self.assertEqual(calls, 1)
        self.assertEqual(self.git(self.bare, "rev-parse", "HEAD"), self.git(self.seed, "rev-parse", "HEAD"))
        self.assertFalse((self.seed / ".agent/claims").exists())

    def test_ambiguous_post_push_ack_is_not_replayed(self):
        before = cas._git
        calls = 0
        pushed = False

        def lose_confirmation(cwd, *args, **kwargs):
            nonlocal calls, pushed
            if args and args[0] == "push":
                calls += 1
                pushed = True
            elif pushed and args[:2] == ("rev-parse", "--verify") and cwd == self.bare.resolve():
                raise ValueError("remote confirmation unavailable")
            return before(cwd, *args, **kwargs)

        with mock.patch.object(cas, "_git", side_effect=lose_confirmation):
            with self.assertRaisesRegex(ValueError, "UNKNOWN; never auto-retry"):
                self.run_claim()
        self.assertEqual(calls, 1)
        self.assertNotEqual(self.git(self.bare, "rev-parse", "HEAD"), self.first)

    def test_canonicalizes_native_temp_but_rejects_arbitrary_parent_alias(self):
        root, bare = cas._require_fixture_root(self.root, self.bare)
        self.assertEqual(root, self.root.resolve())
        self.assertEqual(bare, self.bare.resolve())
        with tempfile.TemporaryDirectory() as temp:
            alias = Path(temp) / "user-alias"
            alias.symlink_to(self.root.parent, target_is_directory=True)
            aliased_root = alias / self.root.name
            with self.assertRaisesRegex(ValueError, "explicit marker"):
                cas._require_fixture_root(aliased_root, aliased_root / self.bare.name)

    def test_bad_fixture_marker_and_symlink_remote(self):
        (self.root / ".local-agent-disposable-cas-fixture").unlink()
        with self.assertRaisesRegex(ValueError, "explicit marker"):
            self.run_claim()
        (self.root / ".local-agent-disposable-cas-fixture").write_text("offline-fixture-only\n")
        other = self.root / "outside.git"
        other.symlink_to(self.bare)
        with self.assertRaisesRegex(ValueError, "bare directory"):
            cas.simulate_host_claim_cas(self.root, other,
                expected_control_sha=self.first, expected_operator_sha=OPERATOR,
                expected_task_digest=task_digest(self.task), task_id=self.task["id"],
                local_identity=IDENTITY, enrollment=self.enrollment,
                repository_id="local-agent", agent_binding=BINDING)


if __name__ == "__main__":
    unittest.main()
