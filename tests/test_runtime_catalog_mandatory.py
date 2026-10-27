from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_agent.repository.binding import validate_repository_control_binding


BINDING = "b5aeb06c-5752-4e7d-9189-150613ccd349"


class RuntimeCatalogMandatoryTests(unittest.TestCase):
    def test_unknown_registry_control_identity_is_rejected_before_control_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = root / "catalog.json"
            catalog.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "agents": [
                            {
                                "id": "example-hub",
                                "repository": "example-owner/example-hub",
                                "agent_binding": BINDING,
                                "execution_enabled": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            control = root / "control"
            (control / ".agent").mkdir(parents=True)
            (control / ".agent" / "binding.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "repository_id": "stale-example-hub",
                        "repository": "example-owner/example-hub",
                        "agent_binding": BINDING,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "no canonical agent binding"):
                validate_repository_control_binding(
                    repository_id="stale-example-hub",
                    repository="example-owner/example-hub",
                    expected_agent_binding=BINDING,
                    control_dir=control,
                    catalog_path=catalog,
                )


if __name__ == "__main__":
    unittest.main()
