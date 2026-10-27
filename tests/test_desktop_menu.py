"""Pure-M1 menu model and optional Qt rendering regression coverage."""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from local_agent.desktop.preferences import safe_refresh_seconds
from local_agent.desktop.menu_model import (
    MAX_MENU_REPOSITORIES, build_tray_presentation, unavailable_presentation,
)
from local_agent.desktop.status import read_local_snapshot

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


class DesktopMenuModelTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.status = self.root / "status.json"
        self.registry = self.root / "repositories.json"
        self.disabled = self.root / "disabled.json"
        self.write(self.status, {
            "version": 1, "state": "running", "pid": 333,
            "updated_at": NOW.isoformat(), "daemon_version": "4.20.6",
            "self_revision": "a" * 40, "current_task_id": None,
            "private": "/private/sensitive",
        })
        self.write(self.registry, {
            "version": 1, "repositories": [{"id": "project-1", "repository": "private/repo"}],
        })

    @staticmethod
    def write(path, record):
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        path.chmod(0o600)

    def presentation(self):
        snapshot = read_local_snapshot(
            status_file=self.status, registry_file=self.registry,
            disabled_file=self.disabled, now=NOW, pid_alive=lambda _: True,
        )
        return build_tray_presentation(snapshot)

    def test_running_not_busy_and_source_identity_is_reported_only(self):
        view = self.presentation()
        self.assertEqual(view.state_label, "Running")
        self.assertIn("Agent: Running", view.information)
        self.assertIn("Reported source: aaaaaaaaaaaa", view.information)
        self.assertIn("Source: local files (not remote authorization)", view.information)
        self.assertEqual(view.repositories, ("project-1",))
        self.assertNotIn("/private/sensitive", str(view))
        self.assertFalse(hasattr(view, "stop_agent"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            view.title = "changed"

    def test_busy_and_task_description(self):
        self.write(self.status, {
            "version": 1, "state": "running", "pid": 333,
            "updated_at": NOW.isoformat(), "current_task_id": "task-42",
        })
        view = self.presentation()
        self.assertEqual(view.state_label, "Busy")
        self.assertIn("Active task: task-42", view.information)

    def test_disabled_stale_and_unavailable_states(self):
        self.disabled.touch()
        self.assertEqual(self.presentation().state_label, "Disabled")
        self.disabled.unlink()
        self.status.unlink()
        self.assertEqual(self.presentation().state_label, "Unavailable")
        self.write(self.status, {
            "version": 1, "state": "idle", "pid": 333,
            "updated_at": "2000-01-01T00:00:00+00:00",
        })
        view = self.presentation()
        self.assertEqual(view.state_label, "Stale status")
        self.assertIn("Status heartbeat is stale", view.warnings)

    def test_zero_missing_and_large_registry(self):
        self.write(self.registry, {"version": 1, "repositories": []})
        self.assertEqual(self.presentation().repositories, ("No managed repositories",))
        self.registry.unlink()
        view = self.presentation()
        self.assertIn("Repositories: unavailable", view.information)
        self.assertIn("Repository information unavailable", view.repositories)
        self.assertIn("Repository information unavailable", view.warnings)
        self.write(self.registry, {"version": 1, "repositories": [
            {"id": f"repo-{i}"} for i in range(35)
        ]})
        view = self.presentation()
        self.assertEqual(len(view.repositories), MAX_MENU_REPOSITORIES + 1)
        self.assertEqual(view.repositories[-1], "+25 more")

    def test_ui_refresh_preference_is_bounded_and_never_bool(self):
        self.assertEqual(safe_refresh_seconds(45), 45)
        self.assertEqual(safe_refresh_seconds(" 45 "), 30)
        self.assertEqual(safe_refresh_seconds("45"), 45)
        for invalid in (True, False, None, -1, 0, 601, "999999999999", "45x", 0.5):
            with self.subTest(invalid=invalid):
                self.assertEqual(safe_refresh_seconds(invalid), 30)

    def test_no_qt_dependency_when_loading_presentation(self):
        script = (
            "import sys; from local_agent.desktop.menu_model import build_tray_presentation;"
            "assert 'PySide6' not in sys.modules;"
            "assert 'local_agent.daemon.service' not in sys.modules;"
            "assert 'local_agent.desktop.tray' not in sys.modules"
        )
        output = subprocess.run([sys.executable, "-c", script], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(output.returncode, 0, output.stderr)

    def test_desktop_menu_help_without_qt_and_no_mutation_entrypoint(self):
        result = subprocess.run(
            [sys.executable, "-m", "local_agent.desktop.tray", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--check", result.stdout)
        self.assertFalse(hasattr(unavailable_presentation(), "host_commands"))


    def test_qt_offscreen_details_window_is_read_only_and_accessible(self):
        from local_agent.desktop.menu_model import TrayPresentation
        self.assertEqual(
            unavailable_presentation().state_label,
            "Unavailable",
        )
        presentation = TrayPresentation(
            title="Local Agent", state_label="Idle", tooltip="Local Agent — Idle",
            information=("Agent: Idle",), repositories=("No managed repositories",),
            warnings=(),
        )
        self.assertNotIn("Stop", " ".join(presentation.information))

class DesktopQtWidgetTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("PySide6"), "optional Qt is not installed")
    def test_qt_menu_fallback_refresh_and_quit_controls_are_ui_only(self):
        code = """
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtWidgets import QApplication
from local_agent.desktop.status import DesktopSnapshot
from local_agent.desktop.tray import create_desktop_menu
app=QApplication([])
def snap():
    return DesktopSnapshot(
      version=1,observed_at='2026-10-15T12:00:00+00:00',state='idle',
      daemon_state='idle',daemon_version='4.20.6',
      installed_source_sha='a'*40,status_updated_at='2026-10-15T12:00:00+00:00',
      status_age_seconds=0,process_alive=True,current_task_id=None,
      repository_count=0,repository_ids=(),registry_state='available',
      disabled_marker_present=False,status_source='local_private_status_file',
      registry_source='local_private_registry_file',warnings=(),
    )
class MemoryPreferences:
    def __init__(self): self.data = {}
    def value(self, key): return self.data.get(key)
    def setValue(self, key, value): self.data[key] = value
    def sync(self): pass
prefs=MemoryPreferences()
ui=create_desktop_menu(app,snapshot_reader=snap,tray_available=False,preferences_store=prefs)
assert ui.tray is None and ui.window is not None
assert app.quitOnLastWindowClosed()
assert ui.presentation.state_label == 'Idle'
assert any(a.text()=='Refresh status' for a in ui.menu.actions())
assert any(a.text()=='Quit Menu' for a in ui.menu.actions())
assert all('Stop Local Agent' not in a.text() for a in ui.menu.actions())
assert ui.status_text.isReadOnly()
assert ui.status_text.accessibleName() == 'Local Agent status details'
assert ui.status_text.tabChangesFocus()
assert 'No managed repositories' in ui.status_text.toPlainText()
ui.refresh()
ui.timer.stop()
ui.window.close()
# Exercise the tray QMenu path with a test-only tray availability override.
trayed=create_desktop_menu(app,snapshot_reader=snap,tray_available=True,preferences_store=prefs)
assert trayed.tray is not None and trayed.window is None
assert not app.quitOnLastWindowClosed()
assert 'Local Agent' in trayed.tray.toolTip()
assert trayed.details is not None
assert trayed.details.accessibleName() == 'Local Agent status details window'
assert trayed.status_text.isReadOnly()
assert 'Read-only' in trayed.status_text.accessibleDescription()
assert trayed.menu.accessibleName() == 'Local Agent status menu'
from PySide6.QtGui import QShortcut
assert len(trayed.details.findChildren(QShortcut)) == 1
assert trayed.menu.actions()[0].isEnabled()
assert trayed.menu.actions()[0].text() == 'Agent: Idle'
repo_action=next(action for action in trayed.menu.actions() if action.text() == 'Repositories — Show details')
assert repo_action.isEnabled()
# Mac first-hover must not create a native child menu or refresh actions
# while Cocoa is tracking the flat parent menu.
assert all(action.menu() is None for action in trayed.menu.actions())
trayed.menu.aboutToShow.emit()
assert not trayed.timer.isActive()
trayed.refresh()
assert trayed.menu.actions()[0].text() == 'Agent: Idle'
trayed.menu.aboutToHide.emit()
assert trayed.timer.isActive()
initial_quit=next(action for action in trayed.menu.actions() if action.text() == 'Quit Menu')
initial_info=trayed.menu.actions()[0]
initial_repo=repo_action
assert len(trayed.menu.actions()) >= 7
assert any(action is repo_action for action in trayed.menu.actions())
repo_action.trigger()
assert trayed.details.isVisible()
trayed.details.hide()
trayed.menu.actions()[0].trigger()
assert trayed.details.isVisible()
trayed.details.hide()
details_action=next(action for action in trayed.menu.actions() if action.text() == 'Show Status Details')
details_action.trigger()
assert trayed.details.isVisible()
trayed.details.findChildren(QShortcut)[0].activated.emit()
assert not trayed.details.isVisible()
assert trayed.tray.isVisible()
assert any(action.text() == 'Refresh status' for action in trayed.menu.actions())
settings_action=next(action for action in trayed.menu.actions() if action.text() == 'Settings…')
settings_action.trigger()
assert trayed.settings_dialog is not None and trayed.settings_dialog.isVisible()
assert trayed.settings_refresh.accessibleName() == 'Menu refresh interval'
trayed.settings_refresh.setValue(45)
from PySide6.QtWidgets import QDialogButtonBox
trayed.settings_dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()
assert prefs.data['ui/refresh_seconds'] == 45
assert trayed.timer.interval() == 45000
assert not trayed.settings_dialog.isVisible()
assert all('Stop' not in a.text() and 'Operator' not in a.text() for a in trayed.menu.actions())
trayed.menu.show()
old=trayed.presentation
trayed.refresh()
assert trayed.presentation is old
trayed.menu.hide()
app.processEvents()
trayed.refresh()
assert trayed.presentation.state_label == 'Idle'
assert trayed.menu.actions()[0] is initial_info
assert next(action for action in trayed.menu.actions() if action.text() == 'Repositories — Show details') is initial_repo
assert next(action for action in trayed.menu.actions() if action.text() == 'Quit Menu') is initial_quit
from PySide6.QtCore import QTimer
QTimer.singleShot(80, initial_quit.trigger)
QTimer.singleShot(2500, lambda: app.exit(88))
code = app.exec()
assert code == 0, f'Quit Menu did not stop the Qt event loop: {code}'
assert not trayed.timer.isActive()
assert not trayed.tray.isVisible()
print('M1_QT_QUIT_EVENTLOOP_PASS')
print('M1_QT_OFFSCREEN_PASS')
"""
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        result = subprocess.run(
            [sys.executable, "-c", code], text=True, capture_output=True,
            timeout=30, env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("M1_QT_OFFSCREEN_PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
