from __future__ import annotations

import contextlib
import fcntl
import json
import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from local_agent.conversation import contract, spawn
from local_agent.conversation.store import WorkflowConversationStore
from local_agent.foundation.process import atomic_write_text, fsync_directory

SPAWN_ATTEMPT_FILENAME_WIDTH = 6


def _json_text(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"


def _timestamp_key(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


class WorkflowConversationSpawnStore:
    """Durable workflow-owned spawn attempts with globally serialized browser use.

    The global lock is coordination only. There is no global manifest or active pointer;
    queue ownership is reconstructed from the durable per-workflow transaction files.
    """

    def __init__(self, conversation_store: WorkflowConversationStore) -> None:
        if not isinstance(conversation_store, WorkflowConversationStore):
            raise TypeError("conversation_store must be a WorkflowConversationStore")
        self.store = conversation_store
        self.workflow_store = conversation_store.workflow_store
        self.root = conversation_store.root / "spawns"
        self.global_lock_path = (
            self.workflow_store.state_dir
            / "locks"
            / "conversation-spawn"
            / "global.lock"
        )

    @contextlib.contextmanager
    def _global_lock(self) -> Iterator[None]:
        self.global_lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.global_lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _request_dir(self, request_id: str) -> Path:
        request = self.store.load_request(request_id)
        return self.root / str(request["id"])

    def _attempt_path(self, request_id: str, attempt: int) -> Path:
        if type(attempt) is not int or not 1 <= attempt <= spawn.MAX_SPAWN_ATTEMPTS:
            raise ValueError(f"spawn attempt must be 1..{spawn.MAX_SPAWN_ATTEMPTS}")
        return self._request_dir(request_id) / (
            f"{attempt:0{SPAWN_ATTEMPT_FILENAME_WIDTH}d}.json"
        )

    def _ensure_request_dir(self, request_id: str) -> Path:
        self.store._ensure_layout()
        root_created = not self.root.exists()
        self.root.mkdir(exist_ok=True)
        if root_created:
            fsync_directory(self.store.root)
        request_dir = self._request_dir(request_id)
        created = not request_dir.exists()
        request_dir.mkdir(exist_ok=True)
        if created:
            fsync_directory(self.root)
        return request_dir

    def load_attempts(self, request_id: str) -> list[dict[str, Any]]:
        request = self.store.load_request(request_id)
        request_dir = self.root / str(request["id"])
        if not request_dir.exists():
            return []
        if request_dir.is_symlink() or not request_dir.is_dir():
            raise ValueError(f"spawn request directory is invalid: {request_id!r}")

        paths = sorted(request_dir.glob("*.json"))
        if len(paths) > spawn.MAX_SPAWN_ATTEMPTS:
            raise ValueError(
                f"spawn attempts exceed {spawn.MAX_SPAWN_ATTEMPTS} for {request_id!r}"
            )
        attempts: list[dict[str, Any]] = []
        for expected, path in enumerate(paths, start=1):
            expected_name = f"{expected:0{SPAWN_ATTEMPT_FILENAME_WIDTH}d}.json"
            if path.name != expected_name:
                raise ValueError(
                    "spawn attempt files are not contiguous: "
                    f"expected {expected_name!r}, got {path.name!r}"
                )
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"spawn attempt path must be a regular file: {path.name}")
            raw = path.read_bytes()
            if len(raw) > spawn.MAX_SPAWN_TRANSACTION_BYTES + 1:
                raise ValueError(f"spawn attempt exceeds bounds: {path.name}")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid spawn attempt file: {path.name}") from exc
            spawn.validate_spawn_transaction(payload, request=request)
            if payload["attempt"] != expected:
                raise ValueError(
                    f"spawn attempt number mismatch in {path.name!r}: expected {expected}"
                )
            attempts.append(payload)
        return attempts

    def load_attempt(self, request_id: str, attempt: int) -> dict[str, Any]:
        attempts = self.load_attempts(request_id)
        if type(attempt) is not int or not 1 <= attempt <= len(attempts):
            raise ValueError(
                f"spawn attempt is unavailable: request={request_id!r} attempt={attempt!r}"
            )
        return attempts[attempt - 1]

    def attach_manual_child(
        self,
        request_id: str,
        *,
        child_conversation_url: str,
        registered_at: str,
    ) -> dict[str, Any]:
        """Register one user-created child without creating a spawn transaction.

        Fresh manual attachment and automatic spawning share the global spawn lock so
        they cannot race. Any existing spawn attempt belongs to the automatic/recovery
        path and must be resolved there instead of being silently replaced.
        """
        with self._global_lock():
            request = self.store.load_request(request_id)
            attempts = self.load_attempts(request_id)
            if attempts:
                raise ValueError(
                    "fresh manual child attach requires zero spawn attempts; "
                    "use spawn recovery instead"
                )

            canonical = contract.canonical_conversation_url(child_conversation_url)
            if canonical != child_conversation_url:
                raise ValueError(
                    "manual child conversation URL must use canonical ChatGPT URL form"
                )
            registration = {
                "schema_version": contract.CHILD_REGISTRATION_SCHEMA_VERSION,
                "child_request_id": request["id"],
                "child_request_digest": contract.child_request_digest(request),
                "parent_conversation_url": request["parent_conversation_url"],
                "child_conversation_url": canonical,
                "registered_at": registered_at,
            }

            existing = self.store.load_registration(request_id)
            if existing is not None:
                return self.store.register_child(registration)

            lifecycle = self.store.load_state(request_id)
            if lifecycle["state"] == "requested":
                self.store.transition_state(request_id, "registration_pending")
                lifecycle = self.store.load_state(request_id)
            if lifecycle["state"] != "registration_pending":
                raise ValueError(
                    "fresh manual child attach requires requested or "
                    "registration_pending state"
                )
            return self.store.register_child(registration)

    def enqueue(self, request_id: str, *, created_at: str) -> dict[str, Any]:
        """Create or return the request's one queued/nonterminal transaction.

        Only a safe pre-submit failed/cancelled transaction may get a later attempt.
        Ambiguous attempts never authorize automatic replacement.
        """
        with self._global_lock():
            request = self.store.load_request(request_id)
            if self.store.load_registration(request_id) is not None:
                raise ValueError("registered child does not require another spawn attempt")

            lifecycle = self.store.load_state(request_id)
            if lifecycle["state"] == "requested":
                self.store.transition_state(request_id, "registration_pending")
                lifecycle = self.store.load_state(request_id)
            if lifecycle["state"] != "registration_pending":
                raise ValueError(
                    "spawn enqueue requires requested or registration_pending child state"
                )

            attempts = self.load_attempts(request_id)
            nonterminal = [
                item
                for item in attempts
                if item["state"] not in spawn.TERMINAL_SPAWN_STATES
            ]
            if len(nonterminal) > 1:
                raise ValueError("multiple nonterminal spawn transactions exist for one request")
            if nonterminal:
                return nonterminal[0]

            if attempts:
                latest = attempts[-1]
                if latest["state"] == "ambiguous":
                    raise ValueError(
                        "unresolved ambiguous spawn forbids an automatic replacement attempt"
                    )
                if latest["state"] == "done":
                    raise ValueError("completed spawn cannot be retried")
                if latest["state"] not in {"failed", "cancelled"}:
                    raise ValueError(
                        f"spawn retry is not allowed after {latest['state']!r}"
                    )

            attempt = len(attempts) + 1
            transaction = spawn.build_spawn_transaction(
                request,
                attempt=attempt,
                created_at=created_at,
            )
            directory = self._ensure_request_dir(request_id)
            target = directory / f"{attempt:0{SPAWN_ATTEMPT_FILENAME_WIDTH}d}.json"
            encoded = _json_text(transaction).encode("utf-8")
            try:
                descriptor = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                loaded = self.load_attempts(request_id)
                if attempt > len(loaded):
                    raise ValueError(
                        "spawn attempt appeared concurrently but history is incomplete"
                    )
                durable = loaded[attempt - 1]
                if durable["id"] != transaction["id"]:
                    raise ValueError(
                        "spawn attempt concurrently exists with a different identity"
                    )
                return durable
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            fsync_directory(directory)
            return self.load_attempt(request_id, attempt)

    def queue_snapshot(self) -> dict[str, Any]:
        with self._global_lock():
            return self._queue_snapshot_locked()

    def _queue_snapshot_locked(self) -> dict[str, Any]:
        entries = self._scan_all_transactions()
        active = [
            entry
            for entry in entries
            if entry["transaction"]["state"] in spawn.BROWSER_ACTIVE_SPAWN_STATES
        ]
        if len(active) > 1:
            raise ValueError("multiple globally active browser spawn transactions detected")

        unresolved: list[dict[str, Any]] = []
        for entry in entries:
            transaction = entry["transaction"]
            if transaction["state"] != "ambiguous":
                continue
            if self._matching_registration(entry["store"], transaction) is not None:
                continue
            lifecycle = entry["store"].load_state(transaction["child_request_id"])
            if lifecycle["state"] == "abandoned":
                continue
            unresolved.append(entry)

        pending = [
            entry for entry in entries if entry["transaction"]["state"] == "pending"
        ]
        pending.sort(
            key=lambda entry: (
                _timestamp_key(entry["transaction"]["created_at"]),
                entry["transaction"]["id"],
            )
        )
        owner = pending[0] if pending else None
        return {
            "active_transaction_id": active[0]["transaction"]["id"] if active else None,
            "unresolved_ambiguous_ids": [
                entry["transaction"]["id"] for entry in unresolved
            ],
            "pending_owner_id": owner["transaction"]["id"] if owner else None,
            "pending_count": len(pending),
        }

    def abandon_ambiguous(
        self,
        request_id: str,
        attempt: int,
        *,
        updated_at: str,
    ) -> dict[str, Any]:
        """Terminally abandon an unresolved ambiguous child without authorizing replacement.

        The original spawn transaction remains ``ambiguous`` forever. Only the child
        lifecycle moves from ``registration_pending`` to ``abandoned`` so unrelated
        workflows can acquire the globally serialized browser after an operator has
        determined that the original child identity can no longer be recovered.
        """
        del updated_at  # Lifecycle storage owns its canonical transition timestamp.
        with self._global_lock():
            transaction = self.load_attempt(request_id, attempt)
            if transaction["state"] != "ambiguous":
                raise ValueError("ambiguous abandonment requires an ambiguous spawn transaction")
            if self.store.load_registration(request_id) is not None:
                raise ValueError("registered ambiguous child must be reconciled, not abandoned")
            lifecycle = self.store.load_state(request_id)
            if lifecycle["state"] == "abandoned":
                return lifecycle
            if lifecycle["state"] != "registration_pending":
                raise ValueError(
                    "ambiguous abandonment requires registration_pending child state"
                )
            abandoned = self.store.transition_state(request_id, "abandoned")
            if transaction["id"] in self._queue_snapshot_locked()["unresolved_ambiguous_ids"]:
                raise RuntimeError("abandoned ambiguous spawn still blocks the global queue")
            return abandoned

    def begin_browser_attempt(
        self,
        request_id: str,
        attempt: int,
        *,
        tab_id: int,
        updated_at: str,
    ) -> dict[str, Any]:
        with self._global_lock():
            transaction = self.load_attempt(request_id, attempt)
            if transaction["state"] == "tab_created":
                if transaction["tab_id"] == tab_id:
                    return transaction
                raise ValueError(
                    "spawn already entered tab_created; reconcile the original tab "
                    "instead of creating a replacement"
                )
            if transaction["state"] != "pending":
                raise ValueError("only a pending spawn can acquire browser ownership")

            snapshot = self._queue_snapshot_locked()
            if snapshot["unresolved_ambiguous_ids"]:
                raise ValueError(
                    "unresolved ambiguous spawn blocks new browser spawn ownership"
                )
            if snapshot["active_transaction_id"] is not None:
                raise ValueError("another browser spawn transaction is already active")
            if snapshot["pending_owner_id"] != transaction["id"]:
                raise ValueError("spawn transaction is queued behind an earlier request")

            request = self.store.load_request(request_id)
            updated = spawn.transition_spawn_transaction(
                transaction,
                request=request,
                target="tab_created",
                tab_id=tab_id,
                updated_at=updated_at,
            )
            self._write_transaction(request_id, attempt, updated)
            return self.load_attempt(request_id, attempt)

    def advance(
        self,
        request_id: str,
        attempt: int,
        *,
        target: str,
        updated_at: str,
        child_conversation_url: str | None = None,
    ) -> dict[str, Any]:
        if target == "tab_created":
            raise ValueError("tab_created must be entered through begin_browser_attempt")
        with self._global_lock():
            request = self.store.load_request(request_id)
            transaction = self.load_attempt(request_id, attempt)
            if transaction["state"] == target:
                if target == "identity_discovered" and child_conversation_url is not None:
                    canonical = contract.canonical_conversation_url(child_conversation_url)
                    if canonical != transaction.get("child_conversation_url"):
                        raise ValueError(
                            "identity_discovered retry conflicts with durable child URL"
                        )
                elif child_conversation_url is not None:
                    raise ValueError(
                        "child_conversation_url is only valid for identity_discovered"
                    )
                if target == "done":
                    self._require_matching_registration(transaction)
                return transaction

            if target == "done":
                self._require_matching_registration(transaction)
            updated = spawn.transition_spawn_transaction(
                transaction,
                request=request,
                target=target,
                updated_at=updated_at,
                child_conversation_url=child_conversation_url,
            )
            self._write_transaction(request_id, attempt, updated)
            return self.load_attempt(request_id, attempt)

    def fail(
        self,
        request_id: str,
        attempt: int,
        *,
        reason: str,
        updated_at: str,
    ) -> dict[str, Any]:
        with self._global_lock():
            request = self.store.load_request(request_id)
            transaction = self.load_attempt(request_id, attempt)
            if transaction["state"] in {"failed", "ambiguous"}:
                if transaction["failure_reason"] == reason:
                    return transaction
                raise ValueError("terminal spawn failure retry conflicts with durable reason")
            updated = spawn.fail_spawn_transaction(
                transaction,
                request=request,
                reason=reason,
                updated_at=updated_at,
            )
            self._write_transaction(request_id, attempt, updated)
            return self.load_attempt(request_id, attempt)

    def cancel(
        self,
        request_id: str,
        attempt: int,
        *,
        reason: str,
        updated_at: str,
    ) -> dict[str, Any]:
        with self._global_lock():
            request = self.store.load_request(request_id)
            transaction = self.load_attempt(request_id, attempt)
            if transaction["state"] in {"cancelled", "ambiguous"}:
                if transaction["failure_reason"] == reason:
                    return transaction
                raise ValueError("terminal spawn cancellation retry conflicts with durable reason")
            updated = spawn.cancel_spawn_transaction(
                transaction,
                request=request,
                reason=reason,
                updated_at=updated_at,
            )
            self._write_transaction(request_id, attempt, updated)
            return self.load_attempt(request_id, attempt)

    def clear_tab_cache(
        self,
        request_id: str,
        attempt: int,
        *,
        updated_at: str,
    ) -> dict[str, Any]:
        with self._global_lock():
            request = self.store.load_request(request_id)
            transaction = self.load_attempt(request_id, attempt)
            updated = spawn.clear_spawn_tab_cache(
                transaction,
                request=request,
                updated_at=updated_at,
            )
            self._write_transaction(request_id, attempt, updated)
            return self.load_attempt(request_id, attempt)

    def reconcile_registration(
        self,
        request_id: str,
        attempt: int,
        *,
        updated_at: str,
    ) -> dict[str, Any]:
        """Reconcile a durable registration after restart without spawning again."""
        with self._global_lock():
            request = self.store.load_request(request_id)
            transaction = self.load_attempt(request_id, attempt)
            if self._matching_registration(self.store, transaction) is None:
                return transaction

            current = transaction["state"]
            if current in {"done", "ambiguous"}:
                return transaction
            if current == "registration_submitting":
                updated = spawn.transition_spawn_transaction(
                    transaction,
                    request=request,
                    target="done",
                    updated_at=updated_at,
                )
            elif current == "identity_discovered":
                updated = spawn.transition_spawn_transaction(
                    transaction,
                    request=request,
                    target="registration_submitting",
                    updated_at=updated_at,
                )
                updated = spawn.transition_spawn_transaction(
                    updated,
                    request=request,
                    target="done",
                    updated_at=updated_at,
                )
            elif current == "bootstrap_submitting":
                updated = spawn.transition_spawn_transaction(
                    transaction,
                    request=request,
                    target="ambiguous",
                    updated_at=updated_at,
                    reason=(
                        "durable registration resolved externally after bootstrap "
                        "submission outcome became ambiguous"
                    ),
                )
            else:
                raise ValueError(
                    "durable registration conflicts with pre-submission spawn state "
                    f"{current!r}"
                )
            self._write_transaction(request_id, attempt, updated)
            return self.load_attempt(request_id, attempt)

    def _matching_registration(
        self,
        conversation_store: WorkflowConversationStore,
        transaction: dict[str, Any],
    ) -> dict[str, Any] | None:
        registration = conversation_store.load_registration(
            transaction["child_request_id"]
        )
        if registration is None:
            return None
        discovered = transaction.get("child_conversation_url")
        if discovered is not None and registration["child_conversation_url"] != discovered:
            raise ValueError(
                "durable registration conflicts with spawn-discovered child URL"
            )
        return registration

    def _require_matching_registration(
        self,
        transaction: dict[str, Any],
    ) -> dict[str, Any]:
        registration = self._matching_registration(self.store, transaction)
        if registration is None:
            raise ValueError("done spawn requires a durable child registration")
        return registration

    def _write_transaction(
        self,
        request_id: str,
        attempt: int,
        transaction: dict[str, Any],
    ) -> None:
        request = self.store.load_request(request_id)
        spawn.validate_spawn_transaction(transaction, request=request)
        text = _json_text(transaction)
        if len(text.encode("utf-8")) > spawn.MAX_SPAWN_TRANSACTION_BYTES:
            raise ValueError(
                f"spawn transaction exceeds {spawn.MAX_SPAWN_TRANSACTION_BYTES} bytes"
            )
        atomic_write_text(self._attempt_path(request_id, attempt), text)

    def _scan_all_transactions(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for workflow_id in self.workflow_store.workflow_ids():
            conversation_store = WorkflowConversationStore(
                self.workflow_store,
                workflow_id,
            )
            spawn_root = conversation_store.root / "spawns"
            if not spawn_root.exists():
                continue
            if spawn_root.is_symlink() or not spawn_root.is_dir():
                raise ValueError(f"workflow spawn root is invalid: {workflow_id!r}")
            bound = WorkflowConversationSpawnStore(conversation_store)
            for request_dir in sorted(spawn_root.iterdir(), key=lambda item: item.name):
                if request_dir.is_symlink() or not request_dir.is_dir():
                    raise ValueError(
                        f"spawn request entry must be a directory: {request_dir.name!r}"
                    )
                request = conversation_store.load_request(request_dir.name)
                for transaction in bound.load_attempts(str(request["id"])):
                    entries.append(
                        {
                            "store": conversation_store,
                            "workflow_id": workflow_id,
                            "transaction": transaction,
                        }
                    )
        return entries
