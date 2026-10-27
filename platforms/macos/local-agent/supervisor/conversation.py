"""Supervisor hooks for optional GitHub-backed Conversation Fabric operator campaigns."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import local_agent.daemon.service as agentd
import local_agent.foundation.storage as storage
from local_agent.conversation import operator_queue
from local_agent.foundation.process import (
    LEASE_FDS_ENV,
    LEASE_KEYS_DIGEST_ENV,
    RESOURCE_LEASE_FDS_ENV,
    popen_registered,
    terminate_process_group,
    unregister_process,
)
from local_agent.paths import repository_root
from local_agent.supervisor.macos_release_admission import macos_task_admission

OPERATOR_ENABLED_ENV = "LOCAL_AGENT_CONVERSATION_OPERATOR_ENABLED"
OPERATOR_ROOT_ENV = "LOCAL_AGENT_CONVERSATION_OPERATOR_ROOT"
OPERATOR_CHECKOUT_ENV = "LOCAL_AGENT_CONVERSATION_OPERATOR_CHECKOUT"
OPERATOR_PRODUCTION_CHECKOUT_ENV = "LOCAL_AGENT_CONVERSATION_OPERATOR_PRODUCTION_CHECKOUT"
OPERATOR_HOME_ENV = "LOCAL_AGENT_CONVERSATION_OPERATOR_HOME"
OPERATOR_CAMPAIGN_TIMEOUT_SECONDS = 15 * 60
_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True, slots=True)
class OperatorRuntimeConfig:
    home: Path
    root: Path
    checkout: Path
    production_checkout: Path


@dataclass
class RunningOperatorCampaign:
    request_id: str
    proc: subprocess.Popen[str]
    started_at: float
    request_path: Path
    result_path: Path


def _log(message: str) -> None:
    agentd.log(f"[conversation-operator] {message}")


def runtime_config(
    env: Mapping[str, str] | None = None,
) -> OperatorRuntimeConfig | None:
    source = os.environ if env is None else env
    enabled = source.get(OPERATOR_ENABLED_ENV, "").strip().lower()
    if enabled not in _TRUTHY:
        return None
    missing = [
        name
        for name in (
            OPERATOR_ROOT_ENV,
            OPERATOR_CHECKOUT_ENV,
            OPERATOR_PRODUCTION_CHECKOUT_ENV,
        )
        if not source.get(name, "").strip()
    ]
    if missing:
        raise ValueError(
            "Conversation Fabric operator runtime is enabled but missing: "
            + ", ".join(missing)
        )
    home = Path(source.get(OPERATOR_HOME_ENV, str(Path.home()))).expanduser().resolve()
    root = Path(source[OPERATOR_ROOT_ENV]).expanduser().resolve()
    checkout = Path(source[OPERATOR_CHECKOUT_ENV]).expanduser().resolve()
    production = Path(source[OPERATOR_PRODUCTION_CHECKOUT_ENV]).expanduser().resolve()
    if root == checkout or root in checkout.parents or checkout in root.parents:
        raise ValueError("Conversation Fabric operator root and checkout must be disjoint")
    if checkout == production or checkout in production.parents or production in checkout.parents:
        raise ValueError("Conversation Fabric operator checkout must be isolated from production")
    return OperatorRuntimeConfig(
        home=home,
        root=root,
        checkout=checkout,
        production_checkout=production,
    )


def _refresh_remote_control() -> None:
    branch = agentd.core.CONTROL_BRANCH
    remote_tracking = f"refs/remotes/origin/{branch}"
    refreshed = storage.run_git_with_network_retry(
        agentd.core,
        [
            "git",
            "fetch",
            "--depth",
            str(storage.CONTROL_HISTORY_DEPTH),
            "--no-tags",
            "origin",
            f"+refs/heads/{branch}:{remote_tracking}",
        ],
        agentd.core.CONTROL,
        timeout=120,
        log_commands=False,
    )
    if refreshed["exit_code"] != 0:
        raise RuntimeError(str(refreshed.get("output", "remote control fetch failed")))


def _require_remote_request_match(request: dict[str, object]) -> None:
    relative = operator_queue.CONTROL_REQUEST_DIR / f"{request['id']}.json"
    remote = f"refs/remotes/origin/{agentd.core.CONTROL_BRANCH}:{relative.as_posix()}"
    shown = agentd.core.process(
        ["git", "show", remote],
        agentd.core.CONTROL,
        timeout=30,
        log_commands=False,
    )
    if shown["exit_code"] != 0:
        raise RuntimeError("remote operator request disappeared after local staging")
    try:
        payload = json.loads(str(shown.get("output", "")))
    except json.JSONDecodeError as exc:
        raise RuntimeError("remote operator request is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("remote operator request must be an object")
    try:
        remote_digest = operator_queue.operator_request_digest(payload)
        local_digest = operator_queue.operator_request_digest(request)
    except ValueError as exc:
        raise RuntimeError("remote operator request is invalid") from exc
    if remote_digest != local_digest or payload != request:
        raise RuntimeError("remote operator request changed after local staging")


def _remote_result_matches(
    request: dict[str, object],
    result: dict[str, object],
) -> bool:
    relative = operator_queue.control_result_relative(str(request["id"]))
    remote = f"refs/remotes/origin/{agentd.core.CONTROL_BRANCH}:{relative.as_posix()}"
    shown = agentd.core.process(
        ["git", "show", remote],
        agentd.core.CONTROL,
        timeout=30,
        log_commands=False,
    )
    if shown["exit_code"] != 0:
        return False
    try:
        payload = json.loads(str(shown.get("output", "")))
    except json.JSONDecodeError as exc:
        raise RuntimeError("remote operator result is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("remote operator result must be an object")
    operator_queue.validate_operator_result(payload, request)
    if payload != result:
        raise RuntimeError("remote operator result conflicts with local durable result")
    return True


def publish_pending_result_only() -> bool:
    """Publish one terminal result and delete its spool only after fresh remote proof."""
    if runtime_config() is None:
        return False
    ready = operator_queue.next_spooled_result(agentd.STATE_DIR)
    if ready is None:
        return False

    item, request, result = ready
    operator_queue.require_control_request_match(agentd.core.CONTROL, request)
    operator_queue.control_result_matches(agentd.core.CONTROL, request, result)
    _require_remote_request_match(request)

    published = False
    if not _remote_result_matches(request, result):
        relative = operator_queue.control_result_relative(item.request_id)

        def validate_after_pull() -> None:
            operator_queue.require_control_request_match(agentd.core.CONTROL, request)

        agentd.publish_control_json(
            relative.as_posix(),
            result,
            commit_message=f"Conversation result: {item.request_id}",
            ensure_remote=True,
            post_pull_validate=validate_after_pull,
        )
        published = True

    # Cached remote-tracking refs are transport evidence, never final authority.
    # Refresh origin and prove both immutable request and exact result immediately
    # before deleting the only local durable publication spool.
    _refresh_remote_control()
    _require_remote_request_match(request)
    if not _remote_result_matches(request, result):
        raise RuntimeError("operator result publication is not confirmed on origin")
    if published:
        _log(f"published operator result request={item.request_id}")

    operator_queue.discard_spool(item)
    return True


def service_control_plane() -> None:
    """Publish completed local results and stage at most one remote request.

    Call only after the bound control checkout has been synchronized while its
    repository lease is held. No browser work runs in this function.
    """
    if runtime_config() is None:
        return

    if publish_pending_result_only():
        return

    staged = operator_queue.stage_next_control_request(agentd.core.CONTROL, agentd.STATE_DIR)
    if staged is not None:
        _log(f"staged operator request request={staged.request_id}")


def _worker_command(config: OperatorRuntimeConfig, item: operator_queue.OperatorWorkItem) -> list[str]:
    return [
        sys.executable,
        "-m",
        "local_agent.development.operator_campaign",
        "run",
        "--request",
        str(item.request_path),
        "--result",
        str(item.result_path),
        "--home",
        str(config.home),
        "--root",
        str(config.root),
        "--checkout",
        str(config.checkout),
        "--production-checkout",
        str(config.production_checkout),
    ]


def start_if_pending() -> RunningOperatorCampaign | None:
    """Guard operator campaign reservation and launch before any task effects."""
    with macos_task_admission(agentd.STATE_DIR):
        return _start_if_pending_admitted()


def _start_if_pending_admitted() -> RunningOperatorCampaign | None:
    """Start one staged campaign without inheriting repository/resource leases."""
    config = runtime_config()
    if config is None:
        return None
    item = operator_queue.next_staged_request(agentd.STATE_DIR)
    if item is None:
        return None
    # The exclusive, fsynced marker is durable before any process can
    # perform a browser effect. An interrupted attempt requires explicit
    # reconciliation; it must never be automatically launched again.
    if not operator_queue.reserve_launch_once(agentd.STATE_DIR, item):
        return None
    env = os.environ.copy()
    for name in (LEASE_FDS_ENV, LEASE_KEYS_DIGEST_ENV, RESOURCE_LEASE_FDS_ENV):
        env.pop(name, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = popen_registered(
        _worker_command(config, item),
        cwd=repository_root(),
        env=env,
        text=True,
        start_new_session=True,
    )
    setattr(proc, "_local_agent_process_group", proc.pid)
    _log(f"started operator campaign request={item.request_id} pid={proc.pid}")
    return RunningOperatorCampaign(
        request_id=item.request_id,
        proc=proc,
        started_at=time.monotonic(),
        request_path=item.request_path,
        result_path=item.result_path,
    )


def reap(
    slot: RunningOperatorCampaign | None,
    *,
    interrupted: bool = False,
) -> RunningOperatorCampaign | None:
    if slot is None:
        return None
    return_code = slot.proc.poll()
    timed_out = False
    if return_code is None and time.monotonic() - slot.started_at > OPERATOR_CAMPAIGN_TIMEOUT_SECONDS:
        timed_out = True
        terminate_process_group(slot.proc, _log)
        return_code = slot.proc.poll()
    if return_code is None:
        return slot

    unregister_process(slot.proc)
    item = operator_queue.OperatorWorkItem(
        request_id=slot.request_id,
        request_digest="",
        request_path=slot.request_path,
        result_path=slot.result_path,
    )
    if not slot.result_path.is_file():
        if interrupted:
            _log(
                f"operator campaign interrupted request={slot.request_id}; "
                "preserving staged request for recovery"
            )
            return None
        reason = (
            "operator campaign exceeded bounded runtime"
            if timed_out
            else f"operator campaign exited {return_code} without durable result"
        )
        try:
            operator_queue.persist_worker_failure(item, reason)
        except (OSError, RuntimeError, ValueError) as exc:
            _log(
                f"failed to persist operator worker failure request={slot.request_id}: "
                f"{type(exc).__name__}: {exc}"
            )
    _log(
        f"operator campaign finished request={slot.request_id} exit={return_code} "
        f"timed_out={timed_out}"
    )
    return None


def result_publish_pending() -> bool:
    """Return whether a local terminal result still needs GitHub publication."""
    if runtime_config() is None:
        return False
    return operator_queue.next_spooled_result(agentd.STATE_DIR) is not None
