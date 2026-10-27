"""Guided terminal repository registry menu; reuse safe admin transactions."""
from __future__ import annotations

import json
import os
from pathlib import Path

import local_agent.daemon.service as agentd
from local_agent.repository.registration_status import repository_registration_status




def _restart_note() -> None:
    if os.environ.get("LOCAL_AGENT_MAINTENANCE_SESSION") == "1":
        print("Saved offline. Exit the menu to automatically restart Local Agent.")
    else:
        print("Registration changed offline; restart through the guarded operator workflow.")

def _registry(registry_file: Path | None) -> Path:
    return registry_file or agentd.multirepo_registry_path()


def _display(value: object) -> str:
    """Keep untrusted registry labels and paths on one terminal line."""
    return "".join(
        char if char.isprintable() else char.encode("unicode_escape").decode("ascii")
        for char in str(value)
    )


def _show(registry_file: Path | None, repository_id: str | None = None) -> None:
    """List compact registrations or explain exact configured paths."""
    snapshot = repository_registration_status(
        registry_path=_registry(registry_file), repository_id=repository_id,
    )
    if repository_id is None:
        counts = snapshot["counts"]
        print(f"Repositories: {counts['enabled']} enabled / "
              f"{counts['total']} registered (configuration, not live health)")
        id_width = max((len(_display(row["repository_id"])) for row in
                        snapshot["repositories"]), default=12)
        id_width = max(id_width, 12)
        for row in snapshot["repositories"]:
            state = "Enabled" if row["enabled"] else "Disabled"
            repo_id = _display(row["repository_id"])
            print(f"  ID: {repo_id:<{id_width}}  [{state}]  "
                  f"{_display(row['repository'])}")
        if not snapshot["repositories"]:
            print("  No optional repositories registered.")
        print("  Use the ID shown above for options 3-6.")
        print("  Option 6 shows paths, branches and worktree type.")
        return

    print("Repository details (configured paths and branches, not live health)")
    row = snapshot["repositories"][0]
    state = "enabled" if row["enabled"] else "disabled"
    print(f"  ID: {_display(row['repository_id'])} — {_display(row['repository'])} [{state}]")
    mode = row["work_mode"]
    if mode == "attached":
        print("  Worktree type: attached (owner-managed; never reset/clean)")
    else:
        print(f"  Worktree type: {_display(mode)} (check ownership before editing)")
    print(f"  Project source/worktree: {_display(row['work_dir'])}")
    print(f"  Project target branch (configured): {_display(row['default_branch'])}")
    print(f"  Private Git control/inbox: {_display(row['control_dir'])}")
    print(f"  Control branch (configured): {_display(row['control_branch'])}")
    print(f"  Task checkpoints/history: {_display(row['checkpoints_dir'])}")
    print("  Paths/branches above are registry settings, not a live Git or daemon check.")
    print("  Disabling preserves folders and history; detach removes only registration.")


def _add(registry_file: Path | None) -> None:
    from local_agent.repository.onboarding import plan_repository_add
    from local_agent.repository.transactional_add import apply_repository_add
    from local_agent.repository.registration_disable import _require_offline_installation

    repository = input("GitHub owner/repository (0 = Back): ").strip()
    if repository == "0":
        return
    if not repository:
        print("No repository selected.")
        return
    # MVP: use the established managed checkout layout. Local path
    # discovery, relocation and optional path entry are deferred.
    opts = {"registry_path": _registry(registry_file)}
    plan = plan_repository_add(repository, **opts)
    print(f"Project: {plan['repository']} ({plan['repository_id']})")
    print(f"Work: {plan['work']['path']} [{plan['work']['action']}]")
    print(f"Control: {plan['control']['path']} [{plan['control']['action']}]")
    print(f"Checkpoints: {plan['checkpoints']['path']}")
    if plan["catalog_binding"] is None:
        print("A new private canonical binding will be created only on APPLY.")
    print("Work and control directories use Local Agent defaults.")
    print("Existing paths are validated and never cleaned or reset.")
    confirmation = input("Type APPLY to register while daemon is OFFLINE (0 = Back): ").strip()
    if confirmation != "APPLY":
        print("Back to Repositories. No changes applied."
              if confirmation == "0" else "No changes applied.")
        return
    with _require_offline_installation(agentd.STATE_DIR):
        if plan["catalog_binding"] is None:
            from local_agent.repository.binding_enrollment import (
                ensure_local_repository_binding,
            )
            ensure_local_repository_binding(plan["repository_id"], plan["repository"])
        result = apply_repository_add(repository, state_dir=agentd.STATE_DIR, **opts)
    print(json.dumps(result, indent=2, sort_keys=True))
    _restart_note()


def _change(registry_file: Path | None, action: str) -> None:
    repo_id = input("Enter repository ID from the list (0 = Back): ").strip()
    if repo_id == "0":
        return
    if not repo_id:
        print("No repository selected.")
        return
    registry = _registry(registry_file)
    if action == "enable":
        from local_agent.repository.registration_enable import (
            apply_repository_enable, plan_repository_enable,
        )
        plan_func, apply_func = plan_repository_enable, apply_repository_enable
    elif action == "disable":
        from local_agent.repository.registration_disable import (
            apply_repository_disable, plan_repository_disable,
        )
        plan_func, apply_func = plan_repository_disable, apply_repository_disable
    else:
        from local_agent.repository.registration_detach import (
            apply_repository_detach, plan_repository_detach,
        )
        plan_func, apply_func = plan_repository_detach, apply_repository_detach
    plan = plan_func(repo_id, registry_path=registry)
    print(json.dumps(plan, indent=2, sort_keys=True))
    if action == "detach":
        print("Detach only removes a DISABLED registry entry.")
        print("Original worktree, Git history, task mailbox and checkpoints stay intact.")
    else:
        print("Enable/disable only flips a registry flag. It does not cancel tasks.")
    token = "DETACH" if action == "detach" else "APPLY"
    print(f"Type {token} for OFFLINE registry change (0 = Back).")
    confirmation = input("> ").strip()
    if confirmation != token:
        print("Back to Repositories. No changes applied."
              if confirmation == "0" else "No changes applied.")
        return
    result = apply_func(
        repo_id, expected_sha256=plan["expected_sha256"], registry_path=registry,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    _restart_note()


def command_repositories_menu(registry_file: Path | None = None) -> None:
    """No second scheduler; all state changes reuse guarded repository Core."""
    while True:
        print("\nRepositories")
        print("1. List repositories")
        print("2. Add repository")
        print("3. Enable repository")
        print("4. Disable repository")
        print("5. Detach repository (keep files)")
        print("6. Repository details (paths and branches)")
        print("\n0. Back")
        choice = input("> ").strip()
        if choice == "0":
            return
        try:
            if choice == "1":
                _show(registry_file)
            elif choice == "2":
                _add(registry_file)
            elif choice == "3":
                _change(registry_file, "enable")
            elif choice == "4":
                _change(registry_file, "disable")
            elif choice == "5":
                _change(registry_file, "detach")
            elif choice == "6":
                repo_id = input("Enter repository ID from the list (0 = Back): ").strip()
                if repo_id and repo_id != "0":
                    _show(registry_file, repo_id)
            else:
                print("Unknown option.")
        except (OSError, ValueError, RuntimeError, UnicodeError) as exc:
            print(f"Blocked (no unsafe action): {type(exc).__name__}: {exc}")
            print("Offline changes require a stopped/drained daemon and valid binding.")
