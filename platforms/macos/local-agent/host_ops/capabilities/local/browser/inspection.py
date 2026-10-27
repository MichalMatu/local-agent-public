"""Read-only local browser process and DevTools endpoint inspection."""

from __future__ import annotations

import http.client
import json
import re
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit, urlunsplit

from local_agent.host_ops.core.execution import ExecutionLimits, ProcessRunner

from ._validation import validate_timeout_seconds
from .models import BrowserInspection, BrowserProcess, DevtoolsEndpoint, DevtoolsTarget

_PS = "/bin/ps"
_MAX_HTTP_BYTES = 1_048_576
_DEFAULT_LIMITS = ExecutionLimits(
    timeout_seconds=5.0,
    max_stdout_bytes=1_048_576,
    max_stderr_bytes=16_384,
)
_PROCESS_RE = re.compile(r"^\s*(\d+)\s+(\d+)\s+(.+?)\s*$")
_PORT_RE = re.compile(r"(?:^|\s)--remote-debugging-port(?:=|\s+)(\d+)(?:\s|$)")
_ADDRESS_RE = re.compile(r"(?:^|\s)--remote-debugging-address(?:=|\s+)([^\s]+)")
_USER_DATA_RE = re.compile(r"(?:^|\s)--user-data-dir(?:=|\s+)([^\s]+)")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_DEVTOOLS_PATHS = frozenset({"/json/version", "/json/list"})
_CHROMIUM_FAMILIES = frozenset({"chrome", "chromium", "edge", "brave"})


class BrowserInspectionError(RuntimeError):
    """Raised when trustworthy local browser process evidence cannot be produced."""


JsonFetcher = Callable[[str, float], object]


class BrowserInspector:
    """Collect bounded, vendor-neutral evidence about local browser processes and CDP."""

    def __init__(
        self,
        runner: ProcessRunner | None = None,
        *,
        fetch_json: JsonFetcher | None = None,
    ) -> None:
        self._runner = runner or ProcessRunner()
        self._fetch_json = fetch_json or _fetch_json

    def inspect(
        self,
        *,
        endpoints: Sequence[str] = (),
        timeout_seconds: float = 5.0,
        limits: ExecutionLimits | None = None,
    ) -> BrowserInspection:
        timeout_seconds = validate_timeout_seconds(timeout_seconds, minimum=0.1)
        processes = self._processes(limits=limits or _DEFAULT_LIMITS)
        warnings: list[str] = []
        endpoint_urls: list[str] = []

        for raw_endpoint in endpoints:
            endpoint_urls.append(_normalize_loopback_endpoint(raw_endpoint))

        for process in processes:
            endpoint = _endpoint_from_process(process, warnings)
            if endpoint is not None:
                endpoint_urls.append(endpoint)

        unique_endpoints = tuple(dict.fromkeys(endpoint_urls))
        endpoint_results = tuple(
            self._inspect_endpoint(endpoint, timeout_seconds=timeout_seconds)
            for endpoint in unique_endpoints
        )
        return BrowserInspection(
            processes=processes,
            endpoints=endpoint_results,
            warnings=tuple(warnings),
        )

    def _processes(self, *, limits: ExecutionLimits) -> tuple[BrowserProcess, ...]:
        result = self._runner.run((_PS, "-axo", "pid=,ppid=,command="), limits=limits)
        if not result.ok or result.stdout_truncated or result.stderr_truncated:
            detail = result.stderr.strip() or result.error or f"exit_code={result.exit_code}"
            if result.stdout_truncated or result.stderr_truncated:
                detail = "process listing output was truncated"
            raise BrowserInspectionError(f"could not inspect browser processes: {detail}")

        processes: list[BrowserProcess] = []
        for line in result.stdout.splitlines():
            parsed = _parse_process_line(line)
            if parsed is not None:
                processes.append(parsed)
        return tuple(processes)

    def _inspect_endpoint(self, endpoint: str, *, timeout_seconds: float) -> DevtoolsEndpoint:
        try:
            version = self._fetch_json(f"{endpoint}/json/version", timeout_seconds)
            targets_raw = self._fetch_json(f"{endpoint}/json/list", timeout_seconds)
            if not isinstance(version, dict):
                raise ValueError("/json/version did not return an object")
            if not isinstance(targets_raw, list):
                raise ValueError("/json/list did not return an array")
            targets = tuple(
                target
                for item in targets_raw
                if isinstance(item, dict) and (target := _parse_target(item)) is not None
            )
            return DevtoolsEndpoint(
                endpoint=endpoint,
                reachable=True,
                browser=_optional_string(version.get("Browser")),
                protocol_version=_optional_string(version.get("Protocol-Version")),
                user_agent=_optional_string(version.get("User-Agent")),
                web_socket_debugger_url=_optional_string(version.get("webSocketDebuggerUrl")),
                targets=targets,
            )
        except Exception as exc:
            return DevtoolsEndpoint(
                endpoint=endpoint,
                reachable=False,
                browser=None,
                protocol_version=None,
                user_agent=None,
                web_socket_debugger_url=None,
                targets=(),
                error=str(exc),
            )


