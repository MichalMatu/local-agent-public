from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import local_agent.daemon.service as agentd
from local_agent.runtime.task_contract import (
    InvalidDedupeMetadata,
    task_dedupe_identity,
    validate_task,
)


class LocalCodexEvalPolicyTests(unittest.TestCase):
    @staticmethod
    def task(command: str) -> dict[str, object]:
        return {
            "id": "eval-policy",
            "resources": [],
            "commands": [command],
        }

    def test_rejects_eval_mediated_codex_execution(self) -> None:
        for command in (
            "eval 'codex exec x'",
            "eval codex exec x",
            "command eval 'codex exec x'",
            "bash -lc \"eval 'codex exec x'\"",
            "cmd='codex exec x'; eval \"$cmd\"",
            "eval 'npx @openai/codex exec x'",
        ):
            with self.subTest(command=command), self.assertRaisesRegex(
                ValueError, "may not invoke local Codex"
            ):
                validate_task(self.task(command))

    def test_preserves_codex_and_eval_as_nonexecuted_data(self) -> None:
        for command in (
            "echo codex",
            "printf '%s' 'codex exec x'",
            "echo 'eval codex exec x'",
        ):
            with self.subTest(command=command):
                validate_task(self.task(command))


class DedupeClassificationTests(unittest.TestCase):
    def test_malformed_dedupe_metadata_uses_dedicated_value_error(self) -> None:
        invalid = (
            {"dedupe_revision": 1},
            {"dedupe_key": "bad key"},
            {"dedupe_key": "ok", "dedupe_revision": True},
            {"dedupe_key": "ok", "dedupe_revision": 0},
        )
        for task in invalid:
            with self.subTest(task=task), self.assertRaises(InvalidDedupeMetadata) as raised:
                task_dedupe_identity(task)
            self.assertIsInstance(raised.exception, ValueError)

    def test_ingestion_preserves_invalid_dedupe_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original_control = agentd.core.CONTROL
            agentd.core.CONTROL = root / "control"
            tasks = agentd.core.CONTROL / ".agent" / "tasks"
            results = agentd.core.CONTROL / ".agent" / "results"
            tasks.mkdir(parents=True)
            results.mkdir(parents=True)
            path = tasks / "bad-dedupe.json"
            path.write_text(
                json.dumps(
                    {
                        "id": "bad-dedupe",
                        "resources": [],
                        "commands": ["true"],
                        "dedupe_key": "bad key",
                    }
                ),
                encoding="utf-8",
            )
            try:
                with mock.patch.object(agentd.core, "publish_result") as publish_result, mock.patch.object(
                    agentd, "publish_run_state"
                ) as publish_run_state:
                    agentd.recover_invalid_task_files()

                publish_result.assert_called_once()
                result = publish_result.call_args.args[1]
                self.assertEqual(result["failure_reason"], "invalid_dedupe_key")
                self.assertIn("InvalidDedupeMetadata", result["error"])
                publish_run_state.assert_called_once()
                self.assertEqual(
                    publish_run_state.call_args.args[1]["failure_reason"],
                    "invalid_dedupe_key",
                )
                self.assertEqual(agentd.pending_tasks(), [])
            finally:
                agentd.core.CONTROL = original_control

    def test_non_dedupe_validation_error_stays_invalid_task_file(self) -> None:
        result = agentd.invalid_task_result("broken", ValueError("broken JSON"))
        self.assertEqual(result["failure_reason"], "invalid_task_file")


if __name__ == "__main__":
    unittest.main()
