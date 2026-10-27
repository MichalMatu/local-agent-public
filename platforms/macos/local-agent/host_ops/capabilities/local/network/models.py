"""Structured results for bounded local network probes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResolvedAddress:
    family: str
    address: str

    def as_dict(self) -> dict[str, str]:
        return {"family": self.family, "address": self.address}


@dataclass(frozen=True, slots=True)
class NetworkResolutionResult:
    host: str
    addresses: tuple[ResolvedAddress, ...]
    duration_seconds: float
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.addresses)

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "host": self.host,
            "addresses": [item.as_dict() for item in self.addresses],
            "duration_seconds": self.duration_seconds,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class TcpProbeResult:
    host: str
    port: int
    connected: bool
    duration_seconds: float
    peer_address: str | None = None
    family: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.connected and self.error is None

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "host": self.host,
            "port": self.port,
            "connected": self.connected,
            "duration_seconds": self.duration_seconds,
            "peer_address": self.peer_address,
            "family": self.family,
            "error": self.error,
        }
