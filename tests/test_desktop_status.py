"""Isolated M0 desktop status-provider contract; requires no Qt installation."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from local_agent.desktop.status import (
    MAX_REGISTRY_BYTES, read_default_local_snapshot, read_local_snapshot,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
SHA = "a" * 40


class DesktopStatusTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.status = self.root / "status.json"
        self.registry = self.root / "repositories.json"
        self.disabled = self.root / "disabled.json"
        self.write(self.status, {
            "version": 1, "state": "idle", "pid": 123,
            "daemon_version": "4.20.6", "self_revision": SHA,
            "updated_at": NOW.isoformat(),
            "current_task_id": None,
            "token": "SHOULD_NEVER_BE_VISIBLE",
        })
        self.write(self.registry, {"version": 1, "repositories": [
            {"id": "project-a", "repository": "private/hidden", "control_dir": "/secret"},
            {"id": "project-b", "enabled": False, "secret": "NEVER_VISIBLE"},
        ]})

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value) + "\n", encoding="utf-8")
        path.chmod(0o600)

    def snap(self, **kwargs):
        return read_local_snapshot(
            status_file=self.status, registry_file=self.registry,
            disabled_file=self.disabled, now=NOW,
            pid_alive=lambda pid: pid == 123, **kwargs,
        )

    def test_idle_local_snapshot_is_sanitized_and_versioned(self):
        snapshot = self.snap()
        data = snapshot.as_record()
        self.assertEqual(snapshot.state, "idle")
        self.assertEqual(snapshot.daemon_version, "4.20.6")
        self.assertEqual(snapshot.installed_source_sha, SHA)
        self.assertEqual(snapshot.repository_count, 1)
        self.assertEqual(snapshot.repository_ids, ("project-a",))
        self.assertEqual(data["version"], 1)
        self.assertEqual(snapshot.status_source, "local_private_status_file")
        self.assertNotIn("secret", json.dumps(data).lower())
        self.assertNotIn("token", json.dumps(data).lower())
        self.assertNotIn(str(self.root), json.dumps(data))

    def test_busy_running_task_and_disabled_marker(self):
        self.write(self.status, {
            "version": 1, "state": "running", "pid": 123,
            "updated_at": NOW.isoformat(), "current_task_id": "task-1",
        })
        self.assertEqual(self.snap().state, "busy")
        self.assertEqual(self.snap().current_task_id, "task-1")
        self.disabled.write_text("not-json")
        self.assertEqual(self.snap().state, "disabled")

    def test_supervisor_running_without_active_task_is_not_busy(self):
        self.write(self.status, {
            "version": 1, "state": "running", "pid": 123,
            "updated_at": NOW.isoformat(), "current_task_id": None,
        })
        self.assertEqual(self.snap().state, "running")
        self.assertIsNone(self.snap().current_task_id)

    def test_zero_repositories_is_a_valid_host_only_state(self):
        self.write(self.registry, {"version": 1, "repositories": []})
        snapshot = self.snap()
        self.assertEqual(snapshot.repository_count, 0)
        self.assertEqual(snapshot.repository_ids, ())
        self.assertEqual(snapshot.registry_state, "available")

    def test_stale_time_dead_process_and_missing_pid(self):
        self.write(self.status, {
            "version": 1, "state": "idle", "pid": 123,
            "updated_at": (NOW - timedelta(minutes=8)).isoformat(),
        })
        self.assertEqual(self.snap().state, "stale")
        self.assertIn("status_stale", self.snap().warnings)
        self.write(self.status, {
            "version": 1, "state": "running", "pid": 44,
            "updated_at": NOW.isoformat(),
        })
        self.assertEqual(self.snap().state, "stale")
        self.assertIn("status_owner_not_running", self.snap().warnings)
        self.write(self.status, {
            "version": 1, "state": "running", "pid": True,
            "updated_at": NOW.isoformat(),
        })
        self.assertIn("status_owner_unknown", self.snap().warnings)

    def test_future_timestamp_invalid_and_unrecognized_state(self):
        for timestamp in (
            (NOW + timedelta(minutes=5)).isoformat(), "not-a-date", "2026-10-15T12:00:00",
        ):
            with self.subTest(timestamp=timestamp):
                self.write(self.status, {
                    "version": 1, "state": "idle", "pid": 123,
                    "updated_at": timestamp,
                })
                self.assertEqual(self.snap().state, "stale")
        self.write(self.status, {
            "version": 1, "state": "magic", "pid": 123,
            "updated_at": NOW.isoformat(),
        })
        self.assertEqual(self.snap().state, "unknown")

    def test_unavailable_status_or_registry_is_not_presented_as_zero(self):
        self.status.unlink()
        self.assertEqual(self.snap().state, "unavailable")
        self.registry.unlink()
        result = self.snap()
        self.assertIsNone(result.repository_count)
        self.assertEqual(result.registry_state, "unavailable")

    def test_duplicate_keys_oversized_registry_and_bad_enabled_field_fail_closed(self):
        self.registry.write_text('{"version":1,"version":1,"repositories":[]}')
        self.assertIsNone(self.snap().repository_count)
        self.write(self.registry, {
            "version": 1, "repositories": [{"id": "a", "enabled": "false"}],
        })
        self.assertIsNone(self.snap().repository_count)
        self.registry.write_bytes(b"x" * (MAX_REGISTRY_BYTES + 1))
        self.assertIsNone(self.snap().repository_count)

    def test_symlink_nonregular_and_world_writable_sources_refused(self):
        original = self.root / "real-status"
        self.write(original, {
            "version": 1, "state": "idle", "pid": 123,
            "updated_at": NOW.isoformat(),
        })
        self.status.unlink()
        self.status.symlink_to(original)
        self.assertEqual(self.snap().state, "unavailable")
        self.status.unlink()
        self.write(self.status, {
            "version": 1, "state": "idle", "pid": 123,
            "updated_at": NOW.isoformat(),
        })
        if os.name == "posix":
            self.status.chmod(0o666)
            self.assertEqual(self.snap().state, "unavailable")
            self.status.chmod(0o600)
        self.status.unlink()
        self.status.mkdir()
        self.assertEqual(self.snap().state, "unavailable")

    def test_persistent_disabled_marker_is_fail_closed_even_if_status_missing(self):
        self.status.unlink()
        self.disabled.symlink_to(self.root / "missing-target")
        snapshot = self.snap()
        self.assertEqual(snapshot.state, "disabled")
        self.assertTrue(snapshot.disabled_marker_present)
        self.assertIn("status_unavailable", snapshot.warnings)

    def test_untrusted_visible_ids_are_filtered(self):
        self.write(self.registry, {"version": 1, "repositories": [
            {"id": "project\nsecret"},
        ]})
        self.assertIsNone(self.snap().repository_count)
        self.write(self.status, {
            "version": 1, "state": "running", "pid": 123,
            "updated_at": NOW.isoformat(),
            "current_task_id": "/private/credentials.txt",
            "self_revision": "not-a-sha",
        })
        self.assertIsNone(self.snap().current_task_id)
        self.assertIsNone(self.snap().installed_source_sha)

    def test_separate_default_home_and_no_production_side_effects(self):
        home = self.root / "home"
        base = home / "Library" / "Application Support" / "local-agent"
        base.mkdir(parents=True)
        snapshot = read_default_local_snapshot(home=home)
        self.assertEqual(snapshot.state, "unavailable")
        self.assertFalse((base / "status.json").exists())
        self.assertFalse((base / "repositories.json").exists())

    def test_missing_qt_not_required_and_snapshot_import_has_no_service_modules(self):
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys; import local_agent.desktop.status as s;"
             "assert 'PySide6' not in sys.modules;"
             "assert 'local_agent.daemon.service' not in sys.modules;"
             "assert 'local_agent.repository.worker' not in sys.modules;"
             "print(s.DesktopSnapshot.__name__)"],
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DesktopSnapshot", result.stdout)


if __name__ == "__main__":
    unittest.main()
