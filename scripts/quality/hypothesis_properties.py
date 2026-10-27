"""Opt-in Hypothesis property/state-machine suite; not imported by baseline tests.

Run only in a disposable QA virtualenv with hypothesis==6.170.0.
No Git network, production task queues, process termination or hardware.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hypothesis import given, settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from local_agent.conversation.state import (
    CHILD_STATES, initial_child_state, transition_child_state,
)
from local_agent.runtime.task_contract import (
    InvalidDedupeMetadata, task_dedupe_identity, task_digest, task_resources_for,
)
from local_agent.supervisor import task_dedupe


_TERMINAL = frozenset({"retired", "cancelled", "abandoned"})
_NEXT = {
    "requested": {"registration_pending", "cancelled", "abandoned"},
    "registration_pending": {"active", "cancelled", "abandoned"},
    "active": {"terminal_pending_evidence", "cancelled", "abandoned"},
    "terminal_pending_evidence": {"terminal_recorded"},
    "terminal_recorded": {"retired"},
    "retired": set(),
    "cancelled": set(),
    "abandoned": set(),
}
_OFFLINE = settings(max_examples=55, derandomize=True, database=None, deadline=None)


def _task(task_id: str, dedupe: str, revision: int, command: str = "true") -> dict:
    return {
        "id": task_id,
        "agent_binding": "3da0947d-9acf-4ecf-adce-a29be7dc5c09",
        "mode": "commands",
        "work_branch": "main",
        "allow_write": False,
        "resources": [],
        "task_timeout": 1800,
        "dedupe_key": dedupe,
        "dedupe_revision": revision,
        "steps": [{"command": command, "timeout": 120}],
    }


class HypothesisQueueProperties(unittest.TestCase):
    @given(
        st.text(alphabet="abcDEF123-_.", min_size=1, max_size=25),
        st.sampled_from(("true", "false", "echo ok")),
    )
    @_OFFLINE
    def test_finished_duplicate_is_suppressed_but_higher_revision_is_new(
        self, key: str, command: str,
    ) -> None:
        first = _task("task-A", "intent:" + key, 1, command)
        second = _task("task-B", "intent:" + key, 1, command)
        with tempfile.TemporaryDirectory(prefix="la-hypothesis-queue-") as tmp:
            state = Path(tmp).resolve()
            queued = task_dedupe.plan_pending(
                state, [(Path("a.json"), first), (Path("b.json"), second)],
                now_epoch=100,
            )
            self.assertEqual(len(queued.candidates), 1)
            self.assertEqual(len(queued.suppressed), 1)
            self.assertEqual(len(queued.invalid), 0)

            task_dedupe.record_completion(state, first, "published", now_epoch=110)
            repeated = task_dedupe.plan_pending(
                state, [(Path("b.json"), second)], now_epoch=120,
            )
            self.assertEqual(len(repeated.candidates), 0)
            self.assertEqual(len(repeated.suppressed), 1)

            newer = dict(second, dedupe_revision=2)
            revised = task_dedupe.plan_pending(
                state, [(Path("b.json"), newer)], now_epoch=120,
            )
            self.assertEqual(len(revised.candidates), 1)
            self.assertEqual(len(revised.suppressed), 0)

    @given(
        st.text(alphabet="abc123-_.", min_size=1, max_size=20),
        st.integers(min_value=2, max_value=20),
    )
    @_OFFLINE
    def test_active_claim_never_admits_changed_revision(
        self, key: str, revision: int,
    ) -> None:
        first = _task("task-A", "intent:" + key, 1)
        later = _task("task-B", "intent:" + key, revision, "false")
        with tempfile.TemporaryDirectory(prefix="la-hypothesis-claim-") as tmp:
            state = Path(tmp).resolve()
            task_dedupe.record_admission(state, first, now_epoch=100)
            claim = task_dedupe._claim_path(state, first["id"])
            claim.parent.mkdir(parents=True, exist_ok=True)
            claim.write_text("{}\n", encoding="utf-8")
            result = task_dedupe.plan_pending(
                state, [(Path("b.json"), later)], now_epoch=110,
            )
            self.assertEqual(result.candidates, ())
            self.assertEqual(result.suppressed, ())
            self.assertEqual(len(result.invalid), 1)
            self.assertEqual(result.invalid[0].reason, "dedupe_intent_conflict")

    @given(st.text(alphabet="abc123-_.", min_size=1, max_size=20))
    @_OFFLINE
    def test_published_result_after_claim_release_recovers_without_reexecution(
        self, key: str,
    ) -> None:
        first = _task("task-A", "recovery:" + key, 1)
        duplicate = _task("task-B", "recovery:" + key, 1)
        with tempfile.TemporaryDirectory(prefix="la-hypothesis-recovery-") as tmp:
            state = Path(tmp).resolve()
            task_dedupe.record_admission(state, first, now_epoch=100)
            # Crash window fixture: local claim disappeared after durable Git
            # result publication, without a completed dedupe receipt.
            runs = state / "runs"
            runs.mkdir()
            (runs / "task-A.json").write_text(
                json.dumps({"event": "result_published",
                            "task_digest": task_digest(first)}),
                encoding="utf-8",
            )
            result = task_dedupe.plan_pending(
                state, [(Path("b.json"), duplicate)], now_epoch=110,
            )
            self.assertEqual(result.candidates, ())
            self.assertEqual(len(result.suppressed), 1)
            self.assertEqual(result.invalid, ())
            receipt = task_dedupe._read_receipt(
                state, task_dedupe.queue_key(first), now_epoch=111,
            )
            self.assertIsNotNone(receipt)
            self.assertEqual(receipt["state"], "completed")
            self.assertTrue(receipt["reconciled_from_published_run"])

    @given(st.one_of(
        st.booleans(), st.none(), st.floats(allow_nan=False),
        st.text(min_size=0, max_size=8), st.integers(max_value=0),
        st.integers(min_value=1_000_001),
    ))
    @_OFFLINE
    def test_invalid_revision_types_and_bounds_fail_closed(self, revision: object) -> None:
        with self.assertRaises(InvalidDedupeMetadata):
            task_dedupe_identity({"dedupe_key": "work", "dedupe_revision": revision})

    @given(st.lists(st.sampled_from(["network", "device:a", "machine"]), max_size=9))
    @_OFFLINE
    def test_resource_limits_and_exclusivity(self, resources: list[str]) -> None:
        invalid = len(resources) > 8 or len(resources) != len(set(resources))
        invalid = invalid or ("machine" in resources and len(resources) != 1)
        if invalid:
            with self.assertRaises(ValueError):
                task_resources_for({"resources": resources})
        else:
            self.assertEqual(task_resources_for({"resources": resources}), tuple(resources))


class ChildLifecycleMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.state = initial_child_state()

    @rule(target_state=st.sampled_from(sorted(CHILD_STATES)))
    def attempt_transition(self, target_state: str) -> None:
        target = target_state
        if target == self.state or target in _NEXT[self.state]:
            self.state = transition_child_state(self.state, target)
        else:
            with unittest.TestCase().assertRaises(ValueError):
                transition_child_state(self.state, target)

    @invariant()
    def state_remains_valid_and_terminal_states_cannot_revive(self) -> None:
        assert self.state in CHILD_STATES
        if self.state in _TERMINAL:
            for next_state in CHILD_STATES - {self.state}:
                try:
                    transition_child_state(self.state, next_state)
                except ValueError:
                    pass
                else:
                    raise AssertionError("terminal lifecycle state unexpectedly revived")


TestChildLifecycle = ChildLifecycleMachine.TestCase
TestChildLifecycle.settings = settings(
    max_examples=40, stateful_step_count=30,
    derandomize=True, database=None, deadline=None,
)


if __name__ == "__main__":
    unittest.main()
