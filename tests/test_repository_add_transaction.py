"""Offline transaction, retry, binding and personal-checkout preservation tests."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.repository import admin, checkout_identity, transactional_add


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, timeout=20,
    )
    if result.returncode:
        raise AssertionError(f"git {args[0]}: {result.stderr}")
    return result.stdout.strip()


class RepositoryAddTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="la-add-tx-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.config = self.root / "gitconfig"
        self.config.write_text(
            "[user]\n\tname = Local Agent Test\n\temail = test@example.invalid\n",
            encoding="utf-8",
        )
        self.registry = self.home / "repositories.json"
        self.registry.write_text('{"version":1,"repositories":[]}\n', encoding="utf-8")
        self.catalog = self.home / "catalog.json"
        self.catalog.write_text(json.dumps({
            "version": 1, "agents": [{
                "id": "demo", "repository": "Owner/demo",
                "agent_binding": "12222222-2222-4222-8222-222222222222",
                "execution_enabled": True,
            }],
        }), encoding="utf-8")
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        git("init", "-q", "--bare", str(self.remote))
        git("init", "-q", "-b", "main", str(self.seed))
        git("config", "user.name", "Fixture", cwd=self.seed)
        git("config", "user.email", "test@example.invalid", cwd=self.seed)
        (self.seed / "tracked.txt").write_text("base\n", encoding="utf-8")
        git("add", ".", cwd=self.seed)
        git("commit", "-qm", "base", cwd=self.seed)
        git("remote", "add", "origin", str(self.remote), cwd=self.seed)
        git("push", "-q", "origin", "main", cwd=self.seed)
        git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.remote)
        self.work = self.root / "user-work"
        git("clone", "-q", str(self.remote), str(self.work))
        git("switch", "-q", "-c", "feature", cwd=self.work)
        (self.work / "tracked.txt").write_text("staged\n", encoding="utf-8")
        git("add", "tracked.txt", cwd=self.work)
        (self.work / "tracked.txt").write_text("staged\nunstaged\n", encoding="utf-8")
        (self.work / "new.txt").write_text("untracked\n", encoding="utf-8")
        self.control = self.root / "agent-control"
        self.checkpoints = self.root / "checkpoints"
        self.patches = [
            mock.patch.object(transactional_add, "_remote_url", return_value=str(self.remote)),
            mock.patch.object(
                checkout_identity, "normalize_remote_url", return_value="Owner/demo",
            ),
            mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(self.config)}),
            mock.patch.dict(
                transactional_add.core.ENV, {"GIT_CONFIG_GLOBAL": str(self.config)},
            ),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def apply(self, **kwargs) -> dict:
        return transactional_add.apply_repository_add(
            "Owner/demo", repository_id="demo",
            work_dir=self.work, control_dir=self.control,
            checkpoints_dir=self.checkpoints,
            registry_path=self.registry, catalog_path=self.catalog,
            home=self.home, state_dir=self.home, **kwargs,
        )

    def work_state(self) -> tuple[str, str, str, str, bytes, str]:
        return (
            git("branch", "--show-current", cwd=self.work),
            git("diff", "--cached", cwd=self.work),
            git("diff", cwd=self.work),
            git("status", "--porcelain", cwd=self.work),
            (self.work / ".git" / "index").read_bytes(),
            (self.work / "new.txt").read_text(encoding="utf-8"),
        )

    def test_add_then_maintenance_exit_resumes_with_complete_ledgers(self) -> None:
        """Exercise real Git registration and the real pre-restart ledger gate."""
        from scripts import macos_repository_maintenance as maintenance

        before = self.work_state()
        session = self.root / "maintenance-session"
        session.mkdir()
        record = {"session_id": "session-" + "a" * 24, "source_sha": "a" * 40}
        marker = self.home / maintenance.FROZEN
        marker.write_bytes(maintenance._owned_freeze(record))
        self.assertEqual(self.apply()["status"], "registered")
        with mock.patch.object(maintenance, "_resume") as resume:
            maintenance._finish(session, record, self.root, self.home, self.root / "service.plist")
        resume.assert_called_once()
        self.assertFalse(marker.exists())
        self.assertEqual(json.loads((session / maintenance.JOURNAL).read_text())["state"],
                         "accepted")
        self.assertEqual(self.work_state(), before)

    def test_ledger_preparation_failure_never_commits_registry_and_retry_is_safe(self) -> None:
        from local_agent.repository import state_layout

        before = self.work_state()
        original = self.registry.read_bytes()
        with mock.patch.object(state_layout.os, "rename", side_effect=OSError("ledger publish failed")):
            with self.assertRaisesRegex(OSError, "ledger publish failed"):
                self.apply()
        self.assertEqual(self.registry.read_bytes(), original)
        self.assertFalse((self.home / "repositories/demo").exists())
        journal = self.home / "pending-repository-add/demo.json"
        self.assertEqual(json.loads(journal.read_text())["state"], "prepared")
        self.assertEqual(self.apply()["status"], "registered")
        self.assertEqual(self.work_state(), before)

    def test_registered_retry_does_not_recreate_missing_history(self) -> None:
        self.apply()
        ledger = self.home / "repositories/demo"
        (ledger / "claims").rmdir()
        original = self.registry.read_bytes()
        with self.assertRaises(FileNotFoundError):
            self.apply()
        self.assertFalse((ledger / "claims").exists())
        self.assertEqual(self.registry.read_bytes(), original)

    def test_managed_new_repo_uses_private_binding_and_initializes_control(self) -> None:
        """Real local Git remote; no packaged catalog mutation or owner checkout."""
        from local_agent.repository import binding, binding_enrollment
        from local_agent.repository.binding import validate_repository_control_binding

        bundled = self.root / "bundled-catalog.json"
        bundled.write_text('{"version":1,"agents":[]}', encoding="utf-8")
        private = self.home / "agent_bindings.local.json"
        source_before = bundled.read_bytes()
        with (
            mock.patch.object(binding, "DEFAULT_CATALOG_PATH", bundled),
            mock.patch.object(binding, "local_binding_catalog_path", return_value=private),
            mock.patch.object(binding_enrollment, "local_binding_catalog_path",
                              return_value=private),
        ):
            created = binding_enrollment.ensure_local_repository_binding("demo", "Owner/demo")
            self.assertEqual(
                binding_enrollment.ensure_local_repository_binding("demo", "Owner/demo"),
                created,
            )
            registered = transactional_add.apply_repository_add(
                "Owner/demo", registry_path=self.registry, home=self.home,
            )
            self.assertEqual(registered["status"], "registered")
            self.assertEqual(registered["work_mode"], "managed")
            self.assertEqual(registered["agent_binding"], created)
            control = self.home / "agent-workspace/repos/demo/control"
            work = self.home / "agent-workspace/repos/demo/work"
            checkpoint = self.home / "agent-workspace/repos/demo/checkpoints"
            self.assertTrue(work.is_dir())
            self.assertTrue(control.is_dir())
            self.assertTrue(checkpoint.is_dir())
            self.assertEqual(git("branch", "--show-current", cwd=control), "agent-control")
            self.assertEqual(
                validate_repository_control_binding(
                    repository_id="demo", repository="Owner/demo",
                    expected_agent_binding=created, control_dir=control,
                ),
                created,
            )
            self.assertEqual(
                transactional_add.apply_repository_add(
                    "Owner/demo", registry_path=self.registry, home=self.home,
                )["status"],
                "already_registered",
            )
        self.assertEqual(bundled.read_bytes(), source_before)
        self.assertEqual((self.work / "new.txt").read_text(), "untracked\n")
        self.assertEqual(len(json.loads(self.registry.read_text())["repositories"]), 1)

    def test_existing_dirty_checkout_bound_control_and_retry_are_nondestructive(self) -> None:
        before = self.work_state()
        result = self.apply()
        self.assertEqual(result["status"], "registered")
        self.assertEqual(result["work_mode"], "attached")
        self.assertEqual(self.work_state(), before)
        self.assertEqual(git("branch", "--show-current", cwd=self.control), "agent-control")
        binding = json.loads((self.control / ".agent/binding.json").read_text())
        self.assertEqual(binding["agent_binding"], result["agent_binding"])
        registry = json.loads(self.registry.read_text())
        self.assertEqual(len(registry["repositories"]), 1)
        self.assertEqual(registry["repositories"][0]["work_mode"], "attached")
        self.assertEqual(self.apply()["status"], "already_registered")
        self.assertEqual(self.work_state(), before)
        self.assertEqual(len(json.loads(self.registry.read_text())["repositories"]), 1)

    def test_failed_commit_preserves_owner_and_retry_recovers_local_clones(self) -> None:
        before = self.work_state()
        original = transactional_add._atomic_json

        def fail_registry(path, payload):
            if path == self.registry:
                raise OSError("simulated registry write failure")
            return original(path, payload)

        with mock.patch.object(transactional_add, "_atomic_json", side_effect=fail_registry):
            with self.assertRaisesRegex(OSError, "simulated"):
                self.apply()
        self.assertEqual(self.work_state(), before)
        self.assertFalse(json.loads(self.registry.read_text())["repositories"])
        self.assertTrue((self.control / ".agent/binding.json").exists())
        self.assertEqual(self.apply()["status"], "registered")
        self.assertEqual(self.work_state(), before)

    def test_apply_rejects_linked_git_worktree_before_control_clone(self) -> None:
        linked = self.root / "linked-work"
        git("worktree", "add", "-q", "-b", "other", str(linked), cwd=self.work)
        self.assertTrue((linked / ".git").is_file())
        with self.assertRaisesRegex(ValueError, "standalone"):
            transactional_add.apply_repository_add(
                "Owner/demo", repository_id="demo",
                work_dir=linked, control_dir=self.control,
                checkpoints_dir=self.checkpoints,
                registry_path=self.registry, catalog_path=self.catalog,
                home=self.home,
            )
        self.assertFalse(self.control.exists())
        self.assertEqual(json.loads(self.registry.read_text())["repositories"], [])

    def test_apply_rejects_unborn_checkout_before_control_clone(self) -> None:
        unborn = self.root / "unborn"
        git("init", "-q", "-b", "feature", str(unborn))
        git("remote", "add", "origin", str(self.remote), cwd=unborn)
        with self.assertRaisesRegex(RuntimeError, "Git operation failed"):
            transactional_add.apply_repository_add(
                "Owner/demo", repository_id="demo",
                work_dir=unborn, control_dir=self.control,
                checkpoints_dir=self.checkpoints,
                registry_path=self.registry, catalog_path=self.catalog,
                home=self.home,
            )
        self.assertFalse(self.control.exists())
        self.assertEqual(json.loads(self.registry.read_text())["repositories"], [])

    def test_missing_binding_fails_closed_before_control_creation(self) -> None:
        self.catalog.write_text('{"version":1,"agents":[]}\n')
        baseline = self.registry.read_bytes()
        with self.assertRaisesRegex(ValueError, "no canonical agent binding"):
            self.apply()
        self.assertEqual(self.registry.read_bytes(), baseline)
        self.assertFalse(self.control.exists())

    def test_different_origin_rejected_and_user_changes_preserved(self) -> None:
        before = self.work_state()
        with mock.patch.object(checkout_identity, "normalize_remote_url",
                               return_value="Owner/other"):
            with self.assertRaisesRegex(RuntimeError, "origin mismatch"):
                self.apply()
        self.assertEqual(self.work_state(), before)
        self.assertFalse(self.control.exists())

    def test_concurrent_apply_lock_fails_without_any_checkout_write(self) -> None:
        baseline = self.registry.read_bytes()
        before = self.work_state()
        with transactional_add._registry_lock(self.registry):
            with self.assertRaisesRegex(RuntimeError, "registration is active"):
                self.apply()
        self.assertFalse(self.control.exists())
        self.assertEqual(self.registry.read_bytes(), baseline)
        self.assertEqual(self.work_state(), before)

    def test_new_managed_checkout_is_cloned_separately_from_owner_tree(self) -> None:
        before = self.work_state()
        managed = self.root / "managed-work"
        result = transactional_add.apply_repository_add(
            "Owner/demo", repository_id="demo", work_dir=managed,
            control_dir=self.control, checkpoints_dir=self.checkpoints,
            registry_path=self.registry, catalog_path=self.catalog, home=self.home,
        )
        self.assertEqual(result["work_mode"], "managed")
        self.assertTrue((managed / ".git").is_dir())
        self.assertEqual(git("branch", "--show-current", cwd=managed), "main")
        self.assertEqual(self.work_state(), before)
        self.assertEqual(json.loads(self.registry.read_text())["repositories"][0]["work_mode"],
                         "managed")

    def test_cli_add_without_apply_is_still_dry_run(self) -> None:
        baseline = self.registry.read_bytes()
        argv = [
            "admin.py", "--registry", str(self.registry),
            "add", "Owner/demo", "--repository-id", "demo",
            "--work-dir", str(self.work), "--control-dir", str(self.control),
            "--checkpoints-dir", str(self.checkpoints), "--catalog", str(self.catalog),
        ]
        output = io.StringIO()
        with mock.patch("sys.argv", argv), contextlib.redirect_stdout(output):
            self.assertEqual(admin.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["changes_applied"], False)
        self.assertFalse(self.control.exists())
        self.assertEqual(self.registry.read_bytes(), baseline)

    def test_preexisting_disabled_registration_is_not_reenabled_on_retry(self) -> None:
        self.registry.write_text(json.dumps({"version": 1, "repositories": [{
            "id": "demo", "repository": "Owner/demo", "enabled": False,
            "work_dir": str(self.work), "control_dir": str(self.control),
            "checkpoints_dir": str(self.checkpoints),
            "agent_binding": "12222222-2222-4222-8222-222222222222",
            "work_mode": "attached",
        }]}))
        before = self.registry.read_bytes()
        with self.assertRaisesRegex((ValueError, RuntimeError), "control"):
            self.apply()
        self.assertEqual(self.registry.read_bytes(), before)


    def test_crash_after_registry_fsync_reconciles_prepared_journal_on_retry(self) -> None:
        original = transactional_add._atomic_json
        before_owner = self.work_state()

        def crash_between_durable_writes(path, payload):
            original(path, payload)
            if path == self.registry:
                raise OSError("simulated crash after registry commit")

        with mock.patch.object(
            transactional_add, "_atomic_json", side_effect=crash_between_durable_writes,
        ):
            with self.assertRaisesRegex(OSError, "after registry commit"):
                self.apply()
        entry = json.loads(self.registry.read_text())["repositories"]
        self.assertEqual(len(entry), 1)
        journal = self.home / "pending-repository-add/demo.json"
        self.assertEqual(json.loads(journal.read_text())["state"], "prepared")
        self.assertEqual(self.work_state(), before_owner)

        with mock.patch.object(
            transactional_add, "_clone_to_destination",
            side_effect=AssertionError("retry must never re-clone"),
        ):
            result = self.apply()
        self.assertEqual(result["status"], "already_registered")
        self.assertFalse(result["changes_applied"])
        self.assertEqual(json.loads(journal.read_text())["state"], "committed")
        self.assertEqual(self.work_state(), before_owner)
        self.assertEqual(len(json.loads(self.registry.read_text())["repositories"]), 1)

    def test_registered_retry_rejects_mismatched_pending_journal(self) -> None:
        self.apply()
        before_owner = self.work_state()
        journal = self.home / "pending-repository-add/demo.json"
        payload = json.loads(journal.read_text())
        payload["state"] = "prepared"
        payload["agent_binding"] = "00000000-0000-4000-8000-000000000000"
        journal.write_text(json.dumps(payload))
        registry_before = self.registry.read_bytes()
        with self.assertRaisesRegex(ValueError, "journal identity mismatch"):
            self.apply()
        self.assertEqual(self.registry.read_bytes(), registry_before)
        self.assertEqual(self.work_state(), before_owner)

    def test_symlinked_pending_journal_is_never_read_or_followed(self) -> None:
        folder = self.home / "pending-repository-add"
        folder.mkdir()
        outside = self.root / "private.json"
        outside.write_text('{"secret":"preserve"}')
        (folder / "demo.json").symlink_to(outside)
        before = self.work_state()
        with self.assertRaises((OSError, ValueError)):
            self.apply()
        self.assertEqual(outside.read_text(), '{"secret":"preserve"}')
        self.assertEqual(self.work_state(), before)
        self.assertFalse(self.control.exists())
        self.assertEqual(json.loads(self.registry.read_text())["repositories"], [])


    def test_pre_replace_registry_file_fsync_failure_recovers_on_explicit_retry(self) -> None:
        # The prepared journal is durable; registry is still old when the
        # temporary registry file fails fsync before its atomic replace.
        original = transactional_add.os.fsync
        before = self.work_state()
        calls = 0

        def fail_registry_file_fsync(fd: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("simulated registry file fsync failure")
            original(fd)

        with mock.patch.object(transactional_add.os, "fsync", side_effect=fail_registry_file_fsync):
            with self.assertRaisesRegex(OSError, "registry file fsync"):
                self.apply()
        self.assertEqual(calls, 3)
        self.assertEqual(json.loads(self.registry.read_text())["repositories"], [])
        journal = self.home / "pending-repository-add" / "demo.json"
        self.assertEqual(json.loads(journal.read_text())["state"], "prepared")
        self.assertEqual(list(self.home.glob(".local-agent-write-*")), [])
        self.assertEqual(self.work_state(), before)
        self.assertEqual(self.apply()["status"], "registered")
        self.assertEqual(self.work_state(), before)

    def test_final_journal_parent_fsync_failure_has_safe_idempotent_retry(self) -> None:
        # A failure after the committed journal rename is uncertain to the
        # caller. Never repeat clone or mutate the owner's dirty checkout.
        original = transactional_add.os.fsync
        before = self.work_state()
        failed = False
        journal = self.home / "pending-repository-add" / "demo.json"
        journal_parent = journal.parent

        def fail_final_directory_fsync(fd: int) -> None:
            nonlocal failed
            if (not failed and journal.exists()
                    and json.loads(journal.read_text())["state"] == "committed"
                    and os.fstat(fd).st_ino == journal_parent.stat().st_ino):
                failed = True
                raise OSError("simulated committed journal directory fsync failure")
            original(fd)

        with mock.patch.object(transactional_add.os, "fsync", side_effect=fail_final_directory_fsync):
            with self.assertRaisesRegex(OSError, "committed journal directory fsync"):
                self.apply()
        self.assertTrue(failed)
        self.assertEqual(len(json.loads(self.registry.read_text())["repositories"]), 1)
        journal = self.home / "pending-repository-add" / "demo.json"
        self.assertEqual(json.loads(journal.read_text())["state"], "committed")
        with mock.patch.object(
            transactional_add, "_clone_to_destination",
            side_effect=AssertionError("uncertain fsync retry must not clone"),
        ):
            self.assertEqual(self.apply()["status"], "already_registered")
        self.assertEqual(self.work_state(), before)


    def test_symlinked_journal_parent_is_refused_before_registration(self) -> None:
        outside = self.root / "external-journals"
        outside.mkdir()
        (self.home / "pending-repository-add").symlink_to(
            outside, target_is_directory=True,
        )
        before = self.work_state()
        with self.assertRaises((OSError, ValueError)):
            self.apply()
        self.assertFalse((outside / "demo.json").exists())
        self.assertFalse(self.control.exists())
        self.assertEqual(json.loads(self.registry.read_text())["repositories"], [])
        self.assertEqual(self.work_state(), before)

    def test_journal_parent_swap_after_pin_cannot_redirect_atomic_write(self) -> None:
        journal_dir = self.home / "pending-repository-add"
        journal_dir.mkdir()
        external = self.root / "redirect-target"
        external.mkdir()
        payload = {"version": 1, "state": "prepared"}
        real_open = os.open
        swapped = False

        def swap_after_open(
            name: str, flags: int, mode: int = 0o777,
            *, dir_fd: int | None = None,
        ) -> int:
            nonlocal swapped
            fd = real_open(name, flags, mode, dir_fd=dir_fd)
            if name == "pending-repository-add" and flags & os.O_DIRECTORY and not swapped:
                swapped = True
                journal_dir.rename(self.home / "pinned-journals")
                journal_dir.symlink_to(external, target_is_directory=True)
            return fd

        with mock.patch.object(transactional_add.os, "open", side_effect=swap_after_open):
            transactional_add._atomic_json(journal_dir / "demo.json", payload)
        self.assertTrue(swapped)
        written = self.home / "pinned-journals" / "demo.json"
        self.assertEqual(json.loads(written.read_text()), payload)
        self.assertFalse((external / "demo.json").exists())
        self.assertEqual(json.loads(self.registry.read_text())["repositories"], [])


    def test_full_attached_repository_lifecycle_offline_e2e(self) -> None:
        """One real offline flow: add, execute twice, review, disable, detach."""
        from local_agent.foundation import core
        from local_agent.foundation.attached_checkout import execute_in_task_workspace
        from local_agent.repository.context import (
            default_registry_path, load_repository_registry,
        )
        from local_agent.repository.registration_detach import (
            apply_repository_detach, plan_repository_detach,
        )
        from local_agent.repository.registration_disable import (
            apply_repository_disable, plan_repository_disable,
        )
        from local_agent.repository.registration_status import repository_registration_status
        from local_agent.repository.snapshot_compare import compare_attached_snapshots
        from local_agent.repository.snapshot_review import inspect_attached_snapshots

        owner_before = self.work_state()
        catalog_before = self.catalog.read_bytes()
        self.assertEqual(self.apply()["status"], "registered")
        self.assertEqual(self.apply()["status"], "already_registered")
        self.assertEqual(self.work_state(), owner_before)
        self.assertEqual(
            repository_registration_status(
                registry_path=self.registry, home=self.home,
            )["repositories"][0]["work_mode"], "attached",
        )
        repository = load_repository_registry(
            home=self.home, path=self.registry,
        )[0]
        root = self.checkpoints / "attached-task-workspaces"

        # Use the actual task executor inside the attached workspace adapter.
        # A personal checkout must never enter the legacy reset/clean path.
        with (
            mock.patch.object(core, "WORK", self.work),
            mock.patch.object(core, "CHECKPOINTS", self.checkpoints),
            mock.patch.object(core, "WORK_PREPARATION_MODE", "managed"),
            mock.patch.object(core, "prepare_work", side_effect=AssertionError("owner reset")),
            mock.patch.object(core, "cleanup_work", side_effect=AssertionError("owner clean")),
        ):
            for task_id, body in (("lifecycle-first", "first\n"), ("lifecycle-second", "second\n")):
                task = {
                    "id": task_id, "mode": "commands", "work_branch": "feature",
                    "allow_write": True,
                    "writes": [{"path": "generated.txt", "content": body}],
                }
                outcome = execute_in_task_workspace(
                    repository, core, task,
                    lambda payload, **_kwargs: core.process_task(payload),
                )
                self.assertEqual(outcome["status"], "done", outcome)
                self.assertEqual(self.work_state(), owner_before)
                self.assertFalse((self.work / "generated.txt").exists())

        retained = sorted(root.iterdir())
        self.assertEqual(len(retained), 2)
        snapshots = inspect_attached_snapshots(self.checkpoints)["snapshots"]
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(
            {record["review_state"] for record in snapshots},
            {"completed_with_changes"},
        )
        self.assertTrue(all(not record["promotion_allowed"] for record in snapshots))
        comparison = compare_attached_snapshots(
            self.checkpoints, retained[0].name, retained[1].name,
        )
        self.assertEqual(comparison["changed_paths"], [
            {"path": "generated.txt", "change": "modified"},
        ])
        self.assertFalse(comparison["promotion_allowed"])
        self.assertFalse(comparison["owner_checkout_read"])

        # Mimic a stopped/drained disposable installation; do not touch B1c.
        state = default_registry_path(self.home).parent
        state.mkdir(parents=True)
        (state / "agentd.lock").write_text("offline fixture\n")
        for name in ("claims", "corrupt-claims", "result-spool"):
            (state / name).mkdir()
        disable_plan = plan_repository_disable(
            "demo", home=self.home, registry_path=self.registry,
        )
        disabled = apply_repository_disable(
            "demo", home=self.home, registry_path=self.registry,
            expected_sha256=disable_plan["expected_sha256"],
        )
        self.assertEqual(disabled["status"], "disabled")
        self.assertEqual(
            repository_registration_status(
                registry_path=self.registry, home=self.home,
            )["repositories"][0]["enabled"], False,
        )
        self.assertEqual(len(inspect_attached_snapshots(self.checkpoints)["snapshots"]), 2)
        detach_plan = plan_repository_detach(
            "demo", home=self.home, registry_path=self.registry,
        )
        detached = apply_repository_detach(
            "demo", home=self.home, registry_path=self.registry,
            expected_sha256=detach_plan["expected_sha256"],
        )
        self.assertEqual(detached["status"], "detached")
        self.assertEqual(json.loads(self.registry.read_text())["repositories"], [])
        self.assertEqual(self.work_state(), owner_before)
        self.assertEqual(self.catalog.read_bytes(), catalog_before)
        self.assertTrue((self.control / ".agent/binding.json").is_file())
        self.assertEqual(len(list(root.iterdir())), 2)
        self.assertEqual(
            json.loads((self.home / "pending-repository-add/demo.json").read_text())["state"],
            "committed",
        )
        self.assertFalse((self.work / "generated.txt").exists())

if __name__ == "__main__":
    unittest.main()
