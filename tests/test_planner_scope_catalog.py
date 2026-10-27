from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_agent.repository.binding import load_binding_catalog

BINDING = "b5aeb06c-5752-4e7d-9189-150613ccd349"


class PlannerScopeCatalogTests(unittest.TestCase):
    @staticmethod
    def write_catalog(root: Path, agent: dict[str, object]) -> Path:
        path = root / "agent_bindings.json"
        path.write_text(
            json.dumps({"version": 1, "agents": [agent]}),
            encoding="utf-8",
        )
        return path

    def test_missing_scope_defaults_to_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_catalog(
                Path(tmp),
                {
                    "id": "a",
                    "repository": "Owner/A",
                    "agent_binding": BINDING,
                    "execution_enabled": True,
                },
            )
            record = load_binding_catalog(path)[0]
        self.assertEqual(record.planner_scope, "repository")

    def test_unknown_scope_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_catalog(
                Path(tmp),
                {
                    "id": "a",
                    "repository": "Owner/A",
                    "agent_binding": BINDING,
                    "execution_enabled": True,
                    "planner_scope": "global",
                },
            )
            with self.assertRaisesRegex(ValueError, "planner_scope"):
                load_binding_catalog(path)

    def test_multirepo_requires_execution_enabled_operator_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_catalog(
                Path(tmp),
                {
                    "id": "a",
                    "repository": "Owner/A",
                    "agent_binding": BINDING,
                    "execution_enabled": False,
                    "planner_scope": "multirepo",
                },
            )
            with self.assertRaisesRegex(ValueError, "requires execution_enabled=true"):
                load_binding_catalog(path)


if __name__ == "__main__":
    unittest.main()
