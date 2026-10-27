from __future__ import annotations

import json
import unittest
from pathlib import Path

from local_agent.repository.binding import load_binding_catalog, resolve_execution_target
from local_agent.runtime.task_preparation import prepare_task

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_AGENT_BINDING = "b0e94f93-478f-4fc6-abed-159d3702362f"


class HostOpsRetirementTests(unittest.TestCase):
    def test_standalone_host_ops_is_not_an_execution_target(self) -> None:
        ids = {record.repository_id for record in load_binding_catalog()}
        self.assertNotIn("host-ops", ids)

    def test_local_agent_owns_absorbed_host_ops_execution(self) -> None:
        matches = [record for record in load_binding_catalog() if record.repository_id == "local-agent"]
        self.assertEqual(len(matches), 1)
        record = matches[0]
        self.assertEqual(record.repository, "example-owner/local-agent")
        self.assertEqual(record.agent_binding, LOCAL_AGENT_BINDING)
        self.assertTrue(record.execution_enabled)

    def test_retired_donor_cannot_resolve_or_prepare_host_maintenance(self) -> None:
        for target in ("host-ops", "example-owner/host-ops"):
            with self.subTest(target=target):
                with self.assertRaisesRegex(ValueError, "unknown or ambiguous"):
                    resolve_execution_target(target)
                with self.assertRaisesRegex(ValueError, "unknown or ambiguous"):
                    prepare_task(
                        {"id": "retired-host-ops", "commands": ["true"]},
                        repository=target,
                        profile="host-maintenance",
                    )

    def test_canonical_host_maintenance_prepares_for_local_agent_only(self) -> None:
        task = prepare_task(
            {
                "id": "canonical-host-maintenance",
                "commands": ["python -m local_agent.host_ops --json-contract-version"],
            },
            repository="local-agent",
            profile="host-maintenance",
        )
        self.assertEqual(task["agent_binding"], LOCAL_AGENT_BINDING)
        self.assertEqual(task["resources"], [])

    def test_bridge_runtime_example_has_no_standalone_host_ops_identity(self) -> None:
        runtime = json.loads(
            (REPO_ROOT / "chat_bridge" / "runtime.example.json").read_text(encoding="utf-8")
        )
        ids = {item["repository_id"] for item in runtime["agents"]}
        self.assertNotIn("host-ops", ids)
        self.assertIn("local-agent", ids)


if __name__ == "__main__":
    unittest.main()
