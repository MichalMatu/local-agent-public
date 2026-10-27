"""Source-only preservation and incompatibility gates for historical Pi B2.

No Mac runtime import, local service interaction or physical hardware claim.
These are *source contract* tests, not proof of B2 project parity on Pi.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from linux_agent.agent import validate_linux_host_subset

REPOSITORY = Path(__file__).resolve().parents[4]
LEGACY_UNIT = (
    REPOSITORY / "platforms/macos/local-agent/platform/linux_systemd.py"
)
PI_ENTRYPOINT = (
    REPOSITORY / "platforms/raspberry-pi/local-agent/pi_agent/__main__.py"
)


class HistoricalB2PreservationTests(unittest.TestCase):
    def test_legacy_b2_project_supervisor_source_is_preserved(self) -> None:
        """Protect the source still required by an old Pi B2 rollback."""
        source = LEGACY_UNIT.read_text(encoding="utf-8")
        for required in (
            "def render_service(",
            "def check_installation(",
            "-m local_agent.supervisor.serial --registry",
            "local_agent/supervisor/worker.py",
            "load_repository_registry(",
            "repo.agent_binding is None",
            "repo.host_execution_enabled",
            "User={user}",
            "ExecStartPre={preflight}",
        ):
            with self.subTest(required=required):
                self.assertIn(required, source)
        self.assertNotIn("-m pi_agent", source)

    def test_new_pi_port_does_not_import_legacy_mac_scheduler(self) -> None:
        source = PI_ENTRYPOINT.read_text(encoding="utf-8")
        self.assertIn("from linux_agent.__main__ import main as linux_main", source)
        self.assertNotIn("local_agent.supervisor.serial", source)
        self.assertNotIn("local_agent.platform.linux_systemd", source)

    def test_new_linux_rejects_repository_fields_before_claim(self) -> None:
        """The Linux engine must not silently accept B2 project metadata."""
        with tempfile.TemporaryDirectory() as directory:
            task = {
                "version": 1,
                "id": "pi-project-contract",
                "mode": "commands",
                "cwd": directory,
                "commands": ["printf source_only"],
                "repository_id": "local-agent",
                "agent_binding": "b0e94f93-478f-4fc6-abed-159d3702362f",
            }
            with self.assertRaisesRegex(ValueError, "agent_binding|repository_id"):
                validate_linux_host_subset(task)

    def test_new_linux_refuses_b2_project_execution_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task = {
                "version": 1, "id": "pi-old-project",
                "mode": "repository", "cwd": directory,
                "commands": ["printf source_only"],
            }
            with self.assertRaisesRegex(ValueError, "unsupported"):
                validate_linux_host_subset(task)

    def test_new_linux_host_only_commands_are_not_project_parity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task = {
                "version": 1, "id": "pi-host-only",
                "mode": "commands", "cwd": directory,
                "commands": ["printf source_only"],
            }
            validate_linux_host_subset(task)
            # The local subset contract permits host commands but contains
            # no per-project mailbox, registry or persistent project worker.
            self.assertFalse({"repository_id", "agent_binding"} & set(task))


if __name__ == "__main__":
    unittest.main()
