"""Thin host-first producer/readback CLI; never acts as a worker or grant authority."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from local_agent.repository.host_mailbox_reader import _json_object
from local_agent.supervisor.host_pairing_plan import plan_private_github_pairing
from local_agent.supervisor.host_pairing_boundary import inspect_root_owned_pairing
from local_agent.supervisor.host_mvp_readiness import assess_owner_mvp_readiness
from local_agent.supervisor.host_owner_admin import (
    DEFAULT_RECORD, _require_local_admin, change_protected_owner_choice,
)
from local_agent.supervisor.host_privileged_helper_repair import protect_sudo_helper_parent
from local_agent.supervisor.host_local_pairing import (
    pair_local_github_host, pairing_confirmation, read_local_github_pairing,
    unpair_confirmation, unpair_local_github_host,
)
from local_agent.supervisor.host_control import HostControlSource
from local_agent.supervisor.host_private_producer import (
    load_private_host_producer, setup_private_host_producer, sync_private_host_producer,
)
from local_agent.supervisor.host_remote_client import (
    publish_staged_host_command, read_host_command_result, stage_host_command,
)
from local_agent.supervisor.host_tasks import MAX_LOCAL_REQUEST_BYTES
from local_agent.supervisor.host_task_flow import (
    current_host_command_result, submit_host_command,
)


def _print(payload: dict) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _interactive_local_confirmation(expected: str, *, repo: str, host: str) -> str:
    """Fail on redirected/piped input; no --yes or remotely usable approval flag."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("owner pairing changes require an interactive local terminal")
    print(f"Local Agent host: {host}; dedicated GitHub repo: {repo}")
    print("CAUTION: anyone who can write tasks in this repository may eventually")
    print("run commands as the Local Agent service OS user after a separate rollout.")
    print("This operation does NOT enable remote host commands or change grants.")
    print(f"To confirm, type exactly: {expected}")
    return input("Local owner confirmation: ")


def command_pair_local(args: argparse.Namespace) -> int:
    try:
        expected = pairing_confirmation(args.repository, args.expected_host_id)
        received = _interactive_local_confirmation(
            expected, repo=args.repository, host=args.expected_host_id,
        )
        payload = pair_local_github_host(
            state_dir=args.state_dir, identity_file=args.identity_file,
            control_checkout=args.control_checkout, repository=args.repository,
            expected_host_id=args.expected_host_id,
            expected_installation_id=args.expected_installation_id,
            expected_control_sha=args.expected_control_sha,
            owner_confirmation=received,
        )
    except (ValueError, OSError, RuntimeError, UnicodeError, EOFError) as exc:
        _print({"status": "unverified", "paired": False,
                "standing_execution_enabled": False,
                "execution_authority": "not_asserted", "error": type(exc).__name__})
        return 2
    _print(payload)
    return 0


def command_unpair_local(args: argparse.Namespace) -> int:
    try:
        from local_agent.repository.host_facts import load_local_host_identity
        identity = load_local_host_identity(args.identity_file)
        expected = unpair_confirmation(args.repository, identity.host_id)
        received = _interactive_local_confirmation(
            expected, repo=args.repository, host=identity.host_id,
        )
        payload = unpair_local_github_host(
            state_dir=args.state_dir, identity_file=args.identity_file,
            expected_repository=args.repository, owner_confirmation=received,
        )
    except (ValueError, OSError, RuntimeError, UnicodeError, EOFError) as exc:
        _print({"status": "unverified", "paired": False,
                "standing_execution_enabled": False,
                "execution_authority": "not_asserted", "error": type(exc).__name__})
        return 2
    _print(payload)
    return 0


def command_pair_status(args: argparse.Namespace) -> int:
    try:
        payload = read_local_github_pairing(
            state_dir=args.state_dir, identity_file=args.identity_file,
        )
    except (ValueError, OSError, RuntimeError, UnicodeError) as exc:
        _print({"status": "unverified", "paired": False,
                "standing_execution_enabled": False,
                "execution_authority": "not_asserted", "error": type(exc).__name__})
        return 2
    _print(payload)
    return 0


