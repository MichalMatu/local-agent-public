from __future__ import annotations

import os
from pathlib import Path

from local_agent.host_ops.capabilities.remote.ssh import SYSTEM_SSH_EXECUTABLE, build_ssh_command
from local_agent.host_ops.core.config import HostTarget
from local_agent.host_ops.core.execution import ExecutionLimits, ProcessRunner


def test_generated_options_are_accepted_by_pinned_system_openssh() -> None:
    ssh_path = Path(SYSTEM_SSH_EXECUTABLE)
    assert ssh_path.is_file(), f"pinned system OpenSSH is missing: {ssh_path}"
    assert os.access(ssh_path, os.X_OK), f"pinned system OpenSSH is not executable: {ssh_path}"

    target = HostTarget(
        alias="integration-host",
        host="example.test",
        port=8022,
        user="integration_user",
        identity_file="/dev/null",
    )
    command = build_ssh_command(target, ("true",))
    assert command[0] == SYSTEM_SSH_EXECUTABLE

    # -G asks OpenSSH to parse and print its effective configuration without connecting.
    # OpenSSH does not consistently echo the command-line -T flag as a requesttty entry,
    # so -T itself remains covered by the deterministic command-builder unit test.
    query = (SYSTEM_SSH_EXECUTABLE, "-G", *command[1:-1])
    result = ProcessRunner().run(
        query,
        limits=ExecutionLimits(timeout_seconds=5, max_stdout_bytes=256_000),
    )

    assert result.ok, result.stderr or result.error
    rendered = result.stdout.lower()
    assert "batchmode yes" in rendered
    assert "stricthostkeychecking true" in rendered
    assert "preferredauthentications publickey" in rendered
    assert "pubkeyauthentication true" in rendered
    assert "passwordauthentication no" in rendered
    assert "identitiesonly yes" in rendered
    assert "addkeystoagent false" in rendered or "addkeystoagent no" in rendered
    assert "updatehostkeys false" in rendered or "updatehostkeys no" in rendered
    assert "identityfile /dev/null" in rendered
    assert "clearallforwardings yes" in rendered
    assert "forwardagent no" in rendered
    assert "permitlocalcommand no" in rendered
    assert "controlmaster false" in rendered
    assert "controlpersist no" in rendered
    assert "forkafterauthentication no" in rendered
    assert "port 8022" in rendered
    assert "user integration_user" in rendered
