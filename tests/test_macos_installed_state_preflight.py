"""macOS installed preflight is read-only; no source-switch authority."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from scripts import macos_installed_state_preflight as proof


@unittest.skipUnless(sys.platform == "darwin", "Mac-only acceptance")
class InstalledMacPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="la-mac-installed-readonly-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve(strict=True)
        self.state = self.home / "Library/Application Support/local-agent"
        self.make_dir(self.state)
        self.entries = []
        self.ids = tuple(f"project-{x}" for x in range(9))
        for idx, name in enumerate(self.ids):
            root = self.home / "agent-workspace" / "repos" / name
            control = root / "control"
            work = root / "work"
            checkpoints = root / "checkpoints"
            for folder in (control / ".git", work / ".git", checkpoints):
                self.make_dir(folder)
            host_enabled = idx == 3
            mailbox = control / ".agent"
            repo_state = self.state / "repositories" / name
            if host_enabled:
                mailbox = mailbox / "hosts" / "mac-fixture"
                repo_state = repo_state / "hosts" / "mac-fixture"
            for folder in (mailbox / "tasks", mailbox / "results",
                           repo_state / "claims", repo_state / "result-spool"):
                self.make_dir(folder)
            entry = {"id": name, "repository": f"example-owner/fake-{idx}",
                     "enabled": True, "control_dir": str(control),
                     "work_dir": str(work),
                     "checkpoints_dir": str(checkpoints)}
            if host_enabled:
                entry["host_id"] = "mac-fixture"
                entry["host_mailbox_enabled"] = True
            self.entries.append(entry)
        for category, names in (("host-tasks", ("requests", "results", "claims",
                                                "remote-approvals")),
                                ("conversation-operator", ("requests", "results",
                                                           "launches"))):
            for name in names:
                self.make_dir(self.state / category / name)
        self.save_registry()
        self.write_json(self.state / "status.json", {
            "version": 1, "state": "idle", "pid": 3211,
            "self_revision": proof.EXPECTED_BASE_SHA,
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        })

    def make_dir(self, path: Path):
        path.mkdir(parents=True, exist_ok=True)
        node = path
        while node != self.home and self.home in node.parents:
            node.chmod(0o700)
            node = node.parent

    def write_json(self, path: Path, content: dict):
        path.write_text(json.dumps(content, sort_keys=True) + "\n")
        path.chmod(0o600)

    def save_registry(self):
        self.write_json(self.state / "repositories.json", {
            "version": 1, "repositories": self.entries,
        })

    def inspect(self):
        return proof.inspect_installed_mac(home=self.home, expected_project_count=9)

    def test_nine_projects_and_host_mailbox_are_observed_without_write(self):
        original_registry = (self.state / "repositories.json").read_bytes()
        report = self.inspect()
        self.assertEqual(report["project_count"], 9)
        self.assertEqual(report["platform"], "macos")
        self.assertTrue(report["read_only"])
        self.assertTrue(report["status_idle_fresh"])
        self.assertEqual(report["standalone_host_intake"], "absent")
        self.assertTrue(report["projects"]["project-3"]["host_mailbox"])
        self.assertEqual(report["projects"]["project-3"]["tasks"], 0)
        self.assertFalse(report["source_switch_authorized"])
        self.assertFalse(report["unknown_no_replay_proven"])
        self.assertFalse(report["installed_release_admission_proven"])
        self.assertEqual(report["actions_performed"], [])
        self.assertEqual((self.state / "repositories.json").read_bytes(), original_registry)
        self.assertFalse((self.state / "macos-release-admission.lock").exists())

    def test_remote_pending_is_observed_not_executed(self):
        path = self.home / "agent-workspace/repos/project-3/control/.agent/hosts/mac-fixture/tasks/new.json"
        self.write_json(path, {"id": "new"})
        report = self.inspect()
        self.assertEqual(report["projects"]["project-3"]["pending"], 1)
        self.assertTrue(path.exists())
        self.assertEqual(report["actions_performed"], [])

    def test_project_ledger_absence_is_unknown_not_empty(self):
        missing = self.state / "repositories/project-2/result-spool"
        missing.rmdir()
        report = self.inspect()
        self.assertIsNone(report["projects"]["project-2"]["spool"])
        self.assertIn("unverified_project_spool", report["blockers"])

    def test_gap_diagnostics_reveal_causes_not_private_paths(self):
        (self.state / "repositories/project-2/result-spool").rmdir()
        (self.state / "host-tasks/claims").chmod(0o755)
        report = self.inspect()
        gaps = report["observational_gap_causes"]
        self.assertEqual(gaps["missing_directory"], 1)
        self.assertEqual(gaps["readable_legacy_mode_not_release_private"], 1)
        self.assertEqual(report["local_host"]["claims"], 0)
        self.assertIn("host_claims_mode_not_private_for_release", report["blockers"])
        self.assertNotIn(str(self.home), json.dumps(gaps))

    def test_symlinked_git_metadata_rejected(self):
        gitdir = self.home / "agent-workspace/repos/project-2/control/.git"
        gitdir.rmdir()
        gitdir.symlink_to(self.state, target_is_directory=True)
        report = self.inspect()
        self.assertIn("invalid_project_checkout", report["blockers"])
        self.assertFalse(report["source_switch_authorized"])

    def test_empty_gitkeep_is_explicitly_recognized_not_counted_as_task(self):
        folder = self.home / "agent-workspace/repos/project-4/control/.agent/tasks"
        (folder / ".gitkeep").write_text("")
        report = self.inspect()
        self.assertEqual(report["projects"]["project-4"]["tasks"], 0)
        self.assertEqual(report["projects"]["project-4"]["tasks_placeholders"], 1)
        self.assertEqual(report["projects"]["project-4"]["tasks_payload_directories"], 0)
        self.assertNotIn("unverified_project_tasks", report["blockers"])
        self.assertFalse(report["source_switch_authorized"])

    def test_source_linked_payload_directory_is_observational_only(self):
        folder = self.home / "agent-workspace/repos/project-4/control/.agent/tasks"
        self.write_json(folder / "task-1.json", {"id": "task-1"})
        self.make_dir(folder / "task-1.payload")
        report = self.inspect()
        self.assertEqual(report["projects"]["project-4"]["tasks"], 1)
        self.assertEqual(report["projects"]["project-4"]["tasks_payload_directories"], 1)
        self.assertEqual(report["projects"]["project-4"]["pending"], 1)
        self.assertIn("unverified_externalized_payload_contents", report["blockers"])
        self.assertFalse(report["unknown_no_replay_proven"])

    def test_orphan_payload_directory_remains_unknown(self):
        folder = self.home / "agent-workspace/repos/project-4/control/.agent/tasks"
        self.make_dir(folder / "old-removed-task.payload")
        report = self.inspect()
        self.assertIsNone(report["projects"]["project-4"]["tasks"])
        self.assertIn("unverified_project_tasks", report["blockers"])
        self.assertEqual(report["observational_gap_causes"]["orphan_payload_directory"], 1)
        self.assertFalse(report["source_switch_authorized"])

    def test_multiple_orphan_payloads_counted_without_read_or_delete(self):
        folder = self.home / "agent-workspace/repos/project-4/control/.agent/tasks"
        for item in ("first", "second", "third"):
            target = folder / f"{item}.payload"
            self.make_dir(target)
            (target / "unknown.txt").write_text("keep untouched")
        report = self.inspect()
        self.assertIsNone(report["projects"]["project-4"]["tasks"])
        self.assertEqual(report["observational_gap_causes"]["orphan_payload_directory"], 3)
        for item in ("first", "second", "third"):
            self.assertEqual(
                (folder / f"{item}.payload/unknown.txt").read_text(), "keep untouched",
            )
        self.assertEqual(report["actions_performed"], [])
        self.assertFalse(report["source_switch_authorized"])

    def test_orphan_directory_only_shape_matches_file_only_runtime_gc(self):
        folder = self.home / "agent-workspace/repos/project-4/control/.agent/tasks"
        payload = folder / "retired.payload/commands"
        self.make_dir(payload)
        report = self.inspect()
        self.assertEqual(report["observational_gap_causes"]["orphan_payload_directory"], 1)
        self.assertEqual(report["observational_gap_causes"]["orphan_payload_directories_only"], 1)
        self.assertEqual(report["actions_performed"], [])
        self.assertTrue(payload.exists())

    def test_orphan_empty_and_file_bearing_trees_stay_separate(self):
        folder = self.home / "agent-workspace/repos/project-4/control/.agent/tasks"
        self.make_dir(folder / "empty.payload")
        self.make_dir(folder / "retained.payload/commands")
        (folder / "retained.payload/commands/001.sh").write_text("never execute")
        report = self.inspect()
        gaps = report["observational_gap_causes"]
        self.assertEqual(gaps["orphan_payload_directory"], 2)
        self.assertEqual(gaps["orphan_payload_empty"], 1)
        self.assertEqual(gaps["orphan_payload_files_present"], 1)
        self.assertEqual((folder / "retained.payload/commands/001.sh").read_text(),
                         "never execute")
        self.assertFalse(report["source_switch_authorized"])

    def test_orphan_symlink_child_yields_untrusted_not_empty(self):
        folder = self.home / "agent-workspace/repos/project-4/control/.agent/tasks"
        payload = folder / "untrusted.payload"
        self.make_dir(payload)
        (payload / "redirect").symlink_to(self.state / "status.json")
        report = self.inspect()
        gaps = report["observational_gap_causes"]
        self.assertEqual(gaps["orphan_payload_directory"], 1)
        self.assertEqual(gaps["orphan_payload_untrusted"], 1)
        self.assertIsNone(report["projects"]["project-4"]["tasks"])

    def test_arbitrary_non_json_file_remains_blocked(self):
        folder = self.home / "agent-workspace/repos/project-4/control/.agent/tasks"
        (folder / ".DS_Store").write_text("unknown")
        report = self.inspect()
        self.assertIsNone(report["projects"]["project-4"]["tasks"])
        self.assertEqual(report["observational_gap_causes"]["non_json_metadata_file"], 1)

    def test_nonempty_or_symlinked_gitkeep_fails_closed(self):
        folder = self.home / "agent-workspace/repos/project-4/control/.agent/tasks"
        placeholder = folder / ".gitkeep"
        placeholder.write_text("unexpected")
        report = self.inspect()
        self.assertIsNone(report["projects"]["project-4"]["tasks"])
        self.assertIn("unsafe_git_mailbox_auxiliary", report["observational_gap_causes"])
        placeholder.unlink()
        placeholder.symlink_to(self.state / "status.json")
        report = self.inspect()
        self.assertIsNone(report["projects"]["project-4"]["tasks"])
        self.assertIn("symlink_route_or_entry", report["observational_gap_causes"])

    def test_result_payload_subdirectory_not_whitelisted(self):
        folder = self.home / "agent-workspace/repos/project-4/control/.agent/results"
        self.write_json(folder / "task-1.json", {"id": "task-1", "status": "done"})
        self.make_dir(folder / "task-1.payload")
        report = self.inspect()
        self.assertIsNone(report["projects"]["project-4"]["results"])
        self.assertIn("unexpected_nested_directory", report["observational_gap_causes"])

    def test_nested_result_directory_is_not_treated_as_a_file(self):
        folder = self.home / "agent-workspace/repos/project-7/control/.agent/results"
        self.make_dir(folder / "old")
        report = self.inspect()
        self.assertIsNone(report["projects"]["project-7"]["results"])
        self.assertEqual(report["observational_gap_causes"]["unexpected_nested_directory"], 1)

    def test_intermediate_git_agent_symlink_rejected_before_inspection(self):
        folder = self.home / "agent-workspace/repos/project-1/control/.agent"
        target = self.home / "moved-agent-metadata"
        folder.rename(target)
        folder.symlink_to(target, target_is_directory=True)
        report = self.inspect()
        self.assertIsNone(report["projects"]["project-1"]["tasks"])
        self.assertIsNone(report["projects"]["project-1"]["results"])
        self.assertGreaterEqual(report["observational_gap_causes"]["symlink_route_or_entry"], 2)

    def test_intermediate_local_project_state_symlink_rejected(self):
        parent = self.state / "repositories/project-2"
        target = self.home / "moved-local-state"
        parent.rename(target)
        parent.symlink_to(target, target_is_directory=True)
        report = self.inspect()
        self.assertIsNone(report["projects"]["project-2"]["claims"])
        self.assertIsNone(report["projects"]["project-2"]["spool"])
        self.assertGreaterEqual(report["observational_gap_causes"]["symlink_route_or_entry"], 2)

    def test_group_writable_intermediate_state_rejected(self):
        parent = self.state / "repositories/project-2"
        parent.chmod(0o770)
        report = self.inspect()
        self.assertIsNone(report["projects"]["project-2"]["claims"])
        self.assertIn("unsafe_route_ancestor", report["observational_gap_causes"])

    def test_owned_readable_legacy_project_claims_are_only_observational(self):
        claims = self.state / "repositories/project-6/claims"
        claims.chmod(0o755)
        self.write_json(claims / "unknown.json", {"state": "unknown"})
        report = self.inspect()
        self.assertEqual(report["projects"]["project-6"]["claims"], 1)
        self.assertIn("project_claims_mode_not_private_for_release", report["blockers"])
        self.assertFalse(report["unknown_no_replay_proven"])
        self.assertFalse(report["source_switch_authorized"])

    def test_group_writable_legacy_claim_directory_stays_unverified(self):
        claims = self.state / "repositories/project-6/claims"
        claims.chmod(0o775)
        report = self.inspect()
        self.assertIsNone(report["projects"]["project-6"]["claims"])
        self.assertIn("unverified_project_claims", report["blockers"])
        self.assertFalse(report["source_switch_authorized"])

    def test_owner_readable_legacy_conversation_ledger_is_not_release_ready(self):
        requests = self.state / "conversation-operator/requests"
        requests.chmod(0o755)
        report = self.inspect()
        self.assertEqual(report["conversation"]["requests"], 0)
        self.assertIn("conversation_requests_mode_not_private_for_release", report["blockers"])
        self.assertFalse(report["source_switch_authorized"])

    def test_local_host_missing_claim_dir_blocks(self):
        (self.state / "host-tasks/claims").rmdir()
        report = self.inspect()
        self.assertIsNone(report["local_host"]["claims"])
        self.assertIn("unverified_host_claims", report["blockers"])

    def test_conversation_launch_ledger_is_observed(self):
        self.write_json(self.state / "conversation-operator/launches/one.json",
                        {"state": "reserved"})
        report = self.inspect()
        self.assertEqual(report["conversation"]["launches"], 1)
        self.assertFalse(report["unknown_no_replay_proven"])

    def test_readable_state_root_does_not_grant_release_trust(self):
        self.state.chmod(0o755)
        report = self.inspect()
        self.assertEqual(report["project_count"], 9)
        self.assertIn("state_root_not_private_for_release", report["blockers"])
        self.assertFalse(report["source_switch_authorized"])
        self.assertEqual(report["actions_performed"], [])

    def test_readable_registry_is_observable_but_release_blocked(self):
        (self.state / "repositories.json").chmod(0o644)
        report = self.inspect()
        self.assertEqual(report["project_count"], 9)
        self.assertIn("registry_not_private_for_release", report["blockers"])
        self.assertFalse(report["installed_release_admission_proven"])

    def test_untrusted_writable_registry_is_never_read_as_routing_authority(self):
        registry = self.state / "repositories.json"
        registry.chmod(0o666)
        with self.assertRaisesRegex(proof.InstalledMacPreflightBlocked,
                                    "registry writable"):
            self.inspect()

    def test_group_writable_state_root_is_rejected(self):
        self.state.chmod(0o775)
        with self.assertRaisesRegex(proof.InstalledMacPreflightBlocked,
                                    "owner/write"):
            self.inspect()

    def test_registry_missing_or_count_mismatch_fails_closed(self):
        self.entries.pop()
        self.save_registry()
        with self.assertRaisesRegex(proof.InstalledMacPreflightBlocked,
                                    "registry inventory mismatch"):
            self.inspect()
        (self.state / "repositories.json").unlink()
        with self.assertRaisesRegex(proof.InstalledMacPreflightBlocked,
                                    "missing readable"):
            self.inspect()

    def test_duplicate_registry_keys_rejected(self):
        reg = self.state / "repositories.json"
        reg.write_text('{"version":1,"version":1,"repositories":[]}\n')
        with self.assertRaisesRegex(proof.InstalledMacPreflightBlocked, "duplicate"):
            self.inspect()

    def test_symlinked_worktree_rejected_before_resolution(self):
        original = self.home / "agent-workspace/repos/project-5"
        target = self.home / "moved-project"
        original.rename(target)
        original.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(proof.InstalledMacPreflightBlocked,
                                    "unsafe Mac registry raw path"):
            self.inspect()

    def test_symlinked_git_mailbox_does_not_become_empty(self):
        route = self.home / "agent-workspace/repos/project-1/control/.agent/tasks"
        route.rmdir()
        route.symlink_to(self.state / "host-tasks/results", target_is_directory=True)
        report = self.inspect()
        self.assertIsNone(report["projects"]["project-1"]["tasks"])
        self.assertIn("unverified_project_tasks", report["blockers"])

    def test_stale_wrong_revision_status_cannot_prove_idle(self):
        self.write_json(self.state / "status.json", {
            "state": "idle", "pid": 99, "updated_at": "2020-01-01T00:00:00+00:00",
            "self_revision": "another-revision",
        })
        report = self.inspect()
        self.assertFalse(report["status_idle_fresh"])
        self.assertIn("installed_supervisor_status_not_verified", report["blockers"])

    def test_insecure_local_status_is_observed_not_authorized(self):
        (self.state / "status.json").chmod(0o644)
        report = self.inspect()
        self.assertTrue(report["read_only"])
        self.assertFalse(report["source_switch_authorized"])

    def test_symlinked_registry_rejected(self):
        registry = self.state / "repositories.json"
        spare = self.home / "registry-backup.json"
        spare.write_bytes(registry.read_bytes())
        registry.unlink()
        registry.symlink_to(spare)
        with self.assertRaisesRegex(proof.InstalledMacPreflightBlocked, "symlinked"):
            self.inspect()

    def test_host_intake_presence_does_not_enable_or_modify_it(self):
        host = self.state / "host-task-intake.json"
        self.write_json(host, {"version": 1, "enabled": True})
        report = self.inspect()
        self.assertEqual(report["standalone_host_intake"], "unverified")
        self.assertTrue(host.exists())
        self.assertFalse(report["source_switch_authorized"])

    def test_invalid_arg_or_nondarwin_rejected(self):
        with self.assertRaises(proof.InstalledMacPreflightBlocked):
            proof.inspect_installed_mac(home=self.home, expected_project_count=0)
        with mock.patch.object(proof.sys, "platform", "linux"):
            with self.assertRaisesRegex(proof.InstalledMacPreflightBlocked, "macOS-only"):
                self.inspect()


if __name__ == "__main__":
    unittest.main()
