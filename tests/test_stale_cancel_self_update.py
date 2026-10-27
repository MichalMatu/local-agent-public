from __future__ import annotations

import contextlib
import unittest
from unittest import mock

import local_agent.supervisor.orchestrator as parallel
from local_agent.repository.context import RepositoryContext


def repository(repository_id: str) -> RepositoryContext:
    from pathlib import Path

    root = Path("/tmp") / repository_id
    return RepositoryContext(
        repository_id=repository_id,
        repository=f"owner/{repository_id}",
        control=root / "control",
        work=root / "work",
        checkpoints=root / "checkpoints",
    )


class StaleCancelSelfUpdateTests(unittest.TestCase):
    def test_cancel_task_does_not_block_self_update_control_service(self) -> None:
        target = repository("control")
        request = {
            "id": "old-cancel",
            "action": "cancel_task",
            "task_id": "already-finished-task",
        }

        # Force legacy repository control independent of the test host enrollment.
        with mock.patch.object(
            parallel.host_supervisor_control,
            "selected_host_control_source",
            return_value=None,
        ), \
             mock.patch.object(parallel, "publish_local_supervisor_status"), \
             mock.patch.object(
                 parallel,
                 "supervisor_control_leases",
                 return_value=contextlib.nullcontext(),
             ), \
             mock.patch.object(parallel.supervisor_control, "bind_supervisor_control"), \
             mock.patch.object(parallel.supervisor_control, "sync_control_quietly"), \
             mock.patch.object(parallel, "handle_bound_disable_control", return_value=False), \
             mock.patch.object(parallel.agentd, "publish_daemon_status"), \
             mock.patch.object(
                 parallel,
                 "route_parallel_restarts",
                 return_value=contextlib.nullcontext(),
             ), \
             mock.patch.object(
                 parallel,
                 "bound_control_request_from_checkout",
                 return_value=request,
             ), \
             mock.patch.object(parallel.agentd, "handle_control_request") as handle_control, \
             mock.patch.object(parallel.agentd, "maybe_self_update") as self_update, \
             mock.patch.object(parallel.agent_operator, "is_disabled", return_value=False):
            self.assertTrue(
                parallel.service_control(
                    [target],
                    registry_path=None,
                    max_workers=4,
                    once=False,
                )
            )

        handle_control.assert_not_called()
        self_update.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
