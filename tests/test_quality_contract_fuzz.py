"""Deterministic, bounded, offline property/fault cases for task inputs.

No remote requests, daemon/service actions, random global state or user paths.
Hypothesis can complement this suite later; these cases run with stdlib only.
"""
from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path

from local_agent.runtime.health_snapshot import _private_json, _repository_ids
from local_agent.runtime.task_contract import (
    InvalidDedupeMetadata, task_dedupe_identity, task_resources_for,
)


class InputFaultMatrixTests(unittest.TestCase):
    def test_fuzz_dedupe_revision_never_accepts_bool_or_invalid_ranges(self) -> None:
        rng = random.Random(0xA6E17)
        invalid = [False, True, None, 0, -1, 1_000_001, 1.0, "1", [], {}]
        invalid.extend(rng.randrange(-100_000, 0) for _ in range(64))
        invalid.extend(rng.randrange(1_000_001, 2_000_000) for _ in range(64))
        for revision in invalid:
            with self.subTest(revision=repr(revision)):
                with self.assertRaises(InvalidDedupeMetadata):
                    task_dedupe_identity({"dedupe_key": "valid-key", "dedupe_revision": revision})
        for revision in (1, 2, 100, 1_000_000):
            self.assertEqual(
                task_dedupe_identity({"dedupe_key": "valid-key", "dedupe_revision": revision}),
                ("valid-key", revision),
            )

    def test_resource_malformed_inputs_do_not_override_exclusivity(self) -> None:
        rng = random.Random(0xB01D)
        for _ in range(160):
            candidate = rng.choice([
                ["machine", "network"], ["machine", "machine"], ["Machine"],
                ["disk 1"], ["RESOURCE"], [False], [None], [42],
                ["disk:1", "disk:1"], [" disk:1"], ["disk:1 "], "disk:1",
                None, {}, list(range(10)),
            ])
            with self.subTest(resource=repr(candidate)):
                with self.assertRaises(ValueError):
                    task_resources_for({"resources": candidate})
        self.assertEqual(task_resources_for({"resources": []}), ())
        self.assertEqual(task_resources_for({"resources": ["machine"]}), ("machine",))
        self.assertEqual(
            task_resources_for({"resources": ["disk:1", "network"]}), ("disk:1", "network")
        )

    def test_registry_duplicate_casefold_ids_never_count_as_two(self) -> None:
        rng = random.Random(0xC011)
        for _ in range(96):
            original = "".join(rng.choice("abcDEF123") for _ in range(rng.randrange(1, 18)))
            registry = {"version": 1, "repositories": [
                {"id": original}, {"id": original.swapcase()}
            ]}
            with self.subTest(original=original), self.assertRaises(ValueError):
                _repository_ids(registry)
        self.assertEqual(
            _repository_ids({"version": 1, "repositories": [
                {"id": "One"}, {"id": "two", "enabled": False}
            ]}),
            ["One"],
        )

    def test_private_json_duplicate_keys_and_truncated_payload_fail_closed(self) -> None:
        rng = random.Random(0xDA7A)
        with tempfile.TemporaryDirectory(prefix="la-fault-quality-") as tmp:
            target = Path(tmp).resolve() / "status.json"
            for _ in range(96):
                key = "key_" + str(rng.randrange(0, 1_000_000))
                target.write_text(
                    json.dumps({key: "first"})[:-1] + f',"{key}":"second"}}',
                    encoding="utf-8",
                )
                with self.subTest(key=key), self.assertRaises(ValueError):
                    _private_json(target, 4096)
            for bad in ('{', '', '[]', '{"ok":', '{"a":true,', '\\x00'):
                target.write_text(bad, encoding="utf-8")
                with self.subTest(bad=repr(bad)), self.assertRaises((ValueError, UnicodeError)):
                    _private_json(target, 4096)
            target.write_text('{"ok": true}', encoding="utf-8")
            self.assertEqual(_private_json(target, 4096), {"ok": True})
            with self.assertRaises(ValueError):
                _private_json(target, 4)


if __name__ == "__main__":
    unittest.main()
