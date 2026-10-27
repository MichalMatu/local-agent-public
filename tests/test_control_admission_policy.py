from __future__ import annotations

import unittest

from local_agent.supervisor import scheduling


class ControlAdmissionPolicyTests(unittest.TestCase):
    def test_five_consecutive_lease_busy_retries_without_pause_or_drain(self) -> None:
        state = scheduling.ControlDeferralState(repository_id="control")
        for attempt in range(1, 6):
            retry = scheduling.record_control_deferral(
                state,
                now=float(attempt),
                lease_busy=True,
            )
            self.assertGreater(retry, 0.0)
            self.assertEqual(state.consecutive_lease_busy, attempt)
            self.assertIs(
                scheduling.control_lease_busy_action(
                    state,
                    control_repository_running=True,
                ),
                scheduling.ControlLeaseBusyAction.RETRY,
            )

    def test_sixth_known_worker_lease_busy_pauses_only_control_repository(self) -> None:
        state = scheduling.ControlDeferralState(repository_id="control")
        for attempt in range(1, 7):
            scheduling.record_control_deferral(
                state,
                now=float(attempt),
                lease_busy=True,
            )
        self.assertIs(
            scheduling.control_lease_busy_action(
                state,
                control_repository_running=True,
            ),
            scheduling.ControlLeaseBusyAction.PAUSE_CONTROL_REPOSITORY,
        )

    def test_sixth_unexplained_lease_busy_requires_defensive_global_drain(self) -> None:
        state = scheduling.ControlDeferralState(repository_id="control")
        for attempt in range(1, 7):
            scheduling.record_control_deferral(
                state,
                now=float(attempt),
                lease_busy=True,
            )
        self.assertIs(
            scheduling.control_lease_busy_action(
                state,
                control_repository_running=False,
            ),
            scheduling.ControlLeaseBusyAction.DRAIN_ALL,
        )

    def test_deferred_probe_breaks_lease_busy_streak_without_losing_retry_backoff(self) -> None:
        state = scheduling.ControlDeferralState(repository_id="control")
        for attempt in range(1, 6):
            scheduling.record_control_deferral(
                state,
                now=float(attempt),
                lease_busy=True,
            )
        scheduling.record_control_deferral(state, now=6.0, lease_busy=False)
        scheduling.record_control_deferral(state, now=7.0, lease_busy=True)

        self.assertEqual(state.consecutive_deferrals, 7)
        self.assertEqual(state.consecutive_lease_busy, 1)
        self.assertEqual(
            state.retry_not_before,
            7.0 + scheduling.control_defer_retry_seconds(7),
        )
        self.assertIs(
            scheduling.control_lease_busy_action(
                state,
                control_repository_running=False,
            ),
            scheduling.ControlLeaseBusyAction.RETRY,
        )

    def test_deferred_probe_does_not_clear_existing_control_repository_pause(self) -> None:
        state = scheduling.ControlDeferralState(
            repository_id="control",
            consecutive_deferrals=6,
            consecutive_lease_busy=6,
            paused_repository_id="control",
        )
        scheduling.record_control_deferral(state, now=7.0, lease_busy=False)

        self.assertEqual(state.consecutive_lease_busy, 0)
        self.assertEqual(state.paused_repository_id, "control")
        self.assertGreater(state.retry_not_before, 7.0)

    def test_control_repository_identity_change_clears_stale_pause_and_retry_state(self) -> None:
        state = scheduling.ControlDeferralState(
            repository_id="old-control",
            retry_not_before=123.0,
            consecutive_deferrals=8,
            consecutive_lease_busy=8,
            last_log_at=100.0,
            paused_repository_id="old-control",
        )
        self.assertTrue(scheduling.bind_control_repository(state, "new-control"))
        self.assertEqual(state.repository_id, "new-control")
        self.assertEqual(state.retry_not_before, 0.0)
        self.assertEqual(state.consecutive_deferrals, 0)
        self.assertEqual(state.consecutive_lease_busy, 0)
        self.assertIsNone(state.last_log_at)
        self.assertIsNone(state.paused_repository_id)
        self.assertFalse(scheduling.bind_control_repository(state, "new-control"))

    def test_success_reset_can_clear_pause_and_all_control_deferral_evidence(self) -> None:
        state = scheduling.ControlDeferralState(
            repository_id="control",
            retry_not_before=123.0,
            consecutive_deferrals=8,
            consecutive_lease_busy=6,
            last_log_at=100.0,
            paused_repository_id="control",
        )
        scheduling.reset_control_deferral_state(state, clear_pause=True)
        self.assertEqual(state.repository_id, "control")
        self.assertEqual(state.retry_not_before, 0.0)
        self.assertEqual(state.consecutive_deferrals, 0)
        self.assertEqual(state.consecutive_lease_busy, 0)
        self.assertIsNone(state.last_log_at)
        self.assertIsNone(state.paused_repository_id)


if __name__ == "__main__":
    unittest.main()
