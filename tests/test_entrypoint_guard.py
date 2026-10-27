from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import local_agent.entrypoint as agent_entrypoint
import local_agent.operator.local as agent_operator
import local_agent.operator.remote as agent_remote_operator
import local_agent.cli.diagnostics as agentctl
from local_agent.repository.context import RepositoryContext


class RemoteOperatorControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state_dir = root / "state"
        self.disabled_path = self.state_dir / "disabled.json"
        self.state_patch = mock.patch.object(agent_operator, "STATE_DIR", self.state_dir)
        self.path_patch = mock.patch.object(agent_operator, "DISABLED_PATH", self.disabled_path)
        self.state_patch.start()
        self.path_patch.start()

    def tearDown(self) -> None:
        self.path_patch.stop()
        self.state_patch.stop()
        self.tmp.cleanup()

    def test_remote_disabled_persists_local_marker(self) -> None:
        state = agent_remote_operator.RemoteOperatorState()
        payload = {
            "version": 1,
            "desired_state": "disabled",
            "request_id": "stop-1",
        }
        with mock.patch.object(
            agent_remote_operator, "_remote_ref", return_value="a" * 40
        ), mock.patch.object(
            agent_remote_operator, "_load_remote_payload", return_value=payload
        ):
            desired = agent_remote_operator.poll_remote_operator(
                state,
                self_repo=Path("/tmp/self"),
                force=True,
            )

        self.assertEqual(desired, "disabled")
        self.assertTrue(agent_operator.is_disabled())
        marker = json.loads(self.disabled_path.read_text(encoding="utf-8"))
        self.assertEqual(marker["control_id"], "stop-1")
        self.assertEqual(marker["reason"], "remote_operator_control")

    def test_remote_enabled_never_clears_local_disable(self) -> None:
        agent_operator.disable_agent(reason="manual")
        state = agent_remote_operator.RemoteOperatorState()
        payload = {
            "version": 1,
            "desired_state": "enabled",
            "request_id": "resume-intent-1",
        }
        with mock.patch.object(
            agent_remote_operator, "_remote_ref", return_value="b" * 40
        ), mock.patch.object(
            agent_remote_operator, "_load_remote_payload", return_value=payload
        ):
            desired = agent_remote_operator.poll_remote_operator(
                state,
                self_repo=Path("/tmp/self"),
                force=True,
            )

        self.assertEqual(desired, "enabled")
        self.assertTrue(agent_operator.is_disabled())

    def test_cached_remote_disable_reasserts_deleted_marker_without_network_poll(self) -> None:
        state = agent_remote_operator.RemoteOperatorState(
            last_poll_at=10.0,
            last_ref="c" * 40,
            desired_state="disabled",
            request_id="stop-cached",
        )
        with mock.patch.object(agent_remote_operator, "_remote_ref") as remote_ref:
            desired = agent_remote_operator.poll_remote_operator(
                state,
                self_repo=Path("/tmp/self"),
                now=10.5,
            )
        remote_ref.assert_not_called()
        self.assertEqual(desired, "disabled")
        self.assertTrue(agent_operator.is_disabled())


class RuntimeResetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state_dir = root / "state"
        self.disabled_path = self.state_dir / "disabled.json"
        self.registry = root / "repositories.json"
        self.registry.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repositories": [
                        {
                            "id": "project-a",
                            "repository": "owner/project-a",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.home_patch = mock.patch("pathlib.Path.home", return_value=root)
        self.state_patch = mock.patch.object(agent_operator, "STATE_DIR", self.state_dir)
        self.path_patch = mock.patch.object(agent_operator, "DISABLED_PATH", self.disabled_path)
        self.home_patch.start()
        self.state_patch.start()
        self.path_patch.start()

    def tearDown(self) -> None:
        self.path_patch.stop()
        self.state_patch.stop()
        self.home_patch.stop()
        self.tmp.cleanup()

    def test_runtime_reset_requires_disabled_state(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "requires Local Agent to be disabled"):
            agent_operator.reset_runtime_state(registry_path=self.registry)

    def test_runtime_reset_removes_repository_legacy_and_global_status(self) -> None:
        agent_operator.disable_agent(reason="test-reset")
        repository_state = self.state_dir / "repositories" / "project-a"
        for directory in ("claims", "corrupt-claims", "runs", "result-spool"):
            target = repository_state / directory
            target.mkdir(parents=True, exist_ok=True)
            (target / "old.json").write_text("{}", encoding="utf-8")
        (repository_state / "status.json").write_text("{}", encoding="utf-8")
        legacy_claims = self.state_dir / "claims"
        legacy_claims.mkdir(parents=True, exist_ok=True)
        (legacy_claims / "old.json").write_text("{}", encoding="utf-8")
        global_status = self.state_dir / "status.json"
        global_status.write_text('{"state":"disabled","pid":9548}', encoding="utf-8")

        result = agent_operator.reset_runtime_state(registry_path=self.registry)

        self.assertTrue(result["reset"])
        self.assertEqual(result["repository_ids"], ["project-a"])
        self.assertFalse((repository_state / "status.json").exists())
        self.assertEqual(list((repository_state / "claims").iterdir()), [])
        self.assertEqual(list(legacy_claims.iterdir()), [])
        self.assertFalse(global_status.exists())
        self.assertTrue(agent_operator.is_disabled())


class AgentCtlStatusTests(unittest.TestCase):
    def test_guard_entrypoint_pid_owns_status_when_supervisor_is_absent(self) -> None:
        payload = {
            "state": "disabled",
            "pid": 111,
            "entrypoint_pid": 222,
            "supervisor_pid": None,
        }
        with mock.patch.object(agentctl, "_pid_alive", return_value=True) as alive:
            result = agentctl._status_with_liveness(payload)

        alive.assert_called_once_with(222)
        self.assertEqual(result["state"], "disabled")
        self.assertEqual(result["status_owner_pid"], 222)
        self.assertTrue(result["process_alive"])

    def test_dead_status_owner_is_reported_as_stale(self) -> None:
        payload = {"state": "disabled", "pid": 9548, "supervisor_pid": 9548}
        with mock.patch.object(agentctl, "_pid_alive", return_value=False):
            result = agentctl._status_with_liveness(payload)

        self.assertEqual(result["state"], "stale")
        self.assertEqual(result["last_state"], "disabled")
        self.assertEqual(result["status_owner_pid"], 9548)
        self.assertFalse(result["process_alive"])
        self.assertIn("not running", result["error"])


class GuardedEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.enterContext(mock.patch.object(agent_entrypoint.agentd, "STATE_DIR", Path(temporary.name)))
        self.previous_stop = agent_entrypoint._stop_requested
        agent_entrypoint._stop_requested = False

    def tearDown(self) -> None:
        agent_entrypoint._stop_requested = self.previous_stop

    def test_prepare_removes_generated_bytecode_and_provisions_missing_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = RepositoryContext(
                repository_id="project-a",
                repository="owner/project-a",
                control=root / "control",
                work=root / "work",
                checkpoints=root / "checkpoints",
            )
            (repository.control / ".git").mkdir(parents=True)
            pycache = repository.control / ".agent" / "patches" / "__pycache__"
            pycache.mkdir(parents=True)
            (pycache / "generated.pyc").write_bytes(b"bytecode")

            with mock.patch.object(agent_entrypoint, "provision_repository") as provision:
                agent_entrypoint.prepare_repositories([repository])

            provision.assert_called_once_with(repository)
            self.assertFalse(pycache.exists())

    def test_start_supervisor_disables_python_bytecode_writes(self) -> None:
        args = argparse.Namespace(registry=Path("/tmp/registry.json"), max_workers=2)
        fake = mock.Mock(pid=1234)
        with mock.patch.object(agent_entrypoint, "popen_registered", return_value=fake) as popen:
            result = agent_entrypoint.start_supervisor(args)
        self.assertIs(result, fake)
        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_commands_preserve_registry_and_worker_count(self) -> None:
        args = argparse.Namespace(registry=Path("/tmp/registry.json"), max_workers=3)
        supervisor = agent_entrypoint.supervisor_command(args)
        reexec = agent_entrypoint._self_reexec_args(args)
        self.assertEqual(supervisor[-4:], ["--registry", "/tmp/registry.json", "--max-workers", "3"])
        self.assertEqual(reexec[-4:], ["--registry", "/tmp/registry.json", "--max-workers", "3"])

    def test_stop_supervisor_terminates_only_live_child(self) -> None:
        live = mock.Mock()
        live.poll.return_value = None
        exited = mock.Mock()
        exited.poll.return_value = 0
        with mock.patch.object(agent_entrypoint, "terminate_process_group") as terminate:
            agent_entrypoint.stop_supervisor(None)
            agent_entrypoint.stop_supervisor(exited)
            agent_entrypoint.stop_supervisor(live)
        self.assertEqual(terminate.call_args_list, [mock.call(exited, agent_entrypoint.log), mock.call(live, agent_entrypoint.log)])

    def test_disabled_loop_publishes_status_without_starting_supervisor(self) -> None:
        args = argparse.Namespace(registry=Path("/tmp/registry.json"), max_workers=2)

        def stop_after_sleep(_seconds: float) -> None:
            agent_entrypoint._stop_requested = True

        with mock.patch.object(agent_entrypoint, "parse_args", return_value=args), mock.patch.object(
            agent_entrypoint,
            "install_signal_handlers",
        ), mock.patch.object(
            agent_entrypoint.agentd,
            "self_revision",
            return_value="rev-a",
        ), mock.patch.object(
            agent_entrypoint.agent_remote_operator,
            "poll_remote_operator",
        ), mock.patch.object(
            agent_entrypoint.agent_operator,
            "is_disabled",
            return_value=True,
        ), mock.patch.object(
            agent_entrypoint.time,
            "monotonic",
            return_value=10.0,
        ), mock.patch.object(
            agent_entrypoint,
            "publish_guard_status",
        ) as publish, mock.patch.object(
            agent_entrypoint,
            "start_supervisor",
        ) as start, mock.patch.object(
            agent_entrypoint.time,
            "sleep",
            side_effect=stop_after_sleep,
        ):
            self.assertEqual(agent_entrypoint.main(), 0)

        publish.assert_called_once_with("disabled", max_workers=2)
        start.assert_not_called()

    def test_enabled_loop_prepares_and_starts_supervisor_once(self) -> None:
        args = argparse.Namespace(registry=Path("/tmp/registry.json"), max_workers=2)
        repository = RepositoryContext(
            repository_id="project-a",
            repository="owner/project-a",
            control=Path("/tmp/control"),
            work=Path("/tmp/work"),
            checkpoints=Path("/tmp/checkpoints"),
        )
        child = mock.Mock()

        def stop_after_sleep(_seconds: float) -> None:
            agent_entrypoint._stop_requested = True

        with mock.patch.object(agent_entrypoint, "parse_args", return_value=args), mock.patch.object(
            agent_entrypoint,
            "install_signal_handlers",
        ), mock.patch.object(
            agent_entrypoint.agentd,
            "self_revision",
            return_value="rev-a",
        ), mock.patch.object(
            agent_entrypoint.agent_remote_operator,
            "poll_remote_operator",
        ), mock.patch.object(
            agent_entrypoint.agent_operator,
            "is_disabled",
            return_value=False,
        ), mock.patch.object(
            agent_entrypoint,
            "load_repository_registry",
            return_value=[repository],
        ), mock.patch.object(
            agent_entrypoint,
            "prepare_repositories",
        ) as prepare, mock.patch.object(
            agent_entrypoint,
            "start_supervisor",
            return_value=child,
        ) as start, mock.patch.object(
            agent_entrypoint,
            "stop_supervisor",
        ) as stop, mock.patch.object(
            agent_entrypoint.time,
            "sleep",
            side_effect=stop_after_sleep,
        ):
            self.assertEqual(agent_entrypoint.main(), 0)

        prepare.assert_called_once_with([repository])
        start.assert_called_once_with(args)
        stop.assert_called_once_with(child)

    def test_active_installation_lock_does_not_terminate_supervisor(self) -> None:
        args = argparse.Namespace(registry=Path("/tmp/registry.json"), max_workers=2)
        repository = RepositoryContext(
            repository_id="project-a", repository="owner/project-a",
            control=Path("/tmp/control"), work=Path("/tmp/work"),
            checkpoints=Path("/tmp/checkpoints"),
        )
        child = mock.Mock()
        sleeps = 0

        def stop_after_installation_probe(_seconds: float) -> None:
            nonlocal sleeps
            sleeps += 1
            if sleeps == 2:
                agent_entrypoint._stop_requested = True

        with (
            mock.patch.object(agent_entrypoint, "parse_args", return_value=args),
            mock.patch.object(agent_entrypoint, "install_signal_handlers"),
            mock.patch.object(agent_entrypoint.agentd, "self_revision", return_value="rev-a"),
            mock.patch.object(agent_entrypoint.agent_remote_operator, "poll_remote_operator"),
            mock.patch.object(agent_entrypoint.agent_operator, "is_disabled", return_value=False),
            mock.patch.object(agent_entrypoint, "installation_pending", side_effect=[False, True]),
            mock.patch.object(agent_entrypoint, "recover_interrupted_installation",
                              return_value=(False, "installation_lock_busy")) as recover,
            mock.patch.object(agent_entrypoint, "load_repository_registry",
                              return_value=[repository]),
            mock.patch.object(agent_entrypoint, "prepare_repositories"),
            mock.patch.object(agent_entrypoint, "start_supervisor", return_value=child) as start,
            mock.patch.object(agent_entrypoint, "stop_supervisor") as stop,
            mock.patch.object(agent_entrypoint.time, "sleep",
                              side_effect=stop_after_installation_probe),
        ):
            self.assertEqual(agent_entrypoint.main(), 0)

        self.assertEqual(sleeps, 2)
        start.assert_called_once_with(args)
        recover.assert_called_once_with()
        # One stop on explicit shutdown; never stop the live installing child.
        stop.assert_called_once_with(child)

    def test_emergency_disable_stops_active_installing_supervisor(self) -> None:
        args = argparse.Namespace(registry=Path("/tmp/registry.json"), max_workers=2)
        repository = RepositoryContext(
            repository_id="project-a", repository="owner/project-a",
            control=Path("/tmp/control"), work=Path("/tmp/work"),
            checkpoints=Path("/tmp/checkpoints"),
        )
        child = mock.Mock()
        state = {"installing": False, "sleep_count": 0}

        def recovery():
            state["installing"] = True
            return False, "installation_lock_busy"

        def disabled() -> bool:
            return state["installing"]

        def stop_after_sleep(_seconds: float) -> None:
            state["sleep_count"] += 1
            if state["sleep_count"] == 2:
                # Explicit emergency stop takes effect before the busy-lock
                # loop returns, not only when the installer unlocks.
                stop.assert_called_once_with(child)
                agent_entrypoint._stop_requested = True

        with (
            mock.patch.object(agent_entrypoint, "parse_args", return_value=args),
            mock.patch.object(agent_entrypoint, "install_signal_handlers"),
            mock.patch.object(agent_entrypoint.agentd, "self_revision", return_value="rev-a"),
            mock.patch.object(agent_entrypoint.agent_remote_operator, "poll_remote_operator"),
            mock.patch.object(agent_entrypoint.agent_operator, "is_disabled", side_effect=disabled),
            mock.patch.object(agent_entrypoint, "installation_pending", side_effect=[False, True]),
            mock.patch.object(agent_entrypoint, "recover_interrupted_installation",
                              side_effect=recovery),
            mock.patch.object(agent_entrypoint, "load_repository_registry",
                              return_value=[repository]),
            mock.patch.object(agent_entrypoint, "prepare_repositories"),
            mock.patch.object(agent_entrypoint, "start_supervisor", return_value=child),
            mock.patch.object(agent_entrypoint, "stop_supervisor") as stop,
            mock.patch.object(agent_entrypoint.time, "sleep", side_effect=stop_after_sleep),
        ):
            self.assertEqual(agent_entrypoint.main(), 0)

        self.assertEqual(state["sleep_count"], 2)
        stop.assert_called_once_with(child)

    def test_revision_change_reexecs_guard_before_task_admission(self) -> None:
        args = argparse.Namespace(registry=Path("/tmp/registry.json"), max_workers=2)
        with mock.patch.object(agent_entrypoint, "parse_args", return_value=args), mock.patch.object(
            agent_entrypoint,
            "install_signal_handlers",
        ), mock.patch.object(
            agent_entrypoint.agentd,
            "self_revision",
            side_effect=["rev-a", "rev-b", "rev-b"],
        ), mock.patch.object(
            agent_entrypoint.agent_remote_operator,
            "poll_remote_operator",
        ), mock.patch.object(
            agent_entrypoint,
            "stop_supervisor",
        ) as stop, mock.patch.object(
            agent_entrypoint.os,
            "execv",
            side_effect=RuntimeError("reexec intercepted"),
        ) as execv:
            with self.assertRaisesRegex(RuntimeError, "reexec intercepted"):
                agent_entrypoint.main()

        stop.assert_called_once_with(None)
        execv.assert_called_once_with(
            agent_entrypoint.sys.executable,
            agent_entrypoint._self_reexec_args(args),
        )


if __name__ == "__main__":
    unittest.main()
