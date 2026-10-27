"""Optional read-only Qt Widgets tray, isolated from Local Agent runtime.

The native menu owns persistent actions: clearing them during macOS menu
dismissal may prevent Cocoa from dispatching the selected Quit callback.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Callable

from local_agent.desktop.preferences import safe_refresh_seconds
from local_agent.desktop.menu_model import (
    TrayPresentation,
    build_tray_presentation,
    unavailable_presentation,
)
from local_agent.desktop.status import DesktopSnapshot, read_default_local_snapshot

DEFAULT_REFRESH_MS = 30_000
_MAX_MENU_INFO_LINES = 12
_MAX_MENU_WARNINGS = 12


@dataclass
class DesktopMenuController:
    """Own optional GUI objects for the lifetime of the menu process."""

    app: object
    tray: object | None
    menu: object
    window: object | None
    details: object | None
    status_text: object
    timer: object
    refresh: Callable[[], None]
    settings_dialog: object | None = None
    settings_refresh: object | None = None
    presentation: TrayPresentation | None = None


def create_desktop_menu(
    app: object,
    snapshot_reader: Callable[[], DesktopSnapshot] = read_default_local_snapshot,
    *,
    refresh_ms: int = DEFAULT_REFRESH_MS,
    tray_available: bool | None = None,
    preferences_store: object | None = None,
) -> DesktopMenuController:
    """Create a read-only menu with stable native actions and fallback window."""
    from PySide6.QtCore import QSettings, QTimer
    from PySide6.QtGui import QKeySequence, QShortcut
    from PySide6.QtWidgets import (
        QApplication, QDialog, QDialogButtonBox, QFormLayout, QLabel, QMenu,
        QPlainTextEdit, QPushButton, QSpinBox, QStyle, QSystemTrayIcon,
        QVBoxLayout, QWidget,
    )

    if type(refresh_ms) is not int or not 5_000 <= refresh_ms <= 600_000:
        raise ValueError("refresh period must be between 5 and 600 seconds")
    available = (
        QSystemTrayIcon.isSystemTrayAvailable()
        if tray_available is None else bool(tray_available)
    )
    app.setQuitOnLastWindowClosed(not available)
    # Preferences belong to this optional UI only, never the daemon/service.
    store = preferences_store if preferences_store is not None else QSettings(
        "MichalMatu", "Local Agent Menu", app,
    )
    try:
        raw_refresh = store.value("ui/refresh_seconds")
    except (TypeError, ValueError, OSError):
        raw_refresh = None
    configured_refresh_ms = (
        safe_refresh_seconds(raw_refresh, fallback=refresh_ms // 1000) * 1000
        if raw_refresh is not None else refresh_ms
    )
    menu = QMenu()
    menu.setAccessibleName("Local Agent status menu")
    tray = None
    window = None
    details = None
    if available:
        icon = QApplication.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        if sys.platform == "darwin":
            icon.setIsMask(True)
        tray = QSystemTrayIcon(icon, app)
        tray.setContextMenu(menu)
        details = QWidget()
        details.setWindowTitle("Local Agent — Status Details")
        details.setAccessibleName("Local Agent status details window")
        details.setMinimumSize(420, 260)
        layout = QVBoxLayout(details)
    else:
        window = QWidget()
        window.setWindowTitle("Local Agent — Status (tray unavailable)")
        window.setMinimumSize(420, 260)
        layout = QVBoxLayout(window)

    status_text = QPlainTextEdit()
    status_text.setReadOnly(True)
    status_text.setAccessibleName("Local Agent status details")
    status_text.setAccessibleDescription("Read-only locally observed agent status and repositories")
    status_text.setTabChangesFocus(True)
    layout.addWidget(status_text)
    if details is not None:
        close_button = QPushButton("Close Details")
        close_button.setAccessibleName("Close Local Agent status details")
        close_button.clicked.connect(details.hide)
        layout.addWidget(close_button)
        # macOS Command+W / Windows Ctrl+W (Qt's platform StandardKey).
        # Closing details never quits the background tray or the agent service.
        QShortcut(QKeySequence.StandardKey.Close, details).activated.connect(details.hide)
    else:
        refresh_button = QPushButton("Refresh status")
        layout.addWidget(refresh_button)
        quit_button = QPushButton("Quit Menu")
        layout.addWidget(quit_button)

    controller = DesktopMenuController(
        app=app, tray=tray, menu=menu, window=window, details=details,
        status_text=status_text, timer=QTimer(app), refresh=lambda: None,
    )
    settings_dialog = QDialog()
    settings_dialog.setWindowTitle("Local Agent — Menu Settings")
    settings_dialog.setAccessibleName("Local Agent menu settings")
    settings_dialog.setMinimumWidth(360)
    settings_layout = QVBoxLayout(settings_dialog)
    notice = QLabel("These preferences affect only the menu application, not the agent service, admission, or permissions.")
    notice.setWordWrap(True)
    settings_layout.addWidget(notice)
    form = QFormLayout()
    settings_refresh = QSpinBox()
    settings_refresh.setRange(5, 600)
    settings_refresh.setSuffix(" seconds")
    settings_refresh.setAccessibleName("Menu refresh interval")
    settings_refresh.setValue(max(5, min(600, configured_refresh_ms // 1000)))
    form.addRow("Refresh interval:", settings_refresh)
    settings_layout.addLayout(form)
    settings_message = QLabel("")
    settings_message.setWordWrap(True)
    settings_layout.addWidget(settings_message)
    settings_buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
    )
    settings_layout.addWidget(settings_buttons)
    controller.settings_dialog = settings_dialog
    controller.settings_refresh = settings_refresh
    shutting_down = False
    # Keep native menu actions immutable during the menu tracking event.
    # Flat repository/warning actions avoid fragile Cocoa submenu hover.
    open_menus = 0

    def pause_menu_refresh() -> None:
        nonlocal open_menus
        open_menus += 1
        controller.timer.stop()

    def resume_menu_refresh() -> None:
        nonlocal open_menus
        open_menus = max(0, open_menus - 1)
        if open_menus == 0 and not shutting_down:
            # Native Cocoa callbacks may still be dispatching after hide.
            controller.timer.start(configured_refresh_ms)

    def show_details() -> None:
        if details is not None and not shutting_down:
            details.show()
            details.raise_()
            details.activateWindow()

    def show_settings() -> None:
        if shutting_down:
            return
        settings_message.setText("")
        settings_refresh.setValue(max(5, min(600, configured_refresh_ms // 1000)))
        settings_dialog.show()
        settings_dialog.raise_()
        settings_dialog.activateWindow()

    def save_settings() -> None:
        nonlocal configured_refresh_ms
        seconds = settings_refresh.value()
        try:
            store.setValue("ui/refresh_seconds", seconds)
            store.sync()
            if hasattr(store, "status") and store.status() != QSettings.Status.NoError:
                raise OSError("settings store reported a write failure")
        except (OSError, RuntimeError, ValueError, TypeError):
            settings_message.setText("Could not save menu preferences.")
            return
        configured_refresh_ms = seconds * 1000
        controller.timer.start(configured_refresh_ms)
        settings_dialog.hide()

    settings_buttons.accepted.connect(save_settings)
    settings_buttons.rejected.connect(settings_dialog.hide)

    def quit_menu() -> None:
        nonlocal shutting_down
        if shutting_down:
            return
        shutting_down = True
        controller.timer.stop()
        if tray is not None:
            tray.hide()
        if details is not None:
            details.hide()
        settings_dialog.hide()
        app.quit()  # The daemon, supervisor and operator state are untouched.

    # Allocate each QAction exactly once. menu.clear() may invalidate a
    # native macOS action before Cocoa delivers its 'triggered' event.
    info_actions = [menu.addAction("") for _ in range(_MAX_MENU_INFO_LINES)]
    for action in info_actions:
        action.setStatusTip("Read-only status: opens details")
        action.triggered.connect(show_details)

    # macOS native QMenu submenus can dismiss the tray menu on first hover.
    # Use stable flat actions and expose every repository/warning in the
    # existing keyboard-accessible, read-only details window instead.
    repositories_action = menu.addAction("Repositories — Show details")
    repositories_action.setStatusTip("Show locally observed repository details")
    repositories_action.triggered.connect(show_details)
    warnings_action = menu.addAction("Warnings — Show details")
    warnings_action.setStatusTip("Show locally observed warnings")
    warnings_action.triggered.connect(show_details)
    menu.addSeparator()

    if details is not None:
        details_action = menu.addAction("Show Status Details")
        details_action.triggered.connect(show_details)
    refresh_action = menu.addAction("Refresh status")
    settings_action = menu.addAction("Settings…")
    settings_action.triggered.connect(show_settings)
    quit_action = menu.addAction("Quit Menu")
    quit_action.triggered.connect(quit_menu)
    if window is not None:
        refresh_button.clicked.connect(lambda: QTimer.singleShot(0, refresh))
        settings_button = QPushButton("Settings…")
        layout.insertWidget(layout.count() - 1, settings_button)
        settings_button.clicked.connect(show_settings)
        quit_button.clicked.connect(quit_menu)

    def update_actions(actions: list[object], lines: tuple[str, ...]) -> None:
        for index, action in enumerate(actions):
            visible = index < len(lines)
            if visible:
                # Enabled, keyboard-focusable text has normal macOS contrast.
                # Clicking status/repository info only opens read-only details.
                action.setText(lines[index])
            action.setVisible(visible)

    def refresh() -> None:
        if (
            shutting_down
            or open_menus
            or menu.isVisible()
        ):
            return
        try:
            model = build_tray_presentation(snapshot_reader())
        except (OSError, ValueError, TypeError):
            model = unavailable_presentation()
        controller.presentation = model
        update_actions(info_actions, model.information)
        warnings_action.setVisible(bool(model.warnings))
        if tray is not None:
            tray.setToolTip(model.tooltip)
        lines = [*model.information, "Repositories:", *model.repositories]
        if model.warnings:
            lines.extend(("Warnings:", *model.warnings))
        status_text.setPlainText("\n".join(lines))

    controller.refresh = refresh
    # A selected native QAction may be delivered *after* aboutToHide.
    # Never change QAction visibility/text during that transition.
    refresh_action.triggered.connect(lambda: QTimer.singleShot(300, refresh))
    if available:
        menu.aboutToShow.connect(pause_menu_refresh)
        menu.aboutToHide.connect(resume_menu_refresh)
    controller.timer.timeout.connect(refresh)
    controller.timer.start(configured_refresh_ms)
    refresh()
    if tray is not None:
        tray.show()
    else:
        window.show()
    return controller


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Optional read-only Local Agent desktop menu")
    parser.add_argument(
        "--check", action="store_true",
        help="check whether the optional Qt Widgets dependency is available",
    )
    args = parser.parse_args(argv)
    try:
        import PySide6  # noqa: F401 - intentionally optional, GUI-only import
    except ImportError:
        print(
            "PySide6 is not installed. Install it in a separate desktop UI environment.",
            file=sys.stderr,
        )
        return 2
    if args.check:
        print("PySide6 is available; GUI support has not been verified in this check.")
        return 0
    if sys.platform != "darwin":
        print(
            "The default local-state reader is currently supported on macOS only.",
            file=sys.stderr,
        )
        return 2
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        print("Headless Qt is for isolated tests, not an interactive tray.", file=sys.stderr)
        return 2
    from PySide6.QtWidgets import QApplication
    from local_agent.desktop.single_instance import acquire_desktop_menu_lock

    # This GUI-only lock does not touch Local Agent daemon or authorization.
    # A second Finder/Terminal invocation must not create another tray.
    try:
        lock = acquire_desktop_menu_lock()
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Cannot obtain Local Agent Menu instance lock: {error}", file=sys.stderr)
        return 3
    if lock is None:
        print("Local Agent Menu is already running.", file=sys.stderr)
        return 0
    try:
        app = QApplication.instance() or QApplication([sys.argv[0]])
        controller = create_desktop_menu(app)
        result = int(app.exec())
        controller.timer.stop()
        if controller.tray is not None:
            controller.tray.hide()
        return result
    finally:
        lock.unlock()


if __name__ == "__main__":
    raise SystemExit(main())
