from __future__ import annotations

import json
import unittest

from local_agent.runtime import progress


class RuntimeProgressModuleTests(unittest.TestCase):
    def test_progress_marker_parses_independently(self) -> None:
        marker = progress.parse_progress_marker(
            "[AGENT_PROGRESS] "
            + json.dumps(
                {
                    "stage_name": "build",
                    "message": "step 2",
                    "current": 2,
                    "total": 5,
                    "metrics": {"passed": 1},
                }
            )
        )
        self.assertEqual(marker["stage_name"], "build")
        self.assertEqual(marker["message"], "step 2")
        self.assertEqual(marker["metrics"], {"passed": 1})
        self.assertIsNone(progress.parse_progress_marker("[AGENT_PROGRESS] {broken"))

    def test_progress_dispatcher_delivers_and_closes(self) -> None:
        events: list[dict[str, object]] = []
        logs: list[str] = []
        dispatcher = progress.ProgressDispatcher(events.append, logs.append)
        dispatcher.submit({"event": "heartbeat", "current": 1})
        dispatcher.close(timeout=1.0)
        self.assertEqual(events, [{"event": "heartbeat", "current": 1}])
        self.assertEqual(logs, [])


if __name__ == "__main__":
    unittest.main()
