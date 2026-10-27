"""Structured results for bounded local serial transactions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SerialTransactionResult:
    requested_port: str
    resolved_port: str
    baudrate: int
    bytes_written: int
    data: bytes
    elapsed_seconds: float
    deadline_reached: bool
    idle_complete: bool
    read_limit_reached: bool

    def as_dict(self) -> dict[str, object]:
        try:
            read_utf8: str | None = self.data.decode("utf-8")
        except UnicodeDecodeError:
            read_utf8 = None
        return {
            "requested_port": self.requested_port,
            "resolved_port": self.resolved_port,
            "baudrate": self.baudrate,
            "bytes_written": self.bytes_written,
            "bytes_read": len(self.data),
            "read_hex": self.data.hex(),
            "read_utf8": read_utf8,
            "elapsed_seconds": self.elapsed_seconds,
            "deadline_reached": self.deadline_reached,
            "idle_complete": self.idle_complete,
            "read_limit_reached": self.read_limit_reached,
        }
