from __future__ import annotations

import json
import unittest
from pathlib import Path

from local_agent.repository.binding import load_binding_catalog

REPO_ROOT = Path(__file__).resolve().parents[1]
HARDWARE_LAB_BINDING = "8953ae71-168d-4d62-8949-e15d34447f3b"


class HardwareLabOnboardingTests(unittest.TestCase):
    def test_hardware_lab_is_execution_enabled_in_canonical_catalog(self) -> None:
        matches = [
            record for record in load_binding_catalog() if record.repository_id == "example-hardware"
        ]

        self.assertEqual(len(matches), 1)
        record = matches[0]
        self.assertEqual(record.repository, "example-owner/example-hardware")
        self.assertEqual(record.agent_binding, HARDWARE_LAB_BINDING)
        self.assertTrue(record.execution_enabled)

    def test_bridge_runtime_example_matches_hardware_lab_canonical_identity(self) -> None:
        runtime = json.loads(
            (REPO_ROOT / "chat_bridge" / "runtime.example.json").read_text(encoding="utf-8")
        )
        matches = [
            item for item in runtime["agents"] if item.get("repository_id") == "example-hardware"
        ]

        self.assertEqual(
            matches,
            [
                {
                    "repository_id": "example-hardware",
                    "repository": "example-owner/example-hardware",
                    "agent_binding": HARDWARE_LAB_BINDING,
                    "execution_enabled": True,
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