def command_pair_boundary_check(args: argparse.Namespace) -> int:
    """Inspect only a pre-existing macOS root-owned record; no enrollment."""
    try:
        payload = inspect_root_owned_pairing(
            record_path=args.record_path, identity_file=args.identity_file,
            expected_repository=args.repository,
        )
    except (ValueError, OSError, RuntimeError, UnicodeError) as exc:
        _print({"status": "unverified", "paired": False,
                "macos_root_write_boundary": "unverified",
                "github_writers_verified": False,
                "standing_execution_enabled": False,
                "execution_authority": "not_asserted", "error": type(exc).__name__})
        return 2
    _print(payload)
    return 0


def _interactive_helper_repair_confirmation(expected: str) -> str:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("helper remediation requires a local interactive terminal")
    print("LOCAL ADMINISTRATOR ONLY: /usr/local/sbin is a shared system directory.")
    print("Review all software depending on its existing ownership BEFORE changing it.")
    print("A root-owned journal is written before modification. No sudoers rule is changed.")
    print(f"To confirm this exact repair, type: {expected}")
    return input("Local administrator confirmation: ")


def command_owner_admin_protect_helper(args: argparse.Namespace) -> int:
    """Manually accepted shared-directory repair, never an agent task action."""
    try:
        _require_local_admin()
        expected = f"PROTECT /usr/local/sbin {args.expected_uid} 0775"
        received = _interactive_helper_repair_confirmation(expected)
        payload = protect_sudo_helper_parent(
            expected_uid=args.expected_uid, expected_mode=args.expected_mode,
            confirmation=received,
            reviewed_shared_directory=args.reviewed_shared_directory,
        )
    except (ValueError, OSError, RuntimeError, UnicodeError, EOFError) as exc:
        _print({"status": "repair_outcome_unverified",
                "manual_journal_and_permissions_review_required": True,
                "standing_execution_enabled": False,
                "execution_authority": "not_asserted", "error": type(exc).__name__})
        return 2
    _print(payload)
    return 0


def command_owner_admin_change(args: argparse.Namespace) -> int:
    """Local root-only pairing choice: no standing worker authorization."""
    try:
        _require_local_admin()
        from local_agent.repository.host_facts import load_local_host_identity
        identity = load_local_host_identity(args.identity_file)
        if (identity.host_id != args.expected_host_id
                or identity.installation_id != args.expected_installation_id):
            raise ValueError("owner-supplied host and installation must match local identity")
        prefix = (pairing_confirmation(args.repository, identity.host_id)
                  if args.admin_action == "pair"
                  else unpair_confirmation(args.repository, identity.host_id))
        expected = f"{prefix} {identity.installation_id}"
        received = _interactive_local_confirmation(
            expected, repo=args.repository, host=identity.host_id,
        )
        payload = change_protected_owner_choice(
            action=args.admin_action, record_path=DEFAULT_RECORD,
            identity_file=args.identity_file, repository=args.repository,
            expected_host_id=args.expected_host_id,
            expected_installation_id=args.expected_installation_id,
            expected_control_sha=getattr(args, "expected_control_sha", None),
            typed_confirmation=received,
        )
    except (ValueError, OSError, RuntimeError, UnicodeError, EOFError) as exc:
        _print({"status": "unverified", "paired": False,
                "standing_execution_enabled": False,
                "execution_authority": "not_asserted", "error": type(exc).__name__})
        return 2
    _print(payload)
    return 0


def command_mvp_readiness(args: argparse.Namespace) -> int:
    """Show the macOS pairing blocker, never authorize a task."""
    payload = assess_owner_mvp_readiness(
        record_path=args.record_path, identity_file=args.identity_file,
        expected_repository=args.repository,
    )
    _print(payload)
    return 2


def command_pairing_plan(args: argparse.Namespace) -> int:
    """Review an existing private origin and identity; NEVER pair or authorize."""
    try:
        payload = plan_private_github_pairing(
            identity_file=args.identity_file,
            control_checkout=args.control_checkout,
            repository=args.repository,
            expected_host_id=args.expected_host_id,
            expected_installation_id=args.expected_installation_id,
            expected_control_sha=args.expected_control_sha,
        )
    except (ValueError, OSError, RuntimeError, UnicodeError) as exc:
        _print({"status": "unverified", "paired": False, "published": False,
                "github_writers_verified": False,
                "execution_authority": "not_asserted", "error": type(exc).__name__})
        return 2
    _print(payload)
    return 0


