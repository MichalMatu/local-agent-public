from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from local_agent import entrypoint

REPO_ROOT = Path(__file__).resolve().parents[1]


class EntrypointCliTests(unittest.TestCase):
    def test_packaged_entrypoint_resolves_repository_root(self) -> None:
        self.assertEqual(entrypoint.REPO_ROOT, REPO_ROOT)
        self.assertEqual(
            entrypoint.supervisor_command(type("Args", (), {"registry": None, "max_workers": 2})())[1:3],
            ["-m", "local_agent.supervisor.orchestrator"],
        )

    def test_root_entrypoint_shim_executes_as_cli(self) -> None:
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "agent_entrypoint.py"), "--help"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Guarded Local Agent launcher", result.stdout)
        self.assertIn("--max-workers", result.stdout)


if __name__ == "__main__":
    unittest.main()
