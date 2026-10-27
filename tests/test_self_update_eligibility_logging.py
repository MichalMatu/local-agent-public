"""Transition-only diagnostics for safely ineligible self-update checkouts."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.daemon import service


class SelfUpdateEligibilityLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name) / "installed"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.root = mock.patch.object(service, "SELF_REPO", self.repo)
        self.root.start()
        self.addCleanup(self.root.stop)
        self.state = mock.patch.object(service, "_last_self_update_skip_reason", None)
        self.state.start()
        self.addCleanup(self.state.stop)
        self.clock = mock.patch.object(service, "_last_self_update_check", 0.0)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    @staticmethod
    def _git_ok(_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, stdout="")

    def test_pinned_checkout_logs_once_no_fetch_even_when_forced(self) -> None:
        with mock.patch.object(service, "self_repo_on_main_branch", return_value=False), \
             mock.patch.object(service, "tracked_self_repo_clean") as clean, \
             mock.patch.object(service, "_git") as git, \
             mock.patch.object(service, "log") as log:
            self.assertFalse(service._self_update_transaction(force=True))
            self.assertFalse(service._self_update_transaction(force=True))
            log.assert_called_once_with(
                "self-update disabled: checkout is not on main (pinned source)"
            )
            clean.assert_not_called()
            git.assert_not_called()

    def test_transition_pinned_dirty_clean_and_back_uses_one_log_per_change(self) -> None:
        with mock.patch.object(service, "self_repo_on_main_branch", side_effect=[False, True, True, True, False]), \
             mock.patch.object(service, "tracked_self_repo_clean", side_effect=[False, False, True]), \
             mock.patch.object(service, "_git", side_effect=[
                 self._git_ok(None),
                 subprocess.CompletedProcess([], 0, stdout="a" * 40),
                 subprocess.CompletedProcess([], 0, stdout="a" * 40),
             ]) as git, \
             mock.patch.object(service, "REJECTED_UPDATE_PATH", self.repo / "rejected.json"), \
             mock.patch.object(service, "log") as log:
            for _ in range(5):
                self.assertFalse(service._self_update_transaction(force=True))
            self.assertEqual(
                [args.args[0] for args in log.call_args_list],
                [
                    "self-update disabled: checkout is not on main (pinned source)",
                    "self-update paused: checkout is not clean",
                    "self-update checks resumed: local checkout eligible",
                    "self-update disabled: checkout is not on main (pinned source)",
                ],
            )
            self.assertEqual(git.call_count, 3)

    def test_non_fast_forward_is_not_repeated_and_eligibility_returns(self) -> None:
        count = {"diverged": 0}
        def fake_git(args, **_kwargs):
            if args[1] == "fetch":
                return self._git_ok(args)
            if args[1] == "rev-parse":
                if args[2] == "HEAD":
                    return subprocess.CompletedProcess(args, 0, stdout="a" * 40)
                count["diverged"] += 1
                return subprocess.CompletedProcess(
                    args, 0, stdout=("b" if count["diverged"] < 3 else "a") * 40,
                )
            if args[1] == "merge-base":
                return subprocess.CompletedProcess(args, 1, stdout="")
            raise AssertionError(args)

        with mock.patch.object(service, "self_repo_on_main_branch", return_value=True), \
             mock.patch.object(service, "tracked_self_repo_clean", return_value=True), \
             mock.patch.object(service, "_read_rejected_update", return_value={}), \
             mock.patch.object(service, "_git", side_effect=fake_git), \
             mock.patch.object(service, "REJECTED_UPDATE_PATH", self.repo / "rejected.json"), \
             mock.patch.object(service, "log") as log:
            for _ in range(3):
                self.assertFalse(service._self_update_transaction(force=True))
            self.assertEqual(
                [call.args[0] for call in log.call_args_list],
                [
                    "self-update paused: remote main is not a fast-forward",
                    "self-update checks resumed: local checkout eligible",
                ],
            )

    def test_poll_interval_still_limits_logging_and_forced_checks_do_not_bypass_gate(self) -> None:
        with mock.patch.object(service.time, "monotonic", side_effect=[1000.0, 1001.0, 1060.1]), \
             mock.patch.object(service, "self_repo_on_main_branch", return_value=False) as branch, \
             mock.patch.object(service, "log") as log:
            for force in [False, False, False]:
                self.assertFalse(service._self_update_transaction(force=force))
            self.assertEqual(branch.call_count, 2)
            log.assert_called_once_with(
                "self-update disabled: checkout is not on main (pinned source)"
            )

    def test_missing_git_metadata_reports_once_and_never_fetches(self) -> None:
        (self.repo / ".git").rmdir()
        with mock.patch.object(service, "_git") as git, \
             mock.patch.object(service, "log") as log:
            self.assertFalse(service._self_update_transaction(force=True))
            self.assertFalse(service._self_update_transaction(force=True))
            git.assert_not_called()
            log.assert_called_once_with(
                "self-update disabled: installed checkout has no .git metadata"
            )


if __name__ == "__main__":
    unittest.main()
