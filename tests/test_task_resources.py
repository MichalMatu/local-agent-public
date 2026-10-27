from __future__ import annotations

import unittest

from local_agent.runtime.task_contract import MAX_MEMORY_LIMIT_MB, validate_task


def task_with(resources: object) -> dict[str, object]:
    return {
        "id": "resource-contract",
        "mode": "commands",
        "resources": resources,
        "memory_limit_mb": MAX_MEMORY_LIMIT_MB,
        "commands": ["true"],
    }


class TaskResourceContractTests(unittest.TestCase):
    def test_software_only_resources_are_valid_with_large_memory_limit(self) -> None:
        validate_task(task_with([]))

    def test_named_hardware_resources_are_valid(self) -> None:
        validate_task(task_with(["board:growbox-s3", "serial:s3"]))

    def test_missing_resources_is_rejected(self) -> None:
        task = task_with([])
        del task["resources"]
        with self.assertRaisesRegex(ValueError, "declared explicitly"):
            validate_task(task)

    def test_machine_must_be_alone(self) -> None:
        validate_task(task_with(["machine"]))
        with self.assertRaisesRegex(ValueError, "must be declared alone"):
            validate_task(task_with(["machine", "board:zigbee-c6"]))

    def test_resources_are_canonical_and_unique(self) -> None:
        for resources in (
            ["USB"],
            [" usb"],
            ["bad value"],
            ["board:s3", "board:s3"],
        ):
            with self.subTest(resources=resources), self.assertRaises(ValueError):
                validate_task(task_with(resources))


if __name__ == "__main__":
    unittest.main()
