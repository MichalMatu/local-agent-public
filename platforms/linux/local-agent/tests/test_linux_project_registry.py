"""Isolated source contracts for optional Linux project registry parsing.

These tests do not start a scheduler, publish to Git or change host state.
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from linux_agent.agent import validate_linux_host_subset
from linux_agent.repository.projects import (
    load_project_registry,
    resolve_project_identity,
)


BINDING = "b0e94f93-478f-4fc6-abed-159d3702362f"
OTHER_BINDING = "21010ac9-7eaa-47f2-9c58-180ac0a0819c"
HOST = "pi-zero2w"


class OptionalProjectRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(self.temp)
        self.path = self.home / "projects.json"
        self.entry = {
            "id": "example-project",
            "repository": "example-owner/example-project",
            "agent_binding": BINDING,
            "host_id": HOST,
            "host_mailbox_enabled": True,
            "host_execution_enabled": True,
            "host_identity_file": str(self.home / "private/identity.json"),
            "control_dir": str(self.home / "project/example-project/control"),
            "work_dir": str(self.home / "project/example-project/work"),
            "checkpoints_dir": str(self.home / "project/example-project/checkpoints"),
        }

    def write(self, repositories: list[dict]) -> None:
        self.path.write_text(json.dumps({"version": 1, "repositories": repositories}))

    def load(self, **kwargs):
        return load_project_registry(
            self.path, home=self.home, expected_host_id=HOST, **kwargs
        )

    def test_host_only_empty_registry_has_no_implicit_project(self) -> None:
        self.write([])
        self.assertEqual(self.load(), ())
        self.assertFalse((self.home / "agent-workspace").exists())

    def test_valid_b2_style_entry_is_read_only_and_canonical(self) -> None:
        self.write([self.entry])
        before = self.path.read_bytes()
        projects = self.load()
        self.assertEqual(len(projects), 1)
        p = projects[0]
        self.assertEqual(p.repository_id, "example-project")
        self.assertEqual(p.repository, "example-owner/example-project")
        self.assertEqual(p.agent_binding, BINDING)
        self.assertEqual(p.work, Path(self.entry["work_dir"]))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.home / "project").exists())
        self.assertFalse((self.home / "private").exists())

    def test_explicit_binding_matches_only_exact_selected_project(self) -> None:
        self.write([self.entry])
        projects = self.load()
        task = {"id": "project-1", "mode": "commands", "repository_id": "example-project",
                "agent_binding": BINDING, "commands": ["printf hello"]}
        self.assertEqual(resolve_project_identity(task, projects).repository_id, "example-project")
        for changed in (
            {"agent_binding": OTHER_BINDING},
            {"agent_binding": None},
            {"agent_binding": BINDING.upper()},
            {"repository_id": "another"},
            {"repository_id": "EXAMPLE-PROJECT"},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                resolve_project_identity(task | changed, projects)

    def test_existing_linux_host_only_intake_still_rejects_project(self) -> None:
        self.write([self.entry])
        projects = self.load()
        task = {"id": "project-1", "version": 1, "mode": "commands",
                "cwd": str(self.home), "commands": ["printf hello"],
                "repository_id": "example-project", "agent_binding": BINDING}
        self.assertIsNotNone(resolve_project_identity(task, projects))
        with self.assertRaisesRegex(ValueError, "not implemented"):
            validate_linux_host_subset(task)

    def test_execution_flag_disabled_fails_closed(self) -> None:
        self.write([{**self.entry, "host_execution_enabled": False}])
        task = {"repository_id": "example-project", "agent_binding": BINDING}
        with self.assertRaisesRegex(ValueError, "not enabled"):
            resolve_project_identity(task, self.load())

    def test_disabled_registry_entries_are_not_selected(self) -> None:
        self.write([{**self.entry, "enabled": False}])
        self.assertEqual(self.load(), ())
        self.assertEqual(len(self.load(include_disabled=True)), 1)
        with self.assertRaisesRegex(ValueError, "not explicitly selected"):
            resolve_project_identity(
                {"repository_id": "example-project", "agent_binding": BINDING},
                self.load(),
            )

    def test_wrong_host_and_missing_binding_are_rejected(self) -> None:
        for changed in ({"host_id": "different-host"}, {"agent_binding": None},
                        {"host_id": None}, {"agent_binding": "non-uuid"}):
            with self.subTest(changed=changed):
                self.write([self.entry | changed])
                with self.assertRaises(ValueError):
                    self.load()

    def test_unsupported_project_capability_is_not_silently_ignored(self) -> None:
        for field in ("auto_execute", "signing_required", "scheduler", "unknown_policy"):
            with self.subTest(field=field):
                self.write([self.entry | {field: True}])
                with self.assertRaisesRegex(ValueError, "unsupported project fields"):
                    self.load()

    def test_invalid_boolean_and_version_are_rejected(self) -> None:
        for changed in ({"enabled": 1}, {"host_execution_enabled": "true"},
                        {"host_mailbox_enabled": 1},
                        {"legacy_workspace": 1}):
            with self.subTest(changed=changed):
                self.write([self.entry | changed])
                with self.assertRaises(ValueError):
                    self.load()
        for version in (True, 2, "1", None):
            self.path.write_text(json.dumps({"version": version, "repositories": []}))
            with self.assertRaises(ValueError):
                self.load()

    def test_duplicate_id_remote_and_binding_are_rejected(self) -> None:
        for new in ({"id": "EXAMPLE-PROJECT", "repository": "Other/second"},
                    {"id": "second", "repository": "example-owner/EXAMPLE-PROJECT"},
                    {"id": "second", "repository": "Other/second"},
                    {"id": "second", "repository": "Other/second",
                     "agent_binding": OTHER_BINDING}):
            entry = {**self.entry, **new}
            if entry["id"] != "example-project":
                for key in ("control_dir", "work_dir", "checkpoints_dir"):
                    entry[key] = str(self.home / entry["id"] / key)
            self.write([self.entry, entry])
            with self.subTest(new=new):
                if entry["repository"] == "Other/second" and entry["agent_binding"] == OTHER_BINDING:
                    self.assertEqual(len(self.load()), 2)
                else:
                    with self.assertRaises(ValueError):
                        self.load()

    def test_path_collisions_between_and_within_projects_are_rejected(self) -> None:
        self.write([{**self.entry, "work_dir": self.entry["control_dir"] + "/child"}])
        with self.assertRaisesRegex(ValueError, "overlapping"):
            self.load()
        second = {**self.entry, "id": "second", "repository": "Other/second",
                  "agent_binding": OTHER_BINDING,
                  "work_dir": self.entry["work_dir"] + "/nested"}
        second["control_dir"] = str(self.home / "second/control")
        second["checkpoints_dir"] = str(self.home / "second/checkpoints")
        self.write([self.entry, second])
        with self.assertRaisesRegex(ValueError, "overlapping"):
            self.load()

    def test_relative_paths_and_symlink_registry_refuse(self) -> None:
        self.write([{**self.entry, "control_dir": "relative/path"}])
        with self.assertRaisesRegex(ValueError, "absolute"):
            self.load()
        self.write([self.entry])
        link = self.home / "linked.json"
        link.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, "regular"):
            load_project_registry(link, home=self.home, expected_host_id=HOST)

    def test_bounded_and_duplicate_key_json_fail_closed(self) -> None:
        self.path.write_text('{"version":1,"version":1,"repositories":[]}')
        with self.assertRaisesRegex(ValueError, "JSON"):
            self.load()
        self.path.write_bytes(b" " * (256 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "oversized"):
            self.load()


if __name__ == "__main__":
    unittest.main()
