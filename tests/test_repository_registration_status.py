"""Offline registration status is not a daemon health or execution authority check."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.repository import admin
from local_agent.repository.registration_status import repository_registration_status


class RepositoryRegistrationStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="la-registry-status-")
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name) / "home"
        self.home.mkdir()
        self.registry = self.home / "repositories.json"

    def write(self, entries: list[dict], *, version: int = 1) -> bytes:
        data = (json.dumps({"version": version, "repositories": entries}) + "\n").encode()
        self.registry.write_bytes(data)
        return data

    def entry(self, repo_id: str = "first", **extra: object) -> dict:
        result = {
            "id": repo_id,
            "repository": f"owner/{repo_id}",
            "work_dir": str(self.home / repo_id / "work"),
            "control_dir": str(self.home / repo_id / "control"),
            "checkpoints_dir": str(self.home / repo_id / "checkpoints"),
            "work_mode": "attached",
        }
        result.update(extra)
        return result

    def status(self, **kwargs: object) -> dict:
        return repository_registration_status(
            home=self.home, registry_path=self.registry, **kwargs,
        )

    def test_includes_disabled_and_never_asserts_running_state(self) -> None:
        first = self.entry(agent_binding="b0e94f93-478f-4fc6-abed-159d3702362f")
        second = self.entry("second", enabled=False, work_mode="managed")
        before = self.write([first, second])
        owner_file = self.home / "first" / "work" / "uncommitted.txt"
        owner_file.parent.mkdir(parents=True)
        owner_file.write_text("KEEP")
        with mock.patch.object(admin, "run_git", side_effect=AssertionError("git forbidden")):
            result = self.status()
        self.assertEqual(result["counts"], {"total": 2, "enabled": 1, "disabled": 1})
        self.assertEqual([p["enabled"] for p in result["repositories"]], [True, False])
        self.assertEqual(result["repositories"][0]["work_mode"], "attached")
        self.assertEqual(result["active_claims"], "not_checked")
        self.assertEqual(result["active_leases"], "not_checked")
        self.assertEqual(result["execution_authority"], "not_asserted")
        self.assertFalse(result["changes_applied"])
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(owner_file.read_text(), "KEEP")
        self.assertFalse((self.home / "second").exists())

    def test_no_fixed_repository_count_for_zero_three_four_nine_or_more(self) -> None:
        entries = [self.entry(f"repo-{i}", work_mode="managed")
                   for i in range(12)]
        for enabled_count in (0, 3, 4, 9, 12):
            with self.subTest(enabled_count=enabled_count):
                selected = [{**item, "enabled": i < enabled_count}
                            for i, item in enumerate(entries)]
                before = self.write(selected)
                status = self.status()
                self.assertEqual(status["counts"], {
                    "total": 12,
                    "enabled": enabled_count,
                    "disabled": 12 - enabled_count,
                })
                self.assertEqual(
                    [row["repository_id"] for row in status["repositories"]
                     if row["enabled"]],
                    [f"repo-{i}" for i in range(enabled_count)],
                )
                self.assertEqual(before, self.registry.read_bytes())
                self.assertFalse((self.home / "repo-0").exists())

    def test_empty_explicit_registry_is_legitimate(self) -> None:
        before = self.write([])
        self.assertEqual(self.status()["counts"]["total"], 0)
        self.assertEqual(self.status()["status"], "configured")
        self.assertEqual(self.registry.read_bytes(), before)

    def test_missing_implicit_registry_is_unconfigured_without_legacy_invention(self) -> None:
        result = repository_registration_status(home=self.home)
        self.assertEqual(result["status"], "unconfigured")
        self.assertEqual(result["repositories"], [])
        self.assertFalse(result["changes_applied"])
        self.assertFalse((self.home / "Library").exists())

    def test_missing_explicit_registry_and_unknown_id_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit repository registry"):
            self.status()
        self.write([self.entry()])
        with self.assertRaisesRegex(ValueError, "not registered"):
            self.status(repository_id="missing")
        self.assertEqual(self.status(repository_id="first")["counts"]["total"], 1)

    def test_disabled_duplicate_or_nested_path_is_rejected(self) -> None:
        self.write([self.entry(), self.entry("first", enabled=False)])
        with self.assertRaisesRegex(ValueError, "duplicate repository id"):
            self.status()
        second = self.entry("second", enabled=False)
        second["control_dir"] = str(self.home / "first" / "work" / "nested")
        self.write([self.entry(), second])
        with self.assertRaisesRegex(ValueError, "workspace path collision"):
            self.status()

    def test_invalid_enabled_versions_and_entries_fail_closed(self) -> None:
        self.write([self.entry(enabled="false")])
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            self.status()
        self.write([], version=2)
        with self.assertRaisesRegex(ValueError, "unsupported repository registry version"):
            self.status()
        self.registry.write_text('{"version":1,"repositories":[null]}')
        with self.assertRaisesRegex(ValueError, "entries must be objects"):
            self.status()
        self.registry.write_text("{broken")
        with self.assertRaises(ValueError):
            self.status()

    def test_symlink_registry_is_refused_without_following(self) -> None:
        target = self.home / "private.json"
        target.write_text('{"version":1,"repositories":[]}')
        self.registry.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "must not be a symlink"):
            self.status()
        self.assertTrue(target.exists())

    def test_cli_status_json_and_errors_are_read_only(self) -> None:
        before = self.write([self.entry(enabled=False)])
        output = io.StringIO()
        argv = ["admin.py", "--registry", str(self.registry), "status", "--repository-id", "first"]
        with mock.patch("sys.argv", argv), contextlib.redirect_stdout(output):
            self.assertEqual(admin.main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["counts"]["disabled"], 1)
        self.assertEqual(self.registry.read_bytes(), before)

        output = io.StringIO()
        with mock.patch("sys.argv", argv[:-1] + ["unknown"]), contextlib.redirect_stdout(output):
            self.assertEqual(admin.main(), 2)
        failure = json.loads(output.getvalue())
        self.assertEqual(failure["status"], "unverified")
        self.assertFalse(failure["changes_applied"])


if __name__ == "__main__":
    unittest.main()
