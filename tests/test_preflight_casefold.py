from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from local_agent.cli import diagnostics

BINDING = "fe99a852-0486-4b3a-b09b-5d295e9355a8"


class PreflightRepositoryIdCasefoldTests(unittest.TestCase):
    def test_catalog_and_registry_repository_ids_match_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = root / "catalog.json"
            registry = root / "registry.json"
            control = root / "control"
            work = root / "work"
            checkpoints = root / "checkpoints"

            catalog.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "agents": [
                            {
                                "id": "demo",
                                "repository": "owner/demo",
                                "agent_binding": BINDING,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            registry.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "repositories": [
                            {
                                "id": "DEMO",
                                "repository": "owner/demo",
                                "agent_binding": BINDING,
                                "control_dir": str(control),
                                "work_dir": str(work),
                                "checkpoints_dir": str(checkpoints),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            binding_file = control / ".agent" / "binding.json"
            binding_file.parent.mkdir(parents=True)
            binding_file.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "repository_id": "DEMO",
                        "repository": "owner/demo",
                        "agent_binding": BINDING,
                    }
                ),
                encoding="utf-8",
            )

            task = {
                "id": "casefold-preflight",
                "agent_binding": BINDING,
                "resources": [],
                "commands": ["true"],
            }
            args = SimpleNamespace(
                repository="demo",
                catalog=catalog,
                registry=registry,
            )

            with (
                mock.patch.object(diagnostics, "is_disabled", return_value=False),
                mock.patch.object(diagnostics, "validate_repository"),
                mock.patch.object(diagnostics.core, "validate_branch"),
            ):
                report = diagnostics.task_preflight(task, args)

            self.assertTrue(report["ready"], report.get("blocked_reason"))
            self.assertEqual(report["repository_id"], "demo")


if __name__ == "__main__":
    unittest.main()
