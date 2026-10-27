"""Four-platform source-only wire contract. Never contacts services or GitHub."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
CASES = (
    ({"id": "matrix-ok", "commands": ["true"], "resources": []}, True),
    ({"id": "matrix-named", "commands": ["true"], "resources": ["disk:1"]}, True),
    ({"id": "matrix-missing", "commands": ["true"]}, False),
    ({"id": "matrix/invalid", "commands": ["true"], "resources": []}, False),
    ({"id": "x" * 201, "commands": ["true"], "resources": []}, False),
    ({"id": "matrix-timeout", "commands": ["true"], "resources": [],
      "task_timeout": True}, False),
    ({"id": "matrix-duplicate", "commands": ["true"],
      "resources": ["disk:1", "disk:1"]}, False),
    ({"id": "matrix-empty", "commands": [""], "resources": []}, False),
    ({"id": "matrix-dedupe-valid", "commands": ["true"], "resources": [],
      "dedupe_key": "intent:sync", "dedupe_revision": 3}, True),
    ({"id": "matrix-dedupe-bool", "commands": ["true"], "resources": [],
      "dedupe_key": "intent:sync", "dedupe_revision": True}, False),
    ({"id": "matrix-machine-exclusive", "commands": ["true"],
      "resources": ["machine", "network"]}, False),
    ({"id": "matrix-too-many-resources", "commands": ["true"],
      "resources": [f"network:{i}" for i in range(9)]}, False),
    ({"id": "matrix-nested-invalid-step", "resources": [],
      "steps": [{"name": "run", "command": ""}]}, False),
    ({"id": "matrix-resource-case", "commands": ["true"],
      "resources": ["Network"]}, False),
    ({"id": "bad\nname", "commands": ["true"], "resources": []}, False),
)
PLATFORMS = {
    "macos": ("local_agent.runtime.task_contract", ()),
    "linux": ("linux_agent.runtime.task_contract", ("platforms/linux/core",)),
    "pi": ("linux_agent.runtime.task_contract", (
        "platforms/raspberry-pi/local-agent", "platforms/linux/local-agent",
        "platforms/linux/core")),
    "termux": ("linux_agent.runtime.task_contract", (
        "platforms/android/local-agent", "platforms/linux/local-agent",
        "platforms/linux/core")),
}
PROBE = """
import importlib, json, sys
name, platform = sys.argv[1:]
contract = importlib.import_module(name)
if platform == "termux":
    from termux_agent.agent import TermuxAgent
    from linux_agent.agent import Agent
    assert issubclass(TermuxAgent, Agent)
if platform == "pi":
    from pi_agent.__main__ import main
    assert callable(main)
results = []
for task in json.load(sys.stdin):
    digest = contract.task_digest(task)
    try:
        contract.validate_task(dict(task))
    except ValueError:
        valid = False
    else:
        valid = True
    try:
        resources = list(contract.task_resources_for(task))
    except ValueError:
        resources = None
    results.append(dict(valid=valid, digest=digest, resources=resources))
print(json.dumps(results, sort_keys=True))
"""


def probe(platform: str) -> list[dict]:
    module, roots = PLATFORMS[platform]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT), *(str(ROOT / p) for p in roots)))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for key in tuple(env):
        if key.startswith(("LOCAL_AGENT_LEASE_", "LOCAL_AGENT_RESOURCE_LEASE_")):
            env.pop(key)
    result = subprocess.run(
        [sys.executable, "-B", "-c", PROBE, module, platform],
        input=json.dumps([task for task, _ in CASES]), text=True,
        capture_output=True, cwd=ROOT, env=env, check=True, timeout=40,
    )
    return json.loads(result.stdout)


class PlatformWireContractTests(unittest.TestCase):
    def test_mac_linux_contract_sources_have_no_silent_drift(self) -> None:
        mac = (ROOT / "platforms/macos/local-agent/runtime/task_contract.py").read_text(
            encoding="utf-8"
        )
        linux = (ROOT / "platforms/linux/core/linux_agent/runtime/task_contract.py").read_text(
            encoding="utf-8"
        )
        self.assertEqual(mac, linux.replace("linux_agent", "local_agent"))

    def test_four_platforms_have_identical_contracts(self) -> None:
        observed = {name: probe(name) for name in PLATFORMS}
        baseline = observed["macos"]
        self.assertEqual(len(baseline), len(CASES))
        for name, result in observed.items():
            with self.subTest(platform=name):
                self.assertEqual(result, baseline)
        for index, (task, accepted) in enumerate(CASES):
            with self.subTest(case=index):
                self.assertEqual(baseline[index]["valid"], accepted)
                canonical = json.dumps(
                    task, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                ).encode("utf-8")
                self.assertEqual(
                    baseline[index]["digest"], hashlib.sha256(canonical).hexdigest()
                )


if __name__ == "__main__":
    unittest.main()
