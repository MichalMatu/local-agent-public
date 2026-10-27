from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from local_agent.foundation.process import fsync_directory
from local_agent.workflow import revisions
from local_agent.workflow.store import WorkflowStore

REVISION_FILENAME_WIDTH = 6


def _json_text(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


class WorkflowRevisionStore:
    """Durable append-only continuation records layered over WorkflowStore.

    This store does not dispatch nodes and does not mutate the immutable base manifest.
    It only persists and validates the revision lineage.
    """

    def __init__(self, workflow_store: WorkflowStore) -> None:
        self.workflow_store = workflow_store

    def _root(self, workflow_id: str) -> Path:
        # load_manifest performs canonical workflow-id validation and proves the base
        # workflow exists before a revisions path is derived.
        self.workflow_store.load_manifest(workflow_id)
        return self.workflow_store.root / workflow_id / "revisions"

    def _path(self, workflow_id: str, revision: int) -> Path:
        if type(revision) is not int or revision < 1:
            raise ValueError("revision number must be a positive integer")
        return self._root(workflow_id) / f"{revision:0{REVISION_FILENAME_WIDTH}d}.json"

    def load(self, workflow_id: str) -> list[dict[str, Any]]:
        base = self.workflow_store.load_manifest(workflow_id)
        root = self._root(workflow_id)
        if not root.exists():
            return []

        records: list[dict[str, Any]] = []
        paths = sorted(root.glob("*.json"))
        for expected_revision, path in enumerate(paths, start=1):
            expected_name = f"{expected_revision:0{REVISION_FILENAME_WIDTH}d}.json"
            if path.name != expected_name:
                raise ValueError(
                    f"workflow revision files are not contiguous: expected {expected_name!r}, "
                    f"got {path.name!r}"
                )
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"workflow revision path must be a regular file: {path.name}")
            raw = path.read_bytes()
            if len(raw) > revisions.MAX_REVISION_FILE_BYTES:
                raise ValueError(f"workflow revision exceeds bounds: {path.name}")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid workflow revision file: {path.name}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"workflow revision file must contain an object: {path.name}")
            records.append(payload)

        revisions.validate_revision_sequence(base, records)
        return records

    def append(self, workflow_id: str, record: dict[str, Any]) -> dict[str, Any]:
        """Persist exactly one next revision using create-only semantics.

        Repeating the identical append is idempotent. A different payload attempting to
        occupy an existing revision number fails closed.
        """
        if not isinstance(record, dict):
            raise ValueError("workflow revision must be an object")
        raw_revision = record.get("revision")
        if type(raw_revision) is not int or raw_revision < 1:
            raise ValueError("revision number must be a positive integer")
        revision_number = raw_revision

        with self.workflow_store.execution_lock(workflow_id):
            base = self.workflow_store.load_manifest(workflow_id)
            existing = self.load(workflow_id)

            if revision_number <= len(existing):
                current = existing[revision_number - 1]
                if revisions.revision_digest(current) != revisions.revision_digest(record):
                    raise ValueError(
                        f"workflow revision {revision_number} already exists with a different digest"
                    )
                return current

            expected_revision = len(existing) + 1
            if revision_number != expected_revision:
                raise ValueError(
                    f"workflow revision number must be contiguous; expected {expected_revision}"
                )

            candidate = [*existing, record]
            revisions.validate_revision_sequence(base, candidate)

            target = self._path(workflow_id, revision_number)
            root = target.parent
            root.mkdir(parents=True, exist_ok=True)
            fsync_directory(root.parent)

            encoded = _json_text(record).encode("utf-8")
            if len(encoded) > revisions.MAX_REVISION_FILE_BYTES:
                raise ValueError(
                    f"workflow revision exceeds {revisions.MAX_REVISION_FILE_BYTES} bytes"
                )
            try:
                descriptor = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                # Another writer won the create race. Re-read the authoritative chain
                # and accept only the exact same record.
                loaded = self.load(workflow_id)
                if revision_number > len(loaded):
                    raise ValueError(
                        f"workflow revision {revision_number} appeared concurrently but lineage is incomplete"
                    )
                current = loaded[revision_number - 1]
                if revisions.revision_digest(current) != revisions.revision_digest(record):
                    raise ValueError(
                        f"workflow revision {revision_number} concurrently exists with a different digest"
                    )
                return current

            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            fsync_directory(root)

            # Re-validate from disk so publication never returns a record that cannot
            # be reconstructed as the same contiguous lineage after restart.
            loaded = self.load(workflow_id)
            return loaded[revision_number - 1]

    def effective_manifest(self, workflow_id: str) -> dict[str, Any]:
        base = self.workflow_store.load_manifest(workflow_id)
        return revisions.effective_manifest(base, self.load(workflow_id))
