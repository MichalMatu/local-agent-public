"""Opt-in, installation-bound worker using the local intake authorization choice.

Preserve exact-digest grants where configured and durable at-most-once effects.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

import local_agent.daemon.service as agentd
import local_agent.operator.local as operator_local
from local_agent.foundation.process import ExecutionLeaseBusy, fsync_directory
from local_agent.repository.host_facts import load_local_host_identity
from local_agent.repository.host_mailbox_reader import _git
from local_agent.repository.host_routing import HostIdentity, reject_host_namespace_symlinks
from local_agent.repository.path_safety import has_untrusted_symlink
from local_agent.runtime.task_contract import task_digest
from local_agent.supervisor import control as supervisor_control
from local_agent.supervisor import host_tasks
from local_agent.supervisor.host_control import (
    HostControlSource, host_supervisor_lease, load_host_control_source,
    require_host_publication_identity, validate_host_control_checkout,
)
from local_agent.supervisor.host_remote_intake import (
    HostControlSnapshotChanged, preview_authorized_remote_host_task,
    refresh_host_control_checkout,
)
from local_agent.supervisor.resources import MachineResourceBusy
from local_agent.supervisor.macos_release_admission import (
    MacReleaseAdmissionBlocked, macos_task_admission,
)

MAX_INTAKE_CONFIG_BYTES = 4096
HOST_REMOTE_RESULT_DIR = "task-results"
HOST_WORKER_PUBLISHED = 10
HOST_WORKER_RESOURCE_DEFERRED = 11
HOST_WORKER_CONTROL_DEFERRED = 15


@dataclass(frozen=True, slots=True)
class RemoteIntakeConfig:
    identity_file: Path
    authorization: str = "operator_grants"


def load_remote_intake_config(state_dir: Path, source: HostControlSource) -> RemoteIntakeConfig | None:
    """One local host/inbox choice. GitHub owns task-writer access when selected."""
    path = Path(state_dir) / "host-task-intake.json"
    if not path.is_absolute() or has_untrusted_symlink(path):
        raise ValueError("host task intake configuration path is unsafe")
    try:
        metadata = path.stat()
    except FileNotFoundError:
        # A fresh installation can use one local supervisor-control.json
        # for both routing and the owner's explicit host-intake choice.
        # Legacy standalone intake configuration takes precedence if present.
        if not source.host_intake_enabled:
            return None
        if source.identity_file is None:
            raise ValueError("integrated host intake has no local identity file")
        identity = load_local_host_identity(source.identity_file)
        if (identity.host_id, identity.installation_id) != (source.host_id, source.installation_id):
            raise ValueError("integrated host intake identity changed")
        return RemoteIntakeConfig(
            identity_file=source.identity_file,
            authorization=source.host_intake_authorization,
        )
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
        raise ValueError("host task intake configuration must be private")
    if os.name == "posix" and metadata.st_uid != os.getuid():
        raise ValueError("host task intake configuration owner is not the current OS user")
    raw = path.read_bytes()
    if not 0 < len(raw) <= MAX_INTAKE_CONFIG_BYTES:
        raise ValueError("host task intake config exceeds byte bound")

    def strict_pairs(items):
        result = {}
        for k, value in items:
            if k in result:
                raise ValueError("duplicate host task intake field")
            result[k] = value
        return result

    payload = json.loads(raw.decode("utf-8"), object_pairs_hook=strict_pairs)
    if (
        not isinstance(payload, dict) or
        not {"version", "enabled", "host_id", "installation_id", "identity_file"} <= set(payload)
        or set(payload) - {"version", "enabled", "host_id", "installation_id", "identity_file", "authorization"}
        or type(payload["version"]) is not int or payload["version"] != 1
        or type(payload["enabled"]) is not bool
    ):
        raise ValueError("host task intake config shape is invalid")
    if not payload["enabled"]:
        return None
    authorization = payload.get("authorization", "operator_grants")
    if authorization not in ("operator_grants", "github_writers") or type(authorization) is not str:
        raise ValueError("unsupported host task authorization model")
    if payload["host_id"] != source.host_id or payload["installation_id"] != source.installation_id:
        raise ValueError("host task intake is configured for a different installation")
    name = payload["identity_file"]
    if not isinstance(name, str) or not Path(name).is_absolute():
        raise ValueError("host task intake identity file must be absolute")
    identity = load_local_host_identity(Path(name))
    if (identity.host_id, identity.installation_id) != (source.host_id, source.installation_id):
        raise ValueError("host task intake identity mismatch")
    return RemoteIntakeConfig(identity_file=Path(name), authorization=authorization)


def host_intake_polling_enabled(state_dir: Path, source: HostControlSource | None) -> bool:
    """Use the same local intake choice for Git refresh and task admission.

    Invalid configs retain conservative control polling, while the normal
    host intake path independently logs and refuses malformed requests.
    """
    if source is None:
        return False
    try:
        return load_remote_intake_config(state_dir, source) is not None
    except (OSError, ValueError):
        return False


def _published_host_result_names(source: HostControlSource) -> frozenset[str]:
    """Only the observed remote ref proves publication; local commits do not."""
    prefix = f"{source.namespace}/{HOST_REMOTE_RESULT_DIR}/"
    paths = _git(
        source.checkout, "ls-tree", "-r", "--name-only",
        "refs/remotes/origin/agent-control", "--", prefix,
        limit=host_tasks.MAX_LOCAL_REQUEST_BYTES,
    )
    return frozenset(path[len(prefix):] for path in paths.splitlines()
                     if path.startswith(prefix))


def _known_foreign_host_task(path: Path, source: HostControlSource) -> bool:
    """Ignore only unambiguously foreign, well-formed target identities.

    This discovery filter has no admission authority. Ambiguous, malformed,
    duplicate-key or oversized inputs still reach the normal fail-closed
    worker validation. An exact-target task always gets revalidated by the
    worker against a fresh authenticated Git snapshot before any claim.
    """
    try:
        if not 0 < path.stat().st_size <= host_tasks.MAX_LOCAL_REQUEST_BYTES:
            return False

        def strict_object(items):
            obj = {}
            for key, value in items:
                if key in obj:
                    raise ValueError("duplicate host task field")
                obj[key] = value
            return obj

        with path.open("rb") as handle:
            payload = handle.read(host_tasks.MAX_LOCAL_REQUEST_BYTES + 1)
        if len(payload) > host_tasks.MAX_LOCAL_REQUEST_BYTES:
            return False
        task = json.loads(payload, object_pairs_hook=strict_object)
        if not isinstance(task, dict):
            return False
        target = HostIdentity(task.get("target_host_id"), task.get("target_installation_id"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return False
    return (
        target.host_id != source.host_id
        or target.installation_id != source.installation_id
    )


def pending_host_task_ids(source: HostControlSource, state_dir: Path, *,
                          limit: int = 32) -> tuple[str, ...]:
    """Find candidates ONLY; each worker performs fresh remote/grant checks."""
    reject_host_namespace_symlinks(
        source.checkout, f"{source.namespace}/tasks/placeholder",
    )
    reject_host_namespace_symlinks(
        source.checkout, f"{source.namespace}/{HOST_REMOTE_RESULT_DIR}/placeholder",
    )
    folder = source.checkout / source.namespace / "tasks"
    base = Path(state_dir) / "host-tasks"
    results, claims, approvals = (base / name for name in (
        "results", "claims", "remote-approvals",
    ))
    remote_results = source.checkout / source.namespace / HOST_REMOTE_RESULT_DIR
    published = _published_host_result_names(source)
    selected: list[str] = []

    def unpublished(name: str) -> bool:
        target = remote_results / name
        if target.is_symlink():
            raise ValueError("host task result path is a symlink")
        return name not in published

    # Durably completed results take priority over new tasks. Publication
    # still works if the producer has removed the entire Git inbox.
    if results.is_symlink() or approvals.is_symlink() or claims.is_symlink():
        raise ValueError("private host task ledger directory is a symlink")
    if results.is_dir():
        for item in sorted(results.glob("*.json")):
            if (host_tasks._TASK_ID_RE.fullmatch(item.stem)
                    and (approvals / item.name).is_file()
                    and unpublished(item.name)):
                selected.append(item.stem)
                if len(selected) >= limit:
                    return tuple(selected)

    # Claims left by a crashed worker are recoverable as UNKNOWN, even
    # when their original Git task is gone. Recovery holds the runner lock.
    if claims.is_dir():
        for item in sorted(claims.glob("*.json")):
            if (host_tasks._TASK_ID_RE.fullmatch(item.stem)
                    and (approvals / item.name).is_file()
                    and not (results / item.name).exists()
                    and unpublished(item.name)
                    and item.stem not in selected):
                selected.append(item.stem)
                if len(selected) >= limit:
                    return tuple(selected)

    if not folder.exists():
        return tuple(selected)
    if not folder.is_dir():
        raise ValueError("host command inbox is not a directory")
    for item in sorted(folder.iterdir()):
        if item.is_symlink() or not item.is_file() or item.suffix != ".json":
            continue
        task_id = item.stem
        if (host_tasks._TASK_ID_RE.fullmatch(task_id)
                and unpublished(item.name) and task_id not in selected
                and not _known_foreign_host_task(item, source)):
            selected.append(task_id)
            if len(selected) >= limit:
                break
    return tuple(selected)


def _stage_private_remote_task(preview, state_dir: Path) -> None:
    base = Path(state_dir) / "host-tasks"
    host_tasks._require_private_dir(base, create=True)
    requests = base / "requests"
    host_tasks._require_private_dir(requests, create=True)
    path = requests / f"{preview.task_id}.json"
    if path.exists():
        current = host_tasks._load_private_request(path)
        if task_digest(current) != preview.task_digest:
            raise ValueError("private host task differs from immutable approved Git task")
        return
    data = json.dumps(preview.task, sort_keys=True, ensure_ascii=False) + "\n"
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    fsync_directory(requests)



def _approved_remote_receipt(state_dir: Path, task_id: str) -> dict:
    """Require a durable local proof that *this* result belonged to remote intake."""
    path = Path(state_dir) / "host-tasks" / "remote-approvals" / f"{task_id}.json"
    record = host_tasks._load_private_request(path)
    if (set(record) != {
        "id", "host_id", "installation_id", "task_digest", "control_sha", "operator_sha",
    } or record["id"] != task_id):
        raise ValueError("remote host task receipt is missing or invalid")
    return record


def _record_approved_remote_task(preview, source: HostControlSource, state_dir: Path) -> dict:
    base = Path(state_dir) / "host-tasks"
    folder = base / "remote-approvals"
    host_tasks._require_private_dir(base, create=True)
    host_tasks._require_private_dir(folder, create=True)
    path = folder / f"{preview.task_id}.json"
    proposed = {
        "id": preview.task_id,
        "host_id": source.host_id, "installation_id": source.installation_id,
        "task_digest": preview.task_digest, "control_sha": preview.control_sha,
        "operator_sha": preview.operator_sha,
    }
    if not host_tasks._durable_exclusive_claim(path, proposed):
        original = _approved_remote_receipt(state_dir, preview.task_id)
        if any(original[key] != proposed[key] for key in (
            "id", "host_id", "installation_id", "task_digest",
        )):
            raise ValueError("remote task id has previously approved different bytes")
        # Fresh preview revalidated authority and bytes. Keep the first
        # provenance receipt when unrelated Git commits advanced the snapshot.
        return original
    return proposed


def run_remote_host_task_once(
    task_id: str, *, state_dir: Path, operator_checkout: Path,
    production_checkout: Path,
) -> str:
    """Protect staging, claiming and executing against the optional Mac freeze."""
    with macos_task_admission(state_dir):
        return _run_remote_host_task_admitted(
            task_id, state_dir=state_dir, operator_checkout=operator_checkout,
            production_checkout=production_checkout,
        )


def _run_remote_host_task_admitted(
    task_id: str, *, state_dir: Path, operator_checkout: Path,
    production_checkout: Path,
) -> str:
    """Run at most one verified host task; retry network result publication.

    Returns processed, deferred or published; never silently retries a
    claimed command after a crash. Does not schedule more work by itself.
    """
    source = load_host_control_source(Path(state_dir) / "supervisor-control.json")
    if source is None:
        raise ValueError("host control is not enabled")
    config = load_remote_intake_config(state_dir, source)
    if config is None:
        raise ValueError("remote host task intake is not locally enabled")
    if operator_local.is_disabled():
        raise ValueError("operator emergency disable is active")
    if host_tasks._TASK_ID_RE.fullmatch(task_id) is None:
        raise ValueError("invalid remote host task id")
    # Fail before admitting any command when the destination can redirect a
    # result into a different namespace. Recheck after execution for races.
    result_relative = f"{source.namespace}/{HOST_REMOTE_RESULT_DIR}/{task_id}.json"
    reject_host_namespace_symlinks(source.checkout, result_relative)
    if (source.checkout / result_relative).is_symlink():
        raise ValueError("host task result destination is a symlink")
    # The result publisher must be able to commit BEFORE effects are admitted.
    # Without a checkout-local author, an OS command can succeed but its
    # terminal result cannot reach the producer (observed on real Pi).
    require_host_publication_identity(source)
    result_path = Path(state_dir) / "host-tasks" / "results" / f"{task_id}.json"
    claim_path = Path(state_dir) / "host-tasks" / "claims" / f"{task_id}.json"
    if not result_path.exists() and claim_path.exists():
        # Only a truly interrupted claim may be recovered. A live executor
        # owns the exclusive ledger lock and cannot be misclassified UNKNOWN.
        host_tasks.recover_interrupted_local_tasks(state_dir)
    # The local durable result takes precedence over a new/modified/revoked
    # Git task so it can be published after a network or process failure.
    if result_path.exists():
        receipt = _approved_remote_receipt(state_dir, task_id)
        result = host_tasks._load_private_request(result_path)
    else:
        with host_supervisor_lease(source):
            refresh_host_control_checkout(source, production_checkout=production_checkout)
            preview = preview_authorized_remote_host_task(
                source, task_id=task_id, identity_file=config.identity_file,
                production_checkout=production_checkout,
                operator_checkout=operator_checkout,
                authorization=config.authorization,
            )
        receipt = _record_approved_remote_task(preview, source, state_dir)
        _stage_private_remote_task(preview, state_dir)
        host_tasks.recover_interrupted_local_tasks(state_dir)
        try:
            if preview.validation_error is not None:
                result = host_tasks.record_rejected_host_task(
                    preview.task, state_dir=state_dir,
                    identity=load_local_host_identity(config.identity_file),
                    error=preview.validation_error,
                )
            else:
                result = host_tasks.run_local_host_task(
                    task_id, state_dir=state_dir, identity_file=config.identity_file,
                )
        except MachineResourceBusy:
            return "deferred"
        if result is None:
            result = host_tasks._load_private_request(result_path)
        if result.get("task_digest") != preview.task_digest:
            raise ValueError("host result digest differs from approved immutable request")

    # Publish the canonical PRIVATE DURABLE record on the first attempt too.
    # A freshly returned executor dictionary has a different insertion order
    # than the sorted JSON loaded during recovery; serializing those directly
    # would make identical results produce another Git commit after lost ACK.
    # The on-disk record is already fsynced before entering publication.
    result = host_tasks._load_private_request(result_path)

    if (receipt["host_id"] != source.host_id
            or receipt["installation_id"] != source.installation_id
            or result.get("task_digest") != receipt["task_digest"]):
        raise ValueError("host task result has no matching remote authorization receipt")
    if (result.get("target_host_id") not in (None, source.host_id)
            or result.get("target_installation_id") not in (None, source.installation_id)):
        raise ValueError("host result belongs to another installation")
    if result.get("id") != task_id:
        raise ValueError("host result task ID mismatch")
    # A separate process owns this Git checkout for the publication turn.
    # The service control namespace remains independent of host commands.
    with host_supervisor_lease(source):
        validate_host_control_checkout(
            source, production_checkout=production_checkout,
        )
        reject_host_namespace_symlinks(
            source.checkout,
            f"{source.namespace}/{HOST_REMOTE_RESULT_DIR}/{task_id}.json",
        )
        target = source.checkout / source.namespace / HOST_REMOTE_RESULT_DIR / f"{task_id}.json"
        if target.is_symlink():
            raise ValueError("host task result destination is a symlink")
        supervisor_control.bind_supervisor_control(
            source, daemon_version=agentd.DAEMON_VERSION,
        )
        agentd.publish_control_json(
            f"{source.namespace}/{HOST_REMOTE_RESULT_DIR}/{task_id}.json",
            {
                "version": 1, "id": task_id, "status": result["status"],
                "target_host_id": source.host_id,
                "target_installation_id": source.installation_id,
                "task_digest": result["task_digest"],
                "result": result,
            },
            commit_message=f"Host task result: {task_id}",
            ensure_remote=True,
        )
    return "published"


def worker_command(task_id: str, *, state_dir: Path, production_checkout: Path) -> list[str]:
    """Use the same isolated host worker entrypoint for serial and parallel."""
    import sys

    if host_tasks._TASK_ID_RE.fullmatch(task_id) is None:
        raise ValueError("invalid host worker task ID")
    return [
        sys.executable, "-m", "local_agent.supervisor.host_remote_worker",
        "--id", task_id, "--state-dir", str(state_dir),
        "--operator-checkout", str(production_checkout),
        "--production-checkout", str(production_checkout), "--execute",
    ]

def main() -> int:
    parser = argparse.ArgumentParser(description="Operator-granted, opt-in host task worker")
    parser.add_argument("--id", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--operator-checkout", type=Path, required=True)
    parser.add_argument("--production-checkout", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        parser.error("remote host task worker must be explicitly invoked")
    try:
        status = run_remote_host_task_once(
            args.id, state_dir=args.state_dir,
            operator_checkout=args.operator_checkout,
            production_checkout=args.production_checkout,
        )
    except (ExecutionLeaseBusy, HostControlSnapshotChanged) as exc:
        print(f"host intake deferred: {type(exc).__name__}: {exc}", flush=True)
        return HOST_WORKER_CONTROL_DEFERRED
    except MacReleaseAdmissionBlocked as exc:
        print(f"host intake deferred by Mac release fence: {exc}", flush=True)
        return HOST_WORKER_RESOURCE_DEFERRED
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"host remote task intake failed: {type(exc).__name__}: {exc}", flush=True)
        return 2
    print(f"host remote task: {status}", flush=True)
    return HOST_WORKER_RESOURCE_DEFERRED if status == "deferred" else HOST_WORKER_PUBLISHED


if __name__ == "__main__":
    raise SystemExit(main())
