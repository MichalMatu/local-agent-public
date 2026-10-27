from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from local_agent.runtime.task_contract import task_digest
from local_agent.workflow import contract, publishing


FIXTURES = Path(__file__).parent / "fixtures" / "workflows"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class WorkflowPublishingTests(unittest.TestCase):
    def test_materialized_child_uses_exact_node_binding_and_provenance(self) -> None:
        manifest = load_fixture("single_repo.json")
        child = publishing.materialize_child_task(manifest, "build")
        node = manifest["nodes"][0]

        self.assertEqual(child["agent_binding"], node["agent_binding"])
        self.assertEqual(child["resources"], [])
        self.assertEqual(child["commands"], ["true"])
        self.assertEqual(
            child["workflow"],
            {
                "id": manifest["id"],
                "node_id": "build",
                "manifest_digest": contract.manifest_digest(manifest),
            },
        )
        self.assertRegex(child["id"], r"^wf-[0-9a-f]{64}$")

    def test_materialization_is_deterministic(self) -> None:
        manifest = load_fixture("single_repo.json")
        first = publishing.materialize_child_task(manifest, "build")
        second = publishing.materialize_child_task(copy.deepcopy(manifest), "build")
        self.assertEqual(first, second)
        self.assertEqual(task_digest(first), task_digest(second))

    def test_manifest_change_changes_child_identity(self) -> None:
        manifest = load_fixture("single_repo.json")
        changed = copy.deepcopy(manifest)
        changed["nodes"][0]["task"]["commands"] = ["false"]
        first = publishing.materialize_child_task(manifest, "build")
        second = publishing.materialize_child_task(changed, "build")
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(task_digest(first), task_digest(second))

    def test_unknown_node_is_rejected(self) -> None:
        manifest = load_fixture("single_repo.json")
        with self.assertRaisesRegex(ValueError, "unknown workflow node"):
            publishing.materialize_child_task(manifest, "missing")

    def test_non_task_node_is_rejected(self) -> None:
        manifest = load_fixture("parallel_multi_repo.json")
        with self.assertRaisesRegex(ValueError, "is not a task node"):
            publishing.materialize_child_task(manifest, "join")

    def test_task_digest_helper_matches_runtime_contract(self) -> None:
        manifest = load_fixture("single_repo.json")
        child = publishing.materialize_child_task(manifest, "build")
        self.assertEqual(
            publishing.materialized_task_digest(manifest, "build"),
            task_digest(child),
        )


if __name__ == "__main__":
    unittest.main()
