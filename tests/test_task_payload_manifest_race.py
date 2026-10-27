from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.runtime import task_transport


class TaskPayloadManifestRaceTests(unittest.TestCase):
    def test_manifest_created_at_publish_boundary_is_not_replaced(self) -> None:
        task = {
            "id": "manifest-race",
            "resources": [],
            "commands": ["true"],
            "command_timeout": 60,
            "task_timeout": 180,
        }
        with tempfile.TemporaryDirectory() as temporary:
            tasks_dir = Path(temporary) / ".agent/tasks"
            manifest_path = tasks_dir / "manifest-race.json"
            payload_root = tasks_dir / "manifest-race.payload"
            real_link = task_transport.os.link

            def racing_link(source: Path, destination: Path) -> None:
                if Path(destination) == manifest_path:
                    manifest_path.write_text("foreign-manifest\n", encoding="utf-8")
                real_link(source, destination)

            with mock.patch.object(
                task_transport.os,
                "link",
                side_effect=racing_link,
            ):
                with self.assertRaisesRegex(
                    FileExistsError,
                    "task manifest already exists",
                ):
                    task_transport.write_task_bundle(tasks_dir, task)

            self.assertEqual(
                manifest_path.read_text(encoding="utf-8"),
                "foreign-manifest\n",
            )
            self.assertFalse(payload_root.exists())
            self.assertEqual(list(tasks_dir.glob(".manifest-race.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
