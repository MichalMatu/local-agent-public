"""Real isolated Mac Git task execution under required B1c admission."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from local_agent.supervisor.macos_release_admission import CONFIG, REQUIRED_ENV
from tests.test_multirepo_integration import (
    REPO_ROOT,
    create_repository_fixture,
    test_environment,
    write_registry,
)


CANDIDATE_ROOT = Path(os.environ.get("LOCAL_AGENT_B1C_CANDIDATE_ROOT", str(REPO_ROOT)))


@unittest.skipUnless(sys.platform == "darwin", "Real isolated macOS runtime")
class MacB1cRuntimeEndToEndTests(unittest.TestCase):
    def run_candidate(self, *, enrolled: bool) -> tuple[Path, dict, str]:
        directory = tempfile.TemporaryDirectory(prefix="la-mac-b1c-runtime-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        item = create_repository_fixture(root, "b1c-disposable-task")
        registry = write_registry(root, (item,))
        home, env = test_environment(root)
        env[REQUIRED_ENV] = "1"
        env["PYTHONPATH"] = os.pathsep.join((str(root), str(CANDIDATE_ROOT)))
        state = home / "Library" / "Application Support" / "local-agent"
        if enrolled:
            state.mkdir(mode=0o700, parents=True)
            conf = state / CONFIG
            conf.write_text('{"version":1,"enabled":true}\n')
            conf.chmod(0o600)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [
                sys.executable, str(CANDIDATE_ROOT / "agent_parallel.py"),
                "--registry", str(registry), "--max-workers", "1", "--once",
            ],
            cwd=CANDIDATE_ROOT, env=env, capture_output=True, text=True,
            timeout=60, check=False,
        )
        self.assertEqual(result.returncode, 0 if enrolled else 75, result.stderr + result.stdout)
        output = result.stdout + result.stderr
        return Path(item["control"]), json.loads(
            (Path(item["control"]) / ".agent/tasks/shared-task-id.json").read_text()
        ), output

    def test_enrolled_new_engine_executes_once_and_publishes_result(self):
        control, task, output = self.run_candidate(enrolled=True)
        result_path = control / ".agent/results/shared-task-id.json"
        self.assertTrue(result_path.exists(), output)
        result = json.loads(result_path.read_text())
        self.assertEqual(result["status"], "done", output)
        self.assertEqual(result["id"], task["id"], output)
        self.assertIn("b1c-disposable-task-ok", str(result["commands"]), output)
        self.assertFalse(any((control / ".agent").glob("**/*replayed*")))

    def test_unenrolled_engine_never_claims_or_executes_git_task(self):
        control, task, output = self.run_candidate(enrolled=False)
        self.assertFalse(
            (control / ".agent/results/shared-task-id.json").exists(), output,
        )
        self.assertEqual(task["id"], "shared-task-id")
        self.assertTrue((control / ".agent/tasks/shared-task-id.json").is_file())
        self.assertIn("admission", output.lower())


if __name__ == "__main__":
    unittest.main()