def command_producer_setup(args: argparse.Namespace) -> int:
    try:
        payload = setup_private_host_producer(
            identity_file=args.identity_file, operator_checkout=args.operator_checkout,
            private_root=args.private_root, origin_url=args.origin_url,
            expected_control_sha=args.expected_control_sha,
            git_author_name=args.git_author_name, git_author_email=args.git_author_email,
            repository=args.repository, authorization=args.authorization,
        )
    except (ValueError, OSError, RuntimeError, UnicodeError) as exc:
        _print({"status": "unverified", "published": False,
                "execution_authority": "not_asserted", "error": type(exc).__name__})
        return 2
    _print(payload)
    return 0


def _source(args: argparse.Namespace, *, task_id: str | None = None,
            digest: str | None = None, result: bool = False) -> HostControlSource:
    if args.profile is not None:
        source = load_private_host_producer(
            args.profile, task_id=task_id, task_digest=digest,
            verify_authority=not result,
        )
        if args.host_id is not None and args.host_id != source.host_id:
            raise ValueError("explicit host differs from private producer profile")
        if args.installation_id is not None and args.installation_id != source.installation_id:
            raise ValueError("explicit installation differs from private producer profile")
        return source
    if args.host_id is None or args.installation_id is None:
        raise ValueError("host and installation are required without a private producer profile")
    checkout = args.control_checkout if result else getattr(args, "producer_checkout", None)
    return HostControlSource(args.host_id, args.installation_id, checkout)


def command_producer_sync(args: argparse.Namespace) -> int:
    try:
        payload = sync_private_host_producer(
            args.profile, expected_local_sha=args.expected_local_sha,
            expected_remote_sha=args.expected_remote_sha,
        )
    except (ValueError, OSError, RuntimeError, UnicodeError) as exc:
        _print({"status": "unverified", "published": False,
                "execution_authority": "not_asserted", "error": type(exc).__name__})
        return 2
    _print(payload)
    return 0


def command_submit(args: argparse.Namespace) -> int:
    """One ordinary submit: safely catch up clean Git, stage and publish."""
    try:
        draft = Path(args.path)
        if draft.is_symlink():
            raise ValueError("host task draft must not be a symlink")
        with draft.open("rb") as handle:
            raw = handle.read(MAX_LOCAL_REQUEST_BYTES + 1)
        if len(raw) > MAX_LOCAL_REQUEST_BYTES:
            raise ValueError("host task draft exceeds byte bound")
        task = _json_object(raw.decode("utf-8"))
        result = submit_host_command(args.profile, task)
    except (ValueError, OSError, RuntimeError, UnicodeError) as exc:
        _print({"status": "unverified", "published": False,
                "error": type(exc).__name__})
        return 2
    _print(result)
    return 0


def command_host_status(args: argparse.Namespace) -> int:
    """Read pending or terminal result without caller-maintained Git SHAs."""
    try:
        payload = current_host_command_result(args.profile, args.task_id)
    except (ValueError, OSError, RuntimeError, UnicodeError) as exc:
        _print({"status": "unverified", "error": type(exc).__name__})
        return 2
    _print(payload)
    return 0


def command_stage(args: argparse.Namespace) -> int:
    try:
        draft = Path(args.path)
        if draft.is_symlink():
            raise ValueError("host task draft must not be a symlink")
        with draft.open("rb") as handle:
            raw = handle.read(MAX_LOCAL_REQUEST_BYTES + 1)
        if len(raw) > MAX_LOCAL_REQUEST_BYTES:
            raise ValueError("host task draft exceeds byte bound")
        task = _json_object(raw.decode("utf-8"))
        source = _source(args)
        output_root = args.output_root or source.checkout
        if output_root is None or (args.profile is not None
                                   and output_root != source.checkout):
            raise ValueError("private profile staging must use its dedicated checkout")
        payload = stage_host_command(
            task, host_id=source.host_id, installation_id=source.installation_id,
            output_root=output_root,
        )
    except (ValueError, OSError, UnicodeError) as exc:
        _print({"status": "unverified", "published": False,
                "execution_authority": "not_asserted", "error": type(exc).__name__})
        return 2
    _print(payload)
    return 0


