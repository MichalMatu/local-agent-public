from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import local_agent.daemon.service as agentd


class StatusControlMetadataTests(unittest.TestCase):
    def test_status_control_preserves_caller_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous_control = agentd.core.CONTROL
            try:
                agentd.core.CONTROL = Path(tmp)
                request = agentd.core.CONTROL / agentd.REMOTE_CONTROL_REQUEST
                request.parent.mkdir(parents=True, exist_ok=True)
                request.write_text(
                    json.dumps({"id": "status-metadata", "action": "status"}),
                    encoding="utf-8",
                )
                with mock.patch.object(
                    agentd, "control_ack_published", return_value=False
                ), mock.patch.object(
                    agentd, "publish_daemon_status"
                ) as publish_status, mock.patch.object(
                    agentd, "publish_control_ack"
                ):
                    agentd.handle_control_request(
                        status_extra={
                            "execution_model": "parallel_repository_supervisor",
                            "max_parallel_workers": 2,
                        }
                    )
                publish_status.assert_called_once_with(
                    "idle",
                    force_remote=True,
                    execution_model="parallel_repository_supervisor",
                    max_parallel_workers=2,
                )
            finally:
                agentd.core.CONTROL = previous_control


if __name__ == "__main__":
    unittest.main()
