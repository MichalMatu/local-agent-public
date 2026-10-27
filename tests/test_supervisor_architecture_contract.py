from __future__ import annotations

import ast
import unittest
from pathlib import Path

from local_agent.supervisor import orchestrator, scheduling


class SupervisorArchitectureContractTests(unittest.TestCase):
    def test_scheduling_policy_has_no_runtime_side_effect_dependencies(self) -> None:
        source = Path(scheduling.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        forbidden_prefixes = (
            "subprocess",
            "local_agent.daemon",
            "local_agent.foundation.process",
            "local_agent.operator",
            "local_agent.repository",
        )
        for imported_name in imported:
            with self.subTest(imported=imported_name):
                self.assertFalse(
                    imported_name.startswith(forbidden_prefixes),
                    f"pure scheduling policy imported side-effect owner {imported_name}",
                )

    def test_control_admission_state_is_owned_by_scheduling_not_orchestrator(self) -> None:
        self.assertEqual(
            scheduling.ControlDeferralState.__module__,
            "local_agent.supervisor.scheduling",
        )
        source = Path(orchestrator.__file__).read_text(encoding="utf-8")
        self.assertNotIn("class ControlDeferralState", source)
        self.assertNotIn("control_defer_count", source)
        self.assertNotIn("control_probe_pause_repository", source)


if __name__ == "__main__":
    unittest.main()
