from __future__ import annotations

import json

from local_agent.host_ops.capabilities.remote.ssh import SYSTEM_SCP_EXECUTABLE, SYSTEM_SSH_EXECUTABLE
from local_agent.host_ops.cli.main import main


def test_doctor_json_is_machine_readable(capsys) -> None:
    exit_code = main(["doctor", "--json"])
    captured = capsys.readouterr()

    payload = json.loads(captured.out)
    assert exit_code == 0
    assert isinstance(payload["ok"], bool)
    assert isinstance(payload["checks"], list)
    assert {item["status"] for item in payload["checks"]} <= {"pass", "warn", "fail"}
    check_names = {item["name"] for item in payload["checks"]}
    assert f"command:{SYSTEM_SSH_EXECUTABLE}" in check_names
    assert f"command:{SYSTEM_SCP_EXECUTABLE}" in check_names
    assert "command:adb" in check_names
