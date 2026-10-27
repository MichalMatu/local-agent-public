"""Bounded local DNS and TCP probes."""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field

from local_agent.host_ops.core.execution import ExecutionLimits, ProcessResult, ProcessRunner, ProcessState

from .models import NetworkResolutionResult, ResolvedAddress, TcpProbeResult

_WORKER_MODULE = "local_agent.host_ops.capabilities.local.network._worker"
_MAX_TIMEOUT_SECONDS = 60.0


class NetworkProbeError(ValueError):
    """Raised when network probe input is invalid."""


@dataclass(frozen=True, slots=True)
class NetworkProber:
    runner: ProcessRunner = field(default_factory=ProcessRunner)

    def resolve(self, host: str, *, timeout_seconds: float = 5.0) -> NetworkResolutionResult:
        normalized_host = _validate_host(host)
        timeout = _validate_timeout(timeout_seconds)
        process = self.runner.run(
            (sys.executable, "-m", _WORKER_MODULE, "resolve", normalized_host),
            limits=_limits(timeout),
        )
        if process.state is not ProcessState.COMPLETED or process.exit_code != 0:
            return NetworkResolutionResult(
                host=normalized_host,
                addresses=(),
                duration_seconds=process.duration_seconds,
                error=_process_error(process),
            )
        payload = _parse_payload(process.stdout)
        addresses = _parse_addresses(payload.get("addresses"))
        return NetworkResolutionResult(
            host=normalized_host,
            addresses=addresses,
            duration_seconds=process.duration_seconds,
            error=_optional_string(payload.get("error")),
        )

    def tcp(
        self,
        host: str,
        port: int,
        *,
        timeout_seconds: float = 5.0,
    ) -> TcpProbeResult:
        normalized_host = _validate_host(host)
        normalized_port = _validate_port(port)
        timeout = _validate_timeout(timeout_seconds)
        process = self.runner.run(
            (
                sys.executable,
                "-m",
                _WORKER_MODULE,
                "tcp",
                normalized_host,
                str(normalized_port),
                f"{timeout:g}",
            ),
            limits=_limits(timeout),
        )
        if process.state is not ProcessState.COMPLETED or process.exit_code != 0:
            return TcpProbeResult(
                host=normalized_host,
                port=normalized_port,
                connected=False,
                duration_seconds=process.duration_seconds,
                error=_process_error(process),
            )
        payload = _parse_payload(process.stdout)
        return TcpProbeResult(
            host=normalized_host,
            port=normalized_port,
            connected=payload.get("connected") is True,
            duration_seconds=process.duration_seconds,
            peer_address=_optional_string(payload.get("peer_address")),
            family=_optional_string(payload.get("family")),
            error=_optional_string(payload.get("error")),
        )


def _limits(timeout_seconds: float) -> ExecutionLimits:
    return ExecutionLimits(
        timeout_seconds=timeout_seconds + 1.0,
        max_stdout_bytes=32 * 1024,
        max_stderr_bytes=32 * 1024,
    )


def _validate_host(host: str) -> str:
    if not isinstance(host, str):
        raise NetworkProbeError("host must be a string")
    normalized = host.strip()
    if not normalized or len(normalized) > 253:
        raise NetworkProbeError("host must contain between 1 and 253 characters")
    if "\x00" in normalized or any(character.isspace() for character in normalized):
        raise NetworkProbeError("host must not contain whitespace or NUL bytes")
    if "/" in normalized or "://" in normalized:
        raise NetworkProbeError("host must be a hostname or IP address, not a URL")
    return normalized


def _validate_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise NetworkProbeError("port must be an integer between 1 and 65535")
    return port


def _validate_timeout(timeout_seconds: float) -> float:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int | float)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or timeout_seconds > _MAX_TIMEOUT_SECONDS
    ):
        raise NetworkProbeError(
            f"timeout_seconds must be a finite number in (0, {_MAX_TIMEOUT_SECONDS:g}]"
        )
    return float(timeout_seconds)


def _parse_payload(stdout: str) -> dict[str, object]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("network worker returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("network worker returned a non-object payload")
    return {str(key): value for key, value in payload.items()}


def _parse_addresses(value: object) -> tuple[ResolvedAddress, ...]:
    if not isinstance(value, list):
        return ()
    parsed: list[ResolvedAddress] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        family = item.get("family")
        address = item.get("address")
        if isinstance(family, str) and isinstance(address, str):
            parsed.append(ResolvedAddress(family=family, address=address))
    return tuple(parsed)


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _process_error(process: ProcessResult) -> str:
    if process.error:
        return process.error
    if process.stderr.strip():
        return process.stderr.strip()
    return f"network worker exited with status {process.exit_code}"
