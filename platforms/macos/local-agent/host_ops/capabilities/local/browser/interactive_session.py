"""Interactive Chromium sessions for an exact existing host-ops managed profile.

Interactive mode intentionally launches without CDP and without extension flags. It is
for short user-driven browser interactions such as authentication that may reject an
automation-enabled browser. The profile must already carry the managed-session
ownership marker; this module never adopts an arbitrary browser profile.
"""

from __future__ import annotations

import json
import os
import signal
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from local_agent.host_ops.core.execution import DetachedProcessSpawner, ExecutionLimits

from ._validation import validate_timeout_seconds
from .inspection import BrowserInspectionError, BrowserInspector
from .models import BrowserProcess, ManagedBrowserSession

_PROFILE_MARKER = ".hostops-managed-cdp-profile.json"
_MARKER_PAYLOAD = {"kind": "host-ops-managed-cdp-profile", "version": 1}
_MAX_URL_CHARS = 4096
_PROCESS_SCAN_LIMITS = ExecutionLimits(
    timeout_seconds=5.0,
    max_stdout_bytes=1_048_576,
    max_stderr_bytes=16_384,
)


class InteractiveBrowserSessionError(RuntimeError):
    """Raised when an interactive browser session cannot be controlled safely."""


BrowserLauncher = Callable[[Sequence[str]], int]
SignalSender = Callable[[int, int], None]
PidProbe = Callable[[int], bool]


