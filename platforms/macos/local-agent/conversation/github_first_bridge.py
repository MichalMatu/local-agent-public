"""GitHub-first dispatch admission to Host Ops Chat Bridge transport.

All private repository reads happen in Local Agent, never in the Chrome
extension. The browser receives a bounded immutable child prompt, not a GitHub
token, dispatch-index authority, or parent task scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from local_agent.conversation import github_fabric_dispatch as contract
from local_agent.conversation import github_fabric_github as git
from local_agent.conversation import github_fabric_private_github as private
from local_agent.conversation import github_fabric_private_publication as catalog
from local_agent.host_ops.capabilities.local.browser.chat_bridge_spool import ChatBridgeSpool


@dataclass(frozen=True)
class Admission:
    source_head: str
    dispatch_id: str
    phases: tuple[str, ...]


def read_pinned_dispatch(dispatch_id: str, *, api: Any) -> tuple[str, dict[str, Any]]:
    """Resolve one indexed private dispatch using a single immutable Git SHA."""
    path = catalog.dispatch_path(dispatch_id)
    remote = api.request("GET", private.REF_PATH)
    if (remote.get("ref") != "refs/heads/" + private.PRIVATE_BRANCH
            or remote.get("object", {}).get("type") != "commit"):
        raise ValueError("Private Fabric branch ref invalid")
    head = git._require_sha(remote.get("object", {}).get("sha"), label="private head")
    project = git._read_json_at_commit(api, catalog.CATALOG_PATH, head,
                                       max_bytes=catalog.MAX_INDEX_BYTES)
    workflows = git._read_json_at_commit(api, catalog.WORKFLOWS_PATH, head,
                                         max_bytes=catalog.MAX_INDEX_BYTES)
    index = git._read_json_at_commit(api, catalog.INDEX_PATH, head,
                                     max_bytes=catalog.MAX_INDEX_BYTES)
    if (catalog.PROJECT_ID not in catalog.validate_projects(project)
            or catalog.WORKFLOW_ID not in catalog.validate_workflows(workflows)
            or dispatch_id not in catalog.validate_dispatch_index(index)):
        raise ValueError("Private Fabric dispatch not published in all indexes")
    dispatch = git._read_json_at_commit(api, path, head, max_bytes=catalog.MAX_RECORD_BYTES)
    contract.validate_github_fabric_dispatch(dispatch)
    if dispatch["id"] != dispatch_id:
        raise ValueError("Private Fabric dispatch path conflict")
    return head, dispatch


def admit_dispatch(dispatch_id: str, spool: ChatBridgeSpool, *,
                   enabled: bool = False, api: Any | None = None,
                   token: str | None = None) -> Admission:
    """Explicit admission, independent of any browser Send or legacy DOM read.

    Browser work is merely staged. No private browser Send is activated by
    this function and no result is inferred from a transport ACK.
    """
    if enabled is not True:
        raise PermissionError("GitHub-first browser transport staging is disabled")
    if api is None:
        api = private.PrivateFabricREST(token)
    head, dispatch = read_pinned_dispatch(dispatch_id, api=api)
    phases = []
    for child in dispatch["children"]:
        spawn = child["spawn"]
        phases.append(spool.offer({
            "schema_version": 1, "dispatch_id": dispatch["id"],
            "child_request_id": child["request_id"],
            "intent": {**spawn, "tab_id": None},
        }))
    return Admission(head, dispatch["id"], tuple(phases))


def sync_indexed_private_dispatches(spool: ChatBridgeSpool, *, api: Any,
                                    dispatch_id: str | None = None) -> dict[str, Any]:
    """Source-pinned GitHub -> Host Ops projection, optionally scoped to one pilot."""
    from local_agent.conversation import github_first_results

    remote = api.request("GET", private.REF_PATH)
    if (remote.get("ref") != "refs/heads/" + private.PRIVATE_BRANCH
            or remote.get("object", {}).get("type") != "commit"):
        raise ValueError("Private Fabric sync branch ref invalid")
    head = git._require_sha(remote.get("object", {}).get("sha"), label="private sync head")
    projects = git._read_json_at_commit(api, catalog.CATALOG_PATH, head,
                                        max_bytes=catalog.MAX_INDEX_BYTES)
    workflows = git._read_json_at_commit(api, catalog.WORKFLOWS_PATH, head,
                                         max_bytes=catalog.MAX_INDEX_BYTES)
    if (catalog.PROJECT_ID not in catalog.validate_projects(projects)
            or catalog.WORKFLOW_ID not in catalog.validate_workflows(workflows)):
        raise ValueError("Private Fabric workflow is not indexed")
    index = git._read_json_at_commit(api, catalog.INDEX_PATH, head,
                                     max_bytes=catalog.MAX_INDEX_BYTES)
    phases: dict[str, tuple[str, ...]] = {}
    results: dict[str, tuple[str, ...]] = {}
    indexed_ids = catalog.validate_dispatch_index(index)
    if dispatch_id is not None:
        if not isinstance(dispatch_id, str) or dispatch_id not in indexed_ids:
            raise ValueError("Selected Fabric dispatch is not in the private index")
        selected_ids = (dispatch_id,)
    else:
        selected_ids = indexed_ids
    for indexed_id in selected_ids:
        dispatch = git._read_json_at_commit(api, catalog.dispatch_path(indexed_id), head,
                                             max_bytes=catalog.MAX_RECORD_BYTES)
        contract.validate_github_fabric_dispatch(dispatch)
        if dispatch["id"] != indexed_id:
            raise ValueError("Indexed private dispatch does not match its path")
        # A result's presence is informative; it is not an excuse to replay an
        # unknown Submit, nor a browser-derived completion signal.
        completed = github_first_results.read_pinned_results(api, head, dispatch)
        results[indexed_id] = tuple(sorted(completed))
        delivered = []
        for child in dispatch["children"]:
            if child["request_id"] in completed:
                delivered.append("result_published")
                continue
            delivered.append(spool.offer({
                "schema_version": 1, "dispatch_id": indexed_id,
                "child_request_id": child["request_id"],
                "intent": {**child["spawn"], "tab_id": None},
            }))
        if len(completed) == len(dispatch["children"]):
            import hashlib
            wake_id = "wake-" + hashlib.sha256(
                ("github-first-parent-wake-v1:" + indexed_id).encode("ascii")
            ).hexdigest()
            spool.offer_parent_wake({
                "schema_version": 1, "kind": "parent_wake", "wake_id": wake_id,
                "dispatch_id": indexed_id, "parent_url": dispatch["parent_conversation_url"],
                "child_request_ids": [c["request_id"] for c in dispatch["children"]],
            })
        phases[indexed_id] = tuple(delivered)
    return {"source_head": head, "phases": phases, "completed_ids": results}


def sync_future_private_dispatches(spool: ChatBridgeSpool, *, api: Any,
                                  cursor_path: Any) -> dict[str, Any]:
    """Global discovery with durable first-start fence for pre-existing history.

    First start records all already-indexed dispatches as *ignored*. Future
    dispatch IDs become tracked before any browser effect. On restart, tracked
    IDs are reconciled without replaying any consumed spool transaction.
    """
    import json
    import os
    import tempfile
    from pathlib import Path

    path = Path(cursor_path)
    remote = api.request("GET", private.REF_PATH)
    if (remote.get("ref") != "refs/heads/" + private.PRIVATE_BRANCH
            or remote.get("object", {}).get("type") != "commit"):
        raise ValueError("Private Fabric source ref invalid")
    head = git._require_sha(remote.get("object", {}).get("sha"), label="private intake head")
    projects = git._read_json_at_commit(api, catalog.CATALOG_PATH, head,
                                         max_bytes=catalog.MAX_INDEX_BYTES)
    workflows = git._read_json_at_commit(api, catalog.WORKFLOWS_PATH, head,
                                          max_bytes=catalog.MAX_INDEX_BYTES)
    if (catalog.PROJECT_ID not in catalog.validate_projects(projects)
            or catalog.WORKFLOW_ID not in catalog.validate_workflows(workflows)):
        raise ValueError("Private Fabric project/workflow missing")
    index = git._read_json_at_commit(api, catalog.INDEX_PATH, head,
                                     max_bytes=catalog.MAX_INDEX_BYTES)
    current = catalog.validate_dispatch_index(index)
    if path.exists():
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise PermissionError("Private Fabric intake cursor must be owner-only")
        state = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(state, dict) or set(state) != {"schema_version", "ignored", "tracked"}
                or state["schema_version"] != 1 or not isinstance(state["ignored"], list)
                or not isinstance(state["tracked"], list)
                or state["ignored"] != sorted(set(state["ignored"]))
                or state["tracked"] != sorted(set(state["tracked"]))
                or set(state["ignored"]) & set(state["tracked"])
                or any(not isinstance(i, str) or not i.startswith("fabric-") or len(i) != 39
                       for i in state["ignored"] + state["tracked"])):
            raise ValueError("Invalid durable Fabric intake cursor")
    else:
        state = {"schema_version": 1, "ignored": sorted(current), "tracked": []}
    new = set(current) - set(state["ignored"]) - set(state["tracked"])
    if new:
        state["tracked"] = sorted(set(state["tracked"]) | new)
    if not path.exists() or new:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.is_symlink() or path.parent.stat().st_mode & 0o077:
            raise PermissionError("Fabric intake cursor directory must be private")
        fd, tmp = tempfile.mkstemp(prefix=".fabric-intake-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(state, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    phases = {}
    for dispatch_id in state["tracked"]:
        if dispatch_id not in current:
            raise ValueError("Tracked private dispatch unexpectedly missing from index")
        result = sync_indexed_private_dispatches(spool, api=api, dispatch_id=dispatch_id)
        phases.update(result["phases"])
    return {"source_head": head, "ignored_count": len(state["ignored"]),
            "tracked_count": len(state["tracked"]), "phases": phases}


def main(argv: list[str] | None = None) -> int:
    """Optional isolated source-only runner, not registered as a daemon."""
    import argparse
    import json
    import os
    import threading
    import time
    import secrets
    from pathlib import Path

    from local_agent.host_ops.capabilities.local.browser.chat_bridge_http import serve_bridge

    parser = argparse.ArgumentParser(description="GitHub-first Local Agent/Chat Bridge transport")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sync-once", action="store_true")
    mode.add_argument("--serve", action="store_true")
    mode.add_argument("--init-pairing", action="store_true")
    mode.add_argument("--pair-only", action="store_true",
                      help="loopback pairing with native operator approval, no GitHub token")
    parser.add_argument("--spool", type=Path, required=True)
    parser.add_argument("--dispatch-id", help="exact indexed dispatch for one controlled trial")
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--pairing-key-file", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.pair_only and args.dispatch_id:
        parser.error("--pair-only cannot stage any dispatch")
    if args.init_pairing and args.dispatch_id:
        parser.error("--dispatch-id is not used for --init-pairing")
    if args.sync_once and not args.dispatch_id:
        parser.error("--sync-once requires an exact --dispatch-id")
    if not 30 <= args.poll_seconds <= 3600:
        parser.error("poll-seconds must be 30..3600")
    pairing_file = args.pairing_key_file or args.spool.with_suffix(".key")
    if args.init_pairing:
        pairing_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(pairing_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            print("Existing Local Agent pairing retained; no rotation performed")
        else:
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                stream.write(secrets.token_urlsafe(48) + "\n")
            print("Local Agent pairing created in private file")
        return 0
    key = os.environ.get("LOCAL_AGENT_BRIDGE_PAIRING_KEY", "")
    if not key and pairing_file.is_file():
        if pairing_file.stat().st_mode & 0o077:
            parser.error("pairing key file must be owner-only (chmod 600)")
        key = pairing_file.read_text(encoding="ascii").strip()
    from local_agent.host_ops.capabilities.local.browser.chat_bridge_pairing import BridgePairing

    spool = ChatBridgeSpool(args.spool)
    if args.pair_only:
        # Standalone on-demand pairing never starts GitHub intake or Send.
        serve_bridge(spool, pairing=BridgePairing(pairing_file),
                     pairing_only=True, port=args.port)
        return 0
    token = os.environ.get("LOCAL_AGENT_GITHUB_FABRIC_WRITE_TOKEN", "")
    if not token:
        # Machine login credential stays inside this process, never in a plist,
        # Chrome, the spool, GitHub dispatch records or console output.
        import subprocess
        try:
            acquired = subprocess.run(
                ["gh", "auth", "token", "--hostname", "github.com"],
                capture_output=True, text=True, timeout=10, check=True,
            )
            token = acquired.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            parser.error("trusted machine GitHub login unavailable")
    if len(token) < 16:
        parser.error("trusted machine GitHub credential unavailable")
    api = private.PrivateFabricREST(token)
    if args.sync_once:
        result = sync_indexed_private_dispatches(spool, api=api, dispatch_id=args.dispatch_id)
        print(json.dumps({"source_head": result["source_head"],
                          "phases": result["phases"],
                          "completed_ids": result["completed_ids"]}, sort_keys=True))
        return 0

    def sync_loop() -> None:
        while True:
            try:
                if args.dispatch_id:
                    sync_indexed_private_dispatches(spool, api=api, dispatch_id=args.dispatch_id)
                else:
                    sync_future_private_dispatches(
                        spool, api=api, cursor_path=args.spool.with_suffix(".intake.json")
                    )
            except (ValueError, OSError, git.GithubFabricHTTPError,
                    git.GithubFabricTransportError):
                # Never leak private GitHub request headers or prompt bodies.
                print("GitHub-first intake blocked; operator reconciliation required", flush=True)
            time.sleep(args.poll_seconds)

    thread = threading.Thread(target=sync_loop, name="fabric-intake", daemon=True)
    thread.start()
    serve_bridge(spool, key, port=args.port, pairing=BridgePairing(pairing_file))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
