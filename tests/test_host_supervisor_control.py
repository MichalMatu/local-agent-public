"""Hermetic host control authority tests: no live repository or host mutation."""
from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.repository.host_routing import HostIdentity
import local_agent.daemon.service as agentd
import local_agent.supervisor.orchestrator as parallel
import local_agent.supervisor.serial as serial
import local_agent.foundation.storage as storage
from local_agent.supervisor.control import bind_supervisor_control
from local_agent.supervisor.host_control import (
    HostControlSource, load_host_control_source, validate_host_control_checkout,
    require_host_control_request,
)


class HostControlSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.config = self.home / "supervisor-control.json"
        self.identity = self.home / "identity.json"
        self.checkout = self.home / "dedicated-control"
        self.identity.write_text(json.dumps(HostIdentity(
            host_id="macbook-one",
            installation_id="b0e94f93-478f-4fc6-abed-159d3702362f",
        ).as_record()), encoding="utf-8")
        self.identity.chmod(0o600)

    def write(self, *, enabled: bool = True, **replace: object) -> None:
        payload = {
            "version": 1, "enabled": enabled,
            "host_id": "macbook-one",
            "identity_file": str(self.identity),
            "control_checkout": str(self.checkout),
            "repository": "customer/local-agent-inbox",
        }
        payload.update(replace)
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        self.config.chmod(0o600)

    def test_absence_and_explicit_disable_do_not_select_a_project(self) -> None:
        self.assertIsNone(load_host_control_source(self.config))
        self.write(enabled=False)
        self.assertIsNone(load_host_control_source(self.config))

    def test_active_control_source_is_installation_bound_and_not_a_project_queue(self) -> None:
        self.write()
        source = load_host_control_source(self.config)
        self.assertIsInstance(source, HostControlSource)
        assert source is not None
        self.assertEqual(source.host_id, "macbook-one")
        self.assertEqual(source.installation_id, "b0e94f93-478f-4fc6-abed-159d3702362f")
        self.assertEqual(source.namespace, ".agent/hosts/macbook-one/supervisor/b0e94f93-478f-4fc6-abed-159d3702362f")
        self.assertEqual(source.request_path, ".agent/hosts/macbook-one/supervisor/b0e94f93-478f-4fc6-abed-159d3702362f/control.json")
        self.assertEqual(source.ack_directory, ".agent/hosts/macbook-one/supervisor/b0e94f93-478f-4fc6-abed-159d3702362f/acks")
        self.assertEqual(source.status_path, ".agent/hosts/macbook-one/supervisor/b0e94f93-478f-4fc6-abed-159d3702362f/status.json")
        self.assertNotIn(".agent/daemon/control.json", source.request_path)

    def test_enabled_control_requires_explicit_nonexample_repository(self) -> None:
        self.write(repository="customer/local-agent-inbox")
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        payload.pop("repository")
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "explicitly selected repository"):
            load_host_control_source(self.config)
        self.write(repository="example-owner/local-agent")
        with self.assertRaisesRegex(ValueError, "example repository"):
            load_host_control_source(self.config)
        self.write(repository="customer/local-agent-inbox")
        source = load_host_control_source(self.config)
        assert source is not None
        self.assertEqual(source.repository, "customer/local-agent-inbox")

    def test_mismatched_host_identity_fails_closed(self) -> None:
        self.write(host_id="other-host")
        with self.assertRaisesRegex(ValueError, "identity does not match"):
            load_host_control_source(self.config)

    def test_rejects_unexpected_or_duplicate_fields(self) -> None:
        self.write(extra="unexpected")
        with self.assertRaisesRegex(ValueError, "schema"):
            load_host_control_source(self.config)
        self.config.write_text('{"version":1,"version":1}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            load_host_control_source(self.config)

    def test_rejects_relative_and_symlinked_paths(self) -> None:
        self.write(control_checkout="control")
        with self.assertRaisesRegex(ValueError, "absolute"):
            load_host_control_source(self.config)
        self.checkout.mkdir()
        alias = self.home / "alias-control"
        alias.symlink_to(self.checkout, target_is_directory=True)
        self.write(control_checkout=str(alias))
        with self.assertRaisesRegex(ValueError, "isolated"):
            load_host_control_source(self.config)

    def test_config_oversize_or_non_boolean_toggle_is_invalid(self) -> None:
        self.write(enabled=1)
        with self.assertRaisesRegex(ValueError, "boolean"):
            load_host_control_source(self.config)
        self.config.write_bytes(b"x" * 4097)
        with self.assertRaisesRegex(ValueError, "byte limit"):
            load_host_control_source(self.config)

    def test_new_control_namespace_never_reuses_legacy_queue_or_gc(self) -> None:
        self.write()
        source = load_host_control_source(self.config)
        assert source is not None
        original = {k: getattr(agentd, k) for k in (
            "REMOTE_TASKS_DIR", "REMOTE_RESULTS_DIR", "REMOTE_RUNS_DIR",
            "REMOTE_DAEMON_STATUS", "REMOTE_CONTROL_REQUEST", "REMOTE_CONTROL_ACK_DIR",
        )}
        initial_root, initial_branch = agentd.core.CONTROL, agentd.core.CONTROL_BRANCH
        try:
            bind_supervisor_control(source, daemon_version="test")
            self.assertEqual(agentd.REMOTE_CONTROL_REQUEST, source.request_path)
            self.assertEqual(agentd.REMOTE_DAEMON_STATUS, source.status_path)
            self.assertEqual(agentd.REMOTE_CONTROL_ACK_DIR, source.ack_directory)
            dirs, gc_enabled = storage._recoverable_control_paths(agentd.REMOTE_TASKS_DIR)
            self.assertFalse(gc_enabled)
            self.assertIn(source.status_path, dirs)
            self.assertNotIn(".agent/status", dirs)
            self.assertNotIn(".agent/results", dirs)
        finally:
            for name, value in original.items():
                setattr(agentd, name, value)
            agentd.core.CONTROL = initial_root
            agentd.core.CONTROL_BRANCH = initial_branch

    def test_host_control_selection_never_uses_first_project_if_configured(self) -> None:
        from local_agent.supervisor.host_control import select_global_control
        from local_agent.repository.context import RepositoryContext
        project = RepositoryContext(
            repository_id="example-project", repository="owner/example-project",
            control=self.home / "grow-control", work=self.home / "grow-work",
            checkpoints=self.home / "grow-checkpoints",
        )
        source = HostControlSource(
            "macbook-one", "b0e94f93-478f-4fc6-abed-159d3702362f", self.checkout,
        )
        with mock.patch(
            "local_agent.supervisor.host_control.load_host_control_source",
            return_value=source,
        ), mock.patch(
            "local_agent.supervisor.host_control.validate_host_control_checkout",
        ) as validate:
            chosen = select_global_control(
                [project], state_dir=self.home, production_checkout=self.home / "installed",
            )
        self.assertIs(chosen, source)
        validate.assert_called_once()
        self.assertEqual(chosen.repository_id, "local-agent-supervisor")

    def test_parallel_global_control_runs_with_zero_project_workers(self) -> None:
        source = HostControlSource(
            "macbook-one", "b0e94f93-478f-4fc6-abed-159d3702362f", self.checkout,
        )
        with mock.patch.object(
            parallel.host_supervisor_control, "selected_host_control_source",
            return_value=source,
        ), mock.patch.object(
            parallel, "supervisor_control_leases",
            return_value=contextlib.nullcontext(),
        ) as leases, mock.patch.object(
            parallel.supervisor_control, "bind_supervisor_control",
        ) as bind, mock.patch.object(
            parallel.supervisor_control, "sync_control_quietly",
        ), mock.patch.object(
            parallel, "publish_local_supervisor_status",
        ), mock.patch.object(
            parallel, "handle_bound_disable_control", return_value=False,
        ), mock.patch.object(
            agentd, "publish_daemon_status",
        ), mock.patch.object(
            agentd, "handle_control_request",
        ), mock.patch.object(
            agentd, "maybe_self_update", return_value=False,
        ), mock.patch.object(
            parallel.conversation_supervisor, "service_control_plane",
        ), mock.patch.object(
            parallel.operator_observability, "publish_operator_status",
        ), mock.patch.object(
            parallel, "route_parallel_restarts", return_value=contextlib.nullcontext(),
        ), mock.patch.object(
            parallel, "bound_control_request_from_checkout", return_value=None,
        ):
            self.assertTrue(parallel.service_control(
                [], registry_path=self.home / "empty.json", max_workers=2, once=True,
            ))
        leases.assert_called_once_with([], control_source=source)
        bind.assert_called_once_with(source, daemon_version=parallel.PARALLEL_DAEMON_VERSION)

    def test_parallel_supervisor_starts_and_exits_once_with_zero_projects(self) -> None:
        source = HostControlSource(
            "macbook-one", "b0e94f93-478f-4fc6-abed-159d3702362f", self.checkout,
        )
        options = argparse.Namespace(registry=self.home / "empty.json", max_workers=2, once=True)
        with mock.patch.object(
            parallel, "parse_args", return_value=options,
        ), mock.patch.object(
            parallel.agentd, "acquire_daemon_lock", return_value=object(),
        ), mock.patch.object(
            parallel, "install_signal_handlers",
        ), mock.patch.object(
            parallel.host_supervisor_control, "load_host_control_source",
            return_value=source,
        ), mock.patch.object(
            parallel, "load_repository_registry", return_value=[],
        ) as load_registry, mock.patch.object(
            parallel.host_supervisor_control, "selected_host_control_source",
            return_value=source,
        ), mock.patch.object(
            parallel, "service_control", return_value=True,
        ) as service, mock.patch.object(
            parallel, "publish_local_supervisor_status",
        ), mock.patch.object(
            parallel.conversation_supervisor, "reap", return_value=None,
        ), mock.patch.object(
            parallel.conversation_supervisor, "start_if_pending", return_value=None,
        ), mock.patch.object(
            parallel.conversation_supervisor, "result_publish_pending", return_value=False,
        ), mock.patch.object(
            parallel.agent_operator, "is_disabled", return_value=False,
        ), mock.patch.object(
            parallel, "maintain_local_log_files", return_value=(),
        ):
            self.assertEqual(parallel.main(), 0)
        self.assertTrue(all(call.kwargs.get("allow_empty") for call in load_registry.call_args_list))
        service.assert_called()

    def test_serial_supervisor_can_poll_with_zero_projects(self) -> None:
        source = HostControlSource(
            "macbook-one", "b0e94f93-478f-4fc6-abed-159d3702362f", self.checkout,
        )
        options = argparse.Namespace(registry=self.home / "empty.json", once=True)
        with mock.patch.object(
            serial, "parse_args", return_value=options,
        ), mock.patch.object(
            serial.agentd, "acquire_daemon_lock", return_value=object(),
        ), mock.patch.object(
            serial, "install_signal_handlers",
        ), mock.patch.object(
            serial, "supervisor_control_repository", return_value=source,
        ), mock.patch.object(
            serial, "bind_supervisor_control",
        ), mock.patch.object(
            serial.host_supervisor_control, "host_supervisor_lease",
            return_value=contextlib.nullcontext(),
        ), mock.patch.object(
            serial, "sync_control_quietly",
        ), mock.patch.object(
            serial.agentd, "publish_daemon_status",
        ), mock.patch.object(
            serial.remote_operator, "poll_remote_operator",
        ), mock.patch.object(
            serial, "scheduler_due_actions", return_value=("full_scan",),
        ), mock.patch.object(
            serial, "run_cycle", return_value=(False, None),
        ), mock.patch.object(
            serial, "load_registered_projects", return_value=[],
        ):
            self.assertEqual(serial.main(), 0)

    def test_host_control_request_requires_exact_installation_and_action(self) -> None:
        source = HostControlSource(
            "macbook-one", "b0e94f93-478f-4fc6-abed-159d3702362f", self.checkout,
        )
        req = {
            "version": 1, "id": "host-01", "action": "status",
            "target_host_id": source.host_id,
            "target_installation_id": source.installation_id,
        }
        self.assertEqual(require_host_control_request(source, req), req)
        for patch in (
            {"target_host_id": "other-host"},
            {"target_installation_id": "7180d453-1357-4fbc-be1a-e1e5b8fbb10a"},
            {"action": "shell"},
            {"version": 2},
            {"id": "../escape"},
            {"extra": True},
        ):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                require_host_control_request(source, {**req, **patch})


    def test_invalid_host_request_never_reaches_global_restart(self) -> None:
        source = HostControlSource(
            "macbook-one", "b0e94f93-478f-4fc6-abed-159d3702362f", self.checkout,
        )
        control_path = self.checkout / source.request_path
        control_path.parent.mkdir(parents=True)
        control_path.write_text(json.dumps({
            "version": 1, "id": "old-restart", "action": "restart",
            "target_host_id": "macbook-one",
            "target_installation_id": "7180d453-1357-4fbc-be1a-e1e5b8fbb10a",
        }), encoding="utf-8")
        old_control = agentd.core.CONTROL
        old_request = agentd.REMOTE_CONTROL_REQUEST
        try:
            agentd.core.CONTROL = source.checkout
            agentd.REMOTE_CONTROL_REQUEST = source.request_path
            with mock.patch.object(parallel, "log"):
                self.assertIsNone(parallel.bound_control_request_from_checkout(source))
        finally:
            agentd.core.CONTROL = old_control
            agentd.REMOTE_CONTROL_REQUEST = old_request

    def test_machine_diagnostics_do_not_acquire_unrelated_project_leases(self) -> None:
        source = HostControlSource(
            "macbook-one", "b0e94f93-478f-4fc6-abed-159d3702362f", self.checkout,
        )
        request = {
            "version": 1, "id": "probe-1", "action": "host_diagnostics",
            "target_host_id": source.host_id,
            "target_installation_id": source.installation_id,
        }
        with mock.patch.object(
            parallel.host_supervisor_control, "validate_host_control_checkout",
        ), mock.patch.object(
            parallel.host_supervisor_control, "host_supervisor_lease",
            return_value=contextlib.nullcontext(),
        ) as lease, mock.patch.object(
            parallel.supervisor_control, "bind_supervisor_control",
        ), mock.patch.object(
            parallel.supervisor_control, "sync_control_quietly",
        ), mock.patch.object(
            parallel, "handle_bound_disable_control", return_value=False,
        ), mock.patch.object(
            parallel.conversation_supervisor, "service_control_plane",
        ), mock.patch.object(
            parallel, "current_bound_request", return_value=request,
        ), mock.patch.object(
            parallel, "supervisor_control_leases",
            side_effect=AssertionError("project leases must not be acquired"),
        ):
            self.assertEqual(
                parallel.probe_control_request(source, repositories=[]),
                parallel.ControlProbeResult.CLEAR,
            )
        lease.assert_called_once_with(source)

    def test_checkout_origin_and_branch_are_trusted_not_inferred_from_projects(self) -> None:
        source = HostControlSource("macbook-one", "b0e94f93-478f-4fc6-abed-159d3702362f", self.checkout)
        (self.checkout / ".git").mkdir(parents=True)
        valid = [
            subprocess.CompletedProcess([], 0, "git@github.com:example-owner/local-agent.git\n"),
            subprocess.CompletedProcess([], 0, "agent-control\n"),
        ]
        with mock.patch("subprocess.run", side_effect=valid) as run:
            validate_host_control_checkout(source, production_checkout=self.home / "source")
        self.assertEqual(run.call_count, 2)

        mismatch = [subprocess.CompletedProcess([], 0, "git@github.com:example-owner/example-project.git")]
        with mock.patch("subprocess.run", side_effect=mismatch):
            with self.assertRaisesRegex(ValueError, "trusted Local Agent"):
                validate_host_control_checkout(source, production_checkout=self.home / "source")

    def test_host_git_publication_requires_only_local_author(self) -> None:
        from local_agent.supervisor.host_control import require_host_publication_identity

        source = HostControlSource(
            "macbook-one", "b0e94f93-478f-4fc6-abed-159d3702362f", self.checkout,
        )
        (self.checkout / ".git").mkdir(parents=True)
        with mock.patch("subprocess.run", side_effect=[
            subprocess.CompletedProcess([], 1, ""),
        ]) as run:
            with self.assertRaisesRegex(ValueError, "local Git user.name"):
                require_host_publication_identity(source)
        self.assertIn("--local", run.call_args.args[0])

        with mock.patch("subprocess.run", side_effect=[
            subprocess.CompletedProcess([], 0, "Local Agent\n"),
            subprocess.CompletedProcess([], 0, "local-agent@localhost.invalid\n"),
        ]) as run:
            require_host_publication_identity(source)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all("--local" in call.args[0] for call in run.call_args_list))

    def test_project_workspace_overlap_and_production_alias_are_rejected(self) -> None:
        source = HostControlSource("macbook-one", "b0e94f93-478f-4fc6-abed-159d3702362f", self.checkout)
        with self.assertRaisesRegex(ValueError, "isolated"):
            validate_host_control_checkout(source, production_checkout=self.checkout)
        with self.assertRaisesRegex(ValueError, "overlaps"):
            validate_host_control_checkout(
                source, production_checkout=self.home / "installed",
                project_workspaces=(self.checkout / "nested-work",),
            )


if __name__ == "__main__":
    unittest.main()
