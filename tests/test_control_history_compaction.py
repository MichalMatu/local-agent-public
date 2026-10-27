from __future__ import annotations

import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from local_agent.foundation import storage
import local_agent.repository.cleanup as cleanup
from local_agent.repository.history_policy import (
    CONTROL_HISTORY_INITIAL_ROOT_MESSAGE,
    CONTROL_HISTORY_POLICY_TRAILER,
)


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def git_output(cwd: Path, *args: str) -> str:
    return git(cwd, *args).stdout.strip()


def configure_identity(path: Path) -> None:
    git(path, "config", "user.name", "Local Agent Test")
    git(path, "config", "user.email", "local-agent-test@example.invalid")


class CoreAdapter:
    def __init__(self, control: Path) -> None:
        self.CONTROL = control
        self.CONTROL_BRANCH = "agent-control"
        self.CONTROL_GIT_LOCK = threading.RLock()
        self.ENV: dict[str, str] = {}
        self.messages: list[str] = []
        self.process_hook = None

    def process(self, args, cwd, **_kwargs):
        if self.process_hook is not None:
            hooked = self.process_hook(args, cwd)
            if hooked is not None:
                return hooked
        completed = subprocess.run(
            args,
            cwd=cwd,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        return {
            "exit_code": completed.returncode,
            "output": completed.stdout,
            "timed_out": False,
        }

    def log(self, message: str) -> None:
        self.messages.append(message)


class ControlFixture:
    def __init__(self, root: Path, commits: int = 5, *, managed: bool = True) -> None:
        self.root = root
        self.remote = root / "remote.git"
        self.seed = root / "seed"
        self.control = root / "control"

        git(root, "init", "--bare", str(self.remote))
        git(root, "clone", str(self.remote), str(self.seed))
        configure_identity(self.seed)
        git(self.seed, "switch", "--orphan", "agent-control")
        status = self.seed / ".agent" / "status" / "daemon.json"
        status.parent.mkdir(parents=True)
        status.write_text('{"generation":0}\n', encoding="utf-8")
        git(self.seed, "add", ".agent/status/daemon.json")
        initial_message = (
            CONTROL_HISTORY_INITIAL_ROOT_MESSAGE if managed else "Initialize control branch"
        )
        git(self.seed, "commit", "-m", initial_message)
        for generation in range(1, commits):
            status.write_text(f'{{"generation":{generation}}}\n', encoding="utf-8")
            git(self.seed, "add", ".agent/status/daemon.json")
            git(self.seed, "commit", "-m", f"Control update {generation}")
        git(self.seed, "push", "-u", "origin", "agent-control")

        git(root, "clone", "--branch", "agent-control", str(self.remote), str(self.control))
        configure_identity(self.control)
        self.core = CoreAdapter(self.control)

    def remote_head(self) -> str:
        return git_output(self.remote, "rev-parse", "refs/heads/agent-control")

    def remote_tree(self) -> str:
        return git_output(self.remote, "rev-parse", "refs/heads/agent-control^{tree}")

    def remote_count(self) -> int:
        return int(git_output(self.remote, "rev-list", "--count", "refs/heads/agent-control"))

    def advance_remote(self, name: str = "concurrent.json") -> str:
        git(self.seed, "pull", "--rebase", "origin", "agent-control")
        path = self.seed / ".agent" / "status" / name
        path.write_text('{"source":"concurrent"}\n', encoding="utf-8")
        git(self.seed, "add", f".agent/status/{name}")
        git(self.seed, "commit", "-m", "Concurrent remote update")
        git(self.seed, "push", "origin", "agent-control")
        return self.remote_head()

    def advance_rewritten_remote(self, name: str = "post-compact.json") -> str:
        writer = self.root / f"writer-{name}"
        git(self.root, "clone", "--branch", "agent-control", str(self.remote), str(writer))
        configure_identity(writer)
        path = writer / ".agent" / "status" / name
        path.write_text('{"source":"post-compact"}\n', encoding="utf-8")
        git(writer, "add", f".agent/status/{name}")
        git(writer, "commit", "-m", "Post-compaction remote update")
        git(writer, "push", "origin", "agent-control")
        return self.remote_head()


class AutomaticControlHistoryCompactionTests(unittest.TestCase):
    def test_runtime_default_threshold_keeps_half_window_policy_margin(self) -> None:
        self.assertEqual(
            cleanup.CONTROL_HISTORY_COMPACTION_THRESHOLD,
            storage.CONTROL_HISTORY_DEPTH // 2,
        )
        self.assertGreater(
            storage.CONTROL_HISTORY_DEPTH,
            cleanup.CONTROL_HISTORY_COMPACTION_THRESHOLD,
        )

    def test_below_threshold_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ControlFixture(Path(tmp), commits=3)
            before = fixture.remote_head()

            result = cleanup.compact_control_history(fixture.core, threshold=4)

            self.assertFalse(result["changed"])
            self.assertEqual(result["reason"], "below_threshold")
            self.assertEqual(fixture.remote_head(), before)
            self.assertEqual(fixture.remote_count(), 3)

    def test_unmanaged_legacy_history_is_not_rewritten_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ControlFixture(Path(tmp), commits=5, managed=False)
            old_head = fixture.remote_head()
            old_tree = fixture.remote_tree()

            result = cleanup.compact_control_history(fixture.core, threshold=5)

            self.assertFalse(result["changed"])
            self.assertEqual(result["reason"], "history_policy_unmanaged")
            self.assertEqual(fixture.remote_head(), old_head)
            self.assertEqual(fixture.remote_tree(), old_tree)
            self.assertEqual(fixture.remote_count(), 5)

    def test_policy_marker_older_than_threshold_still_enables_compaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ControlFixture(Path(tmp), commits=6)
            old_tree = fixture.remote_tree()

            result = cleanup.compact_control_history(fixture.core, threshold=3)

            self.assertTrue(result["changed"])
            self.assertEqual(result["visible_commits_before"], 6)
            self.assertEqual(fixture.remote_count(), 1)
            self.assertEqual(fixture.remote_tree(), old_tree)

    def test_threshold_compaction_preserves_exact_tree_and_leaves_one_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ControlFixture(Path(tmp), commits=5)
            old_head = fixture.remote_head()
            old_tree = fixture.remote_tree()

            result = cleanup.compact_control_history(fixture.core, threshold=5)

            self.assertTrue(result["changed"])
            self.assertEqual(result["old_sha"], old_head)
            self.assertNotEqual(result["new_sha"], old_head)
            self.assertEqual(result["tree_sha"], old_tree)
            self.assertEqual(fixture.remote_tree(), old_tree)
            self.assertEqual(fixture.remote_count(), 1)
            self.assertEqual(git_output(fixture.control, "rev-parse", "HEAD"), result["new_sha"])
            self.assertEqual(
                git_output(fixture.control, "rev-parse", "refs/remotes/origin/agent-control"),
                result["new_sha"],
            )
            self.assertIn(
                CONTROL_HISTORY_POLICY_TRAILER,
                git_output(fixture.remote, "log", "-1", "--format=%B", "agent-control"),
            )

    def test_exact_lease_loses_race_without_overwriting_concurrent_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ControlFixture(Path(tmp), commits=5)
            old_head = fixture.remote_head()
            injected = False
            concurrent_head = ""

            def hook(args, _cwd):
                nonlocal injected, concurrent_head
                if len(args) > 1 and args[0] == "git" and args[1] == "push" and not injected:
                    injected = True
                    concurrent_head = fixture.advance_remote()
                return None

            fixture.core.process_hook = hook
            result = cleanup.compact_control_history(fixture.core, threshold=5)

            self.assertTrue(injected)
            self.assertFalse(result["changed"])
            self.assertEqual(result["reason"], "lease_or_push_failed")
            self.assertEqual(result["old_sha"], old_head)
            self.assertEqual(result["remote_sha"], concurrent_head)
            self.assertEqual(fixture.remote_head(), concurrent_head)
            self.assertEqual(fixture.remote_count(), 6)
            self.assertEqual(git_output(fixture.control, "rev-parse", "HEAD"), old_head)

    def test_ambiguous_push_failure_is_reconciled_when_remote_has_new_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ControlFixture(Path(tmp), commits=5)
            old_tree = fixture.remote_tree()
            injected = False

            def hook(args, cwd):
                nonlocal injected
                if len(args) > 1 and args[0] == "git" and args[1] == "push" and not injected:
                    injected = True
                    completed = subprocess.run(
                        args,
                        cwd=cwd,
                        check=False,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stdout)
                    return {
                        "exit_code": 1,
                        "output": "simulated transport failure after server accepted push\n",
                        "timed_out": True,
                    }
                return None

            fixture.core.process_hook = hook
            result = cleanup.compact_control_history(fixture.core, threshold=5)

            self.assertTrue(injected)
            self.assertTrue(result["changed"])
            self.assertTrue(result["push_reconciled"])
            self.assertFalse(result["remote_advanced"])
            self.assertEqual(fixture.remote_count(), 1)
            self.assertEqual(fixture.remote_tree(), old_tree)
            self.assertEqual(git_output(fixture.control, "rev-parse", "HEAD"), result["new_sha"])

    def test_ambiguous_push_with_immediate_remote_advance_keeps_new_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ControlFixture(Path(tmp), commits=5)
            injected = False
            advanced_head = ""

            def hook(args, cwd):
                nonlocal injected, advanced_head
                if len(args) > 1 and args[0] == "git" and args[1] == "push" and not injected:
                    injected = True
                    completed = subprocess.run(
                        args,
                        cwd=cwd,
                        check=False,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stdout)
                    advanced_head = fixture.advance_rewritten_remote()
                    return {
                        "exit_code": 1,
                        "output": "simulated lost push response before remote advanced\n",
                        "timed_out": True,
                    }
                return None

            fixture.core.process_hook = hook
            result = cleanup.compact_control_history(fixture.core, threshold=5)

            self.assertTrue(injected)
            self.assertTrue(result["changed"])
            self.assertTrue(result["push_reconciled"])
            self.assertTrue(result["remote_advanced"])
            self.assertEqual(result["head_sha"], advanced_head)
            self.assertEqual(fixture.remote_head(), advanced_head)
            self.assertEqual(fixture.remote_count(), 2)
            self.assertEqual(git_output(fixture.control, "rev-parse", "HEAD"), advanced_head)
            self.assertTrue((fixture.control / ".agent/status/post-compact.json").exists())

    def test_runtime_gc_checks_history_even_when_no_artifacts_need_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ControlFixture(Path(tmp), commits=3)
            history = {
                "changed": False,
                "reason": "below_threshold",
                "visible_commits": 3,
                "threshold": cleanup.CONTROL_HISTORY_COMPACTION_THRESHOLD,
            }

            with mock.patch.object(
                cleanup,
                "_compact_control_history_locked",
                return_value=history,
            ) as compact:
                result = cleanup.prune_control_runtime(fixture.core)

            compact.assert_called_once_with(fixture.core)
            self.assertEqual(result["deleted"], 0)
            self.assertEqual(result["paths"], ())
            self.assertEqual(result["history"], history)
            self.assertFalse(result["changed"])


if __name__ == "__main__":
    unittest.main()
