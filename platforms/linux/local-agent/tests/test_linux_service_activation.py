"""Narrow offline tests of external persistent-service lifecycle source.

All systemd operations are simulated. No real service is ever staged or
enabled; these tests are NOT native installed-service acceptance.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from linux_agent.release_controller import ServiceSwitchError
from linux_agent.service_activation import PersistentServiceActivation
from linux_agent.service_plan import ReleasePair


class ServiceActivationOfflineTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="linux-service-activation-offline-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.root.chmod(0o700)
        sha1, sha2 = "1" * 40, "2" * 40
        self.candidate = self.root / "releases" / sha1
        self.rollback = self.root / "releases" / sha2
        for item in (self.candidate, self.rollback):
            p = item / "platforms/linux/local-agent/linux_agent"
            p.mkdir(parents=True)
            (p / "__main__.py").write_text("pass\n")
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.stop = self.state / "EMERGENCY_STOP"
        self.stop.write_text("disabled\n")
        self.original = self.root / "pilot"
        self.original.mkdir(mode=0o700)
        (self.original / "EMERGENCY_STOP").write_text("disabled\n")
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "version": 1,
            "host_id": "fixture",
            "installation_id": "fixture-1",
            "repository": "git@github.com:fixture/repo.git",
            "branch": "agent-control", "enabled": True,
            "host_intake": {"enabled": True, "authorization": "github_writers"},
        }))
        self.config.chmod(0o600)
        self.pair = ReleasePair(
            candidate=self.candidate, candidate_sha=sha1,
            rollback=self.rollback, rollback_sha=sha2,
            python=Path(sys.executable).resolve(),
            config=self.config, state=self.state,
        )
        self.controller = PersistentServiceActivation(self.pair)
        self.controller.stage.unit_dir = self.root / "home/.config/systemd/user"
        self.controller.stage.unit_dir.mkdir(mode=0o700, parents=True)
        self.controller.stage.unit_file = self.controller.stage.unit_dir / "local-agent-linux.service"
        unit = self.pair.unit_for(self.candidate, sha1)
        self.controller.stage.unit_file.write_text(unit)
        self.controller.stage._write_phase("staged", unit)
        self.manager = {"active": False, "enabled": False}
        self.actions: list[str] = []
        self.addCleanup(patch.stopall)
        patch("linux_agent.service_activation.verify_clean_exact_source").start()
        patch("linux_agent.service_activation._run_systemctl", side_effect=self.systemd).start()
        self.running_patch = patch.object(
            self.controller, "_is_running",
            side_effect=lambda: 6789 if self.manager["active"] else None,
        )
        self.running_patch.start()
        patch.object(self.controller, "_require_linger").start()

    def systemd(self, *args: str, **_kw: object) -> str:
        cmd = args[0]
        self.actions.append(cmd)
        if cmd == "show":
            prop = args[2].removeprefix("--property=")
            return {
                "LoadState": "loaded",
                "FragmentPath": str(self.controller.stage.unit_file),
                "ActiveState": "active" if self.manager["active"] else "inactive",
                "ControlGroup": "/user.slice/mock",
            }.get(prop, "")
        if cmd == "is-enabled":
            return "enabled" if self.manager["enabled"] else "disabled"
        if cmd == "start":
            self.manager["active"] = True
        if cmd == "stop":
            self.manager["active"] = False
        if cmd == "enable":
            self.manager["enabled"] = True
        if cmd == "disable":
            self.manager["enabled"] = False
        return ""

    def test_stopped_inspect_only_reads_staged_unit(self) -> None:
        self.assertEqual(self.controller.inspect()["phase"], "not_started")
        self.assertTrue(self.stop.exists())
        self.assertFalse(self.manager["active"])

    def test_first_start_then_independent_stop(self) -> None:
        running = self.controller.start()
        self.assertEqual(running["phase"], "running")
        self.assertEqual(running["active_pid"], 6789)
        self.assertTrue(running["enabled"])
        self.assertFalse(running["emergency_stop"])
        self.assertFalse(self.stop.exists())
        self.assertTrue((self.original / "EMERGENCY_STOP").exists())
        stopped = self.controller.stop()
        self.assertEqual(stopped["phase"], "stopped")
        self.assertIsNone(stopped["active_pid"])
        self.assertFalse(stopped["enabled"])
        self.assertTrue(stopped["emergency_stop"])
        self.assertTrue((self.original / "EMERGENCY_STOP").exists())

    def test_repeat_start_of_healthy_known_unit_is_idempotent(self) -> None:
        self.controller.start()
        second = self.controller.start()
        self.assertEqual(second["phase"], "running")
        self.assertEqual(self.actions.count("start"), 1)

    def test_durable_running_receipt_precedes_enable(self) -> None:
        actual_systemd = self.systemd

        def check_enable(*args: str, **kwargs: object) -> str:
            if args[0] == "enable":
                self.assertEqual(
                    json.loads(self.controller.journal.read_text())["phase"],
                    "running",
                )
                self.assertFalse(self.stop.exists())
            return actual_systemd(*args, **kwargs)

        with patch("linux_agent.service_activation._run_systemctl",
                   side_effect=check_enable):
            self.assertEqual(self.controller.start()["phase"], "running")
        self.assertTrue(self.manager["enabled"])

    def test_uncommitted_running_phase_never_enables_autostart(self) -> None:
        original_record = self.controller._record

        def interrupted_receipt(phase: str, unit: str) -> None:
            if phase == "running":
                raise ServiceSwitchError("simulated durable journal failure")
            original_record(phase, unit)

        with patch.object(self.controller, "_record",
                          side_effect=interrupted_receipt):
            with self.assertRaisesRegex(ServiceSwitchError, "journal failure"):
                self.controller.start()
        self.assertEqual(self.actions.count("enable"), 0)
        self.assertTrue(self.stop.exists())
        self.assertFalse(self.manager["enabled"])
        self.assertFalse(self.manager["active"])
        self.assertEqual(self.controller.inspect()["phase"], "stopped")

    def test_failed_start_rearms_stop_and_disables_unit(self) -> None:
        def fail_start(*args: str, **kwargs: object) -> str:
            if args[0] == "start":
                raise ServiceSwitchError("service start refused")
            return self.systemd(*args, **kwargs)

        with patch("linux_agent.service_activation._run_systemctl", side_effect=fail_start):
            with self.assertRaisesRegex(ServiceSwitchError, "start refused"):
                self.controller.start()
        self.assertEqual(self.controller.inspect()["phase"], "stopped")
        self.assertTrue(self.stop.exists())
        self.assertFalse(self.manager["enabled"])
        self.assertFalse(self.manager["active"])

    def test_interrupted_activation_must_reconcile_without_replaying(self) -> None:
        unit = self.pair.unit_for(self.candidate, self.pair.candidate_sha)
        self.controller._record("starting", unit)
        self.stop.unlink()
        self.manager["active"] = True
        self.manager["enabled"] = True
        with self.assertRaisesRegex(ServiceSwitchError, "interrupted activation"):
            self.controller.start()
        info = self.controller.reconcile()
        self.assertEqual(info["phase"], "stopped")
        self.assertTrue(self.stop.exists())
        self.assertFalse(self.manager["active"])
        self.assertFalse(self.manager["enabled"])

    def test_stop_attempts_systemd_stop_even_if_stop_file_write_fails(self) -> None:
        self.controller.start()
        with patch("linux_agent.service_activation._write_stop",
                   side_effect=OSError("simulated storage failure")):
            with self.assertRaisesRegex(ServiceSwitchError, "STOP could not be rearmed"):
                self.controller.stop()
        self.assertIn("disable", self.actions)
        self.assertIn("stop", self.actions)
        self.assertFalse(self.manager["active"])
        self.assertFalse(self.manager["enabled"])
        self.assertEqual(
            json.loads(self.controller.journal.read_text())["phase"], "stopping",
        )  # Cleanup was uncertain, so never mark as accepted stopped.

    def test_stop_attempts_systemd_stop_even_if_disable_fails(self) -> None:
        self.controller.start()
        systemd = self.systemd

        def broken_disable(*args: str, **kwargs: object) -> str:
            if args[0] == "disable":
                raise ServiceSwitchError("simulated disable failure")
            return systemd(*args, **kwargs)

        with patch("linux_agent.service_activation._run_systemctl",
                   side_effect=broken_disable):
            with self.assertRaisesRegex(ServiceSwitchError, "systemd disable failed"):
                self.controller.stop()
        self.assertTrue(self.stop.exists())
        self.assertIn("stop", self.actions)
        self.assertFalse(self.manager["active"])
        self.assertEqual(
            json.loads(self.controller.journal.read_text())["phase"], "stopping",
        )

    def test_crash_after_running_receipt_before_enable_cold_reconciles(self) -> None:
        # Process started and durable journal updated, but the manager was
        # never enabled for a later reboot.
        unit = self.pair.unit_for(self.candidate, self.pair.candidate_sha)
        self.controller._record("starting", unit)
        self.stop.unlink()
        self.manager["active"] = True
        self.controller._record("running", unit)
        self.assertFalse(self.manager["enabled"])
        with self.assertRaisesRegex(ServiceSwitchError, "disagrees with systemd"):
            self.controller.start()
        result = self.controller.reconcile()
        self.assertEqual(result["phase"], "stopped")
        self.assertTrue(result["emergency_stop"])
        self.assertFalse(self.manager["active"])
        self.assertFalse(self.manager["enabled"])

    def test_reconcile_refuses_to_stop_healthy_running_agent(self) -> None:
        self.controller.start()
        before = list(self.actions)
        with self.assertRaisesRegex(ServiceSwitchError, "healthy running service"):
            self.controller.reconcile()
        self.assertNotIn("stop", self.actions[len(before):])
        self.assertFalse(self.stop.exists())
        self.assertTrue(self.manager["active"])

    def test_crash_after_enable_with_healthy_receipt_keeps_service(self) -> None:
        self.controller.start()
        # Emulate operator invocation after SSH connection died during
        # post-enable inspect: durable 'running' and systemd agree.
        before = self.actions.count("start")
        self.assertEqual(self.controller.start()["phase"], "running")
        self.assertEqual(self.actions.count("start"), before)
        self.assertFalse(self.stop.exists())
        self.assertTrue(self.manager["enabled"])

    def test_external_service_restart_preserves_durable_running_identity(self) -> None:
        self.controller.start()
        # A new CLI process instantiates a fresh controller after a simulated
        # boot/login boundary and must treat a matching live systemd unit as
        # the same running installation, without starting a duplicate agent.
        fresh = PersistentServiceActivation(self.pair)
        fresh.stage.unit_dir = self.controller.stage.unit_dir
        fresh.stage.unit_file = self.controller.stage.unit_file
        with patch.object(fresh, "_require_linger"), patch.object(fresh, "_is_running",
                          side_effect=lambda: 6789 if self.manager["active"] else None):
            before = self.actions.count("start")
            report = fresh.start()
            self.assertEqual(report["phase"], "running")
            self.assertEqual(report["active_pid"], 6789)
            self.assertTrue(report["enabled"])
            self.assertEqual(self.actions.count("start"), before)
            self.assertFalse(self.stop.exists())

    def _checked_main_pid(self, *, env: bytes | None = None,
                          argv: bytes | None = None,
                          cgroup: str | None = None,
                          uid: int | None = None, upgrade: bool = False) -> int:
        self.running_patch.stop()
        from linux_agent.service_plan import source_pythonpath
        from linux_agent.service_upgrade import PersistentServiceUpgrade
        expected_source = source_pythonpath(self.candidate)
        controller = PersistentServiceUpgrade(self.pair) if upgrade else self.controller
        module = "linux_agent.service_upgrade" if upgrade else "linux_agent.service_activation"
        values = {
            "ActiveState": "active",
            "MainPID": "12345",
            "ControlGroup": "/user.slice/local-agent-linux.service",
        }
        expected_argv = [
            str(self.pair.python), "-B", "-m", "linux_agent",
            "--config", str(self.pair.config),
            "--state", str(self.pair.state), "serve",
        ]
        raw_env = env if env is not None else (
            "PYTHONPATH=" + expected_source
        ).encode() + b"\x00"
        raw_argv = argv if argv is not None else (
            "\x00".join(expected_argv) + "\x00"
        ).encode()
        raw_group = cgroup if cgroup is not None else (
            "0::/user.slice/local-agent-linux.service\n"
        )

        def read_bytes(path: Path) -> bytes:
            if path.name == "environ":
                return raw_env
            if path.name == "cmdline":
                return raw_argv
            raise AssertionError("unexpected process bytes")

        with patch(module + ".source_pythonpath", return_value=expected_source), patch.object(
            controller, "_service" if upgrade else "_systemd",
                          side_effect=lambda key: values[key]), patch(
            "pathlib.Path.stat",
            return_value=SimpleNamespace(st_uid=os.getuid() if uid is None else uid),
        ), patch("pathlib.Path.read_bytes", autospec=True,
                 side_effect=read_bytes), patch(
            "pathlib.Path.read_text", return_value=raw_group,
        ):
            return (controller._active_pid(self.candidate) if upgrade
                    else controller._is_running())

    def test_shared_core_unit_identity_is_accepted_by_activation_and_upgrade(self) -> None:
        adapter = self.candidate / "platforms/linux/local-agent/linux_agent"
        (adapter / "__init__.py").write_text("# package bridge fixture\n")
        core = self.candidate / "platforms/linux/core/linux_agent"
        core.mkdir(parents=True)
        (core / "__main__.py").write_text("pass\n")
        unit = self.pair.unit_for(self.candidate, self.pair.candidate_sha)
        declared = next(line for line in unit.splitlines()
                        if line.startswith("Environment=PYTHONPATH="))
        raw_env = declared.removeprefix("Environment=").encode() + b"\0"
        self.assertIn(b"/platforms/linux/core", raw_env)
        for upgrade in (False, True):
            with self.subTest(upgrade=upgrade):
                self.assertEqual(self._checked_main_pid(env=raw_env, upgrade=upgrade), 12345)

    def test_live_process_exact_identity_passes(self) -> None:
        self.assertEqual(self._checked_main_pid(), 12345)

    def test_live_process_source_mismatch_rejected(self) -> None:
        with self.assertRaisesRegex(ServiceSwitchError, "wrong source revision"):
            self._checked_main_pid(env=b"PYTHONPATH=/tmp/incorrect-release\x00")

    def test_live_process_argv_mismatch_rejected(self) -> None:
        with self.assertRaisesRegex(ServiceSwitchError, "command/identity mismatch"):
            self._checked_main_pid(argv=b"/usr/bin/python3\x00-m\x00wrong_module\x00")

    def test_live_process_cgroup_mismatch_rejected(self) -> None:
        with self.assertRaisesRegex(ServiceSwitchError, "service cgroup"):
            self._checked_main_pid(cgroup="0::/user.slice/unrelated.service\n")

    def test_live_process_owner_mismatch_rejected(self) -> None:
        with self.assertRaisesRegex(ServiceSwitchError, "another OS user"):
            self._checked_main_pid(uid=os.getuid() + 1)

    def test_live_process_requires_main_pid_and_cgroup(self) -> None:
        self.running_patch.stop()
        for data in (
            {"ActiveState": "active", "MainPID": "0", "ControlGroup": "/mock"},
            {"ActiveState": "active", "MainPID": "12345", "ControlGroup": ""},
        ):
            with self.subTest(data=data), patch.object(
                self.controller, "_systemd", side_effect=lambda field: data[field],
            ):
                with self.assertRaisesRegex(ServiceSwitchError, "verified process/cgroup"):
                    self.controller._is_running()

    def test_missing_original_pilot_stop_refuses_service(self) -> None:
        (self.original / "EMERGENCY_STOP").unlink()
        with self.assertRaisesRegex(ServiceSwitchError, "original pilot"):
            self.controller.start()
        self.assertTrue(self.stop.exists())

    def test_missing_staged_journal_refuses(self) -> None:
        self.controller.stage.journal.unlink()
        with self.assertRaisesRegex(ServiceSwitchError, "staged journal"):
            self.controller.start()
        self.assertTrue(self.stop.exists())

    def test_duplicate_activation_journal_fields_fail_closed(self) -> None:
        unit = self.pair.unit_for(self.candidate, self.pair.candidate_sha)
        self.controller._record("stopped", unit)
        data = self.controller.journal.read_text()
        self.controller.journal.write_text(data[:-2] + ',"phase":"running"}\n')
        with self.assertRaises(ServiceSwitchError):
            self.controller.inspect()
        self.assertTrue(self.stop.exists())


if __name__ == "__main__":
    unittest.main()
