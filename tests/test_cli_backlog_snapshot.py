"""Bounded read-only local claims and publication spool census."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.cli.diagnostics import build_parser, command_backlog
from local_agent.runtime.backlog_snapshot import observe_local_backlog


class BacklogSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="la-backlog-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.registry = self.root / "registry.json"
        self.registry.write_text(json.dumps({
            "version": 1, "repositories": [
                {"id": "repo-a"}, {"id": "disabled", "enabled": False},
                {"id": "repo-b"},
            ],
        }) + "\n", encoding="utf-8")
        self.registry.chmod(0o600)
        for repo_id in ("repo-a", "repo-b"):
            for kind in ("claims", "result-spool", "corrupt-claims"):
                path = self.state / "repositories" / repo_id / kind
                path.mkdir(parents=True, mode=0o700)

    def inspect(self):
        return observe_local_backlog(state_root=self.state, registry_file=self.registry)

    def test_empty_present_directories_are_observed_and_disabled_omitted(self):
        report = self.inspect()
        self.assertEqual(report["status"], "observed")
        self.assertEqual([item["repository_id"] for item in report["repositories"]],
                         ["repo-a", "repo-b"])
        self.assertEqual(report["totals"], {
            "claims": 0, "result-spool": 0, "corrupt-claims": 0,
        })
        self.assertFalse(report["mutated"])
        self.assertEqual(report["replay_authority"], "not_asserted")

    def test_counts_are_observations_not_replay_candidates(self):
        for repo_id, kind, name in (
            ("repo-a", "claims", "unknown.json"),
            ("repo-a", "result-spool", "queued.json"),
            ("repo-b", "corrupt-claims", "old.json"),
        ):
            (self.state / "repositories" / repo_id / kind / name).write_text(
                "sensitive payload", encoding="utf-8",
            )
        report = self.inspect()
        self.assertEqual(report["status"], "observed")
        self.assertEqual(report["totals"], {
            "claims": 1, "result-spool": 1, "corrupt-claims": 1,
        })
        self.assertNotIn("sensitive payload", json.dumps(report))
        self.assertNotIn(str(self.state), json.dumps(report))

    def test_missing_directory_is_unverified_not_zero(self):
        path = self.state / "repositories/repo-a/claims"
        path.rmdir()
        report = self.inspect()
        self.assertEqual(report["status"], "unverified")
        self.assertIsNone(report["totals"]["claims"])
        self.assertIn("claims_unavailable", report["warnings"])

    def test_unexpected_file_symlink_or_untrusted_permissions_fail_closed(self):
        path = self.state / "repositories/repo-a/claims"
        (path / "note.txt").write_text("not a claim", encoding="utf-8")
        self.assertIsNone(self.inspect()["totals"]["claims"])
        (path / "note.txt").unlink()
        (path / "evil.json").symlink_to(self.registry)
        self.assertIsNone(self.inspect()["totals"]["claims"])
        (path / "evil.json").unlink()
        path.chmod(0o777)
        self.assertIsNone(self.inspect()["totals"]["claims"])

    def test_overflow_and_untrusted_root_fail_closed(self):
        folder = self.state / "repositories/repo-b/result-spool"
        for index in range(257):
            (folder / f"{index}.json").touch()
        self.assertIsNone(self.inspect()["totals"]["result-spool"])
        self.assertEqual(observe_local_backlog(
            state_root=Path("relative"), registry_file=self.registry,
        )["status"], "unverified")

    def test_rejects_invalid_registry_and_parent_symlink(self):
        self.registry.write_text('{"version":1,"version":1}', encoding="utf-8")
        self.assertIn("registry_unavailable", self.inspect()["warnings"])
        self.registry.write_text('{"version":1,"repositories":[{"id":"repo-a"}]}',
                                 encoding="utf-8")
        folder = self.state / "repositories/repo-a/claims"
        folder.rmdir()
        folder.symlink_to(self.state / "repositories/repo-b/claims")
        self.assertIsNone(self.inspect()["totals"]["claims"])

    def test_cli_backlog_exit_contract_without_network_or_mutations(self):
        args = build_parser().parse_args([
            "backlog", "--state-root", str(self.state),
            "--registry-file", str(self.registry),
        ])
        before = self.registry.read_bytes()
        with mock.patch("local_agent.cli.diagnostics.core.publish_result",
                        side_effect=AssertionError("network/write forbidden")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(command_backlog(args), 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["status"], "observed")
            (self.state / "repositories/repo-b/claims").rmdir()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(command_backlog(args), 2)
        self.assertEqual(before, self.registry.read_bytes())


if __name__ == "__main__":
    unittest.main()
