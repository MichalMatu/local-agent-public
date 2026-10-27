"""MVP fixed/adaptive polling and terminal menu without daemon side effects."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from local_agent.cli import polling as cli_polling
from local_agent.cli.diagnostics import build_parser
from local_agent.supervisor import polling, scheduling


class PollingMvpTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.state.chmod(0o700)

    def test_absent_config_keeps_existing_adaptive_policy(self) -> None:
        settings = polling.read_polling_settings(self.state)
        self.assertEqual(settings.mode, "adaptive")
        self.assertEqual(polling.repository_interval(settings, None, 100.0), 15.0)
        self.assertEqual(polling.repository_interval(settings, 90.0, 100.0), 2.0)
        self.assertEqual(polling.repository_interval(settings, 50.0, 100.0), 5.0)
        self.assertEqual(polling.control_interval(settings, None, 100.0), 15.0)
        self.assertEqual(polling.host_interval(settings, None, 100.0), 5.0)
        self.assertFalse((self.state / polling.PREFERENCE_FILE).exists())

    def test_adaptive_host_intake_refreshes_actual_git_control_snapshot(self) -> None:
        settings = polling.PollingSettings()
        self.assertEqual(
            polling.control_interval(settings, None, 100.0), 15.0,
        )
        self.assertEqual(
            polling.control_interval(settings, None, 100.0, host_intake_enabled=True), 5.0,
        )
        self.assertEqual(
            polling.control_interval(settings, 99.0, 100.0, host_intake_enabled=True), 2.0,
        )
        self.assertEqual(
            polling.control_interval(settings, 50.0, 100.0, host_intake_enabled=True), 5.0,
        )

    def test_fixed_owner_interval_is_not_changed_by_host_intake(self) -> None:
        settings = polling.PollingSettings("fixed", 30)
        self.assertEqual(
            polling.control_interval(settings, None, 100.0, host_intake_enabled=True), 30.0,
        )
        live = polling.LivePollingPreferences(self.state, lambda _: None)
        polling.save_polling_settings(self.state, settings)
        live.refresh({}, 0, 10, 100.0)
        self.assertEqual(live.control(100.0, host_intake_enabled=True), 30.0)

    def test_polling_host_intake_guard_is_fail_closed(self) -> None:
        from local_agent.supervisor import host_remote_worker
        with mock.patch.object(host_remote_worker, "load_remote_intake_config",
                               return_value=object()) as loader:
            self.assertFalse(
                host_remote_worker.host_intake_polling_enabled(self.state, None))
            loader.assert_not_called()
            self.assertTrue(
                host_remote_worker.host_intake_polling_enabled(self.state, object()))
        with mock.patch.object(host_remote_worker, "load_remote_intake_config",
                               side_effect=ValueError("invalid intake config")):
            self.assertFalse(
                host_remote_worker.host_intake_polling_enabled(self.state, object()))

    def test_fixed_applies_same_interval_to_project_control_and_host(self) -> None:
        settings = polling.PollingSettings("fixed", 30)
        self.assertEqual(polling.repository_interval(settings, 98.0, 100.0), 30)
        self.assertEqual(polling.control_interval(settings, 98.0, 100.0), 30)
        self.assertEqual(polling.host_interval(settings, 98.0, 100.0), 30)

    def test_schedule_uses_fixed_mode(self) -> None:
        schedule = scheduling.RepositorySchedule(last_poll_at=90.0, last_activity_at=99)
        settings = polling.PollingSettings("fixed", 30)
        self.assertFalse(scheduling.repository_due(schedule, 100.0, settings))
        self.assertEqual(scheduling.next_repository_delay(
            {"a": schedule}, ["a"], 100.0, settings,
        ), 20)
        self.assertTrue(scheduling.repository_due(schedule, 120.0, settings))

    def test_save_and_read_persists_new_mode(self) -> None:
        polling.save_polling_settings(self.state, polling.PollingSettings("fixed", 45))
        self.assertEqual(polling.read_polling_settings(self.state),
                         polling.PollingSettings("fixed", 45))
        self.assertEqual((self.state / polling.PREFERENCE_FILE).stat().st_mode & 0o777, 0o600)
        polling.save_polling_settings(self.state, polling.PollingSettings())
        self.assertEqual(polling.read_polling_settings(self.state).mode, "adaptive")

    def test_invalid_seconds_and_modes_do_not_change_disk(self) -> None:
        polling.save_polling_settings(self.state, polling.PollingSettings("fixed", 30))
        before = (self.state / polling.PREFERENCE_FILE).read_bytes()
        for mode, seconds in (("fixed", 1), ("fixed", 301), ("adaptive", True),
                              ("bad", 15), ("fixed", 1.5)):
            with self.subTest(mode=mode, seconds=seconds):
                with self.assertRaises(ValueError):
                    polling.save_polling_settings(
                        self.state, polling.PollingSettings(mode, seconds),
                    )
                self.assertEqual((self.state / polling.PREFERENCE_FILE).read_bytes(), before)

    def test_corrupt_or_duplicate_json_is_rejected(self) -> None:
        path = self.state / polling.PREFERENCE_FILE
        for data in (b"not json", b"{}",
                     b'{"version":1,"mode":"fixed","seconds":15,"seconds":20}'):
            with self.subTest(data=data):
                path.write_bytes(data)
                with self.assertRaises(ValueError):
                    polling.read_polling_settings(self.state)

    def test_symlink_does_not_get_read_or_overwritten(self) -> None:
        target = self.state / "target.json"
        target.write_text("leave alone")
        link = self.state / polling.PREFERENCE_FILE
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            polling.read_polling_settings(self.state)
        with self.assertRaises(ValueError):
            polling.save_polling_settings(self.state, polling.PollingSettings("fixed", 15))
        self.assertEqual(target.read_text(), "leave alone")

    def test_existing_macos_owner_state_directory_0755_is_accepted(self) -> None:
        self.state.chmod(0o755)
        polling.save_polling_settings(self.state, polling.PollingSettings("fixed", 30))
        self.assertEqual(polling.read_polling_settings(self.state).seconds, 30)
        self.assertEqual((self.state / polling.PREFERENCE_FILE).stat().st_mode & 0o777, 0o600)

    def test_world_writable_state_root_rejected(self) -> None:
        self.state.chmod(0o777)
        with self.assertRaises(ValueError):
            polling.read_polling_settings(self.state)

    def test_cli_show_fixed_and_adaptive(self) -> None:
        for verb, kw in (("show", {}), ("fixed", {"seconds": 20}), ("adaptive", {})):
            with self.subTest(verb=verb), redirect_stdout(io.StringIO()) as out:
                rc = cli_polling.command_polling(
                    argparse.Namespace(polling_command=verb, state_dir=self.state, **kw),
                )
                self.assertEqual(rc, 0)
                doc = json.loads(out.getvalue())
                self.assertTrue(doc["ok"])
                self.assertFalse(doc["requires_restart"])
                self.assertEqual(doc["mode"], "fixed" if verb == "fixed" else "adaptive")

    def test_cli_refuses_invalid_fixed_interval(self) -> None:
        with redirect_stdout(io.StringIO()) as out:
            rc = cli_polling.command_polling(
                argparse.Namespace(polling_command="fixed", seconds=1, state_dir=self.state),
            )
        self.assertEqual(rc, 2)
        self.assertFalse(json.loads(out.getvalue())["ok"])

    def test_cli_parser_registers_menu_doctor_and_polling(self) -> None:
        parser = build_parser()
        for args in (("doctor",), ("menu", "--list"), ("polling", "show"),
                     ("polling", "fixed", "--seconds", "20"), ("polling", "adaptive")):
            with self.subTest(args=args):
                self.assertIsNotNone(parser.parse_args(args).func)

    def test_menu_list_requires_no_tty_or_side_effects(self) -> None:
        with redirect_stdout(io.StringIO()) as out:
            rc = cli_polling.command_menu(argparse.Namespace(list=True, state_dir=self.state))
        self.assertEqual(rc, 0)
        self.assertIn("Doctor", out.getvalue())
        self.assertIn("Polling", out.getvalue())

    def test_menu_list_exposes_overview_without_renumbering_existing_actions(self) -> None:
        with (mock.patch("local_agent.cli.overview.command_overview",
                         side_effect=AssertionError("menu --list must not probe")),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(cli_polling.command_menu(
                argparse.Namespace(list=True, state_dir=self.state),
            ), 0)
        listing = output.getvalue().splitlines()
        self.assertEqual(listing, [
            "1. Status", "2. Doctor", "3. Polling",
            "4. Repositories", "5. Overview", "0. Exit",
        ])

    def test_overview_menu_is_read_only_and_can_return_unverified(self) -> None:
        with (mock.patch("sys.stdin") as stdin,
              mock.patch("builtins.input", side_effect=["5", "0"]),
              mock.patch("local_agent.cli.overview.command_overview",
                         return_value=2) as observe,
              mock.patch("local_agent.supervisor.polling.save_polling_settings",
                         side_effect=AssertionError("no polling write")),
              redirect_stdout(io.StringIO()) as output):
            stdin.isatty.return_value = True
            self.assertEqual(cli_polling.command_menu(
                argparse.Namespace(
                    list=False, state_dir=self.state,
                    registry_file=self.state / "repositories.json",
                ),
            ), 0)
        observe.assert_called_once()
        request = observe.call_args.args[0]
        self.assertTrue(request.human)
        self.assertFalse(request.with_host_resources)
        self.assertEqual(request.state_root, self.state)
        self.assertEqual(request.status_file, self.state / "status.json")
        self.assertEqual(request.registry_file, self.state / "repositories.json")
        self.assertEqual(request.max_age_seconds, 180)
        self.assertFalse((self.state / polling.PREFERENCE_FILE).exists())
        self.assertIn("5. Overview", output.getvalue())

    def test_menu_drives_adaptive_and_fixed_through_same_cli(self) -> None:
        with (mock.patch("sys.stdin") as stdin,
              mock.patch("builtins.input", side_effect=["3", "3", "25", "2", "0", "0"]),
              redirect_stdout(io.StringIO())):
            stdin.isatty.return_value = True
            rc = cli_polling.command_menu(
                argparse.Namespace(list=False, state_dir=self.state),
            )
        self.assertEqual(rc, 0)
        self.assertEqual(polling.read_polling_settings(self.state).mode, "adaptive")

    def test_menu_main_exit_zero_does_not_call_handlers(self) -> None:
        with (mock.patch("sys.stdin") as stdin,
              mock.patch("builtins.input", side_effect=["0"]),
              mock.patch("local_agent.cli.diagnostics.command_status",
                         side_effect=AssertionError("no status")),
              mock.patch("local_agent.cli.diagnostics.command_doctor",
                         side_effect=AssertionError("no doctor")),
              mock.patch("local_agent.cli.repositories.command_repositories_menu",
                         side_effect=AssertionError("no repo menu")),
              mock.patch("local_agent.cli.overview.command_overview",
                         side_effect=AssertionError("no overview")),
              mock.patch("local_agent.supervisor.polling.save_polling_settings",
                         side_effect=AssertionError("no preference write")),
              redirect_stdout(io.StringIO()) as out):
            stdin.isatty.return_value = True
            self.assertEqual(cli_polling.command_menu(
                argparse.Namespace(list=False, state_dir=self.state),
            ), 0)
        result = out.getvalue()
        self.assertIn("\n1. Status\n2. Doctor\n3. Polling\n4. Repositories\n", result)
        self.assertIn("\n5. Overview\n\n0. Exit\n", result)
        self.assertFalse((self.state / polling.PREFERENCE_FILE).exists())

    def test_polling_zero_exits_only_submenu_and_writes_nothing(self) -> None:
        with (mock.patch("sys.stdin") as stdin,
              mock.patch("builtins.input", side_effect=["3", "0", "0"]),
              mock.patch("local_agent.supervisor.polling.save_polling_settings",
                         side_effect=AssertionError("no preference write")) as save,
              redirect_stdout(io.StringIO()) as out):
            stdin.isatty.return_value = True
            self.assertEqual(cli_polling.command_menu(
                argparse.Namespace(list=False, state_dir=self.state),
            ), 0)
        save.assert_not_called()
        result = out.getvalue()
        self.assertIn("0. Back", result)
        self.assertEqual(result.count("Local Agent"), 2)
        self.assertFalse((self.state / polling.PREFERENCE_FILE).exists())

    def test_polling_fixed_interval_zero_returns_to_polling_not_root(self) -> None:
        with (mock.patch("sys.stdin") as stdin,
              mock.patch("builtins.input",
                         side_effect=["3", "3", "0", "1", "0", "0"]),
              mock.patch("local_agent.supervisor.polling.save_polling_settings",
                         side_effect=AssertionError("no preference write")) as save,
              redirect_stdout(io.StringIO()) as out):
            stdin.isatty.return_value = True
            self.assertEqual(cli_polling.command_menu(
                argparse.Namespace(list=False, state_dir=self.state),
            ), 0)
        save.assert_not_called()
        result = out.getvalue()
        self.assertEqual(result.count("\nPolling\n"), 3)
        self.assertIn("Polling preference: Adaptive", result)
        self.assertFalse((self.state / polling.PREFERENCE_FILE).exists())

    def test_menu_doctor_displays_progress_and_compact_result_without_json_dump(self) -> None:
        report = {
            "ok": True,
            "checks": [{"name": "service", "ok": True},
                       {"name": "repository:a", "ok": True}],
            "warnings": ["large workspace"],
        }

        def doctor(_args: argparse.Namespace) -> int:
            print(json.dumps(report))
            return 0

        with (mock.patch("local_agent.cli.diagnostics.command_doctor",
                         side_effect=doctor),
              redirect_stdout(io.StringIO()) as out):
            cli_polling._menu_doctor()
        displayed = out.getvalue()
        self.assertIn("Doctor: checking service", displayed)
        self.assertIn("Doctor PASS: 2/2 checks passed; 1 warning(s).", displayed)
        self.assertIn("Warning: large workspace", displayed)
        self.assertNotIn('"checks":', displayed)
        self.assertIn("Full JSON:", displayed)

    def test_menu_doctor_shows_named_failed_checks(self) -> None:
        report = {
            "ok": False,
            "checks": [{"name": "service", "ok": True},
                       {"name": "repository:a", "ok": False}],
            "warnings": [],
        }

        def doctor(_args: argparse.Namespace) -> int:
            print(json.dumps(report))
            return 1

        with (mock.patch("local_agent.cli.diagnostics.command_doctor",
                         side_effect=doctor),
              redirect_stdout(io.StringIO()) as out):
            cli_polling._menu_doctor()
        self.assertIn("Doctor FAIL: 1/2", out.getvalue())
        self.assertIn("FAIL: repository:a", out.getvalue())

    def test_menu_doctor_sanitizes_control_codes_and_never_auto_repairs(self) -> None:
        report = {
            "ok": False,
            "checks": [{"name": "registry\\n\\x1b[31m", "ok": False}],
            "warnings": ["state\\n\\x1b[2Junknown"],
        }

        def source(_args: argparse.Namespace) -> int:
            print(json.dumps(report))
            return 1

        with (mock.patch("local_agent.cli.diagnostics.command_doctor",
                         side_effect=source),
              redirect_stdout(io.StringIO()) as out):
            cli_polling._menu_doctor()
        result = out.getvalue()
        self.assertIn("Doctor FAIL: 0/1 checks", result)
        self.assertIn("No repairs attempted", result)
        self.assertNotIn("\x1b", result)
        self.assertNotIn("state\n", result)
        self.assertIn("Full JSON:", result)

    def test_menu_doctor_and_status_reuse_existing_handlers(self) -> None:
        with (mock.patch("sys.stdin") as stdin,
              mock.patch("builtins.input", side_effect=["1", "2", "0"]),
              mock.patch("local_agent.cli.diagnostics.command_doctor", return_value=0) as doctor,
              mock.patch("local_agent.cli.diagnostics.command_status", return_value=0) as status,
              redirect_stdout(io.StringIO())):
            stdin.isatty.return_value = True
            rc = cli_polling.command_menu(
                argparse.Namespace(list=False, state_dir=self.state),
            )
        self.assertEqual(rc, 0)
        status.assert_called_once()
        doctor.assert_called_once()

    def test_menu_status_is_human_readable_and_never_claims_freshness(self) -> None:
        record = {
            "daemon_version": "4.20.10", "self_revision": "a" * 40,
            "process_alive": True, "state": "idle",
            "updated_at": "2026-10-22T00:00:00+00:00",
        }
        def source(_args: argparse.Namespace) -> int:
            print(json.dumps(record))
            return 0

        with (mock.patch("local_agent.cli.diagnostics.command_status",
                         side_effect=source) as status,
              redirect_stdout(io.StringIO()) as out):
            cli_polling._menu_status()
        data = out.getvalue()
        status.assert_called_once()
        self.assertIn("Installed version reported: 4.20.10", data)
        self.assertIn("Status owner process: alive", data)
        self.assertIn("heartbeat freshness not established", data)
        self.assertIn("Last reported state: idle", data)
        self.assertNotIn('"daemon_version":', data)

    def test_menu_status_unverified_or_untrusted_data_never_proves_live(self) -> None:
        with (mock.patch("local_agent.cli.diagnostics.command_status",
                         return_value=1), redirect_stdout(io.StringIO()) as out):
            cli_polling._menu_status()
        self.assertIn("unverified", out.getvalue())
        record = {
            "daemon_version": "4.20.10\\n\\x1b[31m", "state": "stale",
            "process_alive": False, "updated_at": "invalid",
        }
        def source(_args: argparse.Namespace) -> int:
            print(json.dumps(record))
            return 0
        with (mock.patch("local_agent.cli.diagnostics.command_status",
                         side_effect=source), redirect_stdout(io.StringIO()) as out):
            cli_polling._menu_status()
        data = out.getvalue()
        self.assertIn("not running or unverified", data)
        self.assertIn("stale", data)
        self.assertNotIn("\x1b", data)

    def test_menu_polling_shows_mode_without_raw_json(self) -> None:
        with redirect_stdout(io.StringIO()) as out:
            cli_polling._menu_polling(argparse.Namespace(
                polling_command="show", state_dir=self.state,
            ))
        text = out.getvalue()
        self.assertIn("Polling preference: Adaptive", text)
        self.assertNotIn('"mode"', text)
        self.assertFalse((self.state / polling.PREFERENCE_FILE).exists())

        with redirect_stdout(io.StringIO()) as out:
            cli_polling._menu_polling(argparse.Namespace(
                polling_command="fixed", seconds=35, state_dir=self.state,
            ))
        self.assertIn("Polling preference saved: Fixed (35 seconds)", out.getvalue())
        self.assertEqual(polling.read_polling_settings(self.state).seconds, 35)

    def test_menu_polling_rejected_change_is_not_labelled_saved(self) -> None:
        with redirect_stdout(io.StringIO()) as out:
            cli_polling._menu_polling(argparse.Namespace(
                polling_command="fixed", seconds=1, state_dir=self.state,
            ))
        self.assertIn("change rejected", out.getvalue())
        self.assertNotIn("preference saved", out.getvalue())
        self.assertFalse((self.state / polling.PREFERENCE_FILE).exists())

    def test_live_supervisor_reloads_preference_without_restart(self) -> None:
        logs: list[str] = []
        live = polling.LivePollingPreferences(self.state, logs.append)
        live.refresh({}, 0, 10, 100)
        self.assertEqual(live.host(100), 5)
        polling.save_polling_settings(self.state, polling.PollingSettings("fixed", 45))
        live.refresh({}, 0, 10, 100.2)
        self.assertEqual(live.host(100.2), 5)
        live.refresh({}, 0, 10, 101.2)
        self.assertEqual(live.host(101.2), 45)
        self.assertEqual(live.control(101.2), 45)
        self.assertFalse(logs)

    def test_adaptive_activity_changes_host_and_control_intervals(self) -> None:
        live = polling.LivePollingPreferences(self.state, lambda _: None)
        live.refresh({}, 0, 10, 100)
        self.assertEqual(live.control(100), 15)
        self.assertEqual(live.host(100), 5)
        live.refresh({"project-a": 0}, 0, 10, 101)
        self.assertEqual(live.control(101), 2)
        self.assertEqual(live.host(140), 5)
        self.assertEqual(live.host(230), 5)

    def test_invalid_live_preference_retains_last_valid_and_rate_limits_logs(self) -> None:
        logs: list[str] = []
        polling.save_polling_settings(self.state, polling.PollingSettings("fixed", 20))
        live = polling.LivePollingPreferences(self.state, logs.append)
        live.refresh({}, 0, 10, 100)
        (self.state / polling.PREFERENCE_FILE).write_text("invalid")
        live.refresh({}, 0, 10, 102)
        live.refresh({}, 0, 10, 103)
        self.assertEqual(live.control(103), 20)
        self.assertEqual(len(logs), 1)
        live.refresh({}, 0, 10, 163)
        self.assertEqual(len(logs), 2)


if __name__ == "__main__":
    unittest.main()
