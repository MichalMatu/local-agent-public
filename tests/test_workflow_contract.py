from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from local_agent.workflow import contract, methods


FIXTURES = Path(__file__).parent / "fixtures" / "workflows"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class WorkflowContractTests(unittest.TestCase):
    def test_valid_phase_zero_fixtures(self) -> None:
        for name in (
            "single_repo.json",
            "parallel_multi_repo.json",
            "failed_dependency.json",
            "user_gate.json",
            "planner_checkpoint.json",
        ):
            with self.subTest(name=name):
                manifest = load_fixture(name)
                contract.validate_workflow_manifest(manifest)
                digest = contract.manifest_digest(manifest)
                self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")

    def test_manifest_digest_is_canonical(self) -> None:
        manifest = load_fixture("single_repo.json")
        reordered = {
            "nodes": manifest["nodes"],
            "created_at": manifest["created_at"],
            "id": manifest["id"],
            "schema_version": manifest["schema_version"],
        }
        self.assertEqual(
            contract.manifest_digest(manifest),
            contract.manifest_digest(reordered),
        )

    def test_duplicate_node_ids_are_rejected(self) -> None:
        manifest = load_fixture("single_repo.json")
        manifest["nodes"].append(copy.deepcopy(manifest["nodes"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate workflow node id"):
            contract.validate_workflow_manifest(manifest)

    def test_unknown_dependency_is_rejected(self) -> None:
        manifest = load_fixture("single_repo.json")
        manifest["nodes"][0]["depends_on"] = ["missing"]
        with self.assertRaisesRegex(ValueError, "unknown dependency"):
            contract.validate_workflow_manifest(manifest)

    def test_cycle_is_rejected(self) -> None:
        manifest = load_fixture("failed_dependency.json")
        manifest["nodes"][0]["depends_on"] = ["verify"]
        with self.assertRaisesRegex(ValueError, "cycle"):
            contract.validate_workflow_manifest(manifest)

    def test_invalid_repository_binding_is_rejected(self) -> None:
        manifest = load_fixture("single_repo.json")
        manifest["nodes"][0]["agent_binding"] = "NOT-A-UUID"
        with self.assertRaisesRegex(ValueError, "canonical UUID"):
            contract.validate_workflow_manifest(manifest)

    def test_excessive_node_count_is_rejected(self) -> None:
        manifest = load_fixture("single_repo.json")
        template = manifest["nodes"][0]
        manifest["nodes"] = [
            {**copy.deepcopy(template), "id": f"node-{index}"}
            for index in range(contract.MAX_WORKFLOW_NODES + 1)
        ]
        with self.assertRaisesRegex(ValueError, "nodes exceeds"):
            contract.validate_workflow_manifest(manifest)

    def test_excessive_dependency_fanout_is_rejected(self) -> None:
        manifest = load_fixture("parallel_multi_repo.json")
        template = copy.deepcopy(manifest["nodes"][0])
        manifest["nodes"] = [
            {**copy.deepcopy(template), "id": f"source-{index}", "depends_on": []}
            for index in range(contract.MAX_NODE_DEPENDENCIES + 1)
        ]
        manifest["nodes"].append(
            {
                "id": "join",
                "kind": "barrier",
                "depends_on": [
                    f"source-{index}"
                    for index in range(contract.MAX_NODE_DEPENDENCIES + 1)
                ],
            }
        )
        with self.assertRaisesRegex(ValueError, "depends_on exceeds"):
            contract.validate_workflow_manifest(manifest)

    def test_unknown_node_kind_is_rejected(self) -> None:
        manifest = load_fixture("single_repo.json")
        manifest["nodes"][0]["kind"] = "plugin"
        with self.assertRaisesRegex(ValueError, "unsupported workflow node kind"):
            contract.validate_workflow_manifest(manifest)

    def test_invalid_phase_is_rejected(self) -> None:
        manifest = load_fixture("single_repo.json")
        manifest["nodes"][0]["phase"] = "Not Canonical"
        with self.assertRaisesRegex(ValueError, "workflow phase"):
            contract.validate_workflow_manifest(manifest)

    def test_task_template_cannot_override_child_identity(self) -> None:
        manifest = load_fixture("single_repo.json")
        manifest["nodes"][0]["task"]["id"] = "planner-chosen-child"
        with self.assertRaisesRegex(ValueError, "must not define task id"):
            contract.validate_workflow_manifest(manifest)
        manifest = load_fixture("single_repo.json")
        manifest["nodes"][0]["task"]["agent_binding"] = manifest["nodes"][0]["agent_binding"]
        with self.assertRaisesRegex(ValueError, "must not define agent_binding"):
            contract.validate_workflow_manifest(manifest)

    def test_task_template_uses_existing_task_contract(self) -> None:
        manifest = load_fixture("single_repo.json")
        manifest["nodes"][0]["task"].pop("resources")
        with self.assertRaisesRegex(ValueError, "resources must be declared explicitly"):
            contract.validate_workflow_manifest(manifest)

    def test_gate_fields_are_kind_specific(self) -> None:
        manifest = load_fixture("user_gate.json")
        manifest["nodes"][1]["choices"] = ["approve", "approve"]
        with self.assertRaisesRegex(ValueError, "choices must be unique"):
            contract.validate_workflow_manifest(manifest)
        manifest = load_fixture("planner_checkpoint.json")
        manifest["nodes"][1]["prompt"] = "not allowed here"
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            contract.validate_workflow_manifest(manifest)

    def test_method_reference_is_exact_and_digest_pinned(self) -> None:
        spec = methods.load_builtin_method("deep-refactor")
        reference = methods.method_reference(spec)
        methods.validate_method_reference(reference)
        methods.require_method_match(reference, spec)

        bad = copy.deepcopy(reference)
        bad["digest"] = "sha256:" + "0" * 64
        methods.validate_method_reference(bad)
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            methods.require_method_match(bad, spec)

    def test_method_spec_rejects_non_boolean_requirements(self) -> None:
        spec = methods.load_builtin_method("deep-refactor")
        spec["requirements"]["final_full_verification"] = "yes"
        with self.assertRaisesRegex(ValueError, "requirement values must be booleans"):
            methods.validate_method_spec(spec)


if __name__ == "__main__":
    unittest.main()
