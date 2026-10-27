"""Source-local, offline Linux core and Debian adapter import boundaries."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

LINUX_ROOT = Path(__file__).resolve().parents[2]
CORE = LINUX_ROOT / "core"
DEBIAN = LINUX_ROOT / "local-agent"


class LinuxSharedCoreLayoutTests(unittest.TestCase):
    def _probe(self, pythonpath: str, script: str) -> subprocess.CompletedProcess[str]:
        # A different cwd prevents accidental import from repository root.
        with tempfile.TemporaryDirectory() as directory:
            return subprocess.run(
                [sys.executable, "-B", "-c", script],
                cwd=directory,
                env={**os.environ, "PYTHONPATH": pythonpath,
                     "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"},
                capture_output=True, text=True, timeout=15,
            )

    def test_engine_runs_without_debian_adapter(self):
        script = (
            "from pathlib import Path\n"
            "import linux_agent, linux_agent.agent, linux_agent.__main__\n"
            "import linux_agent.runtime.task_contract, linux_agent.foundation.process\n"
            "import linux_agent.git_transport\n"
            "assert Path(linux_agent.__file__).resolve().is_relative_to(Path(%r))\n"
            "assert Path(linux_agent.agent.__file__).resolve().is_relative_to(Path(%r))\n"
            "assert Path(linux_agent.__main__.__file__).resolve().is_relative_to(Path(%r))\n"
        ) % (str(CORE), str(CORE), str(CORE))
        result = self._probe(str(CORE), script)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_debian_adapter_resolves_same_tree_core(self):
        script = (
            "from pathlib import Path\n"
            "import linux_agent, linux_agent.agent, linux_agent.service_plan\n"
            "assert Path(linux_agent.__file__).resolve().is_relative_to(Path(%r))\n"
            "assert Path(linux_agent.agent.__file__).resolve().is_relative_to(Path(%r))\n"
            "assert Path(linux_agent.service_plan.__file__).resolve().is_relative_to(Path(%r))\n"
        ) % (str(DEBIAN), str(CORE), str(DEBIAN))
        result = self._probe(str(DEBIAN), script)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
