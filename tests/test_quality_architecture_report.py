"""Offline architecture-report correctness tests without live host imports."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.quality.architecture_report import (
    build_report, cyclic_components, import_graph,
)


class ArchitectureReportTests(unittest.TestCase):
    def test_graph_resolves_relative_and_absolute_modules(self) -> None:
        with tempfile.TemporaryDirectory(prefix="la-import-graph-") as tmp:
            root = Path(tmp).resolve()
            paths = {
                "platforms/macos/local-agent/__init__.py": "",
                "platforms/macos/local-agent/core/__init__.py": "",
                "platforms/macos/local-agent/core/base.py": "import os\n",
                "platforms/macos/local-agent/runtime/__init__.py": "",
                "platforms/macos/local-agent/runtime/app.py": (
                    "from ..core import base\n"
                    "from local_agent.runtime import util\n"
                ),
                "platforms/macos/local-agent/runtime/util.py": (
                    "from local_agent.core.base import value\n"
                ),
            }
            for name, contents in paths.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents, encoding="utf-8")
            graph = import_graph(root)
            self.assertIn("local_agent.core.base", graph["local_agent.runtime.app"])
            self.assertIn("local_agent.runtime.util", graph["local_agent.runtime.app"])
            self.assertIn("local_agent.core.base", graph["local_agent.runtime.util"])
            self.assertEqual(build_report(root)["cycle_groups"], 0)

    def test_cycles_are_reported_as_groups_without_code_execution(self) -> None:
        graph = {
            "local_agent.a": {"local_agent.b"},
            "local_agent.b": {"local_agent.a"},
            "local_agent.c": set(),
        }
        self.assertEqual(
            cyclic_components(graph), [["local_agent.a", "local_agent.b"]]
        )

    def test_actual_onboarding_layer_does_not_import_mutating_admin(self) -> None:
        root = Path(__file__).resolve().parents[1]
        graph = import_graph(root)
        self.assertNotIn(
            "local_agent.repository.admin",
            graph["local_agent.repository.onboarding"],
        )
        self.assertFalse(any(
            "local_agent.repository.admin" in component
            and "local_agent.repository.onboarding" in component
            for component in cyclic_components(graph)
        ))

    def test_missing_package_fails_instead_of_empty_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "package not found"):
                import_graph(Path(tmp))


if __name__ == "__main__":
    unittest.main()
