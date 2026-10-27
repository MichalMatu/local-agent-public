from __future__ import annotations

import unittest
from unittest.mock import patch

from local_agent.host_ops.core.execution import ExecutionLimits
from local_agent.tool_runtime import contract


class ToolRuntimeContractTests(unittest.TestCase):
    def _invocation(
        self,
        *,
        arguments: dict[str, object] | None = None,
        schema_version: int = 1,
    ) -> contract.ToolInvocation:
        return contract.ToolInvocation(
            schema_version=schema_version,
            tool=contract.ToolDescriptor(
                tool_id="host_ops.example.read",
                effect=contract.SemanticEffect.PASSIVE_READ,
                authority=contract.AuthorityCeiling.NONE,
            ),
            arguments=arguments or {"alpha": 1, "beta": "two"},
            target=contract.OperationTarget(
                kind="example_target",
                name="example",
                locator=contract.TransportLocator(
                    kind="filesystem_path",
                    attributes={"path": "/tmp/example"},
                ),
            ),
            scheduler_resources=(),
            execution_limits=ExecutionLimits(timeout_seconds=5.0),
        )

    def test_frozen_effect_and_authority_values_are_exact(self) -> None:
        self.assertEqual(
            [item.value for item in contract.SemanticEffect],
            ["PASSIVE_READ", "ACTIVE_READ", "MUTATION", "DISRUPTIVE"],
        )
        self.assertEqual(
            [item.value for item in contract.AuthorityCeiling],
            [
                "NONE",
                "FIXED_LOCAL_EXEC",
                "FIXED_REMOTE_DEVICE_EXEC",
                "FIXED_DEVICE_IO",
                "ARBITRARY_CODE_LIKE",
            ],
        )

    def test_canonical_serialization_is_order_independent_and_bounded(self) -> None:
        first = self._invocation(arguments={"beta": "two", "alpha": 1})
        second = self._invocation(arguments={"alpha": 1, "beta": "two"})

        self.assertEqual(
            contract.canonical_contract_bytes(first),
            contract.canonical_contract_bytes(second),
        )

        with patch.object(contract, "MAX_CONTRACT_BYTES", 16):
            with self.assertRaisesRegex(ValueError, "record exceeds"):
                contract.canonical_contract_bytes(first)

    def test_nested_invocation_and_result_data_cannot_change_after_validation(self) -> None:
        source = {"device": {"serial": "device-1", "flags": ["ready"]}}
        invocation = self._invocation(arguments=source)
        before = contract.canonical_contract_bytes(invocation)
        source["device"]["serial"] = "device-2"
        source["device"]["flags"].append("changed")
        self.assertEqual(contract.canonical_contract_bytes(invocation), before)
        with self.assertRaises(TypeError):
            invocation.arguments["new"] = "mutation"
        with self.assertRaises(TypeError):
            invocation.arguments["device"]["serial"] = "mutation"
        with self.assertRaises(AttributeError):
            invocation.arguments["device"]["flags"].append("mutation")
        output = invocation.as_dict()
        output["arguments"]["device"]["flags"].append("external-only")
        self.assertEqual(contract.canonical_contract_bytes(invocation), before)

        result_data = {"records": [{"state": "ok"}]}
        result = contract.ToolResult(tool=invocation.tool, ok=True, payload=result_data)
        original = contract.canonical_contract_bytes(result)
        result_data["records"][0]["state"] = "tampered"
        self.assertEqual(contract.canonical_contract_bytes(result), original)
        with self.assertRaises(TypeError):
            result.payload["records"][0]["state"] = "tampered"
        exported = result.as_dict()
        exported["payload"]["records"][0]["state"] = "changed"
        self.assertEqual(contract.canonical_contract_bytes(result), original)

        evidence_data = {"detail": ["one"]}
        evidence = contract.IdentityEvidence(kind="serial", attributes=evidence_data)
        evidence_data["detail"].append("two")
        self.assertEqual(evidence.as_dict(), {"kind": "serial", "attributes": {"detail": ["one"]}})

    def test_schema_and_json_validation_fail_closed(self) -> None:
        for value in (0, 2, True):
            with self.subTest(schema_version=value):
                with self.assertRaisesRegex(ValueError, "schema_version"):
                    self._invocation(schema_version=value)

        with self.assertRaisesRegex(ValueError, "canonical JSON"):
            self._invocation(arguments={"path": object()})
        with self.assertRaisesRegex(ValueError, "NaN or infinity"):
            self._invocation(arguments={"value": float("nan")})

    def test_scheduler_resources_are_explicit_and_never_derived_from_target(self) -> None:
        invocation = self._invocation()

        self.assertEqual(invocation.scheduler_resources, ())
        self.assertEqual(invocation.target.name, "example")

        with self.assertRaisesRegex(ValueError, "machine.*alone"):
            contract.ToolInvocation(
                tool=invocation.tool,
                arguments={},
                target=invocation.target,
                scheduler_resources=("machine", "usb"),
                execution_limits=invocation.execution_limits,
            )

    def test_partial_effect_evidence_is_conservative(self) -> None:
        evidence = contract.PartialEffectEvidence(
            action_attempted=True,
            committed=False,
            cleanup_failed=True,
        )
        self.assertEqual(
            evidence.as_dict(),
            {
                "action_attempted": True,
                "committed": False,
                "cleanup_failed": True,
                "stage": None,
            },
        )

        with self.assertRaisesRegex(ValueError, "committed=true"):
            contract.PartialEffectEvidence(
                action_attempted=False,
                committed=True,
            )


if __name__ == "__main__":
    unittest.main()
