from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_agent.repository.context import (
    DEFAULT_REPOSITORY,
    DEFAULT_REPOSITORY_ID,
    RepositoryContext,
    load_repository_registry,
    repository_config_digest,
    repository_lease_keys,
)


class RepositoryRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        self.registry = self.home / "repositories.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, repositories: list[dict]) -> None:
        self.registry.write_text(
            json.dumps({"version": 1, "repositories": repositories}),
            encoding="utf-8",
        )

    def test_missing_default_registry_preserves_legacy_example_project_workspace(self) -> None:
        repositories = load_repository_registry(home=self.home)
        self.assertEqual(len(repositories), 1)
        repository = repositories[0]
        self.assertEqual(repository.repository_id, DEFAULT_REPOSITORY_ID)
        self.assertEqual(repository.repository, DEFAULT_REPOSITORY)
        self.assertEqual(repository.control, self.home / "agent-workspace" / "control")
        self.assertEqual(repository.work, self.home / "agent-workspace" / "work")
        self.assertEqual(
            repository.checkpoints,
            self.home / "agent-workspace" / "checkpoints",
        )

    def test_explicit_host_only_registry_accepts_zero_projects(self) -> None:
        self.write([])
        with self.assertRaisesRegex(ValueError, "at least one"):
            load_repository_registry(home=self.home, path=self.registry)
        self.assertEqual(
            load_repository_registry(home=self.home, path=self.registry, allow_empty=True),
            [],
        )

    def test_host_only_missing_default_does_not_infer_example_project(self) -> None:
        self.assertEqual(
            load_repository_registry(home=self.home, allow_empty=True),
            [],
        )
        with self.assertRaisesRegex(ValueError, "does not exist"):
            load_repository_registry(home=self.home, path=self.registry, allow_empty=True)

    def test_host_only_all_projects_disabled_is_empty(self) -> None:
        self.write([{"id": "example-project", "repository": "owner/example-project", "enabled": False}])
        self.assertEqual(
            load_repository_registry(home=self.home, path=self.registry, allow_empty=True),
            [],
        )

    def test_multiple_repositories_receive_isolated_default_workspaces(self) -> None:
        self.write(
            [
                {
                    "id": "example-project",
                    "repository": "example-owner/example-project",
                    "legacy_workspace": True,
                },
                {"id": "example-maps", "repository": "example-owner/example-maps"},
                {"id": "wreckscanner", "repository": "example-owner/WreckScanner"},
            ]
        )
        repositories = load_repository_registry(home=self.home, path=self.registry)
        self.assertEqual([item.repository_id for item in repositories], ["example-project", "example-maps", "wreckscanner"])
        self.assertEqual(repositories[0].work, self.home / "agent-workspace" / "work")
        self.assertEqual(
            repositories[1].work,
            self.home / "agent-workspace" / "repos" / "example-maps" / "work",
        )
        self.assertEqual(
            repositories[2].control,
            self.home / "agent-workspace" / "repos" / "wreckscanner" / "control",
        )
        all_paths = {
            path
            for repository in repositories
            for path in (repository.control, repository.work, repository.checkpoints)
        }
        self.assertEqual(len(all_paths), 9)

    def test_disabled_repository_is_ignored(self) -> None:
        self.write(
            [
                {"id": "one", "repository": "owner/one"},
                {"id": "two", "repository": "owner/two", "enabled": False},
            ]
        )
        repositories = load_repository_registry(home=self.home, path=self.registry)
        self.assertEqual([item.repository_id for item in repositories], ["one"])

    def test_duplicate_repository_id_is_rejected(self) -> None:
        self.write(
            [
                {"id": "same", "repository": "owner/one"},
                {"id": "same", "repository": "owner/two"},
            ]
        )
        with self.assertRaisesRegex(ValueError, "duplicate repository id"):
            load_repository_registry(home=self.home, path=self.registry)

    def test_case_insensitive_repository_id_is_rejected(self) -> None:
        self.write(
            [
                {"id": "Project-A", "repository": "owner/one"},
                {"id": "project-a", "repository": "owner/two"},
            ]
        )
        with self.assertRaisesRegex(ValueError, "duplicate repository id"):
            load_repository_registry(home=self.home, path=self.registry)

    def test_duplicate_remote_repository_is_rejected_case_insensitively(self) -> None:
        self.write(
            [
                {"id": "one", "repository": "Owner/Project"},
                {"id": "two", "repository": "owner/project"},
            ]
        )
        with self.assertRaisesRegex(ValueError, "duplicate remote repository"):
            load_repository_registry(home=self.home, path=self.registry)

    def test_workspace_collision_is_rejected(self) -> None:
        shared = self.home / "shared"
        self.write(
            [
                {"id": "one", "repository": "owner/one", "work_dir": str(shared)},
                {"id": "two", "repository": "owner/two", "control_dir": str(shared)},
            ]
        )
        with self.assertRaisesRegex(ValueError, "workspace path collision"):
            load_repository_registry(home=self.home, path=self.registry)

        self.write(
            [
                {
                    "id": "nested",
                    "repository": "owner/nested",
                    "work_dir": str(shared),
                    "checkpoints_dir": str(shared / "checkpoints"),
                }
            ]
        )
        with self.assertRaisesRegex(ValueError, "workspace path collision"):
            load_repository_registry(home=self.home, path=self.registry)

    def test_normalized_and_nested_workspace_collisions_are_rejected(self) -> None:
        shared = self.home / "shared"
        self.write(
            [
                {"id": "one", "repository": "owner/one", "work_dir": str(shared)},
                {
                    "id": "two",
                    "repository": "owner/two",
                    "control_dir": str(self.home / "parent" / ".." / "shared"),
                },
            ]
        )
        with self.assertRaisesRegex(ValueError, "workspace path collision"):
            load_repository_registry(home=self.home, path=self.registry)

    def test_case_insensitive_workspace_collision_is_rejected_before_creation(self) -> None:
        self.write(
            [
                {
                    "id": "one",
                    "repository": "owner/one",
                    "work_dir": str(self.home / "Workspace"),
                },
                {
                    "id": "two",
                    "repository": "owner/two",
                    "control_dir": str(self.home / "workspace"),
                },
            ]
        )
        with self.assertRaisesRegex(ValueError, "workspace path collision"):
            load_repository_registry(home=self.home, path=self.registry)

    def test_existing_symlink_workspace_alias_is_rejected(self) -> None:
        shared = self.home / "shared"
        shared.mkdir()
        alias = self.home / "alias"
        alias.symlink_to(shared, target_is_directory=True)
        self.write(
            [
                {"id": "one", "repository": "owner/one", "work_dir": str(shared)},
                {"id": "two", "repository": "owner/two", "control_dir": str(alias)},
            ]
        )
        with self.assertRaisesRegex(ValueError, "workspace path collision"):
            load_repository_registry(home=self.home, path=self.registry)

    def test_config_digest_changes_with_execution_configuration(self) -> None:
        root = self.home / "project"
        first = RepositoryContext(
            repository_id="project",
            repository="owner/project",
            control=root / "control",
            work=root / "work",
            checkpoints=root / "checkpoints",
        )
        second = RepositoryContext(
            repository_id="project",
            repository="owner/project",
            control=root / "control",
            work=root / "work-two",
            checkpoints=root / "checkpoints",
        )
        self.assertNotEqual(
            repository_config_digest(first),
            repository_config_digest(second),
        )

    def test_lease_keys_cover_remote_and_every_workspace_identity(self) -> None:
        root = self.home / "project"
        first = RepositoryContext(
            repository_id="first-id",
            repository="Owner/Project",
            control=root / "control",
            work=root / "work",
            checkpoints=root / "checkpoints",
        )
        renamed = RepositoryContext(
            repository_id="renamed-id",
            repository="owner/project",
            control=root / "control",
            work=root / "work",
            checkpoints=root / "checkpoints",
        )
        keys = repository_lease_keys(first)
        renamed_keys = repository_lease_keys(renamed)
        self.assertEqual(
            set(keys) - {"id:first-id"},
            set(renamed_keys) - {"id:renamed-id"},
        )
        self.assertEqual(len(keys), 5)
        self.assertIn("id:first-id", keys)
        self.assertTrue(any(item.startswith("remote:") for item in keys))
        self.assertTrue(any(item.startswith("work:") for item in keys))

    def test_registry_booleans_require_exact_json_boolean_type(self) -> None:
        for field in ("enabled", "legacy_workspace"):
            with self.subTest(field=field):
                self.write(
                    [{"id": "one", "repository": "owner/one", field: "false"}]
                )
                with self.assertRaisesRegex(ValueError, "must be a boolean"):
                    load_repository_registry(home=self.home, path=self.registry)

    def test_invalid_repository_and_relative_paths_are_rejected(self) -> None:
        self.write([{"id": "bad/id", "repository": "owner/repo"}])
        with self.assertRaisesRegex(ValueError, "invalid repository id"):
            load_repository_registry(home=self.home, path=self.registry)

        self.write([{"id": "ok", "repository": "not-a-repository"}])
        with self.assertRaisesRegex(ValueError, "invalid repository name"):
            load_repository_registry(home=self.home, path=self.registry)

        self.write(
            [
                {
                    "id": "ok",
                    "repository": "owner/repo",
                    "work_dir": "relative/work",
                }
            ]
        )
        with self.assertRaisesRegex(ValueError, "must be absolute"):
            load_repository_registry(home=self.home, path=self.registry)


if __name__ == "__main__":
    unittest.main()
