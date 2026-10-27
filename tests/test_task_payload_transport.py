from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from local_agent.foundation import core
from local_agent.repository import cleanup
from local_agent.repository.cleanup import control_cleanup_plan
from local_agent.runtime import task_contract, task_transport


class TaskPayloadTransportTests(unittest.TestCase):
    def _task(self) -> dict[str, object]:
        return {
            "id": "escape-heavy",
            "resources": [],
            "allow_write": True,
            "patch": (
                "diff --git a/ui.js b/ui.js\n"
                "--- a/ui.js\n"
                "+++ b/ui.js\n"
                '@@ -1 +1 @@\n-old\n+const x = {"path": "C:\\\\tmp\\\\x"};\n'
            ),
            "writes": [
                {
                    "path": "generated/layout.json",
                    "content": '{\n  "layout": "a\\\\b",\n  "quote": "\\\"ok\\\""\n}\n',
                }
            ],
            "commands": [
                "python - <<'PY'\n"
                'value = {"path": "C:\\\\tmp\\\\x", "quote": "\\\"ok\\\""}\n'
                "print(value)\n"
                "PY"
            ],
            "verify_commands": ["python -c 'print(\"verified\")'"],
            "command_timeout": 60,
            "task_timeout": 180,
        }

    def test_bundle_round_trip_externalizes_escape_heavy_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            control = Path(temporary)
            tasks_dir = control / ".agent/tasks"
            task = self._task()
            expected = copy.deepcopy(task)

            manifest_path = task_transport.write_task_bundle(tasks_dir, task)
            manifest_text = manifest_path.read_text(encoding="utf-8")
            raw = json.loads(manifest_text)

            self.assertNotIn(str(task["commands"][0]), manifest_text)
            self.assertEqual(
                raw["commands"][0],
                {"payload_file": "escape-heavy.payload/commands/001.sh"},
            )
            self.assertEqual(
                raw["writes"][0]["content"],
                {"payload_file": "escape-heavy.payload/writes/001.txt"},
            )
            self.assertEqual(
                raw["patch"],
                {"payload_file": "escape-heavy.payload/patch.diff"},
            )

            with mock.patch.object(core, "CONTROL", control):
                task_contract.validate_task(raw)

            self.assertEqual(raw, expected)
            self.assertEqual(
                task_contract.task_digest(raw),
                task_contract.task_digest(expected),
            )

    def test_writer_preflight_rejects_invalid_task_before_enqueue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tasks_dir = Path(temporary) / ".agent/tasks"
            invalid = {
                "id": "missing-resources",
                "commands": ["true"],
                "command_timeout": 60,
                "task_timeout": 180,
            }

            with self.assertRaisesRegex(ValueError, "resources must be declared"):
                task_transport.write_task_bundle(tasks_dir, invalid)

            self.assertFalse(tasks_dir.exists())

    def test_writer_preserves_manifest_created_during_payload_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tasks_dir = Path(temporary) / ".agent/tasks"
            manifest_path = tasks_dir / "escape-heavy.json"
            payload_root = tasks_dir / "escape-heavy.payload"
            real_atomic_write = task_transport.atomic_write_text
            injected = False

            def racing_atomic_write(path: Path, text: str) -> None:
                nonlocal injected
                real_atomic_write(path, text)
                if not injected and payload_root in path.parents:
                    injected = True
                    manifest_path.write_text("foreign-manifest\n", encoding="utf-8")

            with mock.patch.object(
                task_transport,
                "atomic_write_text",
                side_effect=racing_atomic_write,
            ):
                with self.assertRaisesRegex(
                    FileExistsError,
                    "task manifest already exists",
                ):
                    task_transport.write_task_bundle(tasks_dir, self._task())

            self.assertEqual(
                manifest_path.read_text(encoding="utf-8"),
                "foreign-manifest\n",
            )
            self.assertFalse(payload_root.exists())

    def test_writer_refuses_existing_payload_reservation_without_deleting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tasks_dir = Path(temporary) / ".agent/tasks"
            payload_root = tasks_dir / "escape-heavy.payload"
            payload_root.mkdir(parents=True)
            marker = payload_root / "foreign.txt"
            marker.write_text("keep\n", encoding="utf-8")

            with self.assertRaisesRegex(
                FileExistsError,
                "task payload directory already exists",
            ):
                task_transport.write_task_bundle(tasks_dir, self._task())

            self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")
            self.assertFalse((tasks_dir / "escape-heavy.json").exists())

    def test_payload_reference_cannot_escape_task_payload_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            control = Path(temporary)
            task = {
                "id": "bounded",
                "resources": [],
                "commands": [
                    {"payload_file": "bounded.payload/../outside.sh"},
                ],
                "command_timeout": 60,
                "task_timeout": 180,
            }

            with mock.patch.object(core, "CONTROL", control):
                with self.assertRaisesRegex(ValueError, "canonical relative POSIX"):
                    task_contract.validate_task(task)

    def test_missing_payload_is_terminal_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            control = Path(temporary)
            task = {
                "id": "missing-payload",
                "resources": [],
                "commands": [
                    {"payload_file": "missing-payload.payload/commands/001.sh"},
                ],
                "command_timeout": 60,
                "task_timeout": 180,
            }

            with mock.patch.object(core, "CONTROL", control):
                with self.assertRaisesRegex(ValueError, "payload file does not exist"):
                    task_contract.validate_task(task)

    def test_repeated_payload_reference_cannot_expand_resolved_task_past_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            control = Path(temporary)
            payload = control / ".agent/tasks/amplified.payload/commands/shared.sh"
            payload.parent.mkdir(parents=True)
            payload.write_text("x" * 20_000, encoding="utf-8")
            reference = {"payload_file": "amplified.payload/commands/shared.sh"}
            task = {
                "id": "amplified",
                "resources": [],
                "commands": [dict(reference) for _ in range(256)],
                "command_timeout": 60,
                "task_timeout": 180,
            }

            with mock.patch.object(core, "CONTROL", control):
                with self.assertRaisesRegex(ValueError, "resolved task exceeds"):
                    task_contract.validate_task(task)

    def test_legacy_inline_task_remains_unchanged(self) -> None:
        task = {
            "id": "legacy-inline",
            "resources": [],
            "commands": ["true"],
            "command_timeout": 60,
            "task_timeout": 180,
        }
        expected = copy.deepcopy(task)

        task_contract.validate_task(task)

        self.assertEqual(task, expected)

    def test_runtime_cleanup_prunes_payload_files_with_terminal_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            control = Path(temporary)
            tasks_dir = control / ".agent/tasks"
            results_dir = control / ".agent/results"
            results_dir.mkdir(parents=True)
            task = {
                "id": "gc-bundle",
                "resources": [],
                "commands": ["printf '%s\\n' 'done'"],
                "command_timeout": 60,
                "task_timeout": 180,
            }
            task_transport.write_task_bundle(tasks_dir, task)
            (results_dir / "gc-bundle.json").write_text(
                '{"id":"gc-bundle","finished_at":"2026-09-28T10:00:00+00:00"}\n',
                encoding="utf-8",
            )

            plan = control_cleanup_plan(
                control,
                terminal_pair_retention=0,
                run_retention=0,
                ack_retention=0,
                orphan_result_retention=0,
            )

            self.assertIn(".agent/tasks/gc-bundle.json", plan)
            self.assertIn(".agent/results/gc-bundle.json", plan)
            self.assertIn(
                ".agent/tasks/gc-bundle.payload/commands/001.sh",
                plan,
            )

    def test_runtime_cleanup_unlinks_payload_symlink_without_deleting_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            control = Path(temporary)
            (control / ".git").mkdir()
            tasks_dir = control / ".agent/tasks"
            tasks_dir.mkdir(parents=True)
            target = control / ".agent/binding.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('{"version":1}\n', encoding="utf-8")
            link = tasks_dir / "gc-symlink.payload"
            link.symlink_to(target)

            class FakeCore:
                CONTROL = control
                CONTROL_BRANCH = "agent-control"
                CONTROL_GIT_LOCK = threading.Lock()
                ENV: dict[str, str] = {}

                @staticmethod
                def process(*_args, **_kwargs):
                    return {"exit_code": 0, "output": ""}

            with mock.patch.object(
                cleanup,
                "control_cleanup_plan",
                return_value=(".agent/tasks/gc-symlink.payload",),
            ), mock.patch.object(
                cleanup,
                "_compact_control_history_locked",
                return_value={"changed": False, "reason": "below_threshold"},
            ):
                result = cleanup.prune_control_runtime(FakeCore)

            self.assertTrue(result["changed"])
            self.assertFalse(link.exists())
            self.assertFalse(link.is_symlink())
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_text(encoding="utf-8"), '{"version":1}\n')

    def test_cleanup_ignores_noncanonical_payload_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tasks_dir = Path(temporary) / ".agent/tasks"
            tasks_dir.mkdir(parents=True)
            self.assertEqual(cleanup._task_payload_files(tasks_dir, "../escape"), [])


if __name__ == "__main__":
    unittest.main()
