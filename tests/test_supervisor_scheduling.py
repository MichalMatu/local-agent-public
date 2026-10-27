from __future__ import annotations

import os
import unittest
from unittest import mock

import local_agent.supervisor.orchestrator as agent_parallel
from local_agent.supervisor import scheduling


class SupervisorSchedulingTests(unittest.TestCase):
    def test_max_workers_defaults_and_bounds(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(scheduling.resolve_max_workers(None), 1)
        with mock.patch.dict(
            os.environ,
            {scheduling.MAX_WORKERS_ENV: "2"},
            clear=False,
        ):
            self.assertEqual(scheduling.resolve_max_workers(None), 2)
            self.assertEqual(scheduling.resolve_max_workers(3), 3)
        for value in (0, scheduling.MAX_MAX_WORKERS + 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                scheduling.resolve_max_workers(value)
        with mock.patch.dict(
            os.environ,
            {scheduling.MAX_WORKERS_ENV: "broken"},
            clear=False,
        ), self.assertRaises(ValueError):
            scheduling.resolve_max_workers(None)

    def test_backoff_series_are_bounded(self) -> None:
        self.assertEqual(scheduling.bounded_retry_seconds(0, base=2, maximum=10), 0.0)
        self.assertEqual(
            [scheduling.resource_retry_seconds(index) for index in range(1, 8)],
            [2.0, 5.0, 10.0, 30.0, 60.0, 60.0, 60.0],
        )
        self.assertEqual(
            [scheduling.worker_failure_retry_seconds(index) for index in range(1, 10)],
            [2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 256.0, 300.0],
        )
        self.assertEqual(
            [scheduling.control_defer_retry_seconds(index) for index in range(1, 6)],
            [2.0, 4.0, 8.0, 15.0, 15.0],
        )

    def test_failure_and_resource_state_reset_independently(self) -> None:
        schedule = scheduling.RepositorySchedule(
            consecutive_failures=4,
            last_failure_code=7,
            last_failure_log_at=100.0,
            resource_deferrals=3,
        )
        scheduling.reset_worker_failure_state(schedule)
        self.assertEqual(schedule.consecutive_failures, 0)
        self.assertIsNone(schedule.last_failure_code)
        self.assertIsNone(schedule.last_failure_log_at)
        self.assertEqual(schedule.resource_deferrals, 3)
        scheduling.reset_resource_deferral_state(schedule)
        self.assertEqual(schedule.resource_deferrals, 0)

    def test_control_and_log_deadlines_are_boundary_exact(self) -> None:
        self.assertFalse(scheduling.control_lease_busy_should_force_drain(5))
        self.assertTrue(scheduling.control_lease_busy_should_force_drain(6))
        self.assertTrue(scheduling.repeated_failure_log_due(None, 100.0))
        self.assertFalse(scheduling.repeated_failure_log_due(100.0, 159.9))
        self.assertTrue(scheduling.repeated_failure_log_due(100.0, 160.0))
        self.assertTrue(scheduling.operator_idle_log_due(None, 100.0))
        self.assertFalse(scheduling.operator_idle_log_due(100.0, 399.9))
        self.assertTrue(scheduling.operator_idle_log_due(100.0, 400.0))
        self.assertTrue(scheduling.local_log_maintenance_due(None, 100.0))
        self.assertFalse(scheduling.local_log_maintenance_due(100.0, 129.9))
        self.assertTrue(scheduling.local_log_maintenance_due(100.0, 130.0))

    def test_idle_summary_handles_singular_and_plural(self) -> None:
        self.assertEqual(
            scheduling.format_operator_idle_summary(1, 2),
            "IDLE no active task (1 repository); max_workers=2",
        )
        self.assertEqual(
            scheduling.format_operator_idle_summary(3, 2),
            "IDLE no active task (3 repositories); max_workers=2",
        )

    def test_repository_due_uses_retry_deadline_before_adaptive_polling(self) -> None:
        retry = scheduling.RepositorySchedule(last_poll_at=100.0, retry_not_before=102.0)
        self.assertFalse(scheduling.repository_due(retry, 101.9))
        self.assertTrue(scheduling.repository_due(retry, 102.0))

        active = scheduling.RepositorySchedule(last_poll_at=100.0, last_activity_at=100.0)
        self.assertFalse(scheduling.repository_due(active, 101.9))
        self.assertTrue(scheduling.repository_due(active, 102.0))

    def test_next_delay_uses_ids_and_does_not_depend_on_repository_objects(self) -> None:
        schedules = {
            "a": scheduling.RepositorySchedule(
                last_poll_at=100.0,
                retry_not_before=102.0,
            )
        }
        self.assertEqual(
            scheduling.next_repository_delay(schedules, ["a"], 100.0),
            2.0,
        )
        self.assertGreater(
            scheduling.next_repository_delay({}, [], 100.0),
            0.0,
        )

    def test_production_uses_shared_scheduling_owner(self) -> None:
        self.assertIs(agent_parallel.scheduling, scheduling)


if __name__ == "__main__":
    unittest.main()
