"""Repository-independent remote emergency operator control."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import local_agent.foundation.core as core
from local_agent.operator import local as agent_operator
from local_agent.repository.host_routing import HostEnrollment

REMOTE_BRANCH = "operator-control"
REMOTE_TRACKING_REF = f"refs/remotes/origin/{REMOTE_BRANCH}"
REMOTE_STATE_PATH = ".agent/operator/state.json"
REMOTE_HOST_REGISTRY_PATH = ".agent/operator/hosts.json"
REMOTE_HOST_COMMAND_GRANTS_PATH = ".agent/operator/host-commands.json"
MAX_HOST_REGISTRY_BYTES = 64 * 1024
POLL_SECONDS = 2.0
REMOTE_REF_TIMEOUT_SECONDS = 5
REMOTE_FETCH_TIMEOUT_SECONDS = 10
REMOTE_LOCAL_GIT_TIMEOUT_SECONDS = 5
REMOTE_NETWORK_BACKOFF_SECONDS = (5.0, 10.0, 15.0)
_CONTROL_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass
class RemoteOperatorState:
    last_poll_at: float | None = None
    last_ref: str | None = None
    desired_state: str | None = None
    request_id: str | None = None
    consecutive_transport_failures: int = 0
    retry_not_before: float = 0.0


def _git(args: list[str], self_repo: Path, *, timeout: int = 30) -> dict[str, Any]:
    return core.process(
        ["git", *args],
        self_repo,
        timeout=timeout,
        log_commands=False,
    )


def _remote_ref(self_repo: Path) -> str:
    result = _git(
        ["ls-remote", "--heads", "origin", f"refs/heads/{REMOTE_BRANCH}"],
        self_repo,
        timeout=REMOTE_REF_TIMEOUT_SECONDS,
    )
    if result["exit_code"] != 0:
        raise RuntimeError(
            str(result.get("output", "")).strip() or "operator control ref probe failed"
        )
    output = str(result.get("output", "")).strip()
    if not output:
        raise RuntimeError(f"remote operator branch is missing: {REMOTE_BRANCH}")
    ref = output.split()[0]
    if len(ref) != 40 or any(ch not in "0123456789abcdefABCDEF" for ch in ref):
        raise RuntimeError(f"invalid operator control ref: {ref!r}")
    return ref.lower()


def _load_remote_text(self_repo: Path, ref: str, path: str) -> str:
    """Read an allowlisted operator file from one exact, fetched Git commit."""
    if path not in {REMOTE_STATE_PATH, REMOTE_HOST_REGISTRY_PATH, REMOTE_HOST_COMMAND_GRANTS_PATH}:
        raise ValueError("unsupported operator control record")
    fetch = _git(
        [
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-write-fetch-head",
            "origin",
            f"+refs/heads/{REMOTE_BRANCH}:{REMOTE_TRACKING_REF}",
        ],
        self_repo,
        timeout=REMOTE_FETCH_TIMEOUT_SECONDS,
    )
    if fetch["exit_code"] != 0:
        raise RuntimeError(str(fetch.get("output", "")).strip() or "operator control fetch failed")
    resolved = _git(
        ["rev-parse", REMOTE_TRACKING_REF],
        self_repo,
        timeout=REMOTE_LOCAL_GIT_TIMEOUT_SECONDS,
    )
    fetched_ref = str(resolved.get("output", "")).strip().lower()
    if resolved["exit_code"] != 0 or fetched_ref != ref:
        raise RuntimeError(
            f"operator control ref changed during fetch: expected={ref} "
            f"got={fetched_ref or 'unknown'}"
        )
    show = _git(
        ["show", f"{ref}:{path}"],
        self_repo,
        timeout=REMOTE_LOCAL_GIT_TIMEOUT_SECONDS,
    )
    if show["exit_code"] != 0:
        raise ValueError(str(show.get("output", "")).strip() or "operator control state missing")
    value = str(show.get("output", ""))
    if path in {REMOTE_HOST_REGISTRY_PATH, REMOTE_HOST_COMMAND_GRANTS_PATH} and bool(show.get("output_truncated")):
        raise ValueError("operator host registry Git output was truncated")
    if path in {REMOTE_HOST_REGISTRY_PATH, REMOTE_HOST_COMMAND_GRANTS_PATH} and len(value.encode("utf-8")) > MAX_HOST_REGISTRY_BYTES:
        raise ValueError("operator host registry exceeds permitted size")
    return value


def _load_remote_payload(self_repo: Path, ref: str) -> dict[str, Any]:
    payload = json.loads(_load_remote_text(self_repo, ref, REMOTE_STATE_PATH))
    if not isinstance(payload, dict):
        raise ValueError("operator control state root must be an object")
    return payload


# Registration authority is distinct from general emergency-stop transport.
# Never accept enrollment records from a local mirror, fork or rewritten origin.
_OPERATOR_REGISTRY_REPOSITORY = "example-owner/local-agent"


def _require_trusted_host_registry_origin(self_repo: Path) -> None:
    accepted = {
        f"https://github.com/{_OPERATOR_REGISTRY_REPOSITORY}",
        f"https://github.com/{_OPERATOR_REGISTRY_REPOSITORY}.git",
        f"git@github.com:{_OPERATOR_REGISTRY_REPOSITORY}",
        f"git@github.com:{_OPERATOR_REGISTRY_REPOSITORY}.git",
        f"ssh://git@github.com/{_OPERATOR_REGISTRY_REPOSITORY}",
        f"ssh://git@github.com/{_OPERATOR_REGISTRY_REPOSITORY}.git",
    }
    for argv in (["config", "--get", "remote.origin.url"],
                 ["remote", "get-url", "origin"]):
        result = _git(argv, self_repo, timeout=REMOTE_LOCAL_GIT_TIMEOUT_SECONDS)
        value = str(result.get("output", "")).strip()
        if (result.get("exit_code") != 0 or result.get("output_truncated")
                or value.casefold() not in accepted):
            raise ValueError("operator enrollment origin is untrusted or rewritten")


def fetch_operator_host_registry(self_repo: Path) -> tuple[str, tuple[HostEnrollment, ...]]:
    """One-shot read-only operator manifest inspection; NOT task admission.

    Reuses the existing operator-control ref/fetch consistency checks, but does
    not affect emergency disable state or install any registration. Provenance
    still depends on the trusted self repository Git origin/credentials.
    """
    from local_agent.repository.host_routing import parse_operator_host_registry

    _require_trusted_host_registry_origin(self_repo)
    pinned_ref = _remote_ref(self_repo)
    raw = _load_remote_text(self_repo, pinned_ref, REMOTE_HOST_REGISTRY_PATH)
    return pinned_ref, parse_operator_host_registry(raw.encode("utf-8"))


def fetch_operator_host_command_grants(self_repo: Path):
    """Fetch separately approved exact command digests from operator-control.

    A writer to agent-control cannot alter these grants without advancing the
    independent operator-control SHA; each installation also pins that SHA.
    """
    from local_agent.supervisor.host_remote_grants import parse_host_command_grants

    _require_trusted_host_registry_origin(self_repo)
    pinned_ref = _remote_ref(self_repo)
    raw = _load_remote_text(self_repo, pinned_ref, REMOTE_HOST_COMMAND_GRANTS_PATH)
    if _remote_ref(self_repo) != pinned_ref:
        raise RuntimeError("operator command grants changed during admission")
    return pinned_ref, parse_host_command_grants(raw.encode("utf-8"))

def _validated_state(payload: dict[str, Any]) -> tuple[str, str]:
    if type(payload.get("version")) is not int or payload["version"] != 1:
        raise ValueError("operator control version must be 1")
    desired_state = payload.get("desired_state")
    if desired_state not in {"enabled", "disabled"}:
        raise ValueError("operator desired_state must be enabled or disabled")
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or len(request_id) > 120 or not _CONTROL_ID_RE.fullmatch(request_id):
        raise ValueError("operator request_id is invalid")
    return desired_state, request_id


def _record_transport_failure(state: RemoteOperatorState, now: float) -> float:
    state.consecutive_transport_failures += 1
    index = min(
        state.consecutive_transport_failures - 1,
        len(REMOTE_NETWORK_BACKOFF_SECONDS) - 1,
    )
    delay = REMOTE_NETWORK_BACKOFF_SECONDS[index]
    state.retry_not_before = now + delay
    return delay


def _clear_transport_failure(state: RemoteOperatorState) -> None:
    state.consecutive_transport_failures = 0
    state.retry_not_before = 0.0


def poll_remote_operator(
    state: RemoteOperatorState,
    *,
    self_repo: Path,
    now: float | None = None,
    force: bool = False,
) -> str | None:
    """Refresh central desired state and persist disable locally when requested.

    Remote ``enabled`` never clears the local marker. Re-enabling always requires
    an explicit local operator action after the remote desired state was cleared.
    Network failures preserve the last known state and open a bounded probe backoff
    so a GitHub outage cannot monopolize the guarded entrypoint loop.
    """
    current = time.monotonic() if now is None else now
    due = (
        force
        or state.last_poll_at is None
        or current - state.last_poll_at >= POLL_SECONDS
    )
    retry_allowed = force or current >= state.retry_not_before
    if due and retry_allowed:
        state.last_poll_at = current
        try:
            ref = _remote_ref(self_repo)
        except Exception as exc:
            delay = _record_transport_failure(state, current)
            core.log(
                "remote operator ref probe degraded: "
                f"{type(exc).__name__}: {exc}; retry in {delay:g}s"
            )
        else:
            _clear_transport_failure(state)
            if ref != state.last_ref or state.desired_state is None:
                try:
                    payload = _load_remote_payload(self_repo, ref)
                    desired_state, request_id = _validated_state(payload)
                except ValueError as exc:
                    state.last_ref = ref
                    state.desired_state = "disabled"
                    state.request_id = f"invalid-{ref[:12]}"
                    core.log(f"remote operator state invalid; failing closed: {exc}")
                except Exception as exc:
                    delay = _record_transport_failure(state, current)
                    core.log(
                        "remote operator control refresh degraded: "
                        f"{type(exc).__name__}: {exc}; retry in {delay:g}s"
                    )
                else:
                    _clear_transport_failure(state)
                    state.last_ref = ref
                    state.desired_state = desired_state
                    state.request_id = request_id

    if state.desired_state == "disabled":
        if not agent_operator.is_disabled():
            agent_operator.disable_agent(
                control_id=state.request_id,
                reason="remote_operator_control",
            )
        return "disabled"
    return state.desired_state
