from __future__ import annotations

import copy
import unittest

from local_agent.workflow import contract, methods


BINDING = "00000000-0000-4000-8000-000000000051"


def task_node(node_id: str, phase: str, depends_on: list[str]) -> dict:
    return {
        "id": node_id,
        "kind": "task",
        "phase": phase,
        "repository_id": "repo-a",
        "agent_binding": BINDING,
        "depends_on": depends_on,
        "task": {
            "work_branch": "main",
            "allow_write": False,
            "resources": [],
            "commands": ["true"],
        },
    }


def deep_refactor_manifest() -> dict:
    audit = task_node("audit", "audit", [])
    checkpoint = {
        "id": "audit-review",
        "kind": "planner_checkpoint",
        "phase": "audit_checkpoint",
        "depends_on": ["audit"],
    }
    implementation = task_node(
        "implementation",
        "implementation",
        ["audit-review"],
    )
    focused = task_node(
        "focused",
        "focused_verification",
        ["implementation"],
    )
    full = task_node(
        "full",
        "full_verification",
        ["focused"],
    )
    full["task"].pop("commands")
    full["task"]["workflow_policy"] = "efficient-verification-v1"
    full["task"]["steps"] = [
        {
            "name": "prepare",
            "command": "true",
            "verification_level": "work",
        }
    ]
    full["task"]["verify_steps"] = [
        {
            "name": "full",
            "command": "true",
            "verification_level": "full",
        }
    ]
    review = task_node("review", "final_review", ["full"])
    spec = methods.load_builtin_method("deep-refactor")
    return {
        "schema_version": 1,
        "id": "deep-refactor-example",
        "created_at": "2026-09-19T12:06:00Z",
        "method": methods.method_reference(spec),
        "nodes": [audit, checkpoint, implementation, focused, full, review],
    }


def historical_cross_repo_v1_manifest() -> dict:
    spec = methods.load_builtin_method("cross-repo-api-change", version=1)
    contract_node = task_node("contract", "contract", [])
    implementation = task_node("implementation", "implementation", ["contract"])
    integration = task_node("integration", "integration", ["implementation"])
    review = task_node("review", "final_review", ["integration"])
    return {
        "schema_version": 1,
        "id": "historical-cross-repo-v1",
        "created_at": "2026-09-19T12:07:00Z",
        "method": methods.method_reference(spec),
        "nodes": [contract_node, implementation, integration, review],
    }


class WorkflowMethodTests(unittest.TestCase):
    def test_builtin_catalog_lists_latest_version_per_method(self) -> None:
        specs = methods.list_builtin_methods()
        self.assertEqual(
            [(spec["name"], spec["version"]) for spec in specs],
            [
                ("cross-repo-api-change", 2),
                ("deep-refactor", 1),
                ("release-candidate", 1),
            ],
        )
        for spec in specs:
            methods.validate_method_spec(spec)
            self.assertRegex(methods.method_digest(spec), r"^sha256:[0-9a-f]{64}$")

    def test_builtin_version_archive_keeps_historical_method_definitions(self) -> None:
        specs = methods.list_builtin_method_versions()
        self.assertEqual(
            [(spec["name"], spec["version"]) for spec in specs],
            [
                ("cross-repo-api-change", 1),
                ("cross-repo-api-change", 2),
                ("deep-refactor", 1),
                ("release-candidate", 1),
            ],
        )
        legacy = methods.load_builtin_method("cross-repo-api-change", version=1)
        latest = methods.load_builtin_method("cross-repo-api-change")
        self.assertEqual(legacy["version"], 1)
        self.assertEqual(legacy["requirements"], {})
        self.assertEqual(latest["version"], 2)
        self.assertNotEqual(methods.method_digest(legacy), methods.method_digest(latest))

    def test_historical_v1_manifest_remains_admissible_after_v2_is_latest(self) -> None:
        manifest = historical_cross_repo_v1_manifest()
        contract.validate_workflow_manifest(manifest)
        self.assertEqual(manifest["method"]["version"], 1)
        self.assertEqual(methods.load_builtin_method("cross-repo-api-change")["version"], 2)

    def test_builtin_reference_is_digest_pinned(self) -> None:
        spec = methods.load_builtin_method("deep-refactor")
        reference = methods.method_reference(spec)
        methods.require_method_match(reference, spec)
        changed = copy.deepcopy(spec)
        changed["description"] += " changed"
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            methods.require_method_match(reference, changed)

    def test_unknown_method_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown built-in workflow method"):
            methods.load_builtin_method("missing")
        with self.assertRaisesRegex(ValueError, "version=99"):
            methods.load_builtin_method("deep-refactor", version=99)

    def test_unknown_requirement_is_rejected(self) -> None:
        spec = methods.load_builtin_method("deep-refactor")
        spec["requirements"]["magic"] = True
        with self.assertRaisesRegex(ValueError, "unsupported method requirement"):
            methods.validate_method_spec(spec)

    def test_deep_refactor_manifest_satisfies_method(self) -> None:
        manifest = deep_refactor_manifest()
        contract.validate_workflow_manifest(manifest)
        spec = methods.load_builtin_method("deep-refactor")
        methods.validate_workflow_method(manifest, spec)

    def test_missing_required_phase_is_rejected_at_manifest_admission(self) -> None:
        manifest = deep_refactor_manifest()
        manifest["nodes"] = [
            node for node in manifest["nodes"] if node.get("phase") != "final_review"
        ]
        with self.assertRaisesRegex(ValueError, "missing required method phases"):
            contract.validate_workflow_manifest(manifest)

    def test_checkpoint_requirement_is_structural(self) -> None:
        manifest = deep_refactor_manifest()
        checkpoint = next(
            node for node in manifest["nodes"] if node["kind"] == "planner_checkpoint"
        )
        checkpoint["depends_on"] = ["focused"]
        with self.assertRaisesRegex(ValueError, "cycle"):
            contract.validate_workflow_manifest(manifest)

        manifest = deep_refactor_manifest()
        manifest["nodes"] = [
            node for node in manifest["nodes"] if node["kind"] != "planner_checkpoint"
        ]
        implementation = next(
            node for node in manifest["nodes"] if node["id"] == "implementation"
        )
        implementation["depends_on"] = ["audit"]
        with self.assertRaisesRegex(ValueError, "planner checkpoint after audit"):
            contract.validate_workflow_manifest(manifest)

    def test_full_verification_requirement_uses_existing_policy(self) -> None:
        manifest = deep_refactor_manifest()
        full = next(
            node for node in manifest["nodes"] if node.get("phase") == "full_verification"
        )
        full["task"] = {
            "work_branch": "main",
            "allow_write": False,
            "resources": [],
            "commands": ["true"],
        }
        with self.assertRaisesRegex(ValueError, "efficient-verification-v1"):
            contract.validate_workflow_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
