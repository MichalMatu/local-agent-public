from __future__ import annotations

import unittest

from local_agent.runtime import task_contract


class RuntimeTaskContractModuleTests(unittest.TestCase):
    def test_extracted_contract_validates_independently(self) -> None:
        task = {"id": "contract-smoke", "resources": [], "commands": ["true"], "command_timeout": 60, "task_timeout": 180}
        task_contract.validate_task(task)
        self.assertEqual(task_contract.task_timeout_for(task), 180)
        self.assertNotEqual(task_contract.task_digest(task), task_contract.task_digest({**task, "commands": ["false"]}))


if __name__ == "__main__":
    unittest.main()
