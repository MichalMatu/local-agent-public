"""Offline read-only installation and repository publication spool inventory."""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.cli.diagnostics import build_parser
from local_agent.cli.publications import command_publications
from local_agent.runtime.publication_snapshot import observe_local_publications


class PublicationSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="la-publications-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.registry = self.root / "repositories.json"
        self.registry.write_text(json.dumps({
            "version": 1, "repositories": [
                {"id": "repo-a"},
                {"id": "disabled", "enabled": False},
                {"id": "repo-b"},
            ],
        }), encoding="utf-8")
        self.registry.chmod(0o600)
        (self.state / "result-spool").mkdir(mode=0o700)
        for repo_id in ("repo-a", "repo-b"):
            for kind in ("claims", "result-spool", "corrupt-claims"):
                (self.state / "repositories" / repo_id / kind).mkdir(
                    parents=True, exist_ok=True, mode=0o700,
                )

    def snapshot(self) -> dict[str, object]:
        return observe_local_publications(
            state_root=self.state, registry_file=self.registry,
        )

    def test_empty_host_and_project_spools_are_observed(self) -> None:
        report = self.snapshot()
        self.assertEqual(report["status"], "observed")
        self.assertEqual(report["installation_spool_files"], 0)
        self.assertEqual(report["total_spool_files"], 0)
        self.assertEqual(
            report["repositories"],
            [{"repository_id": "repo-a", "durable_spool_files": 0},
             {"repository_id": "repo-b", "durable_spool_files": 0}],
        )
        self.assertFalse(report["additional_host_mailbox_namespaces_observed"])
        self.assertFalse(report["consistent_point_in_time"])
        self.assertEqual(report["publication_eligibility"], "not_asserted")
        self.assertEqual(report["replay_authority"], "not_asserted")
        self.assertFalse(report["mutated"])

    def test_counts_durable_installation_and_project_files_without_contents(self) -> None:
        root_record = self.state / "result-spool" / "host.json"
        project_record = self.state / "repositories" / "repo-a" / "result-spool" / "task.json"
        root_record.write_text("secret host result", encoding="utf-8")
        project_record.write_text("secret project result", encoding="utf-8")
        before = root_record.read_bytes(), project_record.read_bytes(), self.registry.read_bytes()
        with mock.patch("pathlib.Path.open", side_effect=AssertionError("contents forbidden")), \
             mock.patch("fcntl.flock", side_effect=AssertionError("lease probe forbidden")):
            report = self.snapshot()
        self.assertEqual(report["status"], "observed")
        self.assertEqual(report["installation_spool_files"], 1)
        self.assertEqual(report["total_spool_files"], 2)
        self.assertEqual(report["repositories"][0]["durable_spool_files"], 1)
        self.assertEqual(report["publication_eligibility"], "not_asserted")
        self.assertNotIn("secret", json.dumps(report))
        self.assertNotIn(str(self.state), json.dumps(report))
        self.assertEqual(before, (
            root_record.read_bytes(), project_record.read_bytes(), self.registry.read_bytes(),
        ))

    def test_missing_host_spool_is_unknown_not_zero(self) -> None:
        (self.state / "result-spool").rmdir()
        result = self.snapshot()
        self.assertEqual(result["status"], "unverified")
        self.assertIsNone(result["installation_spool_files"])
        self.assertIsNone(result["total_spool_files"])
        self.assertIn("installation_spool_unavailable", result["warnings"])

    def test_missing_project_spool_keeps_other_observation_and_no_false_total(self) -> None:
        (self.state / "repositories" / "repo-b" / "result-spool").rmdir()
        result = self.snapshot()
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["installation_spool_files"], 0)
        self.assertIsNone(result["total_spool_files"])
        self.assertIsNone(result["repositories"][1]["durable_spool_files"])
        self.assertIn("backlog:result_spool_unavailable", result["warnings"])

    def test_untrusted_files_and_symlinks_are_not_followed(self) -> None:
        spool = self.state / "result-spool"
        (spool / "malicious.json").symlink_to(self.registry)
        result = self.snapshot()
        self.assertEqual(result["status"], "unverified")
        self.assertIsNone(result["installation_spool_files"])
        (spool / "malicious.json").unlink()
        (spool / "wrong.txt").write_text("not a result", encoding="utf-8")
        self.assertEqual(self.snapshot()["status"], "unverified")
        (spool / "wrong.txt").unlink()
        spool.chmod(0o777)
        self.assertEqual(self.snapshot()["status"], "unverified")

    def test_empty_project_registry_does_not_require_project_directories(self) -> None:
        self.registry.write_text('{"version":1,"repositories":[]}', encoding="utf-8")
        shutil.rmtree(self.state / "repositories")
        report = self.snapshot()
        self.assertEqual(report["status"], "observed")
        self.assertEqual(report["repositories"], [])
        self.assertEqual(report["total_spool_files"], 0)

    def test_corrupt_registry_and_relative_root_fail_closed(self) -> None:
        self.registry.write_text('{"version":1,"version":1}', encoding="utf-8")
        self.assertEqual(self.snapshot()["status"], "unverified")
        self.assertIn("backlog:registry_unavailable", self.snapshot()["warnings"])
        self.assertIsNone(self.snapshot()["total_spool_files"])
        self.assertEqual(
            observe_local_publications(
                state_root=Path("relative"), registry_file=self.registry,
            )["status"], "unverified",
        )

    def test_cli_is_core_adapter_with_stable_exit_codes(self) -> None:
        args = build_parser().parse_args([
            "publications", "--state-root", str(self.state),
            "--registry-file", str(self.registry),
        ])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(command_publications(args), 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "observed")
        (self.state / "result-spool" / "pending.json").write_text("no replay")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(command_publications(args), 0)
        self.assertEqual(json.loads(out.getvalue())["total_spool_files"], 1)
        self.assertEqual(json.loads(out.getvalue())["replay_authority"], "not_asserted")
        (self.state / "result-spool").chmod(0o777)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(command_publications(args), 2)
        self.assertEqual(json.loads(out.getvalue())["status"], "unverified")


if __name__ == "__main__":
    unittest.main()
