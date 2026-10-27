from __future__ import annotations

import io
import json
import shlex
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import local_agent.foundation.core as core
import local_agent.runtime.executor as runtime_module
from local_agent.runtime import task_contract
from local_agent.runtime.executor import (
    RuntimeExecutor,
)
from local_agent.runtime.task_contract import (
    idle_timeout_for,
    task_digest,
    task_timeout_for,
    validate_task,
)
from local_agent.runtime.telemetry import (
    parse_mac_swapusage,
    parse_mac_ps_cpu,
    parse_mac_top_cpu,
    parse_mac_vm_stat,
    parse_process_group_ps,
)
from local_agent.runtime.progress import (
    parse_progress_marker,
)


class RuntimeExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.original_work = core.WORK
        core.WORK = Path(self.tmp.name)
        self.runtime = RuntimeExecutor(core)

    def tearDown(self) -> None:
        core.WORK = self.original_work
        self.tmp.cleanup()

    @staticmethod
    def _efficient_task(**overrides: object) -> dict[str, object]:
        task: dict[str, object] = {
            "id": "efficient-task",
            "resources": [],
            "workflow_policy": "efficient-verification-v1",
            "steps": [
                {
                    "name": "edit",
                    "command": "edit-command",
                    "verification_level": "work",
                }
            ],
            "verify_steps": [
                {
                    "name": "final",
                    "command": "full-command",
                    "verification_level": "full",
                }
            ],
        }
        task.update(overrides)
        return task

    def test_task_digest_is_stable_and_payload_sensitive(self) -> None:
        a = {"id": "task-1", "commands": ["true"], "mode": "commands"}
        b = {"mode": "commands", "commands": ["true"], "id": "task-1"}
        c = {"id": "task-1", "commands": ["false"], "mode": "commands"}
        self.assertEqual(task_digest(a), task_digest(b))
        self.assertNotEqual(task_digest(a), task_digest(c))

    def test_task_validation_rejects_unsafe_id(self) -> None:
        with self.assertRaises(ValueError):
            validate_task({"id": "../bad", "commands": ["true"]})

    def test_task_validation_requires_exact_json_types_and_size_limits(self) -> None:
        invalid_tasks = [
            {"id": "bad-write-flag", "allow_write": "false"},
            {"id": "bad-command-timeout", "command_timeout": True},
            {"id": "bad-idle-timeout", "idle_timeout": 1.5},
            {"id": "bad-task-timeout", "task_timeout": "120"},
            {"id": "bad-command", "commands": [7]},
            {"id": "bad-delete", "deletes": [False]},
            {"id": "bad-write", "writes": [{"path": "x", "content": 7}]},
        ]
        for task in invalid_tasks:
            with self.subTest(task=task), self.assertRaises(ValueError):
                validate_task(task)

        with self.assertRaisesRegex(ValueError, "patch exceeds"):
            validate_task(
                {
                    "id": "large-patch",
                    "resources": [],
                    "patch": "x" * (task_contract.MAX_PATCH_BYTES + 1),
                }
            )
        with self.assertRaisesRegex(ValueError, "commands exceeds"):
            validate_task(
                {
                    "id": "many-commands",
                    "resources": [],
                    "commands": ["true"] * (task_contract.MAX_TASK_LIST_ITEMS + 1),
                }
            )

    def test_structured_steps_validate_and_reject_ambiguous_payloads(self) -> None:
        task = {
            "id": "staged-1",
            "resources": [],
            "steps": [{"name": "build", "command": "true", "timeout": 120}],
            "verify_steps": [{"name": "inspect", "command": "true", "timeout": 90}],
            "command_timeout": 300,
            "task_timeout": 600,
        }
        validate_task(task)
        with self.assertRaisesRegex(ValueError, "steps and commands"):
            validate_task({**task, "commands": ["true"]})
        with self.assertRaisesRegex(ValueError, "verify_steps and verify_commands"):
            validate_task({**task, "verify_commands": ["true"]})
        with self.assertRaises(ValueError):
            validate_task({"id": "bad-stage", "steps": [{"name": "", "command": "true"}]})
        with self.assertRaisesRegex(ValueError, "item timeout"):
            validate_task(
                {
                    "id": "bad-stage-timeout",
                    "resources": [],
                    "steps": [{"name": "build", "command": "true", "timeout": core.MAX_COMMAND_TIMEOUT + 1}],
                }
            )

    def test_efficient_verification_policy_accepts_valid_structured_plan(self) -> None:
        task = self._efficient_task(
            steps=[
                {
                    "name": "edit",
                    "command": "edit-command",
                    "verification_level": "work",
                },
                {
                    "name": "review",
                    "command": "review-command",
                    "verification_level": "focused",
                },
            ],
            verify_steps=[
                {
                    "name": "affected-checks",
                    "command": "focused-command",
                    "verification_level": "focused",
                },
                {
                    "name": "final-gate",
                    "command": "full-command",
                    "verification_level": "full",
                },
            ],
        )

        validate_task(task)

        plan = core.stage_plan_for(task)
        self.assertEqual(
            [stage["verification_level"] for stage in plan],
            ["work", "focused", "focused", "full"],
        )
        self.assertEqual(plan[-1]["stage_name"], "final-gate")
        self.assertEqual(plan[-1]["stage_phase"], "verification")

    def test_efficient_verification_policy_requires_valid_levels(self) -> None:
        invalid_stages = [
            {
                "steps": [{"name": "edit", "command": "edit-command"}],
            },
            {
                "verify_steps": [{"name": "final", "command": "full-command"}],
            },
            {
                "steps": [
                    {
                        "name": "edit",
                        "command": "edit-command",
                        "verification_level": "quick",
                    }
                ],
            },
            {
                "steps": [
                    {
                        "name": "edit",
                        "command": "edit-command",
                        "verification_level": ["work"],
                    }
                ],
            },
        ]

        for index, stages in enumerate(invalid_stages):
            with self.subTest(case=index), self.assertRaisesRegex(
                ValueError, "verification_level"
            ):
                validate_task(self._efficient_task(**stages))

    def test_efficient_verification_policy_rejects_legacy_command_fields(self) -> None:
        task = self._efficient_task()
        for field in ("commands", "verify_commands"):
            for value in ([], ["legacy-command"]):
                with self.subTest(field=field, value=value), self.assertRaisesRegex(
                    ValueError, "requires structured stages"
                ):
                    validate_task({**task, field: value})

    def test_efficient_verification_policy_rejects_invalid_full_gates(self) -> None:
        invalid_stages = [
            {
                "verify_steps": [
                    {
                        "name": "full-one",
                        "command": "full-one-command",
                        "verification_level": "full",
                    },
                    {
                        "name": "full-two",
                        "command": "full-two-command",
                        "verification_level": "full",
                    },
                ],
            },
            {
                "steps": [
                    {
                        "name": "wrong-full",
                        "command": "full-command",
                        "verification_level": "full",
                    }
                ],
            },
            {
                "verify_steps": [
                    {
                        "name": "wrong-work",
                        "command": "work-command",
                        "verification_level": "work",
                    },
                    {
                        "name": "final",
                        "command": "full-command",
                        "verification_level": "full",
                    },
                ],
            },
            {
                "verify_steps": [
                    {
                        "name": "focused-only",
                        "command": "focused-command",
                        "verification_level": "focused",
                    }
                ],
            },
        ]

        for index, stages in enumerate(invalid_stages):
            with self.subTest(case=index), self.assertRaises(ValueError):
                validate_task(self._efficient_task(**stages))

    def test_efficient_verification_policy_requires_full_gate_to_be_final(self) -> None:
        with self.assertRaisesRegex(ValueError, "final stage"):
            validate_task(
                self._efficient_task(
                    verify_steps=[
                        {
                            "name": "full-too-early",
                            "command": "full-command",
                            "verification_level": "full",
                        },
                        {
                            "name": "focused-after-full",
                            "command": "focused-command",
                            "verification_level": "focused",
                        },
                    ]
                )
            )

    def test_validation_accepts_heavy_task_timeout_values(self) -> None:
        validate_task(
            {
                "id": "heavy-task",
                "resources": [],
                "commands": ["true"],
                "command_timeout": 3600,
                "idle_timeout": 1200,
                "task_timeout": 7200,
            }
        )

    def test_legacy_and_structured_stage_plans_have_ordered_metadata(self) -> None:
        legacy = core.stage_plan_for(
            {"commands": ["one", "two"], "verify_commands": ["check"]}
        )
        self.assertEqual(
            [(item["stage_name"], item["stage_index"], item["stage_phase"]) for item in legacy],
            [("command-1", 1, "commands"), ("command-2", 2, "commands"), ("verification-1", 3, "verification")],
        )
        self.assertTrue(all("verification_level" not in item for item in legacy))
        validate_task(
            {
                "id": "legacy-structured",
                "resources": [],
                "steps": [{"name": "compile", "command": "true"}],
                "verify_steps": [{"name": "inspect", "command": "true"}],
            }
        )
        self.runtime._progress = (events := []).append
        self.runtime._stage_plan = core.stage_plan_for(
            {"steps": [{"name": "compile", "command": "printf ok", "timeout": 5}]}
        )
        self.assertEqual(self.runtime._stage_plan[0]["stage_timeout"], 5)
        self.runtime._command_count = 1
        self.runtime._primary_count = 1
        self.runtime._idle_timeout = 5
        self.runtime._deadline = time.monotonic() + 120
        self.runtime.run_command("printf ok", 5)
        for event_name in ("command_started", "command_finished"):
            event = next(item for item in events if item["event"] == event_name)
            self.assertEqual(event["stage_name"], "compile")
            self.assertEqual(event["stage_index"], 1)
            self.assertEqual(event["stage_total"], 1)
            self.assertEqual(event["stage_phase"], "commands")
            self.assertNotIn("verification_level", event)

    def test_verification_level_reaches_results_and_progress_events(self) -> None:
        task = self._efficient_task(id="efficient-metadata")

        def fake_run(command: str, _timeout: int, *, stage=None):
            return {
                "command": command,
                "exit_code": 0,
                "output": "",
                "elapsed_seconds": 0.1,
            }

        with mock.patch.object(core, "prepare_work"), mock.patch.object(
            core, "cleanup_work"
        ), mock.patch.object(
            core,
            "git_snapshot",
            return_value=(
                {"exit_code": 0, "output": ""},
                {"exit_code": 0, "output": ""},
            ),
        ), mock.patch.object(core, "checkpoint_worktree", return_value=None):
            result = core.process_task(task, command_runner=fake_run)

        self.assertEqual(result["commands"][0]["verification_level"], "work")
        self.assertEqual(result["verification"][0]["verification_level"], "full")
        self.assertEqual(
            [stage["verification_level"] for stage in result["stages"]],
            ["work", "full"],
        )

        events: list[dict] = []
        self.runtime._progress = events.append
        self.runtime._idle_timeout = 5
        self.runtime._deadline = time.monotonic() + 120
        self.runtime._command_count = 1
        self.runtime._primary_count = 1
        stage = core.stage_plan_for(task)[0]
        command = "printf '%s\\n' '[AGENT_PROGRESS] {\"message\":\"editing\"}'"
        with mock.patch.object(runtime_module, "PROGRESS_INTERVAL", 0):
            command_result = self.runtime.run_command(command, 5, stage=stage)

        self.assertEqual(command_result["exit_code"], 0)
        relevant_events = [
            event
            for event in events
            if event["event"]
            in {
                "command_started",
                "stage_progress",
                "command_heartbeat",
                "command_finished",
            }
        ]
        self.assertTrue(relevant_events)
        self.assertIn("stage_progress", {event["event"] for event in relevant_events})
        self.assertTrue(
            all(event["verification_level"] == "work" for event in relevant_events)
        )

    def test_output_policy_is_validated_and_propagated(self) -> None:
        task = self._efficient_task(
            steps=[
                {
                    "name": "edit",
                    "command": "edit-command",
                    "verification_level": "work",
                    "output_policy": "summary",
                }
            ],
            verify_steps=[
                {
                    "name": "final",
                    "command": "full-command",
                    "verification_level": "full",
                    "output_policy": "summary",
                }
            ],
        )
        validate_task(task)
        self.assertEqual(
            [stage["output_policy"] for stage in core.stage_plan_for(task)],
            ["summary", "summary"],
        )
        with self.assertRaisesRegex(ValueError, "output_policy"):
            validate_task(
                self._efficient_task(
                    steps=[
                        {
                            "name": "edit",
                            "command": "edit-command",
                            "verification_level": "work",
                            "output_policy": "quiet",
                        }
                    ]
                )
            )

    def test_summary_output_suppresses_success_and_retains_evidence(self) -> None:
        events: list[dict] = []
        self.runtime._progress = events.append
        self.runtime._idle_timeout = 5
        self.runtime._deadline = time.monotonic() + 120
        self.runtime._command_count = 1
        self.runtime._primary_count = 1
        stage = {
            "stage_name": "quiet-success",
            "stage_index": 1,
            "stage_total": 1,
            "stage_phase": "commands",
            "output_policy": "summary",
        }
        command = (
            f"{shlex.quote(sys.executable)} -c "
            + shlex.quote(
                "print('[AGENT_PROGRESS] {\"message\":\"quiet-progress\"}'); "
                "print('summary-secret')"
            )
        )
        visible = io.StringIO()
        with mock.patch.object(core, "log"), redirect_stdout(visible):
            result = self.runtime.run_command(command, 5, stage=stage)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["output_policy"], "summary")
        self.assertIn("summary-secret", result["output"])
        self.assertGreater(result["captured_output_chars"], 0)
        self.assertNotIn("[CMD] summary-secret", visible.getvalue())
        self.assertIn("[CMD] [progress] quiet-progress", visible.getvalue())
        self.assertIn("stage_progress", {event["event"] for event in events})

    def test_summary_output_failure_emits_bounded_tail(self) -> None:
        self.runtime._idle_timeout = 5
        self.runtime._deadline = time.monotonic() + 120
        self.runtime._command_count = 1
        self.runtime._primary_count = 1
        stage = {
            "stage_name": "quiet-failure",
            "stage_index": 1,
            "stage_total": 1,
            "stage_phase": "commands",
            "output_policy": "summary",
        }
        command = (
            f"{shlex.quote(sys.executable)} -c "
            + shlex.quote("print('x' * 9000); raise SystemExit(7)")
        )
        visible = io.StringIO()
        with mock.patch.object(core, "log"), redirect_stdout(visible):
            result = self.runtime.run_command(command, 5, stage=stage)
        text = visible.getvalue()
        self.assertEqual(result["exit_code"], 7)
        self.assertIn("summary stage failed", text)
        self.assertIn("truncated; showing last", text)
        self.assertGreater(result["captured_output_chars"], 8000)
        self.assertFalse(result["output_truncated"])

    def test_staged_result_contains_summary_and_stops_after_failure(self) -> None:
        task = {
            "id": "staged-result",
            "steps": [
                {"name": "first", "command": "one"},
                {"name": "second", "command": "two"},
            ],
        }
        calls: list[str] = []

        def fake_run(command: str, _timeout: int, *, stage=None):
            calls.append(command)
            return {"command": command, "exit_code": 1 if command == "one" else 0, "elapsed_seconds": 0.25}

        with mock.patch.object(core, "prepare_work"), mock.patch.object(
            core, "cleanup_work"
        ), mock.patch.object(
            core, "git_snapshot", return_value=({"exit_code": 0, "output": ""}, {"exit_code": 0, "output": ""})
        ), mock.patch.object(core, "checkpoint_worktree", return_value=None), mock.patch.object(
            core, "run_command", side_effect=fake_run
        ):
            result = core.process_task(task)

        self.assertEqual(calls, ["one"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_reason"], "command_failed")
        self.assertEqual(result["stages"][0]["stage_name"], "first")
        self.assertEqual(result["stages"][0]["outcome"], "failed")
        self.assertEqual(result["stages"][0]["elapsed_seconds"], 0.25)

    def test_progress_marker_parsing_and_malformed_tolerance(self) -> None:
        marker = parse_progress_marker(
            '[AGENT_PROGRESS] ' + json.dumps(
                {"stage_name": "case-run", "message": "case 2", "current": 2, "total": 5, "metrics": {"passed": 1}}
            )
        )
        self.assertEqual(marker["stage_name"], "case-run")
        self.assertEqual(marker["metrics"], {"passed": 1})
        self.assertIsNone(parse_progress_marker("[AGENT_PROGRESS] {broken"))
        self.assertIsNone(parse_progress_marker("[AGENT_PROGRESS] " + "x" * 5000))

        events: list[dict] = []
        self.runtime._progress = events.append
        self.runtime._idle_timeout = 5
        self.runtime._deadline = time.monotonic() + 120
        self.runtime._command_count = 1
        self.runtime._primary_count = 1
        command = f"{shlex.quote(sys.executable)} -c " + shlex.quote(
            "print('[AGENT_PROGRESS] {broken}'); print('[AGENT_PROGRESS] {\\\"stage_name\\\":\\\"case-run\\\",\\\"message\\\":\\\"case 2\\\",\\\"current\\\":2,\\\"total\\\":5}')"
        )
        result = self.runtime.run_command(command, 5)
        self.assertEqual(result["exit_code"], 0)
        progress_events = [event for event in events if event["event"] == "stage_progress"]
        self.assertEqual(len(progress_events), 1)
        self.assertEqual(progress_events[0]["last_progress_message"], "case 2")
        self.assertEqual(progress_events[0]["total"], 1)
        self.assertEqual(progress_events[0]["progress_current"], 2)
        self.assertEqual(progress_events[0]["progress_total"], 5)
        self.assertEqual(progress_events[0]["stage_progress"]["total"], 5)

    def test_domain_progress_resets_between_commands(self) -> None:
        events: list[dict] = []
        self.runtime._progress = events.append
        self.runtime._idle_timeout = 5
        self.runtime._deadline = time.monotonic() + 120
        self.runtime._command_count = 2
        self.runtime._primary_count = 2
        first = f"{shlex.quote(sys.executable)} -c " + shlex.quote(
            "print('[AGENT_PROGRESS] {\"stage_name\":\"stage-a\",\"message\":\"A\",\"current\":1,\"total\":2}')"
        )
        second = f"{shlex.quote(sys.executable)} -c " + shlex.quote(
            "import time; time.sleep(0.4)"
        )
        with mock.patch.object(runtime_module, "PROGRESS_INTERVAL", 0):
            self.assertEqual(
                self.runtime.run_command(
                    first,
                    5,
                    stage={
                        "stage_name": "stage-a",
                        "stage_index": 1,
                        "stage_total": 2,
                        "stage_phase": "commands",
                    },
                )["exit_code"],
                0,
            )
            self.assertEqual(
                self.runtime.run_command(
                    second,
                    5,
                    stage={
                        "stage_name": "stage-b",
                        "stage_index": 2,
                        "stage_total": 2,
                        "stage_phase": "commands",
                    },
                )["exit_code"],
                0,
            )

        second_heartbeats = [
            event
            for event in events
            if event["event"] == "command_heartbeat" and event["index"] == 2
        ]
        self.assertTrue(second_heartbeats)
        self.assertTrue(all("stage_progress" not in event for event in second_heartbeats))
        self.assertTrue(all("last_progress_at" not in event for event in second_heartbeats))

    def test_telemetry_parsers_are_deterministic(self) -> None:
        vm = parse_mac_vm_stat(
            "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
            "Pages free: 10\nPages inactive: 20\nPages speculative: 5\n"
        )
        self.assertEqual(vm["available_bytes"], 35 * 16384)
        fallback_vm = parse_mac_vm_stat(
            "Pages free: 10\nPages inactive: 20\nPages speculative: 5\n"
        )
        self.assertEqual(fallback_vm["available_bytes"], 35 * 4096)
        self.assertEqual(parse_mac_swapusage("total = 4.00G  used = 1.25G  free = 2.75G"), {"total": 4 * 1024**3, "used": int(1.25 * 1024**3), "free": int(2.75 * 1024**3)})
        self.assertEqual(
            parse_mac_top_cpu("CPU usage: 12.50% user, 3.25% sys, 84.25% idle"),
            15.75,
        )
        self.assertEqual(parse_mac_ps_cpu("50\n75\nnot-a-number\n", 2), 62.5)
        self.assertEqual(parse_mac_ps_cpu("500\n", 2), 100.0)
        self.assertEqual(
            parse_process_group_ps("1 99 2.0 1000\n2 99 3.5 3000\n3 12 9.0 9000", 99),
            {"command_cpu_percent": 5.5, "command_rss_mb": round(4000 / 1024, 2), "command_children": 1},
        )

    def test_heartbeat_includes_telemetry_and_survives_unavailable_metrics(self) -> None:
        events: list[dict] = []
        self.runtime._progress = events.append
        self.runtime._idle_timeout = 5
        self.runtime._deadline = time.monotonic() + 120
        self.runtime._command_count = 1
        self.runtime._primary_count = 1
        telemetry = mock.Mock()
        telemetry.snapshot.return_value = {
            "host_cpu_percent": 12.5,
            "command_rss_mb": 4.0,
        }
        with mock.patch.object(runtime_module, "PROGRESS_INTERVAL", 0), mock.patch.object(
            runtime_module,
            "TelemetrySampler",
            return_value=telemetry,
        ):
            result = self.runtime.run_command("printf ok", 5)
        self.assertEqual(result["exit_code"], 0)
        heartbeats = [event for event in events if event["event"] == "command_heartbeat"]
        self.assertTrue(heartbeats)
        self.assertEqual(heartbeats[0]["host_cpu_percent"], 12.5)

    def test_watchdog_defaults_and_bounds(self) -> None:
        self.assertEqual(idle_timeout_for({}), task_contract.DEFAULT_IDLE_TIMEOUT)
        self.assertEqual(task_timeout_for({}), task_contract.DEFAULT_TASK_TIMEOUT)
        self.assertEqual(idle_timeout_for({"idle_timeout": 0}), 0)
        with self.assertRaises(ValueError):
            task_timeout_for({"task_timeout": task_contract.MAX_TASK_TIMEOUT + 1})
        with self.assertRaises(ValueError):
            idle_timeout_for({"idle_timeout": task_contract.MAX_IDLE_TIMEOUT + 1})

    def test_idle_timeout_kills_silent_command(self) -> None:
        self.runtime._idle_timeout = 1
        self.runtime._deadline = time.monotonic() + 120
        self.runtime._command_count = 1
        self.runtime._primary_count = 1
        command = f"{shlex.quote(sys.executable)} -c " + shlex.quote(
            "import time; time.sleep(5)"
        )
        result = self.runtime.run_command(command, 10)
        self.assertEqual(result["exit_code"], 124)
        self.assertTrue(result["idle_timed_out"])

    def test_blocking_rss_sampler_does_not_delay_idle_timeout(self) -> None:
        def slow_sampler(_process_group: int) -> None:
            time.sleep(3)
            return None

        runtime = RuntimeExecutor(core, rss_sampler=slow_sampler)
        runtime._idle_timeout = 1
        runtime._deadline = time.monotonic() + 120
        runtime._command_count = 1
        runtime._primary_count = 1
        command = f"{shlex.quote(sys.executable)} -c " + shlex.quote(
            "import time; time.sleep(5)"
        )
        result = runtime.run_command(command, 10)
        self.assertTrue(result["idle_timed_out"])
        self.assertLess(result["elapsed_seconds"], 2.5)

    def test_successful_parent_with_surviving_child_is_terminal_failure(self) -> None:
        marker = Path(self.tmp.name) / "leaked-child.txt"
        child_code = (
            "import pathlib,time; time.sleep(1); "
            f"pathlib.Path({str(marker)!r}).write_text('leaked')"
        )
        parent_code = (
            "import subprocess,sys; "
            f"subprocess.Popen([sys.executable, '-c', {child_code!r}])"
        )
        self.runtime._idle_timeout = 5
        self.runtime._deadline = time.monotonic() + 120
        self.runtime._command_count = 1
        self.runtime._primary_count = 1
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(parent_code)}"
        result = self.runtime.run_command(command, 5)
        self.assertEqual(result["exit_code"], 126)
        self.assertTrue(result["background_process_leak"])
        self.assertEqual(result["failure_reason"], "command_background_process_leak")
        time.sleep(1.2)
        self.assertFalse(marker.exists())

    def test_slow_progress_callback_does_not_delay_command_watchdog(self) -> None:
        task = {
            "id": "slow-progress-callback",
            "resources": [],
            "commands": [
                f"{shlex.quote(sys.executable)} -c "
                + shlex.quote("import time; time.sleep(5)")
            ],
            "command_timeout": 1,
            "idle_timeout": 0,
            "task_timeout": 62,
        }

        def slow_progress(_event: dict) -> None:
            time.sleep(1.5)

        with mock.patch.object(core, "prepare_work"), mock.patch.object(
            core, "cleanup_work"
        ), mock.patch.object(
            core,
            "git_snapshot",
            return_value=(
                {"exit_code": 0, "output": ""},
                {"exit_code": 0, "output": ""},
            ),
        ), mock.patch.object(core, "checkpoint_worktree", return_value=None):
            result = self.runtime.process_task(task, progress=slow_progress)

        self.assertEqual(result["failure_reason"], "command_timeout")
        self.assertLess(result["commands"][0]["elapsed_seconds"], 2.5)

    def test_task_budget_refuses_stage_before_start(self) -> None:
        self.runtime._idle_timeout = 0
        self.runtime._deadline = time.monotonic() + 100
        self.runtime._command_count = 1
        self.runtime._primary_count = 1
        stage = {
            "stage_name": "too-large",
            "stage_index": 1,
            "stage_total": 1,
            "stage_phase": "commands",
        }
        with mock.patch.object(runtime_module.subprocess, "Popen") as popen:
            result = self.runtime.run_command("printf never-runs", 60, stage=stage)
        popen.assert_not_called()
        self.assertEqual(result["exit_code"], 125)
        self.assertTrue(result["not_started"])
        self.assertTrue(result["budget_exhausted"])
        self.assertNotIn("task_timed_out", result)
        self.assertEqual(self.runtime._last_failure_reason, "task_budget_exhausted")

    def test_task_budget_starts_before_prepare_but_finalization_is_separately_bounded(self) -> None:
        task = {
            "id": "prepare-consumes-stage-budget",
            "resources": [],
            "commands": ["printf never-runs"],
            "command_timeout": 30,
            "idle_timeout": 0,
            "task_timeout": 120,
        }
        finalization: list[str] = []

        def consume_prepare_budget(*_args, **_kwargs) -> None:
            self.assertIsNotNone(self.runtime._deadline)
            assert self.runtime._deadline is not None
            self.runtime._deadline -= 31

        def checkpoint(*_args, **_kwargs):
            self.assertIsNotNone(self.runtime._deadline)
            self.runtime._deadline = time.monotonic() - 1
            finalization.append("checkpoint")
            return None

        def cleanup() -> None:
            finalization.append("cleanup")

        with mock.patch.object(
            core, "prepare_work", side_effect=consume_prepare_budget
        ), mock.patch.object(
            core, "checkpoint_worktree", side_effect=checkpoint
        ), mock.patch.object(
            core, "cleanup_work", side_effect=cleanup
        ), mock.patch.object(
            core,
            "git_snapshot",
            return_value=(
                {"exit_code": 0, "output": ""},
                {"exit_code": 0, "output": ""},
            ),
        ), mock.patch.object(runtime_module, "spawn_shell") as spawn:
            result = self.runtime.process_task(task)

        spawn.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_reason"], "task_budget_exhausted")
        self.assertEqual(len(result["commands"]), 1)
        self.assertTrue(result["commands"][0]["not_started"])
        self.assertTrue(result["commands"][0]["budget_exhausted"])
        self.assertEqual(finalization, ["checkpoint", "cleanup"])

    def test_structured_stage_timeout_overrides_task_command_timeout(self) -> None:
        task = {
            "id": "stage-timeout-override",
            "resources": [],
            "steps": [{"name": "short", "command": "one", "timeout": 7}],
            "command_timeout": 120,
            "task_timeout": 300,
        }
        calls: list[tuple[str, int]] = []

        def fake_run(command: str, timeout: int, *, stage=None):
            calls.append((command, timeout))
            return {"command": command, "exit_code": 0, "elapsed_seconds": 0.1}

        with mock.patch.object(core, "prepare_work"), mock.patch.object(
            core, "cleanup_work"
        ), mock.patch.object(
            core, "git_snapshot", return_value=({"exit_code": 0, "output": ""}, {"exit_code": 0, "output": ""})
        ), mock.patch.object(core, "checkpoint_worktree", return_value=None), mock.patch.object(
            core, "run_command", side_effect=fake_run
        ):
            result = core.process_task(task)

        self.assertEqual(result["status"], "done")
        self.assertEqual(calls, [("one", 7)])

    def test_process_failure_still_checkpoints_before_cleanup(self) -> None:
        with mock.patch.object(core, "prepare_work"), mock.patch.object(
            core, "cleanup_work"
        ) as cleanup, mock.patch.object(
            core, "checkpoint_worktree", return_value=None
        ) as checkpoint, mock.patch.object(
            core, "run_command", side_effect=RuntimeError("budget exhausted")
        ):
            result = core.process_task(
                {"id": "checkpoint-after-failure", "commands": ["one"]}
            )

        self.assertEqual(result["status"], "failed")
        checkpoint.assert_called_once_with(
            "checkpoint-after-failure", reason="task-exit"
        )
        cleanup.assert_called_once_with()

    def test_checkpoint_failure_preserves_primary_failure_and_skips_cleanup(self) -> None:
        with mock.patch.object(core, "prepare_work"), mock.patch.object(
            core, "cleanup_work"
        ) as cleanup, mock.patch.object(
            core, "checkpoint_worktree", side_effect=OSError("checkpoint disk error")
        ), mock.patch.object(
            core, "run_command", side_effect=RuntimeError("command infrastructure error")
        ):
            result = core.process_task(
                {"id": "checkpoint-failure", "commands": ["one"]}
            )

        self.assertEqual(result["failure_reason"], "workspace_checkpoint_failed")
        self.assertEqual(result["primary_failure_reason"], "exception")
        self.assertIn("checkpoint disk error", result["checkpoint_error"])
        cleanup.assert_not_called()

    def test_cleanup_failure_is_reported_without_losing_primary_failure(self) -> None:
        failed_command = {
            "command": "false",
            "exit_code": 1,
            "output": "",
            "elapsed_seconds": 0.1,
        }
        checkpoint = {"path": "/safe/checkpoint"}
        with mock.patch.object(core, "prepare_work"), mock.patch.object(
            core, "cleanup_work", side_effect=OSError("cleanup refused")
        ), mock.patch.object(
            core, "checkpoint_worktree", return_value=checkpoint
        ), mock.patch.object(
            core,
            "git_snapshot",
            return_value=(
                {"exit_code": 0, "output": ""},
                {"exit_code": 0, "output": ""},
            ),
        ), mock.patch.object(core, "run_command", return_value=failed_command):
            result = core.process_task(
                {"id": "cleanup-failure", "commands": ["false"]}
            )

        self.assertEqual(result["workspace_checkpoint"], checkpoint)
        self.assertEqual(result["failure_reason"], "workspace_cleanup_failed")
        self.assertEqual(result["primary_failure_reason"], "command_failed")
        self.assertIn("cleanup refused", result["cleanup_error"])

    def test_progress_emits_command_transitions(self) -> None:
        events: list[dict] = []
        self.runtime._progress = events.append
        self.runtime._idle_timeout = 5
        self.runtime._deadline = time.monotonic() + 120
        self.runtime._command_count = 1
        self.runtime._primary_count = 1
        result = self.runtime.run_command("printf 'ok\\n'", 5)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(events[0]["event"], "command_started")
        self.assertEqual(events[-1]["event"], "command_finished")


    def test_large_unified_diff_is_collapsed_only_in_live_log(self) -> None:
        self.runtime._idle_timeout = 5
        self.runtime._deadline = time.monotonic() + 120
        self.runtime._command_count = 1
        self.runtime._primary_count = 1
        script = "\n".join(
            [
                "print('before-diff')",
                "print('diff --git a/a.txt b/a.txt')",
                "print('index 1111111..2222222 100644')",
                "print('--- a/a.txt')",
                "print('+++ b/a.txt')",
                "print('@@ -0,0 +1,120 @@')",
                "[print(f'+line-{i:03d}') for i in range(120)]",
                "print()",
                "print('after-diff')",
            ]
        )
        command = f"{shlex.quote(sys.executable)} -c " + shlex.quote(script)
        live = io.StringIO()
        with redirect_stdout(live):
            result = self.runtime.run_command(command, 5)

        rendered = live.getvalue()
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("+line-119", result["output"])
        self.assertIn("[CMD] before-diff", rendered)
        self.assertIn("large unified diff collapsed in live log", rendered)
        self.assertIn("collapsed unified diff: 1 file(s)", rendered)
        self.assertNotIn("+line-119", rendered)
        self.assertIn("[CMD] after-diff", rendered)

    def test_indented_output_after_completed_hunk_is_not_collapsed(self) -> None:
        self.runtime._idle_timeout = 5
        self.runtime._deadline = time.monotonic() + 120
        self.runtime._command_count = 1
        self.runtime._primary_count = 1
        script = "\n".join(
            [
                "print('diff --git a/a.txt b/a.txt')",
                "print('index 1111111..2222222 100644')",
                "print('--- a/a.txt')",
                "print('+++ b/a.txt')",
                "print('@@ -0,0 +1,81 @@')",
                "[print(f'+line-{i:03d}') for i in range(81)]",
                "print('  normal command output')",
            ]
        )
        command = f"{shlex.quote(sys.executable)} -c " + shlex.quote(script)
        live = io.StringIO()
        with redirect_stdout(live):
            result = self.runtime.run_command(command, 5)

        rendered = live.getvalue()
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("[CMD]   normal command output", rendered)
        self.assertIn("collapsed unified diff: 1 file(s), 86 line(s)", rendered)
        self.assertNotIn("collapsed unified diff: 1 file(s), 87 line(s)", rendered)

    def test_runtime_executor_uses_compact_multiline_command_descriptor(self) -> None:
        self.runtime._idle_timeout = 5
        self.runtime._deadline = time.monotonic() + 120
        self.runtime._command_count = 1
        self.runtime._primary_count = 1
        newline = chr(10)
        command = newline.join(["set -e", "printf runtime-concise-smoke"])
        stage = {
            "stage_name": "runtime-concise-stage",
            "stage_index": 1,
            "stage_total": 1,
            "stage_phase": "commands",
            "output_policy": "summary",
        }
        messages: list[str] = []
        with mock.patch.dict(core.os.environ, {}, clear=True), mock.patch.object(
            core, "log", side_effect=messages.append
        ):
            result = self.runtime.run_command(command, 5, stage=stage)

        self.assertEqual(result["exit_code"], 0)
        descriptor = f"stage=runtime-concise-stage lines=2 chars={len(command)}"
        self.assertIn(f"exec: {descriptor}", messages)
        self.assertTrue(
            any(
                message.startswith("exec finished exit=0 elapsed=")
                and message.endswith(descriptor)
                for message in messages
            )
        )
        self.assertFalse(any(command in message for message in messages))
        self.assertEqual(result["command"], command)

    def test_identical_large_diffs_are_aggregated_in_live_log(self) -> None:
        self.runtime._idle_timeout = 5
        self.runtime._deadline = time.monotonic() + 120
        self.runtime._command_count = 1
        self.runtime._primary_count = 1
        script = "\n".join(
            [
                "def emit_diff():",
                "    print('diff --git a/a.txt b/a.txt')",
                "    print('index 1111111..2222222 100644')",
                "    print('--- a/a.txt')",
                "    print('+++ b/a.txt')",
                "    print('@@ -0,0 +1,81 @@')",
                "    [print(f'+line-{i:03d}') for i in range(81)]",
                "emit_diff()",
                "print()",
                "emit_diff()",
                "print()",
                "emit_diff()",
                "print('after-diffs')",
            ]
        )
        command = f"{shlex.quote(sys.executable)} -c " + shlex.quote(script)
        live = io.StringIO()
        with redirect_stdout(live):
            result = self.runtime.run_command(command, 5)

        rendered = live.getvalue()
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["output"].count("diff --git a/a.txt b/a.txt"), 3)
        self.assertEqual(rendered.count("large unified diff collapsed in live log"), 1)
        self.assertEqual(rendered.count("collapsed unified diff:"), 1)
        self.assertIn("suppressed 2 repeated copies of the previous unified diff", rendered)
        self.assertIn("[CMD] after-diffs", rendered)

    def test_small_unified_diff_remains_visible_in_live_log(self) -> None:
        self.runtime._idle_timeout = 5
        self.runtime._deadline = time.monotonic() + 120
        self.runtime._command_count = 1
        self.runtime._primary_count = 1
        script = "\n".join(
            [
                "print('diff --git a/a.txt b/a.txt')",
                "print('--- a/a.txt')",
                "print('+++ b/a.txt')",
                "print('@@ -1 +1 @@')",
                "print('-old')",
                "print('+new')",
                "print()",
                "print('done')",
            ]
        )
        command = f"{shlex.quote(sys.executable)} -c " + shlex.quote(script)
        live = io.StringIO()
        with redirect_stdout(live):
            result = self.runtime.run_command(command, 5)

        rendered = live.getvalue()
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("[CMD] +new", rendered)
        self.assertNotIn("collapsed unified diff", rendered)
        self.assertIn("[CMD] done", rendered)


if __name__ == "__main__":
    unittest.main()
