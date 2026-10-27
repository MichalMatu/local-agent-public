"""Durable, single-effect Chat Bridge tool mailbox.

The Conversation Fabric planner supplies immutable GitHub dispatches. This
Host Ops primitive knows only transport requests and receipts; it cannot
select children, infer completed work, or access GitHub.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_TRANSACTION = re.compile(r"spawn-[0-9a-f]{64}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DISPATCH = re.compile(r"fabric-[0-9a-f]{32}\Z")
_REQUEST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")
_WAKE = re.compile(r"wake-[0-9a-f]{64}\Z")
_CHILD_URL = re.compile(r"https://chatgpt\.com/c/[A-Za-z0-9_-]{1,200}\Z")
_MAX_ITEMS = 32
_MAX_BYTES = 2 * 1024 * 1024


def validate_launch(value: Any) -> dict[str, Any]:
    """Reject extra keys, forged field types, or oversized browser input."""
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "dispatch_id", "child_request_id", "intent"
    } or value["schema_version"] != 1:
        raise ValueError("Invalid Bridge launch envelope")
    if not isinstance(value["dispatch_id"], str) or not _DISPATCH.fullmatch(value["dispatch_id"]):
        raise ValueError("Invalid Bridge dispatch identity")
    if not isinstance(value["child_request_id"], str) or not _REQUEST.fullmatch(value["child_request_id"]):
        raise ValueError("Invalid Bridge child identity")
    intent = value["intent"]
    if not isinstance(intent, dict) or set(intent) != {
        "schema_version", "transaction_id", "child_request_digest", "bootstrap_digest",
        "bootstrap_text", "tab_id",
    } or intent["schema_version"] != 1 or intent["tab_id"] is not None:
        raise ValueError("Invalid Bridge spawn intent")
    if not isinstance(intent["transaction_id"], str) or not _TRANSACTION.fullmatch(intent["transaction_id"]):
        raise ValueError("Invalid Bridge transaction identity")
    for field in ("child_request_digest", "bootstrap_digest"):
        if not isinstance(intent[field], str) or not _DIGEST.fullmatch(intent[field]):
            raise ValueError("Invalid Bridge request digest")
    text = intent["bootstrap_text"]
    if not isinstance(text, str) or not text.strip() or len(text) > 32768:
        raise ValueError("Invalid Bridge prompt")
    import hashlib

    if "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest() != intent["bootstrap_digest"]:
        raise ValueError("Bridge prompt digest mismatch")
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > 131072:
        raise ValueError("Bridge envelope oversized")
    return value


_PARENT_URL = re.compile(r"https://chatgpt\.com/c/[A-Za-z0-9_-]{1,200}\Z")


def validate_parent_url(value: Any) -> str:
    """Browser address-bar identity only; never accept transcript or page data."""
    if not isinstance(value, str) or not _PARENT_URL.fullmatch(value):
        raise ValueError("Invalid Chrome parent conversation URL")
    return value


def save_parent_url(path: Path, parent_url: str) -> None:
    """Persist one operator-selected parent route with owner-only permissions."""
    from datetime import datetime, timezone
    parent_url = validate_parent_url(parent_url)
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_info = path.parent.stat()
    if parent_info.st_uid != os.getuid() or parent_info.st_mode & 0o077:
        raise PermissionError("Parent-route directory must be private")
    if path.is_symlink():
        raise PermissionError("Parent-route path must not be a symlink")
    data = json.dumps({"schema_version": 1, "parent_url": parent_url,
                       "selected_at": datetime.now(timezone.utc).isoformat()},
                      sort_keys=True, separators=(",", ":")) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".chat-parent-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class ChatBridgeSpool:
    """Path-scoped file lock and atomic JSON updates, across OS processes."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @contextmanager
    def _change(self) -> Iterator[dict[str, Any]]:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with os.fdopen(fd, "rb+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                if self.path.exists():
                    if self.path.stat().st_size > _MAX_BYTES:
                        raise ValueError("Bridge state oversized")
                    state = json.loads(self.path.read_text(encoding="utf-8"))
                else:
                    state = {"schema_version": 1, "items": {}}
                if (not isinstance(state, dict) or set(state) != {"schema_version", "items"}
                        or state["schema_version"] != 1 or not isinstance(state["items"], dict)
                        or len(state["items"]) > _MAX_ITEMS):
                    raise ValueError("Bridge state malformed")
                yield state
                encoded = (json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
                if len(encoded) > _MAX_BYTES:
                    raise ValueError("Bridge state capacity exhausted")
                temporary_fd, temporary = tempfile.mkstemp(prefix=".chat-bridge-", dir=self.path.parent)
                try:
                    with os.fdopen(temporary_fd, "wb") as target:
                        os.fchmod(target.fileno(), 0o600)
                        target.write(encoded)
                        target.flush()
                        os.fsync(target.fileno())
                    os.replace(temporary, self.path)
                    directory_fd = os.open(self.path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
        except BaseException:
            # fdopen closes the file on success and on exceptions inside its scope.
            raise

    def offer(self, envelope: dict[str, Any]) -> str:
        launch = validate_launch(envelope)
        key = launch["intent"]["transaction_id"]
        with self._change() as state:
            items = state["items"]
            previous = items.get(key)
            if previous is not None:
                if previous["launch"] != launch:
                    raise ValueError("Immutable Bridge transaction collision")
                return previous["phase"]
            if len(items) >= _MAX_ITEMS:
                raise ValueError("Bridge mailbox full; operator reconciliation required")
            items[key] = {"phase": "queued", "launch": launch, "child_url": ""}
            return "queued"


    def offer_parent_wake(self, wake: dict[str, Any]) -> str:
        """One parent wake after all child-authored GitHub results exist."""
        if (not isinstance(wake, dict) or set(wake) != {
            "schema_version", "kind", "wake_id", "dispatch_id", "parent_url", "child_request_ids"
        } or type(wake["schema_version"]) is not int or wake["schema_version"] != 1
                or wake["kind"] != "parent_wake" or not isinstance(wake["wake_id"], str)
                or not _WAKE.fullmatch(wake["wake_id"])
                or not isinstance(wake["dispatch_id"], str) or not _DISPATCH.fullmatch(wake["dispatch_id"])
                or not isinstance(wake["parent_url"], str) or not _CHILD_URL.fullmatch(wake["parent_url"])
                or not isinstance(wake["child_request_ids"], list)
                or not 1 <= len(wake["child_request_ids"]) <= 4
                or any(not isinstance(i, str) or not _REQUEST.fullmatch(i)
                       for i in wake["child_request_ids"])
                or len(set(wake["child_request_ids"])) != len(wake["child_request_ids"])):
            raise ValueError("Invalid parent wake identity")
        with self._change() as state:
            items = state["items"]
            previous = items.get(wake["wake_id"])
            if previous is not None:
                if previous["launch"] != wake:
                    raise ValueError("Parent wake identity collision")
                return previous["phase"]
            if len(items) >= _MAX_ITEMS:
                raise ValueError("Bridge mailbox full")
            items[wake["wake_id"]] = {"phase": "queued", "launch": wake, "child_url": ""}
            return "queued"

    def acknowledge_parent_wake(self, wake_id: str) -> str:
        if not isinstance(wake_id, str) or not _WAKE.fullmatch(wake_id):
            raise ValueError("Invalid parent wake acknowledgement")
        with self._change() as state:
            item = state["items"].get(wake_id)
            if (item is None or item["launch"].get("kind") != "parent_wake"
                    or item["phase"] not in {"submission_unknown", "acknowledged"}):
                raise ValueError("Unknown parent wake acknowledgement")
            item["phase"] = "acknowledged"
            return "acknowledged"

    def take(self) -> dict[str, Any] | None:
        """Take one item ONCE. Lost HTTP response is UNKNOWN, never replay."""
        with self._change() as state:
            for key in sorted(state["items"]):
                item = state["items"][key]
                if item["phase"] == "queued":
                    item["phase"] = "submission_unknown"
                    return item["launch"]
        return None

    def acknowledge(self, transaction_id: str, child_url: str) -> str:
        if not isinstance(transaction_id, str) or not _TRANSACTION.fullmatch(transaction_id):
            raise ValueError("Invalid Bridge acknowledgement transaction")
        if not isinstance(child_url, str) or not _CHILD_URL.fullmatch(child_url):
            raise ValueError("Invalid Bridge acknowledgement URL")
        with self._change() as state:
            item = state["items"].get(transaction_id)
            if (item is None or item["launch"].get("kind") == "parent_wake"
                    or item["phase"] not in {"submission_unknown", "acknowledged"}):
                raise ValueError("Bridge acknowledgement without consumed transaction")
            if item["child_url"] and item["child_url"] != child_url:
                raise ValueError("Bridge acknowledgement identity conflict")
            item["child_url"] = child_url
            item["phase"] = "acknowledged"
            return "acknowledged"

    def status(self) -> dict[str, dict[str, str]]:
        with self._change() as state:
            return {key: {"phase": item["phase"], "child_url": item["child_url"]}
                    for key, item in state["items"].items()}
