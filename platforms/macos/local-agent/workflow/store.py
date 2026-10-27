from __future__ import annotations

import contextlib
import fcntl
import json
import os
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from local_agent.foundation.process import atomic_write_text, fsync_directory
from local_agent.workflow import contract, state

WORKFLOW_STATE_SCHEMA_VERSION = 1
GATE_DECISION_SCHEMA_VERSION = 1
PLANNER_CHECKPOINT_SCHEMA_VERSION = 1
DEFAULT_STATE_DIR = Path.home() / "Library" / "Application Support" / "local-agent"
MAX_STATE_FILE_BYTES = 1024 * 1024
MAX_EVENT_LOG_BYTES = 4 * 1024 * 1024
MAX_GATE_RESOLVER_CHARS = 200
MAX_CHECKPOINT_RESOLVER_CHARS = 200
MAX_CHECKPOINT_NOTE_CHARS = 4096

_STATE_FIELDS = {
    "schema_version",
    "workflow_id",
    "manifest_digest",
    "workflow_state",
    "node_states",
    "gate_decisions",
    "planner_checkpoints",
    "cancel_requested",
    "created_at",
    "updated_at",
}

_GATE_DECISION_FIELDS = {
    "schema_version",
    "workflow_id",
    "manifest_digest",
    "node_id",
    "decision",
    "resolver",
    "decided_at",
}

