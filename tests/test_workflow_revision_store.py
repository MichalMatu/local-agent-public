from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from local_agent.workflow import contract, revisions
from local_agent.workflow.revision_store import WorkflowRevisionStore
from local_agent.workflow.store import WorkflowStore


BINDING = "00000000-0000-4000-8000-0000000000a1"


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


def manifest() -> dict:
    return {
        "schema_version": 1,
        "id": "revision-store",
        "created_at": "2026-09-19T13:30:00Z",
        "nodes": [
            task_node("audit", depends_on=[]),
            {
                "id": "review-audit",
                "kind": "planner_checkpoint",
                "depends_on": ["audit"],
            },
        ],
    }


def revision_one(base: dict) -> dict:
    return {
        "schema_version": 1,
        "workflow_id": base["id"],
        "revision": 1,
        "created_at": "2026-09-19T13:31:00Z",
        "parent_digest": contract.manifest_digest(base),
        "checkpoint_node_id": "review-audit",
        "nodes": [task_node("implement", depends_on=["review-audit"])],
    }


class WorkflowRevisionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name) / "state"
        self.workflow_store = WorkflowStore(self.state_dir)
        self.base = manifest()
        self.workflow_store.submit(self.base)
        self.store = WorkflowRevisionStore(self.workflow_store)

    def test_append_is_create_only_and_survives_restart(self) -> None:
        record = revision_one(self.base)
        saved = self.store.append(self.base["id"], record)
        self.assertEqual(saved, record)

        path = (
            self.state_dir
            / "workflows"
            / self.base["id"]
            / "revisions"
            / "000001.json"
        )
        self.assertTrue(path.is_file())

        restarted = WorkflowRevisionStore(WorkflowStore(self.state_dir))
        self.assertEqual(restarted.load(self.base["id"]), [record])

    def test_identical_append_is_idempotent(self) -> None:
        record = revision_one(self.base)
        first = self.store.append(self.base["id"], record)
        second = self.store.append(self.base["id"], copy.deepcopy(record))
        self.assertEqual(first, second)
        root = self.state_dir / "workflows" / self.base["id"] / "revisions"
        self.assertEqual([path.name for path in root.glob("*.json")], ["000001.json"])

    def test_conflicting_existing_revision_fails_closed(self) -> None:
        record = revision_one(self.base)
        self.store.append(self.base["id"], record)
        conflicting = copy.deepcopy(record)
        conflicting["nodes"][0]["task"]["commands"] = ["false"]
        with self.assertRaisesRegex(ValueError, "different digest"):
            self.store.append(self.base["id"], conflicting)

    def test_skipped_revision_number_is_rejected(self) -> None:
        record = revision_one(self.base)
        record["revision"] = 2
        with self.assertRaisesRegex(ValueError, "expected 1"):
            self.store.append(self.base["id"], record)

    def test_effective_manifest_contains_appended_nodes_without_rewriting_base(self) -> None:
        record = revision_one(self.base)
        original = copy.deepcopy(self.base)
        self.store.append(self.base["id"], record)
        effective = self.store.effective_manifest(self.base["id"])
        self.assertEqual(
            [node["id"] for node in effective["nodes"]],
            ["audit", "review-audit", "implement"],
        )
        self.assertEqual(self.workflow_store.load_manifest(self.base["id"]), original)

    def test_non_contiguous_revision_files_are_rejected_on_read(self) -> None:
        root = self.state_dir / "workflows" / self.base["id"] / "revisions"
        root.mkdir(parents=True)
        record = revision_one(self.base)
        path = root / "000002.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not contiguous"):
            self.store.load(self.base["id"])

    def test_tampered_revision_breaks_lineage_on_restart(self) -> None:
        record = revision_one(self.base)
        self.store.append(self.base["id"], record)
        path = (
            self.state_dir
            / "workflows"
            / self.base["id"]
            / "revisions"
            / "000001.json"
        )
        tampered = copy.deepcopy(record)
        tampered["parent_digest"] = "sha256:" + "0" * 64
        path.write_text(json.dumps(tampered), encoding="utf-8")
        restarted = WorkflowRevisionStore(WorkflowStore(self.state_dir))
        with self.assertRaisesRegex(ValueError, "parent_digest mismatch"):
            restarted.load(self.base["id"])

    def test_revision_digest_is_preserved_through_disk_roundtrip(self) -> None:
        record = revision_one(self.base)
        self.store.append(self.base["id"], record)
        loaded = self.store.load(self.base["id"])[0]
        self.assertEqual(
            revisions.revision_digest(loaded),
            revisions.revision_digest(record),
        )


if __name__ == "__main__":
    unittest.main()
