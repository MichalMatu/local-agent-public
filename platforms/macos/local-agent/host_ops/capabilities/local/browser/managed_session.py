"""Persistent isolated Chromium CDP sessions with exact profile ownership guards."""

from __future__ import annotations

import json
import os
import signal
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from local_agent.host_ops.core.execution import DetachedProcessSpawner, ExecutionLimits

from ._validation import validate_timeout_seconds
from .inspection import BrowserInspectionError, BrowserInspector
from .models import BrowserProcess, ManagedBrowserSession

_PROFILE_MARKER = ".hostops-managed-cdp-profile.json"
_DEVTOOLS_ACTIVE_PORT = "DevToolsActivePort"
_MARKER_VERSION = 1
_MARKER_KIND = "host-ops-managed-cdp-profile"
_MAX_EXTENSION_DIRS = 8
_MAX_URL_CHARS = 4096
_PROCESS_SCAN_LIMITS = ExecutionLimits(
    timeout_seconds=5.0,
    max_stdout_bytes=1_048_576,
    max_stderr_bytes=16_384,
)


class ManagedBrowserSessionError(RuntimeError):
    """Raised when a persistent managed browser session cannot be controlled safely."""


BrowserLauncher = Callable[[Sequence[str]], int]
SignalSender = Callable[[int, int], None]
PidProbe = Callable[[int], bool]


