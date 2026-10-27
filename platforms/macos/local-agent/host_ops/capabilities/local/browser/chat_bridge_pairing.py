"""Operator-approved, one-time localhost Chat Bridge pairing.

The extension never receives a machine key without a fresh native Mac dialog.
Pairing is transport authentication only; it does not release Chrome Send or
allow a browser to stage GitHub dispatches.
"""

from __future__ import annotations

import hmac
import os
import re
import secrets
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from local_agent.host_ops.core.execution import ExecutionLimits, ProcessRunner

_EXTENSION_ORIGIN = re.compile(r"chrome-extension://[a-p]{32}\Z")
_TICKET = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
_KEY = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
_PAIRING_LIFETIME_SECONDS = 120


def valid_extension_origin(origin: str | None) -> bool:
    return isinstance(origin, str) and bool(_EXTENSION_ORIGIN.fullmatch(origin))


def confirm_pairing_on_mac(origin: str, code: str) -> bool:
    """Prompt the actual desktop operator; never silently approve headless hosts."""
    if sys.platform != "darwin":
        return False
    # Both interpolated values are regex-validated (origin and decimal code).
    extension_id = origin.removeprefix("chrome-extension://")
    script = (
        'button returned of (display dialog '
        f'"Allow Chat Bridge extension {extension_id} to connect to Local Agent?\\n'
        f'Check that this code matches the extension: {code}\\n'
        'Only approve a connection you just requested." '
        'buttons {"Deny", "Allow"} default button "Deny" '
        'with title "Local Agent pairing" giving up after 90)'
    )
    try:
        result = ProcessRunner().run(
            ["/usr/bin/osascript", "-e", script],
            limits=ExecutionLimits(timeout_seconds=100, max_stdout_bytes=256,
                                   max_stderr_bytes=256),
        )
    except (OSError, RuntimeError, ValueError):
        return False
    return result.ok and not result.stdout_truncated and result.stdout.strip() == "Allow"


class BridgePairing:
    """Bounded in-memory requests and a durable private, never-rotated key."""

    def __init__(self, key_file: Path, *,
                 confirm: Callable[[str, str], bool] = confirm_pairing_on_mac,
                 clock: Callable[[], float] = time.monotonic):
        self.key_file = Path(key_file)
        self.confirm = confirm
        self.clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, object] | None = None

    def key(self) -> str:
        try:
            info = self.key_file.lstat()
        except FileNotFoundError:
            return ""
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise PermissionError("Bridge pairing key must be a private regular file")
        data = self.key_file.read_text(encoding="ascii").strip()
        if not _KEY.fullmatch(data):
            raise ValueError("Invalid persisted Bridge pairing key")
        return data

    def _ensure_key(self) -> str:
        existing = self.key()
        if existing:
            return existing
        parent = self.key_file.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = parent.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("Bridge pairing directory must be owner-only")
        key = secrets.token_urlsafe(48)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.key_file, flags, 0o600)
        except FileExistsError:
            return self.key()
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(key + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return key

    def start(self, origin: str) -> dict[str, str]:
        if not valid_extension_origin(origin):
            raise PermissionError("Pairing requires a Chrome extension origin")
        now = self.clock()
        with self._lock:
            if self._pending is not None and now < float(self._pending["expires_at"]):
                raise RuntimeError("Only one pending pairing request is allowed")
            ticket = secrets.token_urlsafe(32)
            code = f"{secrets.randbelow(1_000_000):06d}"
            self._pending = {
                "ticket": ticket, "code": code, "origin": origin,
                "expires_at": now + _PAIRING_LIFETIME_SECONDS, "state": "pending",
            }
        thread = threading.Thread(target=self._ask_operator, args=(ticket, origin, code),
                                  name="chat-bridge-pairing-approval", daemon=True)
        thread.start()
        return {"ticket": ticket, "code": code}

    def _ask_operator(self, ticket: str, origin: str, code: str) -> None:
        try:
            approved = self.confirm(origin, code) is True
        except Exception:
            approved = False
        with self._lock:
            state = self._pending
            if (state is None or not hmac.compare_digest(str(state["ticket"]), ticket)
                    or self.clock() >= float(state["expires_at"])):
                return
            state["state"] = "approved" if approved else "denied"

    def finish(self, origin: str, ticket: str) -> dict[str, str | bool]:
        if not valid_extension_origin(origin) or not isinstance(ticket, str) or not _TICKET.fullmatch(ticket):
            raise PermissionError("Invalid pairing finish identity")
        with self._lock:
            state = self._pending
            if (state is None or not hmac.compare_digest(str(state["ticket"]), ticket)
                    or not hmac.compare_digest(str(state["origin"]), origin)):
                raise PermissionError("Unknown pairing request")
            if self.clock() >= float(state["expires_at"]):
                self._pending = None
                return {"ok": False, "status": "expired"}
            phase = str(state["state"])
            if phase == "pending":
                return {"ok": True, "status": "pending"}
            self._pending = None  # One-time redeem, including failure.
            if phase != "approved":
                return {"ok": False, "status": "denied"}
            key = self._ensure_key()
            return {"ok": True, "status": "paired", "key": key}
