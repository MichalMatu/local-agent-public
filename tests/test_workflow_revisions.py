from __future__ import annotations

import copy
import unittest

from local_agent.workflow import contract, revisions


BINDING = "00000000-0000-4000-8000-000000000081"


def task_node(node_id: str, *, depends_on: list[str]) -> dict:
    return {
        "id": node_id,
        "kind": "task",
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


def base_manifest() -> dict:
    return {
        "schema_version": 1,
        "id": "adaptive-refactor",
        "created_at": "2026-09-19T13:00:00Z",
        "nodes": [
            task_node("audit", depends_on=[]),
            {
                "id": "review-audit",
                "kind": "planner_checkpoint",
                "depends_on": ["audit"],
            },
        ],
    }


def first_revision(base: dict) -> dict:
    return {
        "schema_version": 1,
        "workflow_id": base["id"],
        "revision": 1,
        "created_at": "2026-09-19T13:05:00Z",
        "parent_digest": contract.manifest_digest(base),
        "checkpoint_node_id": "review-audit",
        "nodes": [
            task_node("implement", depends_on=["review-audit"]),
            {
                "id": "review-implementation",
                "kind": "planner_checkpoint",
                "depends_on": ["implement"],
            },
        ],
    }


def second_revision(base: dict, first: dict) -> dict:
    return {
        "schema_version": 1,
        "workflow_id": base["id"],
        "revision": 2,
        "created_at": "2026-09-19T13:10:00Z",
        "parent_digest": revisions.revision_digest(first),
        "checkpoint_node_id": "review-implementation",
        "nodes": [task_node("verify", depends_on=["review-implementation"])],
    }


class WorkflowRevisionTests(unittest.TestCase):
    def test_revision_chain_is_digest_linked_and_builds_effective_manifest(self) -> None:
        base = base_manifest()
        first = first_revision(base)
        second = second_revision(base, first)

        revisions.validate_revision_sequence(base, [first, second])
        effective = revisions.effective_manifest(base, [first, second])

        self.assertEqual(
            [node["id"] for node in effective["nodes"]],
            ["audit", "review-audit", "implement", "review-implementation", "verify"],
        )
        self.assertEqual(
            revisions.lineage_tip_digest(base, [first, second]),
            revisions.revision_digest(second),
        )

    def test_first_revision_parent_must_pin_exact_base_manifest_digest(self) -> None:
        base = base_manifest()
        first = first_revision(base)
        first["parent_digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "parent_digest mismatch"):
            revisions.validate_revision_sequence(base, [first])

    def test_revision_numbers_must_be_contiguous(self) -> None:
        base = base_manifest()
        first = first_revision(base)
        first["revision"] = 2
        with self.assertRaisesRegex(ValueError, "expected 1"):
            revisions.validate_revision_sequence(base, [first])

    def test_second_revision_must_pin_previous_revision_digest(self) -> None:
        base = base_manifest()
        first = first_revision(base)
        second = second_revision(base, first)
        second["parent_digest"] = contract.manifest_digest(base)
        with self.assertRaisesRegex(ValueError, "parent_digest mismatch"):
            revisions.validate_revision_sequence(base, [first, second])

    def test_revision_cannot_replace_existing_node(self) -> None:
        base = base_manifest()
        first = first_revision(base)
        first["nodes"][0]["id"] = "audit"
        with self.assertRaisesRegex(ValueError, "duplicate workflow node id"):
            revisions.validate_revision_sequence(base, [first])

    def test_revision_can_depend_on_historical_node_when_checkpoint_is_ancestor(self) -> None:
        base = base_manifest()
        first = first_revision(base)
        first["nodes"][0]["depends_on"] = ["audit", "review-audit"]
        revisions.validate_revision_sequence(base, [first])

    def test_revision_cannot_add_path_that_bypasses_checkpoint(self) -> None:
        base = base_manifest()
        first = first_revision(base)
        first["nodes"].append(task_node("side-path", depends_on=["audit"]))
        with self.assertRaisesRegex(ValueError, "must descend from checkpoint"):
            revisions.validate_revision_sequence(base, [first])

    def test_revision_unknown_dependency_is_rejected_by_real_graph_contract(self) -> None:
        base = base_manifest()
        first = first_revision(base)
        first["nodes"][0]["depends_on"] = ["missing"]
        with self.assertRaisesRegex(ValueError, "unknown dependency"):
            revisions.validate_revision_sequence(base, [first])

    def test_revision_checkpoint_must_exist_in_prior_graph(self) -> None:
        base = base_manifest()
        first = first_revision(base)
        first["checkpoint_node_id"] = "new-checkpoint"
        first["nodes"].insert(
            0,
            {
                "id": "new-checkpoint",
                "kind": "planner_checkpoint",
                "depends_on": ["review-audit"],
            },
        )
        with self.assertRaisesRegex(ValueError, "does not exist in prior graph"):
            revisions.validate_revision_sequence(base, [first])

    def test_revision_checkpoint_must_be_planner_checkpoint(self) -> None:
        base = base_manifest()
        first = first_revision(base)
        first["checkpoint_node_id"] = "audit"
        with self.assertRaisesRegex(ValueError, "is not a planner_checkpoint"):
            revisions.validate_revision_sequence(base, [first])

    def test_checkpoint_identifier_uses_same_ascii_shape_as_workflow_nodes(self) -> None:
        base = base_manifest()
        first = first_revision(base)
        first["checkpoint_node_id"] = "réview-audit"
        with self.assertRaisesRegex(ValueError, "canonical node id"):
            revisions.validate_revision_sequence(base, [first])

    def test_lineage_digest_changes_if_revision_payload_changes(self) -> None:
        base = base_manifest()
        first = first_revision(base)
        changed = copy.deepcopy(first)
        changed["nodes"][0]["task"]["commands"] = ["printf changed"]
        self.assertNotEqual(
            revisions.revision_digest(first),
            revisions.revision_digest(changed),
        )

    def test_effective_manifest_does_not_mutate_base_or_revision_inputs(self) -> None:
        base = base_manifest()
        first = first_revision(base)
        original_base = copy.deepcopy(base)
        original_first = copy.deepcopy(first)

        effective = revisions.effective_manifest(base, [first])
        effective["nodes"][0]["id"] = "mutated-copy"

        self.assertEqual(base, original_base)
        self.assertEqual(first, original_first)


if __name__ == "__main__":
    unittest.main()
