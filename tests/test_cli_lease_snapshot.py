"""Read-only repository lease inventory; never acquire or infer held locks."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.cli.diagnostics import build_parser
from local_agent.cli.leases import command_leases
from local_agent.runtime.lease_snapshot import observe_local_lease_inventory


class LeaseSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="la-lease-snapshot-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.registry = self.root / "repositories.json"
        self.set_repositories("repo-a")
        self.locks = self.state / "locks" / "repositories"
        self.locks.mkdir(parents=True, mode=0o700)

    def set_repositories(self, *ids: str) -> None:
        self.registry.write_text(
            json.dumps({"version": 1, "repositories": [
                {"id": repo_id, "enabled": True} for repo_id in ids
            ]}) + "\n", encoding="utf-8",
        )
        self.registry.chmod(0o600)

    def lease(self, key: str) -> Path:
        name = hashlib.sha256(key.encode("utf-8")).hexdigest() + ".lock"
        target = self.locks / name
        target.write_text(json.dumps({"pid": 100, "key": key}), encoding="utf-8")
        target.chmod(0o600)
        return target

    def snapshot(self) -> dict[str, object]:
        return observe_local_lease_inventory(
            state_root=self.state, registry_file=self.registry,
        )

    def test_inventory_is_observed_without_reading_or_probing_lock_file(self) -> None:
        path = self.lease("id:repo-a")
        original = path.read_bytes(), self.registry.read_bytes()
        with mock.patch("fcntl.flock", side_effect=AssertionError("flock forbidden")), \
             mock.patch("pathlib.Path.open", side_effect=AssertionError("record content forbidden")):
            # Registry's secure reader uses os.open; no Path.open is required.
            result = self.snapshot()
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["repository_count"], 1)
        self.assertEqual(result["lease_file_count"], 1)
        self.assertIsNone(result["active_lease_count"])
        self.assertFalse(result["active_leases_verified"])
        self.assertFalse(result["mutated"])
        self.assertEqual(result["replay_authority"], "not_asserted")
        self.assertNotIn("id:repo-a", json.dumps(result))
        self.assertNotIn(str(self.state), json.dumps(result))
        self.assertEqual((path.read_bytes(), self.registry.read_bytes()), original)

    def test_zero_project_installation_does_not_require_lease_directory(self) -> None:
        self.set_repositories()
        self.locks.rmdir()
        self.locks.parent.rmdir()
        result = self.snapshot()
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["repository_count"], 0)
        self.assertEqual(result["lease_file_count"], 0)
        self.assertIsNone(result["active_lease_count"])

    def test_nonempty_registry_missing_leases_is_unverified_not_free(self) -> None:
        self.locks.rmdir()
        report = self.snapshot()
        self.assertEqual(report["status"], "unverified")
        self.assertIsNone(report["lease_file_count"])
        self.assertIn("lease_inventory_unavailable", report["warnings"])

    def test_symlinks_and_untrusted_permissions_fail_closed(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        self.locks.rmdir()
        self.locks.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.snapshot()["status"], "unverified")
        self.locks.unlink()
        self.locks.mkdir()
        self.locks.chmod(0o777)
        self.assertEqual(self.snapshot()["status"], "unverified")
        self.locks.chmod(0o700)
        self.lease("id:repo-a").unlink()
        (self.locks / ("a" * 64 + ".lock")).symlink_to(self.registry)
        self.assertEqual(self.snapshot()["status"], "unverified")
        self.assertEqual(self.snapshot()["execution_authority"], "not_asserted")

    def test_unexpected_names_and_nonregular_entries_are_unverified(self) -> None:
        (self.locks / "invalid.lock").write_text("malformed", encoding="utf-8")
        self.assertEqual(self.snapshot()["status"], "unverified")
        (self.locks / "invalid.lock").unlink()
        (self.locks / ("b" * 64 + ".lock")).mkdir()
        self.assertEqual(self.snapshot()["status"], "unverified")

    def test_invalid_registry_and_relative_root_are_unverified(self) -> None:
        self.registry.write_text('{"version":1,"version":1}', encoding="utf-8")
        self.assertEqual(self.snapshot()["warnings"], ["registry_unavailable"])
        self.set_repositories("repo-a")
        report = observe_local_lease_inventory(
            state_root=Path("relative"), registry_file=self.registry,
        )
        self.assertEqual(report["status"], "unverified")
        self.assertIsNone(report["lease_file_count"])

    def test_cli_exit_codes_and_readonly_json(self) -> None:
        args = build_parser().parse_args([
            "leases", "--state-root", str(self.state),
            "--registry-file", str(self.registry),
        ])
        path = self.lease("control:/private/sensitive")
        before = path.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(command_leases(args), 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "observed")
        self.assertNotIn("sensitive", stdout.getvalue())
        self.assertEqual(path.read_bytes(), before)
        (self.locks / "bad").write_text("not allowed")
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(command_leases(args), 2)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "unverified")


if __name__ == "__main__":
    unittest.main()
