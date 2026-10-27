"""Opt-in, repository-independent host supervisor transport configuration.

This describes a dedicated checkout of *Local Agent's own private repository*,
not a project in the executable repository registry. Source-code integration,
local host enrollment and Git transport authority remain separate decisions.
Reading configuration does not provision a checkout or execute a host task.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from local_agent.repository.host_facts import load_local_host_identity
from local_agent.repository.host_routing import canonical_host_id
from local_agent.repository.path_safety import has_untrusted_symlink

CONTROL_REPOSITORY = "example-owner/local-agent"
CONTROL_BRANCH = "agent-control"
CONTROL_VERSION = 1
MAX_CONFIG_BYTES = 4096
_OWNER_REPOSITORY = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}$")


def canonical_control_repository(value: str) -> str:
    """Strict GitHub owner/name; never accept arbitrary URLs or file paths."""
    if not isinstance(value, str) or _OWNER_REPOSITORY.fullmatch(value) is None:
        raise ValueError("expected GitHub owner/repository")
    owner, name = value.split("/")
    if (owner.startswith("-") or owner.endswith("-") or name in (".", "..")
            or name.startswith(".") or name.endswith(".")
            or name.casefold().endswith(".git")):
        raise ValueError("GitHub repository name is not canonical")
    return value.casefold()




@dataclass(frozen=True, slots=True)
class HostControlSource:
    host_id: str
    installation_id: str
    checkout: Path
    repository: str = CONTROL_REPOSITORY
    identity_file: Path | None = None
    host_intake_enabled: bool = False
    host_intake_authorization: str = "operator_grants"

    @property
    def repository_id(self) -> str:
        return "local-agent-supervisor"

    @property
    def control(self) -> Path:
        return self.checkout

    @property
    def control_branch(self) -> str:
        return CONTROL_BRANCH

    @property
    def host_mailbox_enabled(self) -> bool:
        # This is the service's own control stream, not a project task inbox.
        return False

    @property
    def namespace(self) -> str:
        # Never read legacy .agent/daemon/control.json during migration.
        return f".agent/hosts/{self.host_id}/supervisor/{self.installation_id}"

    @property
    def request_path(self) -> str:
        return f"{self.namespace}/control.json"

    @property
    def ack_directory(self) -> str:
        return f"{self.namespace}/acks"

    @property
    def status_path(self) -> str:
        return f"{self.namespace}/status.json"


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f"duplicate host control key: {name}")
        result[name] = value
    return result


def load_host_control_source(path: Path) -> HostControlSource | None:
    """Load an explicitly enabled host control plane, otherwise leave legacy mode.

    No implicit example-project default, no implicit host identity creation, and no
    authority inferred from the current project's control branch.
    """
    path = Path(path).expanduser()
    if not path.is_absolute() or has_untrusted_symlink(path):
        raise ValueError("host supervisor configuration path must be absolute and not symlinked")
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        return None
    if not data or len(data) > MAX_CONFIG_BYTES:
        raise ValueError("host supervisor configuration exceeds its byte limit")
    if os.name == "posix" and path.stat().st_mode & 0o022:
        raise ValueError("host supervisor configuration is writable by group or others")
    try:
        payload = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("host supervisor configuration is not valid UTF-8 JSON") from exc
    required = {"version", "enabled", "host_id", "identity_file", "control_checkout"}
    if (not isinstance(payload, dict) or not required <= set(payload)
            or set(payload) - required - {"repository", "host_intake"}
            or type(payload["version"]) is not int or payload["version"] != CONTROL_VERSION):
        raise ValueError("invalid host supervisor configuration schema")
    if type(payload["enabled"]) is not bool:
        raise ValueError("host supervisor enabled flag must be a boolean")
    if payload["enabled"] is False:
        return None
    host_id = canonical_host_id(payload["host_id"])
    repository = canonical_control_repository(payload.get("repository", CONTROL_REPOSITORY))
    intake = payload.get("host_intake", {"enabled": False})
    if (not isinstance(intake, dict) or "enabled" not in intake
            or set(intake) - {"enabled", "authorization"}
            or type(intake["enabled"]) is not bool):
        raise ValueError("invalid local host intake selection")
    intake_mode = intake.get("authorization", "operator_grants")
    if type(intake_mode) is not str or intake_mode not in ("operator_grants", "github_writers"):
        raise ValueError("invalid local host intake mode")
    identity_file = Path(payload["identity_file"]) if isinstance(payload["identity_file"], str) else Path()
    checkout = Path(payload["control_checkout"]) if isinstance(payload["control_checkout"], str) else Path()
    if (
        not identity_file.is_absolute() or not checkout.is_absolute()
        or identity_file == checkout or checkout == Path(checkout.anchor)
        or has_untrusted_symlink(checkout)
    ):
        raise ValueError("host supervisor requires absolute, isolated control and identity paths")
    identity = load_local_host_identity(identity_file)
    if identity.host_id != host_id:
        raise ValueError("host supervisor identity does not match the configured host")
    return HostControlSource(
        host_id=host_id, installation_id=identity.installation_id,
        checkout=checkout, repository=repository,
        identity_file=identity_file,
        host_intake_enabled=intake["enabled"],
        host_intake_authorization=intake_mode,
    )


def validate_host_control_checkout(
    source: HostControlSource, *, production_checkout: Path, project_workspaces: tuple[Path, ...] = (),
) -> None:
    """Fail closed before a dedicated control source can be used for effects."""
    from local_agent.repository.admin import normalize_remote_url
    from local_agent.repository.path_safety import has_untrusted_symlink
    from subprocess import run, TimeoutExpired

    # macOS /var and /private/var can name the same filesystem object.
    # Resolve after checking for untrusted symlinks so aliases cannot bypass
    # project/production isolation checks.
    if has_untrusted_symlink(source.checkout):
        raise ValueError("host control checkout must be isolated from installed source")
    root = source.checkout.resolve(strict=False)
    installed = production_checkout.resolve()
    if (root == installed
            or installed in root.parents or root in installed.parents):
        raise ValueError("host control checkout must be isolated from installed source")
    for other in project_workspaces:
        other = other.resolve()
        if root == other or root in other.parents or other in root.parents:
            raise ValueError("host control checkout overlaps an executable project workspace")
    if not (root / ".git").exists():
        raise ValueError("dedicated host control checkout is missing")
    for args in (["remote", "get-url", "origin"], ["branch", "--show-current"]):
        try:
            result = run(["git", "-C", str(root), *args], capture_output=True,
                         text=True, timeout=10, check=False)
        except (OSError, TimeoutExpired) as exc:
            raise ValueError("cannot verify dedicated host control checkout") from exc
        if result.returncode != 0:
            raise ValueError("dedicated host control Git verification failed")
        value = result.stdout.strip()
        if args[0] == "remote" and normalize_remote_url(value).casefold() != canonical_control_repository(source.repository):
            raise ValueError("host control origin is not the trusted Local Agent repository")
        if args[0] == "branch" and value != CONTROL_BRANCH:
            from local_agent.foundation.storage import interrupted_control_rebase_metadata

            metadata = interrupted_control_rebase_metadata(root) if not value else None
            if metadata is None or metadata["head-name"] != f"refs/heads/{CONTROL_BRANCH}":
                raise ValueError("host control checkout is not on the expected control branch")
            # A validated interrupted sequencer may detach HEAD. This only
            # permits selection: sync must rebuild under the existing host
            # lease before any task or publication admission.


def require_host_publication_identity(source: HostControlSource) -> None:
    """Require a dedicated Git commit identity before a remote command has effects.

    A machine may have a globally inherited Git identity for project work.
    Host control must instead have its own explicitly provisioned, local
    author settings. Never silently write global/local config from a remotely
    submitted task or accept transport branch permissions as authorization.
    """
    from subprocess import TimeoutExpired, run

    for key in ("user.name", "user.email"):
        try:
            result = run(
                ["git", "-C", str(source.checkout), "config", "--local", "--get", key],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except (OSError, TimeoutExpired) as exc:
            raise ValueError("cannot verify local host control Git author") from exc
        value = result.stdout.strip()
        if (result.returncode != 0 or not value or len(value) > 256
                or any(character in value for character in "\r\n\x00")):
            raise ValueError(
                f"dedicated host control checkout requires local Git {key} "
                "before admitting remote commands"
            )


def selected_host_control_source(
    repositories: list[object], *,
    state_dir: Path, production_checkout: Path,
) -> HostControlSource | None:
    """Choose the explicitly enrolled machine control stream, or legacy mode.

    A present but invalid configuration is an error. It must never silently
    fall back to a project that may have weaker write authority.
    """
    source = load_host_control_source(state_dir / "supervisor-control.json")
    if source is None:
        return None
    paths: list[Path] = []
    for item in repositories:
        paths.extend((item.control, item.work, item.checkpoints))
    validate_host_control_checkout(
        source, production_checkout=production_checkout, project_workspaces=tuple(paths),
    )
    return source


def host_control_lease_keys(source: HostControlSource) -> tuple[str, ...]:
    """Stable keys independent of the installed project registry and its order."""
    return tuple(sorted((
        f"host-supervisor:{source.host_id}",
        f"host-control-checkout:{source.checkout.resolve(strict=False)}",
    )))


@contextlib.contextmanager
def host_supervisor_lease(source: HostControlSource) -> Iterator[None]:
    """Exclude concurrent host control updates without project ownership."""
    from local_agent.foundation.process import acquire_execution_leases
    from local_agent.repository.worker import repository_lease_dir

    leases = acquire_execution_leases(
        repository_lease_dir(), host_control_lease_keys(source),
    )
    try:
        yield
    finally:
        leases.close()


def managed_project_repositories(*, registry_path: Path | None, state_dir: Path):
    """Parse only user-assigned project targets; no implicit example-project in host mode."""
    from local_agent.repository.context import load_repository_registry

    host_enabled = load_host_control_source(
        state_dir / "supervisor-control.json",
    ) is not None
    return load_repository_registry(path=registry_path, allow_empty=host_enabled)


def select_global_control(
    repositories: list[object], *, state_dir: Path, production_checkout: Path,
):
    """The source never depends on the order of enabled project repositories."""
    source = selected_host_control_source(
        repositories, state_dir=state_dir, production_checkout=production_checkout,
    )
    if source is not None:
        return source
    if not repositories:
        raise ValueError("no enabled repositories and no trusted host control")
    return repositories[0]


_CONTROL_ID = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
HOST_CONTROL_ACTIONS = frozenset({
    "status", "restart", "self_update", "disable", "host_diagnostics",
})


def require_host_control_request(source: HostControlSource, request: object) -> dict:
    """Authenticate a host control intent to exactly one enrolled installation.

    A control checkout's location alone is insufficient authorization; old
    project requests and old-installation requests must never become actions.
    """
    expected = {
        "version", "id", "action", "target_host_id", "target_installation_id",
    }
    if not isinstance(request, dict):
        raise ValueError("invalid host supervisor control request shape")
    if request.get("action") == "self_update":
        expected.add("candidate_sha")
    if set(request) != expected:
        raise ValueError("invalid host supervisor control request shape")
    if type(request["version"]) is not int or request["version"] != CONTROL_VERSION:
        raise ValueError("unsupported host supervisor control version")
    ident = request["id"]
    if not isinstance(ident, str) or _CONTROL_ID.fullmatch(ident) is None:
        raise ValueError("invalid host supervisor request id")
    action = request["action"]
    if not isinstance(action, str) or action not in HOST_CONTROL_ACTIONS:
        raise ValueError("unsupported host supervisor control action")
    if (request["target_host_id"] != source.host_id
            or request["target_installation_id"] != source.installation_id):
        raise ValueError("host supervisor control targets a different installation")
    if action == "self_update":
        candidate = request["candidate_sha"]
        if (type(candidate) is not str
                or re.fullmatch(r"[0-9a-f]{40}", candidate) is None):
            raise ValueError("host self-update requires an exact lowercase Git SHA")
    return request
