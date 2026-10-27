from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path

from local_agent.host_ops.version import JSON_CONTRACT_VERSION

ROOT = Path(__file__).resolve().parents[1]
HOST_OPS_ROOT = ROOT / "local_agent" / "host_ops"
BROWSER_REQUIREMENTS = ROOT / "requirements-host-ops-browser.txt"
BROWSER_DOC = ROOT / "docs" / "host_ops" / "operations" / "BROWSER.md"


class HostOpsAbsorptionTests(unittest.TestCase):
    def test_absorbed_runtime_has_no_legacy_absolute_imports(self) -> None:
        offenders: list[str] = []
        for path in sorted(HOST_OPS_ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                else:
                    continue
                if any(module == "host_ops" or module.startswith("host_ops.") for module in modules):
                    offenders.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(offenders, [])

    def test_absorbed_browser_dependency_has_canonical_optional_install_contract(self) -> None:
        requirements = [
            line.strip()
            for line in BROWSER_REQUIREMENTS.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(requirements, ["playwright==1.63.0"])

        documentation = BROWSER_DOC.read_text(encoding="utf-8")
        self.assertIn("-r requirements-host-ops-browser.txt", documentation)
        self.assertNotIn("'.[browser]'", documentation)
        self.assertNotIn("'.[dev,browser]'", documentation)

    def test_json_contract_version_is_current(self) -> None:
        self.assertEqual(JSON_CONTRACT_VERSION, 2)

    def test_absorbed_cli_is_executable_from_local_agent_namespace(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "local_agent.host_ops", "--json-contract-version"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "2")


if __name__ == "__main__":
    unittest.main()
