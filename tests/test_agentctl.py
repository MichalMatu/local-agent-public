from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path

import local_agent.cli.diagnostics as agentctl
import local_agent.daemon.service as agentd


class AgentControlDiagnosticsTests(unittest.TestCase):
    def test_validate_task_reports_effective_configured_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.json"
            path.write_text(json.dumps({"id": "defaults", "resources": []}), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                return_code = agentctl.command_validate(Namespace(path=str(path)))
        payload = json.loads(output.getvalue())
        self.assertEqual(return_code, 0)
        self.assertEqual(payload["command_timeout"], agentd.TIMEOUTS.command_default)
        self.assertEqual(payload["idle_timeout"], agentd.TIMEOUTS.idle_default)
        self.assertEqual(payload["task_timeout"], agentd.TIMEOUTS.task_default)

    def test_pid_probe_supports_supervisor_processes_without_name_matching(self) -> None:
        self.assertTrue(agentctl._pid_alive(os.getpid()))
        self.assertFalse(agentctl._pid_alive(None))
        self.assertFalse(agentctl._pid_alive(-1))


if __name__ == "__main__":
    unittest.main()
