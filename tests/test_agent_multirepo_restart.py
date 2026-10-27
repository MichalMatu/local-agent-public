from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import local_agent.supervisor.serial as multi
from local_agent.foundation.process import (
    LEASE_FDS_ENV,
    LEASE_KEYS_DIGEST_ENV,
    RESOURCE_LEASE_FDS_ENV,
)
from local_agent.repository.context import RepositoryContext


def repository(repository_id: str) -> RepositoryContext:
    root = Path("/tmp") / repository_id
    return RepositoryContext(
        repository_id=repository_id,
        repository=f"owner/{repository_id}",
        control=root / "control",
        work=root / "work",
        checkpoints=root / "checkpoints",
    )


class MultiRepositoryRestartTests(unittest.TestCase):
    def test_restart_command_preserves_supervisor_entrypoint_and_options(self) -> None:
        registry = Path("/tmp/custom-repositories.json")
        command = multi.supervisor_restart_command(registry_path=registry, once=True)
        self.assertEqual(
            command,
            [
                sys.executable,
                str(multi.repository_root() / "agent_multirepo.py"),
                "--registry",
                str(registry),
                "--once",
            ],
        )

    def test_restart_supervisor_execs_directly_and_drops_inherited_leases(self) -> None:
        registry = Path("/tmp/custom-repositories.json")
        expected = multi.supervisor_restart_command(registry_path=registry, once=False)
        lease_env = {
            LEASE_FDS_ENV: "10,11",
            LEASE_KEYS_DIGEST_ENV: "digest",
            RESOURCE_LEASE_FDS_ENV: "12,13",
        }
        with mock.patch.dict(os.environ, lease_env, clear=False), mock.patch.dict(
            multi.agentd.core.ENV,
            lease_env,
            clear=False,
        ), mock.patch.object(
            multi.agentd, "publish_daemon_status"
        ) as publish, mock.patch.object(
            multi, "log"
        ), mock.patch.object(
            multi.os, "execv"
        ) as execv:
            multi.restart_supervisor(
                "test_restart",
                registry_path=registry,
                once=False,
            )
            self.assertNotIn(LEASE_FDS_ENV, os.environ)
            self.assertNotIn(LEASE_KEYS_DIGEST_ENV, os.environ)
            self.assertNotIn(RESOURCE_LEASE_FDS_ENV, os.environ)
            self.assertNotIn(LEASE_FDS_ENV, multi.agentd.core.ENV)
            self.assertNotIn(LEASE_KEYS_DIGEST_ENV, multi.agentd.core.ENV)
            self.assertNotIn(RESOURCE_LEASE_FDS_ENV, multi.agentd.core.ENV)
        publish.assert_called_once_with(
            "restarting",
            force_remote=True,
            reason="test_restart",
        )
        execv.assert_called_once_with(expected[0], expected)

    def test_control_and_self_update_restart_hooks_route_to_supervisor(self) -> None:
        target = repository("a")
        registry = Path("/tmp/custom-repositories.json")
        original_restart = multi.agentd.restart_self

        def handle_control(*, status_extra=None) -> None:
            self.assertEqual(status_extra, multi.supervisor_status_fields(target))
            multi.agentd.restart_self("remote_control:restart-1")

        def check_update() -> None:
            multi.agentd.restart_self("self_update")

        with mock.patch.object(
            multi.agentd,
            "handle_control_request",
            side_effect=handle_control,
        ), mock.patch.object(
            multi.agentd,
            "maybe_self_update",
            side_effect=check_update,
        ), mock.patch.object(
            multi,
            "restart_supervisor",
        ) as restart:
            multi.service_supervisor_control(
                target,
                registry_path=registry,
                once=True,
                sync=False,
            )

        self.assertEqual(
            restart.call_args_list,
            [
                mock.call(
                    "remote_control:restart-1",
                    registry_path=registry,
                    once=True,
                ),
                mock.call("self_update", registry_path=registry, once=True),
            ],
        )
        self.assertIs(multi.agentd.restart_self, original_restart)


if __name__ == "__main__":
    unittest.main()