_PLANNER_CHECKPOINT_FIELDS = {
    "schema_version",
    "workflow_id",
    "manifest_digest",
    "node_id",
    "resolver",
    "note",
    "resolved_at",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_text(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


class WorkflowStore:
    def __init__(self, state_dir: Path | None = None) -> None:
        self.state_dir = (state_dir or DEFAULT_STATE_DIR).expanduser().resolve()
        self.root = self.state_dir / "workflows"
        self.lock_root = self.state_dir / "locks" / "workflows"
        self.execution_lock_root = self.state_dir / "locks" / "workflow-execution"

    def _workflow_dir(self, workflow_id: str) -> Path:
        canonical = contract.validate_workflow_id(workflow_id)
        return self.root / canonical

    def _manifest_path(self, workflow_id: str) -> Path:
        return self._workflow_dir(workflow_id) / "manifest.json"

    def _state_path(self, workflow_id: str) -> Path:
        return self._workflow_dir(workflow_id) / "state.json"

    def _events_path(self, workflow_id: str) -> Path:
        return self._workflow_dir(workflow_id) / "events.ndjson"

    @contextlib.contextmanager
    def _mutation_lock(self, workflow_id: str) -> Iterator[None]:
        canonical = contract.validate_workflow_id(workflow_id)
        self.lock_root.mkdir(parents=True, exist_ok=True)
        path = self.lock_root / f"{canonical}.lock"
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextlib.contextmanager
    def execution_lock(self, workflow_id: str) -> Iterator[None]:
        """Serialize dispatch/reconciliation with operator mutations for one workflow."""
        canonical = contract.validate_workflow_id(workflow_id)
        self.execution_lock_root.mkdir(parents=True, exist_ok=True)
        path = self.execution_lock_root / f"{canonical}.lock"
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def workflow_ids(self) -> list[str]:
        if not self.root.exists():
            return []
        ids: list[str] = []
        for item in self.root.iterdir():
            if not item.is_dir():
                continue
            try:
                workflow_id = contract.validate_workflow_id(item.name)
            except ValueError:
                continue
            ids.append(workflow_id)
        return sorted(ids)

    def submit(self, manifest: dict[str, Any]) -> dict[str, Any]:
        contract.validate_workflow_manifest(manifest)
        workflow_id = str(manifest["id"])
        digest = contract.manifest_digest(manifest)
        with self._mutation_lock(workflow_id):
            workflow_dir = self._workflow_dir(workflow_id)
            if workflow_dir.exists():
                existing_manifest = self.load_manifest(workflow_id)
                existing_digest = contract.manifest_digest(existing_manifest)
                if existing_digest != digest:
                    raise ValueError(
                        f"workflow {workflow_id!r} already exists with a different manifest digest"
                    )
                return self.load_state(workflow_id)

            workflow_dir.mkdir(parents=True, exist_ok=False)
            fsync_directory(workflow_dir.parent)
            atomic_write_text(workflow_dir / "manifest.json", _json_text(manifest))

            node_states = state.initial_node_states(manifest)
            timestamp = now_iso()
            state_payload = {
                "schema_version": WORKFLOW_STATE_SCHEMA_VERSION,
                "workflow_id": workflow_id,
                "manifest_digest": digest,
                "workflow_state": state.workflow_state(node_states),
                "node_states": node_states,
                "gate_decisions": {},
                "planner_checkpoints": {},
                "cancel_requested": False,
                "created_at": manifest["created_at"],
                "updated_at": timestamp,
            }
            self._write_state(workflow_id, state_payload)
            self._record_event(
                workflow_id,
                {
                    "event": "submitted",
                    "workflow_id": workflow_id,
                    "manifest_digest": digest,
                    "workflow_state": state_payload["workflow_state"],
                    "at": timestamp,
                },
            )
            return state_payload

    def load_manifest(self, workflow_id: str) -> dict[str, Any]:
        path = self._manifest_path(workflow_id)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"workflow manifest is unavailable: {workflow_id!r}") from exc
        if len(raw) > contract.MAX_WORKFLOW_FILE_BYTES:
            raise ValueError(f"workflow manifest exceeds bounds: {workflow_id!r}")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid workflow manifest: {workflow_id!r}") from exc
        try:
            contract.validate_workflow_manifest(payload)
        except ValueError as exc:
            raise ValueError(f"invalid workflow manifest: {workflow_id!r}: {exc}") from exc
        if payload["id"] != workflow_id:
            raise ValueError(
                f"workflow manifest identity mismatch: expected {workflow_id!r}, got {payload['id']!r}"
            )
        return payload

    def load_state(self, workflow_id: str) -> dict[str, Any]:
        canonical_id = contract.validate_workflow_id(workflow_id)
        manifest = self.load_manifest(canonical_id)
        path = self._state_path(canonical_id)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"workflow state is unavailable: {canonical_id!r}") from exc
        if len(raw) > MAX_STATE_FILE_BYTES:
            raise ValueError(f"invalid workflow state: {canonical_id!r}: file exceeds bounds")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid workflow state: {canonical_id!r}") from exc
        try:
            self._validate_state_payload(manifest, payload)
        except ValueError as exc:
            raise ValueError(f"invalid workflow state: {canonical_id!r}: {exc}") from exc
        return payload

    def set_node_state(
        self,
        workflow_id: str,
        node_id: str,
        target: str,
    ) -> dict[str, Any]:
        with self._mutation_lock(workflow_id):
            manifest = self.load_manifest(workflow_id)
            current_state = self.load_state(workflow_id)
            node_states = dict(current_state["node_states"])
            if node_id not in node_states:
                raise ValueError(f"unknown workflow node id: {node_id!r}")

            before = dict(node_states)
            node_states[node_id] = state.transition_node_state(node_states[node_id], target)
            node_states = state.advance_dependency_states(manifest, node_states)
            if node_states == before:
                return current_state

            timestamp = now_iso()
            updated = dict(current_state)
            updated["node_states"] = node_states
            updated["workflow_state"] = state.workflow_state(node_states)
            updated["updated_at"] = timestamp
            self._write_state(workflow_id, updated)
            self._record_event(
                workflow_id,
                {
                    "event": "node_state_changed",
                    "workflow_id": workflow_id,
                    "node_id": node_id,
                    "from": before[node_id],
                    "to": node_states[node_id],
                    "workflow_state": updated["workflow_state"],
                    "at": timestamp,
                },
            )
            return updated

    def load_gate_decision(
        self,
        workflow_id: str,
        node_id: str,
    ) -> dict[str, Any] | None:
        current = self.load_state(workflow_id)
        decision = current["gate_decisions"].get(node_id)
        return dict(decision) if isinstance(decision, dict) else None

    def resolve_user_gate(
        self,
        workflow_id: str,
        node_id: str,
        decision: str,
        *,
        resolver: str,
    ) -> dict[str, Any]:
        if (
            not isinstance(resolver, str)
            or not resolver.strip()
            or len(resolver) > MAX_GATE_RESOLVER_CHARS
        ):
            raise ValueError("gate resolver must be a non-empty bounded string")

        with self.execution_lock(workflow_id):
            with self._mutation_lock(workflow_id):
                manifest = self.load_manifest(workflow_id)
                gate = self._user_gate_node(manifest, node_id)
                current = self.load_state(workflow_id)
                existing = current["gate_decisions"].get(node_id)
                if isinstance(existing, dict):
                    if existing["decision"] != decision:
                        raise ValueError(
                            f"user gate {node_id!r} is already resolved as "
                            f"{existing['decision']!r}"
                        )
                    return dict(existing)

                if current["node_states"][node_id] != "waiting_user":
                    raise ValueError(f"user gate {node_id!r} is not waiting for user")
                choices = gate["choices"]
                if decision not in choices:
                    raise ValueError(
                        f"decision {decision!r} is not an allowed choice for user gate {node_id!r}"
                    )

                timestamp = now_iso()
                record = {
                    "schema_version": GATE_DECISION_SCHEMA_VERSION,
                    "workflow_id": workflow_id,
                    "manifest_digest": current["manifest_digest"],
                    "node_id": node_id,
                    "decision": decision,
                    "resolver": resolver,
                    "decided_at": timestamp,
                }
                node_states = dict(current["node_states"])
                node_states[node_id] = state.transition_node_state(
                    node_states[node_id],
                    "succeeded",
                )
                node_states = state.advance_dependency_states(manifest, node_states)
                gate_decisions = dict(current["gate_decisions"])
                gate_decisions[node_id] = record

                updated = dict(current)
                updated["node_states"] = node_states
                updated["gate_decisions"] = gate_decisions
                updated["workflow_state"] = state.workflow_state(node_states)
                updated["updated_at"] = timestamp
                self._write_state(workflow_id, updated)
                self._record_event(
                    workflow_id,
                    {
                        "event": "user_gate_resolved",
                        "workflow_id": workflow_id,
                        "node_id": node_id,
                        "decision": decision,
                        "resolver": resolver,
                        "workflow_state": updated["workflow_state"],
                        "at": timestamp,
                    },
                )
                return record

    def load_planner_checkpoint_resolution(
        self,
        workflow_id: str,
        node_id: str,
    ) -> dict[str, Any] | None:
        current = self.load_state(workflow_id)
        resolution = current["planner_checkpoints"].get(node_id)
        return dict(resolution) if isinstance(resolution, dict) else None

    def resolve_planner_checkpoint(
        self,
        workflow_id: str,
        node_id: str,
        *,
        resolver: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        if (
            not isinstance(resolver, str)
            or not resolver.strip()
            or len(resolver) > MAX_CHECKPOINT_RESOLVER_CHARS
        ):
            raise ValueError("checkpoint resolver must be a non-empty bounded string")
        if note is not None and (
            not isinstance(note, str) or len(note) > MAX_CHECKPOINT_NOTE_CHARS
        ):
            raise ValueError("checkpoint note must be null or a bounded string")

        with self.execution_lock(workflow_id):
            with self._mutation_lock(workflow_id):
                manifest = self.load_manifest(workflow_id)
                self._planner_checkpoint_node(manifest, node_id)
                current = self.load_state(workflow_id)
                existing = current["planner_checkpoints"].get(node_id)
                if isinstance(existing, dict):
                    return dict(existing)
                if current["node_states"][node_id] != "waiting_planner":
                    raise ValueError(
                        f"planner checkpoint {node_id!r} is not waiting for planner"
                    )

                timestamp = now_iso()
                record = {
                    "schema_version": PLANNER_CHECKPOINT_SCHEMA_VERSION,
                    "workflow_id": workflow_id,
                    "manifest_digest": current["manifest_digest"],
                    "node_id": node_id,
                    "resolver": resolver,
                    "note": note,
                    "resolved_at": timestamp,
                }
                node_states = dict(current["node_states"])
                node_states[node_id] = state.transition_node_state(
                    node_states[node_id],
                    "succeeded",
                )
                node_states = state.advance_dependency_states(manifest, node_states)
                planner_checkpoints = dict(current["planner_checkpoints"])
                planner_checkpoints[node_id] = record

                updated = dict(current)
                updated["node_states"] = node_states
                updated["planner_checkpoints"] = planner_checkpoints
                updated["workflow_state"] = state.workflow_state(node_states)
                updated["updated_at"] = timestamp
                self._write_state(workflow_id, updated)
                self._record_event(
                    workflow_id,
                    {
                        "event": "planner_checkpoint_resolved",
                        "workflow_id": workflow_id,
                        "node_id": node_id,
                        "resolver": resolver,
                        "workflow_state": updated["workflow_state"],
                        "at": timestamp,
                    },
                )
                return record

    def cancel(self, workflow_id: str) -> dict[str, Any]:
        with self.execution_lock(workflow_id):
            with self._mutation_lock(workflow_id):
                current = self.load_state(workflow_id)
                if current["cancel_requested"]:
                    return current

                node_states = dict(current["node_states"])
                cancellable = {
                    "pending",
                    "blocked_dependency",
                    "ready",
                    "waiting_user",
                    "waiting_planner",
                }
                for node_id, node_state in list(node_states.items()):
                    if node_state in cancellable:
                        node_states[node_id] = state.transition_node_state(
                            node_state,
                            "cancelled",
                        )

                timestamp = now_iso()
                updated = dict(current)
                updated["cancel_requested"] = True
                updated["node_states"] = node_states
                updated["workflow_state"] = state.workflow_state(node_states)
                updated["updated_at"] = timestamp
                self._write_state(workflow_id, updated)
                self._record_event(
                    workflow_id,
                    {
                        "event": "cancel_requested",
                        "workflow_id": workflow_id,
                        "workflow_state": updated["workflow_state"],
                        "at": timestamp,
                    },
                )
                return updated

    def _user_gate_node(
        self,
        manifest: dict[str, Any],
        node_id: str,
    ) -> dict[str, Any]:
        for node in manifest["nodes"]:
            if node["id"] != node_id:
                continue
            if node["kind"] != "user_gate":
                raise ValueError(f"workflow node {node_id!r} is not a user gate")
            return node
        raise ValueError(f"unknown workflow node id: {node_id!r}")

    def _planner_checkpoint_node(
        self,
        manifest: dict[str, Any],
        node_id: str,
    ) -> dict[str, Any]:
        for node in manifest["nodes"]:
            if node["id"] != node_id:
                continue
            if node["kind"] != "planner_checkpoint":
                raise ValueError(
                    f"workflow node {node_id!r} is not a planner checkpoint"
                )
            return node
        raise ValueError(f"unknown workflow node id: {node_id!r}")

    def _validate_gate_decision(
        self,
        manifest: dict[str, Any],
        node_states: dict[str, str],
        node_id: str,
        payload: Any,
    ) -> None:
        if not isinstance(payload, dict) or set(payload) != _GATE_DECISION_FIELDS:
            raise ValueError(f"invalid gate decision record for {node_id!r}")
        if (
            type(payload.get("schema_version")) is not int
            or payload["schema_version"] != GATE_DECISION_SCHEMA_VERSION
        ):
            raise ValueError(f"invalid gate decision schema for {node_id!r}")
        if payload.get("workflow_id") != manifest["id"]:
            raise ValueError(f"gate decision workflow identity mismatch for {node_id!r}")
        if payload.get("manifest_digest") != contract.manifest_digest(manifest):
            raise ValueError(f"gate decision manifest digest mismatch for {node_id!r}")
        if payload.get("node_id") != node_id:
            raise ValueError(f"gate decision node identity mismatch for {node_id!r}")
        gate = self._user_gate_node(manifest, node_id)
        if payload.get("decision") not in gate["choices"]:
            raise ValueError(f"invalid gate decision choice for {node_id!r}")
        resolver = payload.get("resolver")
        if (
            not isinstance(resolver, str)
            or not resolver.strip()
            or len(resolver) > MAX_GATE_RESOLVER_CHARS
        ):
            raise ValueError(f"invalid gate decision resolver for {node_id!r}")
        if not isinstance(payload.get("decided_at"), str) or not payload["decided_at"]:
            raise ValueError(f"invalid gate decision timestamp for {node_id!r}")
        if node_states.get(node_id) != "succeeded":
            raise ValueError(
                f"gate decision for {node_id!r} requires the gate node to be succeeded"
            )

    def _validate_planner_checkpoint_resolution(
        self,
        manifest: dict[str, Any],
        node_states: dict[str, str],
        node_id: str,
        payload: Any,
    ) -> None:
        if (
            not isinstance(payload, dict)
            or set(payload) != _PLANNER_CHECKPOINT_FIELDS
        ):
            raise ValueError(f"invalid planner checkpoint record for {node_id!r}")
        if (
            type(payload.get("schema_version")) is not int
            or payload["schema_version"] != PLANNER_CHECKPOINT_SCHEMA_VERSION
        ):
            raise ValueError(f"invalid planner checkpoint schema for {node_id!r}")
        if payload.get("workflow_id") != manifest["id"]:
            raise ValueError(
                f"planner checkpoint workflow identity mismatch for {node_id!r}"
            )
        if payload.get("manifest_digest") != contract.manifest_digest(manifest):
            raise ValueError(
                f"planner checkpoint manifest digest mismatch for {node_id!r}"
            )
        if payload.get("node_id") != node_id:
            raise ValueError(f"planner checkpoint node identity mismatch for {node_id!r}")
        self._planner_checkpoint_node(manifest, node_id)
        resolver = payload.get("resolver")
        if (
            not isinstance(resolver, str)
            or not resolver.strip()
            or len(resolver) > MAX_CHECKPOINT_RESOLVER_CHARS
        ):
            raise ValueError(f"invalid planner checkpoint resolver for {node_id!r}")
        note = payload.get("note")
        if note is not None and (
            not isinstance(note, str) or len(note) > MAX_CHECKPOINT_NOTE_CHARS
        ):
            raise ValueError(f"invalid planner checkpoint note for {node_id!r}")
        if not isinstance(payload.get("resolved_at"), str) or not payload["resolved_at"]:
            raise ValueError(f"invalid planner checkpoint timestamp for {node_id!r}")
        if node_states.get(node_id) != "succeeded":
            raise ValueError(
                f"planner checkpoint record for {node_id!r} requires the node to be succeeded"
            )

    def _validate_state_payload(
        self,
        manifest: dict[str, Any],
        payload: Any,
    ) -> None:
        if not isinstance(payload, dict):
            raise ValueError("state must be an object")
        if set(payload) != _STATE_FIELDS:
            raise ValueError("state fields do not match schema")
        if (
            type(payload.get("schema_version")) is not int
            or payload["schema_version"] != WORKFLOW_STATE_SCHEMA_VERSION
        ):
            raise ValueError(
                f"state schema_version must be {WORKFLOW_STATE_SCHEMA_VERSION}"
            )
        workflow_id = contract.validate_workflow_id(payload.get("workflow_id"))
        if workflow_id != manifest["id"]:
            raise ValueError("state workflow_id does not match manifest")
        digest = contract.manifest_digest(manifest)
        if payload.get("manifest_digest") != digest:
            raise ValueError("state manifest_digest does not match manifest")
        if not isinstance(payload.get("cancel_requested"), bool):
            raise ValueError("cancel_requested must be a boolean")
        for field in ("created_at", "updated_at"):
            if not isinstance(payload.get(field), str) or not payload[field]:
                raise ValueError(f"{field} must be a non-empty timestamp string")

        node_states = payload.get("node_states")
        if not isinstance(node_states, dict):
            raise ValueError("node_states must be an object")
        normalized = state.advance_dependency_states(manifest, node_states)
        if normalized != node_states:
            raise ValueError("node_states are not dependency-normalized")

        gate_decisions = payload.get("gate_decisions")
        if not isinstance(gate_decisions, dict):
            raise ValueError("gate_decisions must be an object")
        for node_id, decision in gate_decisions.items():
            if not isinstance(node_id, str):
                raise ValueError("gate_decisions keys must be strings")
            self._validate_gate_decision(
                manifest,
                node_states,
                node_id,
                decision,
            )

        planner_checkpoints = payload.get("planner_checkpoints")
        if not isinstance(planner_checkpoints, dict):
            raise ValueError("planner_checkpoints must be an object")
        for node_id, resolution in planner_checkpoints.items():
            if not isinstance(node_id, str):
                raise ValueError("planner_checkpoints keys must be strings")
            self._validate_planner_checkpoint_resolution(
                manifest,
                node_states,
                node_id,
                resolution,
            )

        computed = state.workflow_state(node_states)
        if payload.get("workflow_state") != computed:
            raise ValueError(
                f"workflow_state mismatch: expected {computed!r}, got {payload.get('workflow_state')!r}"
            )

    def _write_state(self, workflow_id: str, payload: dict[str, Any]) -> None:
        manifest = self.load_manifest(workflow_id)
        self._validate_state_payload(manifest, payload)
        text = _json_text(payload)
        if len(text.encode("utf-8")) > MAX_STATE_FILE_BYTES:
            raise ValueError(f"workflow state exceeds {MAX_STATE_FILE_BYTES} bytes")
        atomic_write_text(self._state_path(workflow_id), text)

    def _record_event(self, workflow_id: str, event: dict[str, Any]) -> bool:
        """Best-effort audit only; authoritative workflow state lives in state.json."""
        try:
            return self._append_event(workflow_id, event)
        except OSError:
            return False

    def _append_event(self, workflow_id: str, event: dict[str, Any]) -> bool:
        path = self._events_path(workflow_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            event,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"
        encoded = line.encode("utf-8")
        try:
            existing_size = path.stat().st_size
        except FileNotFoundError:
            existing_size = 0
        if existing_size + len(encoded) > MAX_EVENT_LOG_BYTES:
            return False
        with path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        fsync_directory(path.parent)
        return True