def command_publish(args: argparse.Namespace) -> int:
    """Publish one already-staged host task; never create authorization."""
    try:
        source = _source(args, task_id=args.task_id, digest=args.expected_task_digest)
        if args.profile is not None and args.producer_checkout is not None:
            if args.producer_checkout != source.checkout:
                raise ValueError("profile publication checkout mismatch")
        payload = publish_staged_host_command(
            source, task_id=args.task_id,
            expected_control_sha=args.expected_control_sha,
            expected_task_digest=args.expected_task_digest,
        )
    except (ValueError, OSError, RuntimeError, UnicodeError) as exc:
        _print({"status": "unverified", "published": False,
                "execution_authority": "not_asserted", "error": type(exc).__name__})
        return 2
    _print(payload)
    return 0


def command_result(args: argparse.Namespace) -> int:
    try:
        source = _source(args, result=True)
        if args.control_checkout is not None:
            source = HostControlSource(
                source.host_id, source.installation_id, args.control_checkout,
            )
        payload = read_host_command_result(
            source, task_id=args.task_id,
            expected_control_sha=args.expected_control_sha,
            expected_task_digest=args.expected_task_digest,
        )
    except (ValueError, OSError, RuntimeError, UnicodeError) as exc:
        _print({"status": "unverified", "execution_authority": "not_asserted",
                "error": type(exc).__name__})
        return 2
    _print(payload)
    return 0


