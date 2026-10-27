"""Real Git private producer reuse across two independently granted host tasks.

No production Git or credentials: the existing local bare-Git fixture is
the only origin trust mock. Worker, durable receipt, and Git sync are real.
"""
from __future__ import annotations

import contextlib
import json
import unittest
from pathlib import Path
from unittest import mock

from local_agent.operator import remote as operator_remote
from local_agent.runtime.task_contract import task_digest
from local_agent.supervisor import host_private_producer as private
from local_agent.supervisor import host_remote_client


class PrivateProducerSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.test_host_private_producer_setup import PrivateProducerSetupTests

        self.fixture = PrivateProducerSetupTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f
        self.first = self.fixture.authorized
        self.second = self.f._task("private-producer-second")
        pin = self.f._publish_grants([self.f.task, self.first, self.second])
        (self.f.state / "operator-control.sha").write_text(pin + "\n")
        self.profile = Path(self.fixture._setup()["profile"])
        self.producer = self.profile.parent / "control"

    @contextlib.contextmanager
    def _fixture_trust(self):
        with (
            mock.patch.object(private, "_require_control_origin"),
            mock.patch.object(private, "_validate_origin"),
            mock.patch.object(operator_remote, "_require_trusted_host_registry_origin"),
            mock.patch.object(host_remote_client, "_require_control_origin"),
        ):
            yield

    def _commit_remote_result(self) -> tuple[str, str]:
        with self._fixture_trust():
            draft = self.f.root / "first-draft.json"
            draft.write_text(json.dumps(self.first))
            code, staged = self.fixture._cli([
                "host-stage", str(draft), "--profile", str(self.profile),
            ])
            self.assertEqual(code, 0, staged)
            old = self.f._git(self.producer, "rev-parse", "HEAD")
            code, published = self.fixture._cli([
                "host-publish", self.first["id"], "--profile", str(self.profile),
                "--expected-control-sha", old,
                "--expected-task-digest", task_digest(self.first),
            ])
            self.assertEqual(code, 0, published)
            published_sha = published["control_sha"]
        self.f._git(self.f.checkout, "pull", "-q", "--ff-only")
        self.assertEqual(self.f._execute(self.first["id"]), "published")
        self.assertEqual((self.f.work / "effect-count.txt").read_text(), "x")
        result_sha = self.f._git(self.f.root, "--git-dir", str(self.f.remote),
                                 "rev-parse", "refs/heads/agent-control")
        self.assertNotEqual(result_sha, published_sha)
        return published_sha, result_sha

    def test_two_tasks_sync_between_worker_results_without_replay(self) -> None:
        previous, remote = self._commit_remote_result()
        with self._fixture_trust():
            code, synced = self.fixture._cli([
                "host-producer-sync", "--profile", str(self.profile),
                "--expected-local-sha", previous, "--expected-remote-sha", remote,
            ])
            self.assertEqual(code, 0, synced)
            self.assertEqual(synced["status"], "fast_forwarded")
            self.assertFalse(synced["published"])
            self.assertEqual(self.f._git(self.producer, "rev-parse", "HEAD"), remote)
            code, same = self.fixture._cli([
                "host-producer-sync", "--profile", str(self.profile),
                "--expected-local-sha", remote, "--expected-remote-sha", remote,
            ])
            self.assertEqual(code, 0, same)
            self.assertEqual(same["status"], "already_current")
            draft = self.f.root / "second-draft.json"
            draft.write_text(json.dumps(self.second))
            code, staged = self.fixture._cli([
                "host-stage", str(draft), "--profile", str(self.profile),
            ])
            self.assertEqual(code, 0, staged)
            code, published = self.fixture._cli([
                "host-publish", self.second["id"], "--profile", str(self.profile),
                "--expected-control-sha", remote,
                "--expected-task-digest", task_digest(self.second),
            ])
            self.assertEqual(code, 0, published)
        self.f._git(self.f.checkout, "pull", "-q", "--ff-only")
        self.assertEqual(self.f._execute(self.second["id"]), "published")
        self.assertEqual(self.f._execute(self.second["id"]), "published")
        self.assertEqual((self.f.work / "effect-count.txt").read_text(), "xx")
        reader = self.f.root / "second-independent-reader"
        self.f._git(self.f.root, "clone", "-q", "-b", "agent-control",
                    str(self.f.remote), str(reader))
        final = self.f._git(reader, "rev-parse", "HEAD")
        with self._fixture_trust():
            for task in (self.first, self.second):
                code, observed = self.fixture._cli([
                    "host-result", task["id"], "--profile", str(self.profile),
                    "--control-checkout", str(reader),
                    "--expected-control-sha", final,
                    "--expected-task-digest", task_digest(task),
                ])
                self.assertEqual(code, 0, observed)
                self.assertEqual(observed["result"]["status"], "done")
        self.assertEqual((self.f.work / "effect-count.txt").read_text(), "xx")

    def test_dirty_producer_refuses_sync_and_keeps_local_head(self) -> None:
        previous, remote = self._commit_remote_result()
        (self.producer / "not-staged-work.txt").write_text("keep me")
        with self._fixture_trust():
            code, denied = self.fixture._cli([
                "host-producer-sync", "--profile", str(self.profile),
                "--expected-local-sha", previous, "--expected-remote-sha", remote,
            ])
        self.assertEqual(code, 2, denied)
        self.assertEqual(denied["status"], "unverified")
        self.assertEqual(self.f._git(self.producer, "rev-parse", "HEAD"), previous)
        self.assertEqual((self.producer / "not-staged-work.txt").read_text(), "keep me")
        self.assertEqual((self.f.work / "effect-count.txt").read_text(), "x")

    def test_wrong_remote_or_local_pin_cannot_sync(self) -> None:
        previous, remote = self._commit_remote_result()
        for local, target in ((previous, "a" * 40), ("b" * 40, remote)):
            with self._fixture_trust():
                code, denied = self.fixture._cli([
                    "host-producer-sync", "--profile", str(self.profile),
                    "--expected-local-sha", local, "--expected-remote-sha", target,
                ])
            self.assertEqual(code, 2, denied)
        self.assertEqual(self.f._git(self.producer, "rev-parse", "HEAD"), previous)

    def test_divergent_local_commit_is_not_reset_rebased_or_pushed(self) -> None:
        previous, remote = self._commit_remote_result()
        (self.producer / "local-only.txt").write_text("do not lose this")
        self.f._git(self.producer, "add", "local-only.txt")
        self.f._git(self.producer, "commit", "-qm", "unpublished local commit")
        local = self.f._git(self.producer, "rev-parse", "HEAD")
        self.assertNotEqual(local, previous)
        with self._fixture_trust():
            code, denied = self.fixture._cli([
                "host-producer-sync", "--profile", str(self.profile),
                "--expected-local-sha", local, "--expected-remote-sha", remote,
            ])
        self.assertEqual(code, 2, denied)
        self.assertEqual(self.f._git(self.producer, "rev-parse", "HEAD"), local)
        self.assertEqual((self.producer / "local-only.txt").read_text(), "do not lose this")
        self.assertEqual(self.f._git(self.f.root, "--git-dir", str(self.f.remote),
                                     "rev-parse", "refs/heads/agent-control"), remote)

    def test_revoked_pin_blocks_sync_before_git_mutation(self) -> None:
        previous, remote = self._commit_remote_result()
        (self.f.state / "operator-control.sha").write_text("a" * 40 + "\n")
        with self._fixture_trust():
            code, denied = self.fixture._cli([
                "host-producer-sync", "--profile", str(self.profile),
                "--expected-local-sha", previous, "--expected-remote-sha", remote,
            ])
        self.assertEqual(code, 2, denied)
        self.assertEqual(self.f._git(self.producer, "rev-parse", "HEAD"), previous)


if __name__ == "__main__":
    unittest.main()
