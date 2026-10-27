"""Read-only preflight for a trusted operator SHA change.

This inspects a locally available candidate commit and the current local pin.
It NEVER installs a pin, advances Git refs, approves a command or changes an
installed service. Only a separately supported local operator transaction may
perform an actual rotation.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

from local_agent.operator.remote import (
    REMOTE_HOST_COMMAND_GRANTS_PATH,
    REMOTE_HOST_REGISTRY_PATH,
    REMOTE_STATE_PATH,
    _remote_ref,
    _require_trusted_host_registry_origin,
)
from local_agent.repository.host_execution import load_local_operator_pin
from local_agent.repository.host_facts import load_local_host_identity
from local_agent.repository.host_routing import parse_operator_host_registry
from local_agent.supervisor.host_remote_grants import parse_host_command_grants

_SHA = re.compile(r"^[0-9a-f]{40}$")
_MAX_RECORD = 65536
_ALLOW_CHANGED = frozenset((REMOTE_HOST_REGISTRY_PATH, REMOTE_HOST_COMMAND_GRANTS_PATH))


def _git(checkout: Path, *args: str, max_bytes: int = 16384) -> str:
    """Use Git for bounded local inspection; never fetch or write objects."""
    result = subprocess.run(
        ["git", "-C", str(checkout), *args],
        capture_output=True, timeout=10, check=False,
    )
    if result.returncode or len(result.stdout) > max_bytes:
        raise ValueError("operator candidate Git inspection failed")
    return result.stdout.decode("utf-8", errors="strict").strip()


def _record(checkout: Path, sha: str, path: str) -> bytes:
    if path not in _ALLOW_CHANGED | {REMOTE_STATE_PATH}:
        raise ValueError("unsupported operator manifest path")
    size = _git(checkout, "cat-file", "-s", f"{sha}:{path}")
    if not size.isascii() or not size.isdigit() or not 0 < int(size) <= _MAX_RECORD:
        raise ValueError("operator candidate manifest size is invalid")
    result = subprocess.run(
        ["git", "-C", str(checkout), "show", f"{sha}:{path}"],
        capture_output=True, timeout=10, check=False,
    )
    if result.returncode or not 0 < len(result.stdout) <= _MAX_RECORD:
        raise ValueError("operator candidate manifest cannot be inspected")
    return result.stdout


def inspect_operator_pin_rotation(
    *, identity_file: Path, operator_checkout: Path, candidate_sha: str,
) -> dict[str, object]:
    """Check an additive candidate without granting it authority.

    The current Git origin and pinned ref must match, the proposed SHA must
    be a direct child, and every existing enrollment/grant must be preserved.
    Newly enrolled installations may not claim project repositories here.
    Any new command grant must belong to a newly enrolled installation.
    """
    if not isinstance(candidate_sha, str) or _SHA.fullmatch(candidate_sha) is None:
        raise ValueError("candidate must be a lowercase complete Git SHA")
    identity_file = Path(identity_file)
    operator_checkout = Path(operator_checkout)
    identity = load_local_host_identity(identity_file)
    current = load_local_operator_pin(identity_file)
    if candidate_sha == current:
        raise ValueError("candidate and current operator pin are identical")

    _require_trusted_host_registry_origin(operator_checkout)
    if _remote_ref(operator_checkout) != current:
        raise ValueError("remote operator ref is not the locally trusted SHA")

    commit = _git(operator_checkout, "rev-list", "--parents", "-n", "1", candidate_sha)
    if commit.split() != [candidate_sha, current]:
        raise ValueError("candidate must be a direct child of the pinned operator commit")
    changed = frozenset(
        _git(operator_checkout, "diff", "--name-only", "--no-renames",
             current, candidate_sha).splitlines()
    )
    if not changed or not changed <= _ALLOW_CHANGED:
        raise ValueError("candidate modifies unrelated operator files or is empty")
    if _record(operator_checkout, current, REMOTE_STATE_PATH) != _record(
        operator_checkout, candidate_sha, REMOTE_STATE_PATH
    ):
        raise ValueError("candidate modifies emergency operator state")

    old_hosts = parse_operator_host_registry(
        _record(operator_checkout, current, REMOTE_HOST_REGISTRY_PATH)
    )
    new_hosts = parse_operator_host_registry(
        _record(operator_checkout, candidate_sha, REMOTE_HOST_REGISTRY_PATH)
    )
    old_grants = parse_host_command_grants(
        _record(operator_checkout, current, REMOTE_HOST_COMMAND_GRANTS_PATH)
    )
    new_grants = parse_host_command_grants(
        _record(operator_checkout, candidate_sha, REMOTE_HOST_COMMAND_GRANTS_PATH)
    )
    if not any(item.enabled and item.identity == identity for item in old_hosts):
        raise ValueError("current installation is not enrolled under the existing pin")
    if not set(old_hosts) <= set(new_hosts) or not set(old_grants) <= set(new_grants):
        raise ValueError("candidate removes or modifies existing operator authority")

    new_enrollments = set(new_hosts) - set(old_hosts)
    added_identities = {record.identity for record in new_enrollments}
    if not added_identities or any(
        not record.enabled or record.repositories for record in new_enrollments
    ):
        raise ValueError("candidate must add only enabled project-free installations")
    new_approvals = set(new_grants) - set(old_grants)
    if not new_approvals or any(
        record.identity not in added_identities for record in new_approvals
    ):
        raise ValueError("new command grants must target newly enrolled installations")
    if {record.identity for record in new_approvals} != added_identities:
        raise ValueError("each newly enrolled installation needs an explicit grant")

    if _remote_ref(operator_checkout) != current:
        raise ValueError("operator ref changed during inspection")
    return {
        "decision": "COMPATIBLE_NOT_AUTHORIZED",
        "host_id": identity.host_id,
        "installation_id": identity.installation_id,
        "old_sha": current,
        "candidate_sha": candidate_sha,
        "preserved_enrollments": len(old_hosts),
        "preserved_grants": len(old_grants),
        "new_enrollments": len(new_enrollments),
        "new_grants": len(new_approvals),
        "local_pin_updated": False,
        "operator_ref_updated": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only compatibility preflight; never authorizes or changes a pin"
    )
    parser.add_argument("--identity-file", type=Path, required=True)
    parser.add_argument("--operator-checkout", type=Path, required=True)
    parser.add_argument("--candidate-sha", required=True)
    args = parser.parse_args()
    try:
        plan = inspect_operator_pin_rotation(
            identity_file=args.identity_file,
            operator_checkout=args.operator_checkout,
            candidate_sha=args.candidate_sha,
        )
    except (ValueError, OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({
            "decision": "REJECTED_NOT_AUTHORIZED",
            "error": str(exc)[:256],
            "local_pin_updated": False,
            "operator_ref_updated": False,
        }, sort_keys=True))
        return 2
    print(json.dumps(plan, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
