"""Disposable bare Git acceptance for the offline-only status CAS contract."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from local_agent.repository.host_routing import HostIdentity, build_host_status, validate_host_enrollments
from local_agent.repository import host_status_cas as cas

IDENTITY = HostIdentity("worker-linux", "550e8400-e29b-41d4-a716-446655440000")
BINDING = "b0e94f93-478f-4fc6-abed-159d3702362f"
OPERATOR = "a" * 40
SOURCE = "b" * 40


class OfflineCASFixtureTests(unittest.TestCase):
    def git(self, cwd, *args):
        return subprocess.check_output(["git", "-C", str(cwd), *args], text=True,
                                       stderr=subprocess.DEVNULL).strip()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / ".local-agent-disposable-cas-fixture").write_text("offline-fixture-only\n")
        self.bare = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", "--initial-branch=agent-control", str(self.bare)],
                       check=True, stdout=subprocess.DEVNULL)
        self.seed = self.root / "seed"
        subprocess.run(["git", "init", "--initial-branch=agent-control", str(self.seed)],
                       check=True, stdout=subprocess.DEVNULL)
        self.git(self.seed, "config", "user.name", "Offline Test")
        self.git(self.seed, "config", "user.email", "offline@example.invalid")
        self.enrollment = validate_host_enrollments([{
            "version": 1, "host_id": IDENTITY.host_id,
            "installation_id": IDENTITY.installation_id, "enabled": True,
            "repositories": [{"repository_id": "local-agent", "agent_binding": BINDING}],
        }])[0]
        identity_dir = self.seed / ".agent" / "hosts" / IDENTITY.host_id
        identity_dir.mkdir(parents=True)
        (identity_dir / "identity.json").write_text(json.dumps({
            **IDENTITY.as_record(), "publication_authority": "not_asserted",
            "record_type": "diagnostic_preview", "operator_registry_sha": OPERATOR,
        }))
        (self.seed / "README").write_text("local fixture\n")
        self.git(self.seed, "add", "--", "README", ".agent/hosts")
        self.git(self.seed, "commit", "-m", "seed fixture host")
        self.git(self.seed, "remote", "add", "origin", str(self.bare))
        self.git(self.seed, "push", "origin", "HEAD:refs/heads/agent-control")
        self.first_sha = self.git(self.seed, "rev-parse", "HEAD")
        self.now = datetime(2026, 10, 13, 10, tzinfo=timezone.utc)

    def raw(self, when=None, **changes):
        record = build_host_status(IDENTITY, source_revision=SOURCE, state="idle",
                                   observed_at=when or self.now)
        return (json.dumps(record | changes) + "\n").encode()

    def run_fixture(self, **changes):
        args = dict(expected_control_sha=self.first_sha, expected_operator_sha=OPERATOR,
                    expected_source_sha=SOURCE,
                    local_identity=IDENTITY, enrollment=self.enrollment,
                    repository_id="local-agent", agent_binding=BINDING,
                    status_bytes=self.raw())
        return cas.simulate_host_status_cas(self.root, self.bare, **(args | changes))

    def test_initial_cas_publication_changes_one_path_only(self):
        result = self.run_fixture()
        self.assertEqual(result["status"], "offline_fixture_only")
        self.assertFalse(result["published_to_production"])
        self.assertFalse(result["daemon_heartbeat_asserted"])
        self.assertEqual(result["changed_path"], ".agent/hosts/worker-linux/status.json")
        self.assertEqual(self.git(self.bare, "rev-parse", "refs/heads/agent-control"), result["fixture_commit_sha"])
        self.assertEqual(self.git(self.bare, "diff-tree", "--no-commit-id", "--name-only", "-r", result["fixture_commit_sha"]),
                         result["changed_path"])

    def test_second_status_advances_only_with_new_timestamp(self):
        first = self.run_fixture()
        with self.assertRaisesRegex(ValueError, "expected SHA"):
            self.run_fixture()
        for when in (self.now, self.now - timedelta(seconds=1)):
            with self.assertRaisesRegex(ValueError, "must advance"):
                self.run_fixture(expected_control_sha=first["fixture_commit_sha"], status_bytes=self.raw(when))
        second = self.run_fixture(expected_control_sha=first["fixture_commit_sha"],
                                  status_bytes=self.raw(self.now + timedelta(seconds=1)))
        self.assertNotEqual(first["fixture_commit_sha"], second["fixture_commit_sha"])

    def test_stale_or_revoked_identity_never_touches_ref(self):
        for change in (
            {"agent_binding": "550e8400-e29b-41d4-a716-446655440001"},
            {"repository_id": None}, {"repository_id": "../escape"},
            {"expected_source_sha": "c" * 40},
            {"status_bytes": self.raw(source_revision="c" * 40)},
            {"expected_operator_sha": "c" * 40},
            {"expected_control_sha": "d" * 40},
            {"status_bytes": self.raw(host_id="other-host")},
            {"status_bytes": self.raw(execution_authority="automatic")},
            {"enrollment": self.enrollment.__class__(identity=IDENTITY, enabled=False,
                                                       repositories=self.enrollment.repositories)},
        ):
            with self.subTest(change=list(change)), self.assertRaises(ValueError):
                self.run_fixture(**change)
            self.assertEqual(self.git(self.bare, "rev-parse", "refs/heads/agent-control"), self.first_sha)

    def test_bare_outside_marker_or_symlink_rejected(self):
        (self.root / ".local-agent-disposable-cas-fixture").unlink()
        with self.assertRaises(ValueError):
            self.run_fixture()
        (self.root / ".local-agent-disposable-cas-fixture").write_text("offline-fixture-only\n")
        link = self.root / "linked.git"
        link.symlink_to(self.bare)
        with self.assertRaises(ValueError):
            cas.simulate_host_status_cas(self.root, link,
                expected_control_sha=self.first_sha, expected_operator_sha=OPERATOR,
                    expected_source_sha=SOURCE,
                local_identity=IDENTITY, enrollment=self.enrollment,
                repository_id="local-agent", agent_binding=BINDING, status_bytes=self.raw())

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

    def test_symlink_inside_marked_fixture_remains_rejected(self):
        nested = self.root / "nested"
        nested.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "bare directory"):
            cas._require_fixture_root(self.root, nested / "remote.git")

    def test_untrusted_previous_identity_is_not_enrollment(self):
        path = self.seed / ".agent" / "hosts" / IDENTITY.host_id / "identity.json"
        record = json.loads(path.read_text())
        record["operator_registry_sha"] = "c" * 40
        path.write_text(json.dumps(record))
        self.git(self.seed, "add", "--", ".agent/hosts/worker-linux/identity.json")
        self.git(self.seed, "commit", "-m", "replace fixture facts")
        self.git(self.seed, "push", "origin", "HEAD:refs/heads/agent-control")
        head = self.git(self.seed, "rev-parse", "HEAD")
        with self.assertRaisesRegex(ValueError, "do not match pinned"):
            self.run_fixture(expected_control_sha=head)
        self.assertEqual(self.git(self.bare, "rev-parse", "HEAD"), head)

    def test_ambiguous_read_after_write_is_unknown_not_replayed(self):
        original = cas._git
        count = 0

        def no_ack(cwd, *args, **kw):
            nonlocal count
            if args[:1] == ("push",):
                count += 1
            if args[:2] == ("rev-parse", "refs/heads/agent-control") and count:
                raise ValueError("simulated uncertain acknowledgement")
            return original(cwd, *args, **kw)

        with mock.patch.object(cas, "_git", side_effect=no_ack):
            with self.assertRaises(ValueError):
                self.run_fixture()
        self.assertEqual(count, 1)
        self.assertNotEqual(self.git(self.bare, "rev-parse", "HEAD"), self.first_sha)

    def test_concurrent_writer_causes_no_replay(self):
        original = cas._git
        invoked = False

        def raced(cwd, *args, **kw):
            nonlocal invoked
            if args[:2] == ("push", "--porcelain") and not invoked:
                invoked = True
                (self.seed / "OTHER").write_text("external writer\n")
                self.git(self.seed, "add", "OTHER")
                self.git(self.seed, "commit", "-m", "race")
                self.git(self.seed, "push", "origin", "HEAD:refs/heads/agent-control")
            return original(cwd, *args, **kw)

        with mock.patch.object(cas, "_git", side_effect=raced):
            with self.assertRaisesRegex(ValueError, "effect unknown"):
                self.run_fixture()
        self.assertTrue(invoked)
        self.assertEqual(self.git(self.bare, "rev-parse", "refs/heads/agent-control"),
                         self.git(self.seed, "rev-parse", "HEAD"))
        self.assertNotIn("status.json", self.git(self.bare, "ls-tree", "-r", "--name-only", "HEAD"))


if __name__ == "__main__":
    unittest.main()