def register_host_remote_parser(sub: argparse._SubParsersAction) -> None:
    for name, func, description in (
        ("host-pair-local", command_pair_local,
         "locally record approved owner GitHub inbox; never enable execution"),
        ("host-unpair-local", command_unpair_local,
         "locally revoke owner GitHub inbox record; no Git change"),
        ("host-pair-status", command_pair_status,
         "inspect local owner GitHub pairing record; no authority"),
    ):
        command = sub.add_parser(name, help=description)
        command.add_argument("--state-dir", type=Path, required=True)
        command.add_argument("--identity-file", type=Path, required=True)
        if name == "host-pair-local":
            command.add_argument("--control-checkout", type=Path, required=True)
            command.add_argument("--repository", required=True)
            command.add_argument("--expected-host-id", required=True)
            command.add_argument("--expected-installation-id", required=True)
            command.add_argument("--expected-control-sha", required=True)
        elif name == "host-unpair-local":
            command.add_argument("--repository", required=True)
        command.set_defaults(func=func)

    for name, action in (
        ("host-owner-admin-pair", "pair"),
        ("host-owner-admin-unpair", "unpair"),
    ):
        command = sub.add_parser(
            name, help="local root-only owner choice; does NOT authorize host execution",
        )
        command.add_argument("--identity-file", type=Path, required=True)
        command.add_argument("--repository", required=True)
        command.add_argument("--expected-host-id", required=True)
        command.add_argument("--expected-installation-id", required=True)
        if action == "pair":
            command.add_argument("--expected-control-sha", required=True)
        command.set_defaults(func=command_owner_admin_change, admin_action=action)

    repair = sub.add_parser(
        "host-owner-admin-protect-helper",
        help="LOCAL ROOT ONLY: journal and protect reviewed shared sudo helper parent",
    )
    repair.add_argument("--expected-uid", type=int, required=True)
    repair.add_argument("--expected-mode", type=lambda x: int(x, 8), required=True)
    repair.add_argument("--reviewed-shared-directory", action="store_true")
    repair.set_defaults(func=command_owner_admin_protect_helper)

    boundary = sub.add_parser(
        "host-pair-boundary-check",
        help="read-only macOS root-owned pairing state check; NEVER an execution grant",
    )
    boundary.add_argument("--record-path", type=Path, required=True)
    boundary.add_argument("--identity-file", type=Path, required=True)
    boundary.add_argument("--repository", required=True)
    boundary.set_defaults(func=command_pair_boundary_check)

    ready = sub.add_parser(
        "host-mvp-readiness",
        help="diagnose macOS standing-owner MVP blockers; read-only, never grants",
    )
    ready.add_argument("--record-path", type=Path, required=True)
    ready.add_argument("--identity-file", type=Path, required=True)
    ready.add_argument("--repository", required=True)
    ready.set_defaults(func=command_mvp_readiness)

    pair = sub.add_parser(
        "host-pairing-plan",
        help="read-only private GitHub transport/host preflight; no pairing or grant",
    )
    pair.add_argument("--identity-file", type=Path, required=True)
    pair.add_argument("--control-checkout", type=Path, required=True)
    pair.add_argument("--repository", required=True, help="owner-selected GitHub owner/repo")
    pair.add_argument("--expected-host-id", required=True)
    pair.add_argument("--expected-installation-id", required=True)
    pair.add_argument("--expected-control-sha", required=True)
    pair.set_defaults(func=command_pairing_plan)

    setup = sub.add_parser(
        "host-producer-setup", help="create isolated, pinned private Git producer; no grant or task",
    )
    setup.add_argument("--identity-file", type=Path, required=True)
    setup.add_argument("--operator-checkout", type=Path,
                       help="required only for legacy operator_grants producer")
    setup.add_argument("--authorization", choices=("operator_grants", "github_writers"),
                       default="operator_grants")
    setup.add_argument("--private-root", type=Path, required=True)
    setup.add_argument("--origin-url", required=True)
    setup.add_argument("--repository", default="example-owner/local-agent",
                       help="selected owner/name private GitHub task repository")
    setup.add_argument("--expected-control-sha", required=True)
    setup.add_argument("--git-author-name", required=True)
    setup.add_argument("--git-author-email", required=True)
    setup.set_defaults(func=command_producer_setup)

    sync = sub.add_parser(
        "host-producer-sync",
        help="explicit exact-SHA fast-forward of a clean isolated producer; no task or grant",
    )
    sync.add_argument("--profile", type=Path, required=True)
    sync.add_argument("--expected-local-sha", required=True)
    sync.add_argument("--expected-remote-sha", required=True)
    sync.set_defaults(func=command_producer_sync)

    submit = sub.add_parser(
        "host-submit",
        help="one safe host task submission: clean Git sync, stage, publish",
    )
    submit.add_argument("path", help="JSON task for exact target host/install")
    submit.add_argument("--profile", required=True, type=Path)
    submit.set_defaults(func=command_submit)

    current = sub.add_parser(
        "host-status",
        help="read pending/terminal host result with safe Git sync; no manual SHA",
    )
    current.add_argument("task_id")
    current.add_argument("--profile", required=True, type=Path)
    current.set_defaults(func=command_host_status)

    stage = sub.add_parser(
        "host-stage", help="stage a machine-targeted task; no Git push, grant or execution",
    )
    stage.add_argument("path", help="input JSON task")
    stage.add_argument("--host-id")
    stage.add_argument("--installation-id")
    stage.add_argument("--profile", type=Path)
    stage.add_argument("--output-root", type=Path)
    stage.set_defaults(func=command_stage)

    publish = sub.add_parser(
        "host-publish", help="publish one staged host task with a non-force Git push",
    )
    publish.add_argument("task_id")
    publish.add_argument("--host-id")
    publish.add_argument("--installation-id")
    publish.add_argument("--profile", type=Path)
    publish.add_argument("--producer-checkout", type=Path)
    publish.add_argument("--expected-control-sha", required=True)
    publish.add_argument("--expected-task-digest", required=True)
    publish.set_defaults(func=command_publish)

    result = sub.add_parser(
        "host-result", help="read one pinned, Git-published host task result",
    )
    result.add_argument("task_id")
    result.add_argument("--host-id")
    result.add_argument("--installation-id")
    result.add_argument("--profile", type=Path)
    result.add_argument("--control-checkout", type=Path)
    result.add_argument("--expected-control-sha", required=True)
    result.add_argument("--expected-task-digest", required=True)
    result.set_defaults(func=command_result)