class InteractiveBrowserSessionController:
    """Start, inspect and stop one non-CDP process using one owned managed profile."""

    def __init__(
        self,
        inspector: BrowserInspector | None = None,
        *,
        launcher: BrowserLauncher | None = None,
        signal_sender: SignalSender | None = None,
        pid_probe: PidProbe | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inspector = inspector or BrowserInspector()
        self._launcher = launcher or _launch_browser
        self._signal_sender = signal_sender or os.kill
        self._pid_probe = pid_probe or _pid_exists
        self._sleep = sleeper
        self._monotonic = monotonic

    def start(
        self,
        profile_dir: str,
        browser_executable: str,
        *,
        start_url: str | None = None,
        timeout_seconds: float = 15.0,
    ) -> ManagedBrowserSession:
        timeout = validate_timeout_seconds(timeout_seconds, minimum=1.0)
        profile = _require_owned_profile(profile_dir)
        executable = _validate_browser_executable(browser_executable)
        url = _validate_start_url(start_url) if start_url is not None else None

        existing = self._profile_process(profile, timeout_seconds=min(timeout, 5.0))
        if existing is not None:
            _require_interactive_process(existing, profile)
            raise InteractiveBrowserSessionError("interactive browser session is already running")

        argv = _launch_argv(executable, profile, start_url=url)
        pid = self._launcher(argv)
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise InteractiveBrowserSessionError("browser launcher returned an invalid pid")

        deadline = self._monotonic() + timeout
        try:
            while self._monotonic() < deadline:
                if not self._pid_probe(pid):
                    raise InteractiveBrowserSessionError(
                        "interactive browser process exited before it could be discovered"
                    )
                remaining = max(0.1, deadline - self._monotonic())
                current = self._profile_process(
                    profile,
                    timeout_seconds=min(remaining, 5.0),
                )
                if current is not None:
                    _require_interactive_process(current, profile)
                    return ManagedBrowserSession(
                        state="running",
                        pid=current.pid,
                        endpoint=None,
                        browser=current.family,
                    )
                self._sleep(0.1)
        except Exception:
            self._terminate_spawned(pid)
            raise

        self._terminate_spawned(pid)
        raise InteractiveBrowserSessionError(
            f"interactive browser was not discoverable within {timeout:g}s"
        )

    def status(
        self,
        profile_dir: str,
        *,
        timeout_seconds: float = 5.0,
    ) -> ManagedBrowserSession:
        timeout = validate_timeout_seconds(timeout_seconds, minimum=0.1)
        profile = _require_owned_profile(profile_dir)
        process = self._profile_process(profile, timeout_seconds=min(timeout, 5.0))
        if process is None:
            return ManagedBrowserSession(state="stopped", pid=None, endpoint=None, browser=None)
        _require_interactive_process(process, profile)
        return ManagedBrowserSession(
            state="running",
            pid=process.pid,
            endpoint=None,
            browser=process.family,
        )

    def stop(
        self,
        profile_dir: str,
        *,
        clear_session_restore: bool = False,
        timeout_seconds: float = 10.0,
    ) -> ManagedBrowserSession:
        timeout = validate_timeout_seconds(timeout_seconds, minimum=0.1)
        profile = _require_owned_profile(profile_dir)
        process = self._profile_process(profile, timeout_seconds=min(timeout, 5.0))
        if process is None:
            if clear_session_restore:
                _clear_session_restore(profile)
            return ManagedBrowserSession(state="stopped", pid=None, endpoint=None, browser=None)
        _require_interactive_process(process, profile)

        try:
            self._signal_sender(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            if clear_session_restore:
                _clear_session_restore(profile)
            return ManagedBrowserSession(state="stopped", pid=None, endpoint=None, browser=None)
        except OSError as exc:
            raise InteractiveBrowserSessionError(
                f"could not terminate interactive browser pid {process.pid}: {exc}"
            ) from exc

        deadline = self._monotonic() + timeout
        while self._monotonic() < deadline:
            current = self._profile_process(
                profile,
                timeout_seconds=min(max(0.1, deadline - self._monotonic()), 5.0),
            )
            if current is None:
                if clear_session_restore:
                    _clear_session_restore(profile)
                return ManagedBrowserSession(state="stopped", pid=None, endpoint=None, browser=None)
            _require_interactive_process(current, profile)
            self._sleep(0.1)
        raise InteractiveBrowserSessionError(
            f"interactive browser did not exit within {timeout:g}s after SIGTERM"
        )

    def _profile_process(self, profile: Path, *, timeout_seconds: float) -> BrowserProcess | None:
        try:
            inspection = self._inspector.inspect(
                timeout_seconds=timeout_seconds,
                limits=_PROCESS_SCAN_LIMITS,
            )
        except (BrowserInspectionError, ValueError) as exc:
            raise InteractiveBrowserSessionError(
                f"could not inspect browser processes: {exc}"
            ) from exc
        matches = tuple(
            process for process in inspection.processes if process.user_data_dir == str(profile)
        )
        if len(matches) > 1:
            raise InteractiveBrowserSessionError(
                "multiple browser root processes use the managed profile; refusing to choose one"
            )
        return matches[0] if matches else None

    def _terminate_spawned(self, pid: int) -> None:
        if not self._pid_probe(pid):
            return
        try:
            self._signal_sender(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            return


def _require_owned_profile(raw: str) -> Path:
    value = raw.strip()
    if not value:
        raise ValueError("profile_dir must not be empty")
    expanded = Path(value).expanduser()
    if not expanded.is_absolute():
        raise ValueError("profile_dir must be an absolute path")
    if any(character.isspace() for character in str(expanded)):
        raise ValueError("profile_dir must not contain whitespace")
    if expanded.is_symlink():
        raise ValueError("profile_dir must not be a symlink")
    profile = expanded.resolve(strict=False)
    if not profile.is_dir():
        raise InteractiveBrowserSessionError(
            "interactive mode requires an existing host-ops managed profile"
        )
    marker = profile / _PROFILE_MARKER
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InteractiveBrowserSessionError(
            "profile_dir is not owned by host-ops managed browser sessions"
        ) from exc
    if payload != _MARKER_PAYLOAD:
        raise InteractiveBrowserSessionError("managed profile ownership marker is invalid")
    return profile


def _require_interactive_process(process: BrowserProcess, profile: Path) -> None:
    if process.user_data_dir != str(profile):
        raise InteractiveBrowserSessionError("browser process profile identity changed")
    if (
        process.remote_debugging_port is not None
        or process.remote_debugging_address is not None
        or process.remote_debugging_pipe
    ):
        raise InteractiveBrowserSessionError(
            "managed profile is in use by a browser outside the interactive no-CDP contract"
        )


def _validate_browser_executable(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise ValueError("browser_executable must not be empty")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("browser_executable must be an absolute path")
    resolved = path.resolve(strict=False)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError("browser_executable must be an existing executable file")
    return str(resolved)


def _validate_start_url(raw: str) -> str:
    value = raw.strip()
    if not value or len(value) > _MAX_URL_CHARS:
        raise ValueError(f"start_url must contain 1 to {_MAX_URL_CHARS} characters")
    if any(character.isspace() for character in value):
        raise ValueError("start_url must not contain whitespace")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValueError("start_url is invalid") from exc
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("start_url must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("start_url must not contain credentials")
    if not parsed.hostname:
        raise ValueError("start_url must include a hostname")
    if parsed.query or parsed.fragment:
        raise ValueError("start_url must not contain a query or fragment")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _launch_argv(executable: str, profile: Path, *, start_url: str | None) -> tuple[str, ...]:
    return (
        executable,
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        start_url or "about:blank",
    )


def _clear_session_restore(profile: Path) -> None:
    default_dir = profile / "Default"
    if not default_dir.exists():
        return
    if default_dir.is_symlink() or not default_dir.is_dir():
        raise InteractiveBrowserSessionError(
            "managed profile Default path is not a regular directory"
        )

    sessions = default_dir / "Sessions"
    if sessions.exists():
        if sessions.is_symlink() or not sessions.is_dir():
            raise InteractiveBrowserSessionError(
                "managed browser Sessions path is not a regular directory"
            )
        for child in sessions.iterdir():
            if child.is_symlink() or not child.is_file():
                raise InteractiveBrowserSessionError(
                    "managed browser Sessions contains an unexpected entry"
                )
        for child in sessions.iterdir():
            child.unlink()

    for name in ("Current Session", "Current Tabs", "Last Session", "Last Tabs"):
        candidate = default_dir / name
        if not candidate.exists():
            continue
        if candidate.is_symlink() or not candidate.is_file():
            raise InteractiveBrowserSessionError(
                f"managed browser {name} path is not a regular file"
            )
        candidate.unlink()


def _launch_browser(argv: Sequence[str]) -> int:
    try:
        return DetachedProcessSpawner().spawn(argv)
    except (RuntimeError, ValueError) as exc:
        raise InteractiveBrowserSessionError(
            f"could not launch interactive browser: {exc}"
        ) from exc


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
