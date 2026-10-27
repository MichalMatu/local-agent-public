from __future__ import annotations

import json
import unittest
from pathlib import Path

from local_agent.repository.binding import load_binding_catalog

REPO_ROOT = Path(__file__).resolve().parents[1]
WOLF_UE5_BINDING = "f945c397-8568-45c8-b995-48bbbb4447d6"


class WolfUe5OnboardingTests(unittest.TestCase):
    def test_wolf_ue5_is_execution_enabled_in_canonical_catalog(self) -> None:
        matches = [
            record for record in load_binding_catalog() if record.repository_id == "example-game"
        ]

        self.assertEqual(len(matches), 1)
        record = matches[0]
        self.assertEqual(record.repository, "example-owner/example-game")
        self.assertEqual(record.agent_binding, WOLF_UE5_BINDING)
        self.assertTrue(record.execution_enabled)

    def test_bridge_runtime_example_matches_wolf_ue5_canonical_identity(self) -> None:
        runtime = json.loads(
            (REPO_ROOT / "chat_bridge" / "runtime.example.json").read_text(encoding="utf-8")
        )
        matches = [
            item for item in runtime["agents"] if item.get("repository_id") == "example-game"
        ]

        self.assertEqual(
            matches,
            [
                {
                    "repository_id": "example-game",
                    "repository": "example-owner/example-game",
                    "agent_binding": WOLF_UE5_BINDING,
                    "execution_enabled": True,
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
