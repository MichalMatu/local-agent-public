from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from local_agent.workflow import store


FIXTURES = Path(__file__).parent / "fixtures" / "workflows"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class WorkflowStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name)
        self.store = store.WorkflowStore(self.state_dir)

    def test_submit_persists_manifest_state_and_is_idempotent(self) -> None:
        manifest = load_fixture("single_repo.json")
        first = self.store.submit(manifest)
        second = self.store.submit(copy.deepcopy(manifest))

        self.assertEqual(first, second)
        self.assertEqual(first["workflow_id"], manifest["id"])
        self.assertEqual(first["node_states"], {"build": "ready"})
        self.assertEqual(first["workflow_state"], "running")
        root = self.state_dir / "workflows" / manifest["id"]
        self.assertTrue((root / "manifest.json").is_file())
        self.assertTrue((root / "state.json").is_file())
        events = (root / "events.ndjson").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0])["event"], "submitted")

    def test_same_id_with_different_manifest_is_rejected(self) -> None:
        manifest = load_fixture("single_repo.json")
        self.store.submit(manifest)
        changed = copy.deepcopy(manifest)
        changed["nodes"][0]["task"]["commands"] = ["false"]
        with self.assertRaisesRegex(ValueError, "different manifest digest"):
            self.store.submit(changed)

    def test_node_transition_advances_dependency_state(self) -> None:
        manifest = load_fixture("failed_dependency.json")
        self.store.submit(manifest)
        self.store.set_node_state(manifest["id"], "implement", "dispatched")
        self.store.set_node_state(manifest["id"], "implement", "running")
        updated = self.store.set_node_state(manifest["id"], "implement", "succeeded")
        self.assertEqual(updated["node_states"]["implement"], "succeeded")
        self.assertEqual(updated["node_states"]["verify"], "ready")
        self.assertEqual(updated["workflow_state"], "running")

    def test_repeated_state_transition_is_idempotent(self) -> None:
        manifest = load_fixture("single_repo.json")
        self.store.submit(manifest)
        first = self.store.set_node_state(manifest["id"], "build", "dispatched")
        root = self.state_dir / "workflows" / manifest["id"]
        events_before = (root / "events.ndjson").read_text(encoding="utf-8").splitlines()
        second = self.store.set_node_state(manifest["id"], "build", "dispatched")
        events_after = (root / "events.ndjson").read_text(encoding="utf-8").splitlines()
        self.assertEqual(first, second)
        self.assertEqual(events_before, events_after)

    def test_cancel_marks_non_active_nodes_cancelled(self) -> None:
        manifest = load_fixture("failed_dependency.json")
        self.store.submit(manifest)
        cancelled = self.store.cancel(manifest["id"])
        self.assertTrue(cancelled["cancel_requested"])
        self.assertEqual(cancelled["node_states"]["implement"], "cancelled")
        self.assertEqual(cancelled["node_states"]["verify"], "cancelled")
        self.assertEqual(cancelled["workflow_state"], "cancelled")

    def test_cancel_keeps_running_node_active_until_terminal_evidence(self) -> None:
        manifest = load_fixture("failed_dependency.json")
        self.store.submit(manifest)
        self.store.set_node_state(manifest["id"], "implement", "dispatched")
        self.store.set_node_state(manifest["id"], "implement", "running")
        cancelled = self.store.cancel(manifest["id"])
        self.assertTrue(cancelled["cancel_requested"])
        self.assertEqual(cancelled["node_states"]["implement"], "running")
        self.assertEqual(cancelled["node_states"]["verify"], "cancelled")
        self.assertEqual(cancelled["workflow_state"], "running")

    def test_corruption_is_isolated_to_one_workflow(self) -> None:
        first = load_fixture("single_repo.json")
        second = load_fixture("planner_checkpoint.json")
        self.store.submit(first)
        self.store.submit(second)
        bad_state = self.state_dir / "workflows" / first["id"] / "state.json"
        bad_state.write_text("{bad", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "invalid workflow state"):
            self.store.load_state(first["id"])
        self.assertEqual(self.store.load_state(second["id"])["workflow_id"], second["id"])

    def test_workflow_id_cannot_escape_store_root(self) -> None:
        with self.assertRaisesRegex(ValueError, "workflow id"):
            self.store.load_state("../outside")

    def test_list_workflow_ids_is_sorted_and_ignores_non_directories(self) -> None:
        first = load_fixture("single_repo.json")
        second = load_fixture("planner_checkpoint.json")
        self.store.submit(second)
        self.store.submit(first)
        workflows = self.state_dir / "workflows"
        (workflows / "README.txt").write_text("ignore", encoding="utf-8")
        self.assertEqual(
            self.store.workflow_ids(),
            sorted([first["id"], second["id"]]),
        )


if __name__ == "__main__":
    unittest.main()