class ManagedBrowserSessionController:
    """Start, inspect and stop one Chromium process bound to one managed profile."""

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
        extension_dirs: Sequence[str] = (),
        start_url: str | None = None,
        download_dir: str | None = None,
        timeout_seconds: float = 15.0,
    ) -> ManagedBrowserSession:
        timeout = validate_timeout_seconds(timeout_seconds, minimum=1.0)
        profile = _initialize_managed_profile(profile_dir)
        executable = _validate_browser_executable(browser_executable)
        extensions = _validate_extension_dirs(extension_dirs)
        url = _validate_start_url(start_url) if start_url is not None else None

        existing = self._profile_process(profile, timeout_seconds=min(timeout, 5.0))
        if existing is not None:
            _require_managed_process(existing, profile)
            raise ManagedBrowserSessionError("managed browser session is already running")

        if download_dir is not None:
            downloads = _prepare_download_dir(download_dir)
            _configure_download_preferences(profile, downloads)

        active_port = profile / _DEVTOOLS_ACTIVE_PORT
        active_port.unlink(missing_ok=True)
        argv = _launch_argv(
            executable,
            profile,
            extension_dirs=extensions,
            start_url=url,
        )
        pid = self._launcher(argv)
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ManagedBrowserSessionError("browser launcher returned an invalid pid")

        deadline = self._monotonic() + timeout
        try:
            while self._monotonic() < deadline:
                if not self._pid_probe(pid):
                    raise ManagedBrowserSessionError(
                        "managed browser process exited before CDP became ready"
                    )
                if active_port.exists():
                    remaining = max(0.1, deadline - self._monotonic())
                    session = self.status(
                        str(profile),
                        timeout_seconds=min(remaining, 5.0),
                    )
                    if session.state == "running" and session.pid == pid:
                        return session
                self._sleep(0.1)
        except Exception:
            self._terminate_spawned(pid)
            raise

        self._terminate_spawned(pid)
        raise ManagedBrowserSessionError(
            f"managed browser did not expose a ready loopback CDP endpoint within {timeout:g}s"
        )

    def status(
        self,
        profile_dir: str,
        *,
        timeout_seconds: float = 5.0,
    ) -> ManagedBrowserSession:
        timeout = validate_timeout_seconds(timeout_seconds, minimum=0.1)
        profile = _validate_profile_path(profile_dir)
        if not profile.exists():
            return ManagedBrowserSession(state="stopped", pid=None, endpoint=None, browser=None)
        _require_managed_profile(profile)

        process = self._profile_process(profile, timeout_seconds=min(timeout, 5.0))
        if process is None:
            return ManagedBrowserSession(state="stopped", pid=None, endpoint=None, browser=None)
        _require_managed_process(process, profile)

        endpoint = _read_devtools_endpoint(profile)
        if endpoint is None:
            return ManagedBrowserSession(
                state="unhealthy",
                pid=process.pid,
                endpoint=None,
                browser=None,
            )

        try:
            inspection = self._inspector.inspect(
                endpoints=(endpoint,),
                timeout_seconds=min(timeout, 5.0),
                limits=_PROCESS_SCAN_LIMITS,
            )
        except (BrowserInspectionError, ValueError) as exc:
            raise ManagedBrowserSessionError(
                f"could not validate managed browser endpoint: {exc}"
            ) from exc
        evidence = next((item for item in inspection.endpoints if item.endpoint == endpoint), None)
        if evidence is None or not evidence.reachable:
            return ManagedBrowserSession(
                state="unhealthy",
                pid=process.pid,
                endpoint=endpoint,
                browser=None,
            )
        return ManagedBrowserSession(
            state="running",
            pid=process.pid,
            endpoint=endpoint,
            browser=evidence.browser,
        )

    def stop(
        self,
        profile_dir: str,
        *,
        timeout_seconds: float = 10.0,
    ) -> ManagedBrowserSession:
        timeout = validate_timeout_seconds(timeout_seconds, minimum=0.1)
        profile = _validate_profile_path(profile_dir)
        if not profile.exists():
            return ManagedBrowserSession(state="stopped", pid=None, endpoint=None, browser=None)
        _require_managed_profile(profile)

        process = self._profile_process(profile, timeout_seconds=min(timeout, 5.0))
        if process is None:
            return ManagedBrowserSession(state="stopped", pid=None, endpoint=None, browser=None)
        _require_managed_process(process, profile)

        try:
            self._signal_sender(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return ManagedBrowserSession(state="stopped", pid=None, endpoint=None, browser=None)
        except OSError as exc:
            raise ManagedBrowserSessionError(
                f"could not terminate managed browser pid {process.pid}: {exc}"
            ) from exc

        deadline = self._monotonic() + timeout
        while self._monotonic() < deadline:
            current = self._profile_process(
                profile,
                timeout_seconds=min(max(0.1, deadline - self._monotonic()), 5.0),
            )
            if current is None:
                return ManagedBrowserSession(state="stopped", pid=None, endpoint=None, browser=None)
            _require_managed_process(current, profile)
            self._sleep(0.1)
        raise ManagedBrowserSessionError(
            f"managed browser did not exit within {timeout:g}s after SIGTERM"
        )

    def _profile_process(self, profile: Path, *, timeout_seconds: float) -> BrowserProcess | None:
        try:
            inspection = self._inspector.inspect(
                timeout_seconds=timeout_seconds,
                limits=_PROCESS_SCAN_LIMITS,
            )
        except (BrowserInspectionError, ValueError) as exc:
            raise ManagedBrowserSessionError(f"could not inspect browser processes: {exc}") from exc
        matches = tuple(
            process for process in inspection.processes if process.user_data_dir == str(profile)
        )
        if len(matches) > 1:
            raise ManagedBrowserSessionError(
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


def _launch_browser(argv: Sequence[str]) -> int:
    try:
        return DetachedProcessSpawner().spawn(argv)
    except (RuntimeError, ValueError) as exc:
        raise ManagedBrowserSessionError(f"could not launch managed browser: {exc}") from exc


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _validate_profile_path(raw: str) -> Path:
    value = raw.strip()
    if not value:
        raise ValueError("profile_dir must not be empty")
    expanded = Path(value).expanduser()
    if not expanded.is_absolute():
        raise ValueError("profile_dir must be an absolute path")
    if any(character.isspace() for character in str(expanded)):
        raise ValueError("profile_dir must not contain whitespace")
    if expanded.exists() and expanded.is_symlink():
        raise ValueError("profile_dir must not be a symlink")
    return expanded.resolve(strict=False)


def _initialize_managed_profile(raw: str) -> Path:
    profile = _validate_profile_path(raw)
    if profile.exists() and not profile.is_dir():
        raise ManagedBrowserSessionError("profile_dir exists but is not a directory")
    if not profile.exists():
        profile.mkdir(parents=True, mode=0o700)
    marker = profile / _PROFILE_MARKER
    if marker.exists():
        _require_managed_profile(profile)
        return profile
    if any(profile.iterdir()):
        raise ManagedBrowserSessionError(
            "profile_dir is not empty and has no host-ops ownership marker"
        )
    marker.write_text(
        json.dumps({"version": _MARKER_VERSION, "kind": _MARKER_KIND}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    marker.chmod(0o600)
    return profile


def _require_managed_profile(profile: Path) -> None:
    if not profile.is_dir() or profile.is_symlink():
        raise ManagedBrowserSessionError("managed profile is not a regular directory")
    marker = profile / _PROFILE_MARKER
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise ManagedBrowserSessionError(
            "profile_dir is not owned by host-ops managed browser sessions"
        ) from exc
    if payload != {"kind": _MARKER_KIND, "version": _MARKER_VERSION}:
        raise ManagedBrowserSessionError("managed profile ownership marker is invalid")


def _prepare_download_dir(raw: str) -> Path:
    value = raw.strip()
    if not value:
        raise ValueError("download_dir must not be empty")
    expanded = Path(value).expanduser()
    if not expanded.is_absolute():
        raise ValueError("download_dir must be an absolute path")
    for candidate in (expanded, *expanded.parents):
        if candidate.is_symlink():
            raise ValueError("download_dir must not contain symlink components")

    resolved = expanded.resolve(strict=False)
    if resolved.exists():
        if not resolved.is_dir():
            raise ValueError("download_dir must be a directory")
        return resolved
    try:
        resolved.mkdir(parents=True, mode=0o700)
    except OSError as exc:
        raise ManagedBrowserSessionError(f"could not create download_dir: {exc}") from exc
    return resolved


def _configure_download_preferences(profile: Path, download_dir: Path) -> None:
    default_dir = profile / "Default"
    if default_dir.exists() and (default_dir.is_symlink() or not default_dir.is_dir()):
        raise ManagedBrowserSessionError("managed profile Default path is not a regular directory")
    default_dir.mkdir(mode=0o700, exist_ok=True)

    preferences = default_dir / "Preferences"
    if preferences.is_symlink():
        raise ManagedBrowserSessionError("managed browser Preferences must not be a symlink")

    payload: dict[str, object] = {}
    if preferences.exists():
        if not preferences.is_file():
            raise ManagedBrowserSessionError("managed browser Preferences is not a regular file")
        try:
            decoded = json.loads(preferences.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ManagedBrowserSessionError(
                "managed browser Preferences contains invalid JSON"
            ) from exc
        if not isinstance(decoded, dict):
            raise ManagedBrowserSessionError(
                "managed browser Preferences must contain a JSON object"
            )
        payload = decoded

    existing_download = payload.get("download")
    if existing_download is None:
        download: dict[str, object] = {}
    elif isinstance(existing_download, dict):
        download = dict(existing_download)
    else:
        raise ManagedBrowserSessionError(
            "managed browser download Preferences must be a JSON object"
        )

    download.update(
        {
            "default_directory": str(download_dir),
            "directory_upgrade": True,
            "prompt_for_download": False,
        }
    )
    payload["download"] = download

    fd, temp_name = tempfile.mkstemp(prefix=".Preferences.", dir=default_dir)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.chmod(0o600)
        os.replace(temp_path, preferences)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


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


def _validate_extension_dirs(raw_dirs: Sequence[str]) -> tuple[str, ...]:
    if len(raw_dirs) > _MAX_EXTENSION_DIRS:
        raise ValueError(f"extension_dirs may contain at most {_MAX_EXTENSION_DIRS} directories")
    result: list[str] = []
    seen: set[str] = set()
    for raw in raw_dirs:
        value = raw.strip()
        if not value:
            raise ValueError("extension_dir must not be empty")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("extension_dir must be an absolute path")
        resolved = path.resolve(strict=False)
        if not resolved.is_dir():
            raise ValueError("extension_dir must be an existing directory")
        text = str(resolved)
        if "," in text:
            raise ValueError("extension_dir must not contain commas")
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return tuple(result)


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


def _launch_argv(
    executable: str,
    profile: Path,
    *,
    extension_dirs: Sequence[str],
    start_url: str | None,
) -> tuple[str, ...]:
    argv = [
        executable,
        f"--user-data-dir={profile}",
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
    ]
    if extension_dirs:
        joined = ",".join(extension_dirs)
        argv.extend(
            [
                f"--disable-extensions-except={joined}",
                f"--load-extension={joined}",
            ]
        )
    argv.append(start_url or "about:blank")
    return tuple(argv)


def _require_managed_process(process: BrowserProcess, profile: Path) -> None:
    if process.user_data_dir != str(profile):
        raise ManagedBrowserSessionError("browser process profile identity changed")
    if process.remote_debugging_port != 0:
        raise ManagedBrowserSessionError(
            "managed profile is in use by a browser outside the dynamic-port CDP contract"
        )
    if process.remote_debugging_address != "127.0.0.1":
        raise ManagedBrowserSessionError(
            "managed profile is in use by a browser outside the loopback CDP contract"
        )
    if process.remote_debugging_pipe:
        raise ManagedBrowserSessionError(
            "managed profile is in use by a browser with an unexpected debugging pipe"
        )


def _read_devtools_endpoint(profile: Path) -> str | None:
    path = profile / _DEVTOOLS_ACTIVE_PORT
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    if len(lines) < 2:
        return None
    try:
        port = int(lines[0])
    except ValueError:
        return None
    if not 1 <= port <= 65535 or not lines[1].startswith("/devtools/browser/"):
        return None
    return f"http://127.0.0.1:{port}"
