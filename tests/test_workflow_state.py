from __future__ import annotations

import json
import unittest
from pathlib import Path

from local_agent.workflow import state


FIXTURES = Path(__file__).parent / "fixtures" / "workflows"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class WorkflowStateTests(unittest.TestCase):
    def test_single_repo_root_task_becomes_ready(self) -> None:
        manifest = load_fixture("single_repo.json")
        states = state.initial_node_states(manifest)
        self.assertEqual(states, {"build": "ready"})
        self.assertEqual(state.workflow_state(states), "running")

    def test_root_barrier_auto_succeeds(self) -> None:
        manifest = {
            "schema_version": 1,
            "id": "root-barrier",
            "created_at": "2026-09-19T12:05:00Z",
            "nodes": [
                {
                    "id": "start",
                    "kind": "barrier",
                    "depends_on": [],
                }
            ],
        }
        states = state.initial_node_states(manifest)
        self.assertEqual(states, {"start": "succeeded"})
        self.assertEqual(state.workflow_state(states), "completed")

    def test_parallel_roots_become_ready_and_barrier_waits(self) -> None:
        manifest = load_fixture("parallel_multi_repo.json")
        states = state.initial_node_states(manifest)
        self.assertEqual(states["backend"], "ready")
        self.assertEqual(states["android"], "ready")
        self.assertEqual(states["firmware"], "ready")
        self.assertEqual(states["join"], "blocked_dependency")

    def test_barrier_auto_succeeds_after_all_dependencies_succeed(self) -> None:
        manifest = load_fixture("parallel_multi_repo.json")
        states = state.initial_node_states(manifest)
        for node_id in ("backend", "android", "firmware"):
            states[node_id] = "succeeded"
        advanced = state.advance_dependency_states(manifest, states)
        self.assertEqual(advanced["join"], "succeeded")
        self.assertEqual(state.workflow_state(advanced), "completed")

    def test_failed_dependency_never_unlocks_successor(self) -> None:
        manifest = load_fixture("failed_dependency.json")
        states = state.initial_node_states(manifest)
        states["implement"] = "failed"
        advanced = state.advance_dependency_states(manifest, states)
        self.assertEqual(advanced["verify"], "blocked_dependency")
        self.assertEqual(state.workflow_state(advanced), "failed")

    def test_user_gate_waits_only_after_dependency_success(self) -> None:
        manifest = load_fixture("user_gate.json")
        states = state.initial_node_states(manifest)
        self.assertEqual(states["choice"], "blocked_dependency")
        states["audit"] = "succeeded"
        advanced = state.advance_dependency_states(manifest, states)
        self.assertEqual(advanced["choice"], "waiting_user")
        self.assertEqual(state.workflow_state(advanced), "waiting_user")
        advanced["choice"] = state.transition_node_state("waiting_user", "succeeded")
        self.assertEqual(state.workflow_state(advanced), "completed")

    def test_planner_checkpoint_waits_only_after_dependency_success(self) -> None:
        manifest = load_fixture("planner_checkpoint.json")
        states = state.initial_node_states(manifest)
        states["audit"] = "succeeded"
        advanced = state.advance_dependency_states(manifest, states)
        self.assertEqual(advanced["review"], "waiting_planner")
        self.assertEqual(state.workflow_state(advanced), "waiting_planner")

    def test_task_lifecycle_transitions_are_explicit(self) -> None:
        current = "ready"
        current = state.transition_node_state(current, "dispatched")
        current = state.transition_node_state(current, "running")
        current = state.transition_node_state(current, "succeeded")
        self.assertEqual(current, "succeeded")

    def test_ready_node_accepts_exact_remote_recovery_evidence(self) -> None:
        for recovered in ("dispatched", "running", "succeeded", "failed", "blocked_interrupted"):
            with self.subTest(recovered=recovered):
                self.assertEqual(
                    state.transition_node_state("ready", recovered),
                    recovered,
                )

    def test_idempotent_transition_is_allowed(self) -> None:
        self.assertEqual(
            state.transition_node_state("running", "running"),
            "running",
        )
        self.assertEqual(
            state.transition_node_state("succeeded", "succeeded"),
            "succeeded",
        )

    def test_terminal_state_cannot_regress(self) -> None:
        for terminal in ("succeeded", "failed", "cancelled", "blocked_interrupted"):
            with self.subTest(terminal=terminal):
                with self.assertRaisesRegex(ValueError, "invalid workflow node transition"):
                    state.transition_node_state(terminal, "running")

    def test_interrupted_child_requires_planner(self) -> None:
        states = {"build": "blocked_interrupted", "verify": "blocked_dependency"}
        self.assertEqual(state.workflow_state(states), "waiting_planner")

    def test_cancelled_workflow_classification(self) -> None:
        states = {"a": "succeeded", "b": "cancelled", "c": "cancelled"}
        self.assertEqual(state.workflow_state(states), "cancelled")

    def test_cancelled_parent_with_blocked_dependents_is_terminal_cancelled(self) -> None:
        states = {"a": "cancelled", "b": "blocked_dependency"}
        self.assertEqual(state.workflow_state(states), "cancelled")

    def test_cancelled_branch_waits_for_other_active_child_to_drain(self) -> None:
        states = {"a": "cancelled", "b": "running", "c": "blocked_dependency"}
        self.assertEqual(state.workflow_state(states), "running")

    def test_unknown_state_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported workflow node state"):
            state.workflow_state({"a": "mystery"})


if __name__ == "__main__":
    unittest.main()
