"""Offline non-destructive attached-checkout safety regressions."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.foundation.attached_checkout import isolated_attached_checkout
from local_agent.repository.context import repository_from_dict


class AttachedCheckoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "user"
        self.source.mkdir()
        self.checkpoints = self.root / "checkpoints"
        self.git("init", "-q", "-b", "feature", cwd=self.source)
        self.git("config", "user.name", "Local Test", cwd=self.source)
        self.git("config", "user.email", "test@example.invalid", cwd=self.source)
        (self.source / "tracked.txt").write_text("base\n")
        self.git("add", ".", cwd=self.source)
        self.git("commit", "-qm", "base", cwd=self.source)
        (self.source / "tracked.txt").write_text("staged\n")
        self.git("add", "tracked.txt", cwd=self.source)
        (self.source / "tracked.txt").write_text("staged\nunstaged\n")
        (self.source / "untracked.txt").write_text("untracked\n")

    @staticmethod
    def git(*args: str, cwd: Path) -> str:
        return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()

    def source_state(self) -> tuple[str, str, str, str, str]:
        return (
            self.git("status", "--porcelain=v1", cwd=self.source),
            self.git("diff", "--cached", cwd=self.source),
            self.git("diff", cwd=self.source),
            self.git("branch", "--show-current", cwd=self.source),
            hashlib.sha256((self.source / ".git" / "index").read_bytes()).hexdigest(),
        )

    def test_dirty_index_staged_unstaged_untracked_and_branch_preserved(self) -> None:
        before = self.source_state()
        with isolated_attached_checkout(self.source, self.checkpoints, "test") as sandbox:
            self.assertNotEqual(sandbox, self.source)
            self.assertEqual(self.git("status", "--porcelain=v1", cwd=sandbox), before[0])
            self.assertEqual(self.git("diff", "--cached", cwd=sandbox), before[1])
            self.assertEqual(self.git("diff", cwd=sandbox), before[2])
            self.assertEqual(self.git("branch", "--show-current", cwd=sandbox), "feature")
            (sandbox / "tracked.txt").write_text("task edit\n")
            (sandbox / "untracked.txt").unlink()
            (sandbox / "new.txt").write_text("task output\n")
        self.assertEqual(self.source_state(), before)
        self.assertEqual((self.source / "untracked.txt").read_text(), "untracked\n")
        self.assertFalse((self.source / "new.txt").exists())
        recovered = list((self.checkpoints / "attached-task-workspaces").iterdir())
        self.assertEqual(len(recovered), 1)
        self.assertEqual((recovered[0] / "work" / "tracked.txt").read_text(), "task edit\n")
        self.assertEqual((recovered[0] / "work" / "new.txt").read_text(), "task output\n")
        metadata = json.loads((recovered[0] / "completion.json").read_text())
        self.assertEqual(metadata["task_id"], "test")
        self.assertEqual(metadata["state"], "completed_with_changes")
        self.assertEqual(metadata["promotion"], "manual_only")
        self.assertNotEqual(metadata["source_fingerprint"], metadata["result_fingerprint"])

    def test_unchanged_success_discards_only_the_private_copy(self) -> None:
        before = self.source_state()
        with isolated_attached_checkout(self.source, self.checkpoints, "read-only") as sandbox:
            self.assertTrue((sandbox / ".git" / "index").is_file())
        self.assertEqual(self.source_state(), before)
        self.assertEqual(list((self.checkpoints / "attached-task-workspaces").iterdir()), [])

    def test_manifest_name_hijack_does_not_overwrite_external_file(self) -> None:
        before = self.source_state()
        outside = self.root / "owner-sentinel.txt"
        outside.write_text("SECRET MUST STAY")
        with self.assertRaises((FileExistsError, OSError)):
            with isolated_attached_checkout(self.source, self.checkpoints, "hijack") as sandbox:
                (sandbox / "tracked.txt").write_text("safe output\n")
                (sandbox.parent / "completion.json").symlink_to(outside)
        self.assertEqual(outside.read_text(), "SECRET MUST STAY")
        self.assertEqual(self.source_state(), before)
        recovered = list((self.checkpoints / "attached-task-workspaces").iterdir())
        self.assertEqual(len(recovered), 1)
        self.assertEqual((recovered[0] / "work" / "tracked.txt").read_text(), "safe output\n")

    def test_concurrent_mutation_aborts_before_execution(self) -> None:
        import local_agent.foundation.attached_checkout as attached
        original_copytree = attached.shutil.copytree

        def concurrent_edit(*args, **kwargs):
            result = original_copytree(*args, **kwargs)
            if Path(args[0]).resolve() == self.source.resolve():
                (self.source / "untracked.txt").write_text("new user revision\n")
            return result

        with patch.object(attached.shutil, "copytree", side_effect=concurrent_edit):
            with self.assertRaisesRegex(RuntimeError, "changed during snapshot"):
                with isolated_attached_checkout(self.source, self.checkpoints, "race"):
                    self.fail("no task should be admitted")
        self.assertEqual((self.source / "untracked.txt").read_text(), "new user revision\n")

    def test_exception_keeps_only_isolated_recovery_copy(self) -> None:
        before = self.source_state()
        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            with isolated_attached_checkout(self.source, self.checkpoints, "recovery") as sandbox:
                (sandbox / "tracked.txt").write_text("recoverable data\n")
                raise RuntimeError("simulated crash")
        self.assertEqual(self.source_state(), before)
        recovery = list((self.checkpoints / "attached-task-workspaces").iterdir())
        self.assertEqual(len(recovery), 1)
        self.assertEqual((recovery[0] / "work" / "tracked.txt").read_text(), "recoverable data\n")
        self.assertEqual(json.loads((recovery[0] / "snapshot.json").read_text())["task_id"], "recovery")

    def test_attached_core_process_never_invokes_managed_reset_or_clean(self) -> None:
        import local_agent.foundation.core as core

        before = self.source_state()
        with isolated_attached_checkout(self.source, self.checkpoints, "core-task") as sandbox:
            with (
                patch.object(core, "CONTROL", sandbox),
                patch.object(core, "WORK", sandbox),
                patch.object(core, "CHECKPOINTS", self.checkpoints),
                patch.object(core, "WORK_PREPARATION_MODE", "attached_isolated"),
                patch.object(core, "prepare_work", side_effect=AssertionError("reset called")),
                patch.object(core, "cleanup_work", side_effect=AssertionError("clean called")),
            ):
                result = core.process_task({
                    "id": "core-task", "mode": "commands", "work_branch": "feature",
                    "allow_write": True,
                    "writes": [{"path": "generated.txt", "content": "agent output\\n"}],
                })
            self.assertEqual(result["status"], "done", result)
            self.assertEqual((sandbox / "generated.txt").read_text(), "agent output\\n")
        self.assertEqual(self.source_state(), before)
        self.assertFalse((self.source / "generated.txt").exists())

    def test_serial_and_parallel_dispatch_use_safe_adapter(self) -> None:
        import inspect
        import local_agent.repository.worker as serial
        import local_agent.supervisor.worker as parallel

        self.assertIn("execute_in_task_workspace(",
                      inspect.getsource(serial._poll_repository_once_admitted))
        self.assertIn("execute_in_task_workspace(",
                      inspect.getsource(parallel._poll_repository_once_admitted))

    def test_adapter_runs_parallel_style_runner_only_in_private_copy(self) -> None:
        from types import SimpleNamespace

        import local_agent.foundation.core as core
        from local_agent.foundation.attached_checkout import execute_in_task_workspace

        before = self.source_state()
        ctx = SimpleNamespace(
            repository_id="fixture", work=self.source,
            checkpoints=self.checkpoints, work_mode="attached",
        )
        called = []

        def runner(task, *, remote_daemon_status, remote_result_published):
            self.assertEqual(task["id"], "parallel-style-task")
            self.assertFalse(remote_daemon_status)
            self.assertFalse(remote_result_published)
            self.assertNotEqual(core.WORK, self.source)
            self.assertEqual(self.git("branch", "--show-current", cwd=core.WORK), "feature")
            called.append(core.WORK)
            return "done"

        with patch.object(core, "WORK", self.source), patch.object(
            core, "WORK_PREPARATION_MODE", "managed",
        ):
            self.assertEqual(execute_in_task_workspace(
                ctx, core, {"id": "parallel-style-task"}, runner,
            ), "done")
            self.assertEqual(core.WORK, self.source)
            self.assertEqual(core.WORK_PREPARATION_MODE, "managed")
        self.assertEqual(len(called), 1)
        self.assertEqual(self.source_state(), before)

    def test_absolute_or_escaping_symlink_is_blocked(self) -> None:
        (self.source / "external").symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "external symlink"):
            with isolated_attached_checkout(self.source, self.checkpoints, "link"):
                self.fail("unsafe symlink admitted")

    def test_attached_mode_is_explicit_and_validated(self) -> None:
        candidate = {"id": "foo", "repository": "owner/foo",
                     "work_dir": str(self.source), "work_mode": "attached"}
        ctx = repository_from_dict(candidate, home=self.root)
        self.assertEqual(ctx.work_mode, "attached")
        with self.assertRaisesRegex(ValueError, "work_mode"):
            repository_from_dict({**candidate, "work_mode": "destructive"}, home=self.root)

    def test_real_child_process_crash_preserves_private_recovery_without_replay(self) -> None:
        from local_agent.repository.snapshot_review import inspect_attached_snapshots

        before = self.source_state()
        code = "\n".join([
            "import os, sys",
            "from pathlib import Path",
            "from local_agent.foundation.attached_checkout import isolated_attached_checkout",
            "source = Path(sys.argv[1])",
            "checkpoints = Path(sys.argv[2])",
            "with isolated_attached_checkout(source, checkpoints, 'crash-test') as work:",
            "    (work / 'tracked.txt').write_text('interrupted result')",
            "    os._exit(77)",
        ])
        proc = subprocess.run(
            [sys.executable, "-c", code, str(self.source), str(self.checkpoints)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 77, proc.stderr)
        self.assertEqual(self.source_state(), before)
        retained = list((self.checkpoints / "attached-task-workspaces").iterdir())
        self.assertEqual(len(retained), 1)
        self.assertFalse((retained[0] / "completion.json").exists())
        self.assertEqual(
            (retained[0] / "work" / "tracked.txt").read_text(), "interrupted result",
        )
        response = inspect_attached_snapshots(self.checkpoints)
        self.assertEqual(response["snapshots"][0]["review_state"], "interrupted_or_unfinished")
        self.assertFalse(response["promotion_allowed"])
        self.assertFalse((self.source / "new.txt").exists())



if __name__ == "__main__":
    unittest.main()