def _parse_process_line(line: str) -> BrowserProcess | None:
    match = _PROCESS_RE.match(line)
    if not match:
        return None
    pid = int(match.group(1))
    parent_pid = int(match.group(2))
    command = match.group(3)
    family = _browser_family(command)
    if family is None or _is_browser_child_process(command, family):
        return None

    port_match = _PORT_RE.search(command)
    address_match = _ADDRESS_RE.search(command)
    user_data_match = _USER_DATA_RE.search(command)
    remote_address = _clean_cli_value(address_match.group(1)) if address_match else None
    user_data_dir = _clean_cli_value(user_data_match.group(1)) if user_data_match else None
    return BrowserProcess(
        pid=pid,
        parent_pid=parent_pid,
        family=family,
        remote_debugging_port=int(port_match.group(1)) if port_match else None,
        remote_debugging_address=remote_address,
        remote_debugging_pipe="--remote-debugging-pipe" in command,
        user_data_dir=user_data_dir,
    )


def _browser_family(command: str) -> str | None:
    lowered = command.lower()
    if "google chrome for testing.app/contents/macos/google chrome for testing" in lowered:
        return "chrome"
    if (
        "google chrome.app/contents/macos/google chrome" in lowered
        or "google-chrome" in lowered
        or "/opt/google/chrome/chrome" in lowered
    ):
        return "chrome"
    if "brave browser.app/contents/macos/brave browser" in lowered or "brave-browser" in lowered:
        return "brave"
    if "microsoft edge.app/contents/macos/microsoft edge" in lowered or "microsoft-edge" in lowered:
        return "edge"
    if "chromium.app/contents/macos/chromium" in lowered or re.search(
        r"(?:^|/)chromium(?:\s|$)", lowered
    ):
        return "chromium"
    if "firefox.app/contents/macos/firefox" in lowered or re.search(
        r"(?:^|/)firefox(?:\s|$)", lowered
    ):
        return "firefox"
    if "/safari.app/contents/macos/safari" in lowered:
        return "safari"
    return None


def _is_browser_child_process(command: str, family: str) -> bool:
    lowered = command.lower()
    if family in _CHROMIUM_FAMILIES:
        return " --type=" in lowered or " helper" in lowered
    if family == "firefox":
        return " -contentproc " in lowered
    if family == "safari":
        return "safari web content" in lowered or "safari networking" in lowered
    return False


def _clean_cli_value(value: str) -> str:
    return value.strip().strip("\"'")


def _endpoint_from_process(process: BrowserProcess, warnings: list[str]) -> str | None:
    if process.family not in _CHROMIUM_FAMILIES:
        return None
    port = process.remote_debugging_port
    if port is None:
        if process.remote_debugging_pipe:
            warnings.append(
                f"pid {process.pid}: remote debugging uses pipe and is not HTTP-attachable"
            )
        return None
    if port == 0:
        warnings.append(
            f"pid {process.pid}: dynamic remote debugging port cannot be inferred from ps"
        )
        return None
    if not 1 <= port <= 65535:
        warnings.append(f"pid {process.pid}: invalid remote debugging port {port}")
        return None
    host = process.remote_debugging_address or "127.0.0.1"
    if host not in _LOOPBACK_HOSTS:
        warnings.append(f"pid {process.pid}: non-loopback remote debugging address was not probed")
        return None
    authority = f"[{host}]:{port}" if host == "::1" else f"{host}:{port}"
    return f"http://{authority}"


def _normalize_loopback_endpoint(raw: str) -> str:
    value = raw.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("browser endpoint must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("browser endpoint must not contain credentials")
    host = parsed.hostname
    if host not in _LOOPBACK_HOSTS:
        raise ValueError("browser endpoint host must be loopback")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("browser endpoint port is invalid") from exc
    if port is None:
        raise ValueError("browser endpoint must include an explicit port")
    if port == 0:
        raise ValueError("browser endpoint port is invalid")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("browser endpoint must not include a path, query or fragment")
    authority = f"[{host}]:{port}" if host == "::1" else f"{host}:{port}"
    return f"{parsed.scheme}://{authority}"


def _parse_target(raw: dict[object, object]) -> DevtoolsTarget | None:
    target_id = _optional_string(raw.get("id"))
    target_type = _optional_string(raw.get("type"))
    if target_id is None or target_type is None:
        return None
    return DevtoolsTarget(
        target_id=target_id,
        target_type=target_type,
        title=_optional_string(raw.get("title")) or "",
        url=_sanitize_target_url(_optional_string(raw.get("url")) or ""),
        web_socket_debugger_url=_optional_string(raw.get("webSocketDebuggerUrl")),
    )


def _sanitize_target_url(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return ""
    if not parsed.scheme:
        return ""
    if parsed.scheme not in {"http", "https"}:
        return f"{parsed.scheme}:"
    host = parsed.hostname
    if not host:
        return ""
    authority_host = f"[{host}]" if ":" in host else host
    authority = authority_host if port is None else f"{authority_host}:{port}"
    return urlunsplit((parsed.scheme, authority, parsed.path, "", ""))


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _fetch_json(url: str, timeout_seconds: float) -> object:
    parsed = urlsplit(url)
    host = parsed.hostname
    if parsed.scheme not in {"http", "https"} or host not in _LOOPBACK_HOSTS:
        raise ValueError("DevTools probe URL must use HTTP(S) loopback")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("DevTools probe URL must not contain credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("DevTools probe port is invalid") from exc
    if port is None or parsed.path not in _DEVTOOLS_PATHS or parsed.query or parsed.fragment:
        raise ValueError("DevTools probe URL is outside the inspection contract")

    connection_type = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_type(host, port, timeout=timeout_seconds)
    try:
        connection.request("GET", parsed.path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f"HTTP status {response.status}")
        payload = response.read(_MAX_HTTP_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"endpoint unavailable: {exc}") from exc
    finally:
        connection.close()

    if len(payload) > _MAX_HTTP_BYTES:
        raise ValueError("endpoint response exceeded byte limit")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("endpoint returned invalid JSON") from exc
