"""Bounded service operations owned by the independent macOS updater.

This is not a task executor. It reads the existing installation's service
namespace through a private bare Git cache, independently of project workers.
"""
from __future__ import annotations

import base64
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
import time

from scripts import macos_cli_polling_rollout as rollout

ACCEPTED = "accepted-service.json"
TRANSACTION = "service-recovery.json"
SERVICE_ACTIONS = frozenset({"host_diagnostics", "status", "restart", "self_update"})
MAX_RECORD = 65536


def _updater():
    from scripts import macos_host_self_update
    return macos_host_self_update


def _run(argv: list[str], *, data: str | None = None,
         env: dict | None = None, timeout: int = 20) -> str:
    # Git objects are size-checked before reading. Other commands have bounded
    # fixed-shape output and never execute task-provided shell text.
    result = subprocess.run(argv, input=data, capture_output=True, text=True,
                            env=env, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"service command failed: {argv[0]} exit={result.returncode}")
    if len(result.stdout.encode()) > MAX_RECORD:
        raise RuntimeError("oversized service command output")
    return result.stdout.strip()


def _state(home: Path) -> Path:
    state = home / "Library/Application Support/local-agent"
    _updater()._check_directory(state, private=False)
    return state


def _read(path: Path) -> dict:
    value = json.loads(_updater()._private_file(path, max_bytes=MAX_RECORD))
    if not isinstance(value, dict):
        raise ValueError("service record must be an object")
    return value


def _write(path: Path, value: dict) -> None:
    rollout._atomic(path, (json.dumps(value, sort_keys=True) + "\n").encode())


def remember_accepted(home: Path, revision: str, raw: bytes) -> None:
    """Record the current accepted definition, never infer it from old intents."""
    updater = _updater()
    updater._sha(revision)
    document = plistlib.loads(raw)
    source = Path(document["WorkingDirectory"])
    argv = document["ProgramArguments"]
    if (document.get("Label") != rollout.LABEL or not source.is_absolute()
            or len(argv) != 6 or argv[1] != str(source / "agent_entrypoint.py")
            or updater._git(source, "rev-parse", "HEAD") != revision
            or updater._git(source, "status", "--porcelain")
            or updater._git(source, "remote", "get-url", "origin") not in rollout.SOURCE_ORIGINS):
        raise ValueError("accepted service source/definition mismatch")
    identity = _read(_state(home) / "host-identity.json")
    _write(_state(home) / ACCEPTED, {
        "version": 1, "revision": revision,
        "host_id": identity["host_id"], "installation_id": identity["installation_id"],
        "plist": base64.b64encode(raw).decode(),
        "plist_sha256": hashlib.sha256(raw).hexdigest(),
        "service_actions_owner": "external_updater" if (source / "scripts/macos_host_service.py").is_file() else "supervisor",
    })


def observation(home: Path) -> dict:
    state = _state(home)
    try:
        heartbeat = _read(state / "status.json")
    except (OSError, ValueError):
        heartbeat = {"state": "unavailable"}
    try:
        raw = _updater()._installed_plist(home / "Library/LaunchAgents/com.michal.local-agent.plist")
        live = plistlib.loads(raw)
    except (OSError, ValueError):
        raw, live = b"", {}
    accepted = _read(state / ACCEPTED) if (state / ACCEPTED).exists() else None
    try:
        stamp = datetime.fromisoformat(heartbeat["updated_at"])
        fresh = stamp.tzinfo is not None and 0 <= (datetime.now(timezone.utc) - stamp).total_seconds() <= 45
    except (KeyError, TypeError, ValueError):
        fresh = False
    result = {
        "version": 1, "observed_at": datetime.now(timezone.utc).isoformat(),
        "running_revision": heartbeat.get("self_revision"),
        "state": heartbeat.get("state"), "heartbeat_at": heartbeat.get("updated_at"),
        "active_repository_ids": heartbeat.get("active_repository_ids"),
        "installed_source": live.get("WorkingDirectory"),
        "accepted_revision": accepted.get("revision") if accepted else None,
        "disabled": (state / "disabled.json").exists() or (state / "disabled.json").is_symlink(),
        "service_channel": "independent_updater", "task_replay": False,
        "heartbeat_fresh": fresh,
    }
    if accepted:
        result["definition_matches"] = hashlib.sha256(raw).hexdigest() == accepted["plist_sha256"]
        result["revision_matches"] = heartbeat.get("self_revision") == accepted["revision"]
        expected = plistlib.loads(base64.b64decode(accepted["plist"], validate=True))["ProgramArguments"][1]
        try:
            result["generation_matches"] = rollout._generation(
                f"gui/{os.getuid()}/{rollout.LABEL}",
                "/__local_agent_unselected_generation__/agent_entrypoint.py", expected,
            ) == "candidate"
        except (RuntimeError, OSError, subprocess.SubprocessError):
            result["generation_matches"] = False
    return result


def _lease(stack: ExitStack, path: Path, *, create: bool = False) -> None:
    if path.is_symlink() or (not create and not path.is_file()):
        raise ValueError("service recovery requires existing owned lease files")
    fd = os.open(path, os.O_RDWR | (os.O_CREAT if create else 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
    stack.callback(os.close, fd)
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
        raise ValueError("untrusted service recovery lease")
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _registered_leases(home: Path, source: Path) -> list[Path]:
    # Reuse canonical registry/key policy from the verified accepted source,
    # without importing runtime globals into the independent controller.
    program = (
        "import json,sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
        "from local_agent.repository.context import load_repository_registry; "
        "from local_agent.foundation.lease_recovery import repository_lease_paths; "
        "from local_agent.foundation.process import execution_lease_path; "
        "from local_agent.supervisor.host_control import load_host_control_source,host_control_lease_keys; "
        "home=Path(sys.argv[2]); state=home/'Library/Application Support/local-agent'; "
        "repos=load_repository_registry(home=home,path=state/'repositories.json',include_disabled=True,allow_empty=True); "
        "paths=list(repository_lease_paths(repos,state_dir=state)); "
        "host=load_host_control_source(state/'supervisor-control.json'); "
        "paths.extend(execution_lease_path(state/'locks/repositories',key) for key in host_control_lease_keys(host)) if host else None; "
        "print(json.dumps([str(p) for p in paths]))"
    )
    paths = json.loads(_run([sys.executable, "-B", "-c", program, str(source), str(home)]))
    parent = _state(home) / "locks/repositories"
    if (not isinstance(paths, list) or len(paths) > 4096
            or any(not isinstance(p, str) or Path(p).parent != parent
                   or re.fullmatch(r"[a-f0-9]{64}\.lock", Path(p).name) is None for p in paths)):
        raise ValueError("invalid registered execution lease inventory")
    # Project-free host commands also inherit the shared machine-resource
    # descriptor. Its exclusive counterpart fences descendants after their
    # admitting worker (and its Mac admission lock) has disappeared.
    resources = _state(home) / "locks/machine-resources"
    resources.mkdir(mode=0o700, parents=True, exist_ok=True)
    _updater()._check_directory(resources, private=False)
    return [Path(p) for p in paths] + [resources / "machine.lock", *resources.glob("*.lock")]


def adopt_accepted_release(home: Path, revision: str) -> dict:
    """Explicit migration from an independently accepted pinned release record."""
    updater = _updater()
    updater._sha(revision)
    state = _state(home)
    release = state / "releases" / revision
    journal = _read(release / rollout.JOURNAL)
    manifest = _read(release / "staging-manifest.json")
    identity = _read(state / "host-identity.json")
    raw = updater._private_file(release / "mvp-candidate.plist", max_bytes=MAX_RECORD)
    if (journal.get("state") != "accepted" or journal.get("candidate_sha") != revision
            or manifest.get("candidate_sha") != revision
            or manifest.get("host_id") != identity.get("host_id")
            or manifest.get("installation_id") != identity.get("installation_id")
            or journal.get("plist_candidate_sha256") != hashlib.sha256(raw).hexdigest()):
        raise ValueError("release has no exact native accepted installation evidence")
    remember_accepted(home, revision, raw)
    return {"status": "accepted_release_recorded", "revision": revision, "service_changed": False}


def _switch(home: Path, raw: bytes, revision: str, previous: bytes, *, release_leases=lambda: None) -> None:
    """Switch outside workers, retaining the existing controller's rollback."""
    state = _state(home)
    plist = home / "Library/LaunchAgents/com.michal.local-agent.plist"
    old = plistlib.loads(previous)["ProgramArguments"][1]
    new = plistlib.loads(raw)["ProgramArguments"][1]
    if old == new:
        old = "/__local_agent_unselected_generation__/agent_entrypoint.py"
    target, domain = f"gui/{os.getuid()}/{rollout.LABEL}", f"gui/{os.getuid()}"
    rollout._generation(target, old, new)
    rollout._atomic(plist, raw)
    rollout._bootout_and_wait(target, old, new)
    with ExitStack() as locks:
        _lease(locks, state / "agentd.lock")
    # The old generation is absent and its daemon lock is free. The new
    # accepted generation honors the still-held exclusive admission lease.
    # Do not present maintenance-owned repo locks as orphaned worker leases.
    release_leases()
    rollout._bootstrap_with_retry(domain, plist, target, expected="candidate", baseline=old, candidate=new)
    started = datetime.now(timezone.utc)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            heartbeat = _read(state / "status.json")
            stamp = datetime.fromisoformat(heartbeat["updated_at"])
            registered = rollout._launchctl("print", target, acceptable=(0,)).stdout
            match = re.search(r"^\s*pid = (\d+)\s*$", registered, re.MULTILINE)
            owner = heartbeat.get("supervisor_pid", heartbeat.get("pid"))
            parent = (_run(["/bin/ps", "-o", "ppid=", "-p", str(owner)])
                      if type(owner) is int and owner > 1 else "")
            process_matches = bool(match and (str(owner) == match[1] or parent == match[1]))
            if (heartbeat.get("self_revision") == revision and stamp.tzinfo is not None
                    and stamp > started and heartbeat.get("state") in ("running", "idle")
                    and process_matches
                    and rollout._generation(target, old, new) == "candidate"):
                return
        except (OSError, KeyError, TypeError, ValueError, RuntimeError, subprocess.SubprocessError):
            pass
        time.sleep(0.5)
    raise RuntimeError("service recovery lacks a fresh exact-source generation")


def recover(home: Path, *, force_restart: bool = False) -> dict:
    """Restore only the accepted release; durable execution ledgers stay intact."""
    state = _state(home)
    root = home / _updater().UPDATER_ROOT
    journal = root / TRANSACTION
    prior = _read(journal) if journal.exists() else {}
    observed = observation(home)
    if observed["disabled"]:
        return {"status": "disabled", "changed": False}
    pending = state / "installation-pending.json"
    if pending.exists() or pending.is_symlink():
        return {"status": "installation_recovery_required", "changed": False}
    if prior.get("state") == "rollback_failed" and not force_restart:
        return {"status": "rollback_failed_requires_new_request", "changed": False}
    if (observed.get("definition_matches") and observed.get("revision_matches")
            and observed.get("heartbeat_fresh") and observed.get("generation_matches")
            and prior.get("state") != "switching" and not force_restart):
        return {"status": "service_matches_accepted", "changed": False}
    accepted = _read(state / ACCEPTED)
    identity = _read(state / "host-identity.json")
    if (accepted.get("host_id") != identity.get("host_id")
            or accepted.get("installation_id") != identity.get("installation_id")):
        raise ValueError("accepted service belongs to another installation")
    raw = base64.b64decode(accepted["plist"], validate=True)
    if hashlib.sha256(raw).hexdigest() != accepted["plist_sha256"]:
        raise ValueError("accepted service definition digest mismatch")
    source = Path(plistlib.loads(raw)["WorkingDirectory"])
    updater = _updater()
    if (updater._git(source, "rev-parse", "HEAD") != accepted["revision"]
            or updater._git(source, "status", "--porcelain")
            or updater._git(source, "remote", "get-url", "origin") not in rollout.SOURCE_ORIGINS):
        raise ValueError("accepted recovery source changed")
    plist = home / "Library/LaunchAgents/com.michal.local-agent.plist"
    previous = updater._installed_plist(plist)
    fingerprint = hashlib.sha256(previous).hexdigest()
    if (prior.get("state") == "failed" and prior.get("drift") == fingerprint
            and not force_restart):
        return {"status": "repair_failed_requires_new_request", "changed": False}
    with ExitStack() as locks:
        repository_locks = ExitStack()
        locks.callback(repository_locks.close)
        try:
            _lease(locks, root / "service-lifecycle.lock", create=True)
            prior = _read(journal) if journal.exists() else {}
            _lease(locks, state / "installation.lock")
            _lease(locks, state / rollout.LOCK)
            # Retained claims/spooled terminal results are not active work.
            # Existing flock ownership, including descendants, is authoritative.
            registry = (state / "repositories.json").read_bytes()
            paths = sorted(set(_registered_leases(home, source)) |
                           set((state / "locks/repositories").glob("*.lock")))
            if len(paths) > 4096:
                raise ValueError("unbounded repository lease inventory")
            for path in paths:
                _lease(repository_locks, path, create=True)
        except BlockingIOError:
            return {"status": "waiting_for_active_leases", "changed": False}
        marker = state / rollout.FROZEN
        if prior.get("state") == "prepared":
            if marker.exists():
                if _read(marker) != {"service_recovery": prior["revision"]}:
                    raise ValueError("foreign admission marker during interrupted preparation")
                marker.unlink()
            _write(journal, {**prior, "state": "failed"})
            return {"status": "interrupted_service_preparation_released", "changed": False}
        if prior.get("state") in ("switching", "rollback_failed"):
            if (not marker.is_file() or marker.is_symlink()
                    or _read(marker) != {"service_recovery": prior["revision"]}):
                raise ValueError("interrupted service recovery has no owned admission marker")
            rollback = base64.b64decode(prior["previous"], validate=True)
            _switch(home, rollback, prior["previous_revision"], previous, release_leases=repository_locks.close)
            _write(journal, {**prior, "state": "failed"})
            marker.unlink()
            return {"status": "interrupted_service_recovery_rolled_back", "changed": True}
        if marker.exists() or marker.is_symlink():
            return {"status": "maintenance_in_progress", "changed": False}
        record = {"state": "prepared", "revision": accepted["revision"],
                  "drift": fingerprint, "previous": base64.b64encode(previous).decode(),
                  "previous_revision": observed["running_revision"]}
        _write(journal, record)
        updater._atomic(marker, {"service_recovery": accepted["revision"]})
        record["state"] = "switching"
        _write(journal, record)
        thaw = False
        try:
            if (state / "repositories.json").read_bytes() != registry:
                raise RuntimeError("repository selection changed during recovery")
            _switch(home, raw, accepted["revision"], previous, release_leases=repository_locks.close)
            _write(journal, {**record, "state": "accepted"})
            thaw = True
        except BaseException:
            try:
                _switch(home, previous, record["previous_revision"], raw, release_leases=repository_locks.close)
            except BaseException:
                _write(journal, {**record, "state": "rollback_failed"})
                raise
            _write(journal, {**record, "state": "failed"})
            thaw = True
            raise
        finally:
            if thaw and _read(marker) == {"service_recovery": accepted["revision"]}:
                marker.unlink()
    return {"status": "accepted_service_recovered", "changed": True,
            "revision": accepted["revision"]}


def _mailbox(home: Path, config: dict) -> Path:
    updater = _updater()
    repository = config.get("repository")
    if not isinstance(repository, str) or re.fullmatch(
            r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}", repository) is None:
        raise ValueError("invalid service Git repository")
    root = home / updater.UPDATER_ROOT
    cache = root / "service.git"
    origin = f"https://github.com/{repository}.git"
    if not cache.exists():
        _run(["/usr/bin/git", "init", "--bare", str(cache)])
        cache.chmod(0o700)
        _run(["/usr/bin/git", "-C", str(cache), "remote", "add", "origin", origin])
    updater._check_directory(cache)
    for args in (("config", "--get", "remote.origin.url"), ("remote", "get-url", "origin")):
        if _run(["/usr/bin/git", "-C", str(cache), *args]) != origin:
            raise ValueError("service Git origin changed")
    return cache


def _fetch(cache: Path) -> str:
    _run(["/usr/bin/git", "-C", str(cache), "fetch", "--quiet", "--depth=64", "--no-tags",
          "origin", "+refs/heads/agent-control:refs/remotes/origin/agent-control"])
    return _run(["/usr/bin/git", "-C", str(cache), "rev-parse", "refs/remotes/origin/agent-control"])


def _blob(cache: Path, commit: str, path: str) -> dict | None:
    object_name = f"{commit}:{path}"
    found = _run(["/usr/bin/git", "-C", str(cache), "ls-tree", commit, "--", path])
    if not found:
        return None
    size = int(_run(["/usr/bin/git", "-C", str(cache), "cat-file", "-s", object_name]))
    if not 0 < size <= MAX_RECORD:
        raise ValueError("service record exceeds transport limit")
    value = json.loads(_run(["/usr/bin/git", "-C", str(cache), "show", object_name]))
    if not isinstance(value, dict):
        raise ValueError("invalid service Git record")
    return value


def _publish(cache: Path, paths: dict[str, dict]) -> None:
    """CAS-style publication from a temporary index; never rebase a checkout."""
    for attempt in range(3):
        commit = _fetch(cache)
        fd, name = tempfile.mkstemp(prefix="service-index-", dir=cache.parent)
        os.close(fd)
        index = Path(name)
        index.unlink()
        env = {**os.environ, "GIT_INDEX_FILE": str(index),
               "GIT_AUTHOR_NAME": "Local Agent service", "GIT_COMMITTER_NAME": "Local Agent service",
               "GIT_AUTHOR_EMAIL": "service@local-agent.invalid", "GIT_COMMITTER_EMAIL": "service@local-agent.invalid"}
        git = ["/usr/bin/git", "-C", str(cache)]
        try:
            _run([*git, "read-tree", commit], env=env)
            for path, value in paths.items():
                existing = _blob(cache, commit, path)
                if existing is not None and existing.get("request_digest") != value.get("request_digest"):
                    raise ValueError("conflicting immutable service response")
                blob = _run([*git, "hash-object", "-w", "--stdin"],
                            data=json.dumps(value, sort_keys=True) + "\n", env=env)
                _run([*git, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}"], env=env)
            tree = _run([*git, "write-tree"], env=env)
            new = _run([*git, "commit-tree", tree, "-p", commit],
                       data="Publish bounded host service response\n", env=env)
            try:
                _run([*git, "push", "--quiet", "origin", f"{new}:refs/heads/agent-control"], env=env)
                return
            except RuntimeError:
                if attempt == 2:
                    raise
        finally:
            index.unlink(missing_ok=True)
            Path(str(index) + ".lock").unlink(missing_ok=True)


def poll(home: Path) -> dict:
    """Consume only fixed service actions, independently of the task scheduler."""
    updater = _updater()
    state = _state(home)
    config, identity = _read(state / "supervisor-control.json"), _read(state / "host-identity.json")
    accepted_path = state / ACCEPTED
    if (not accepted_path.is_file() or _read(accepted_path).get("service_actions_owner") != "external_updater"):
        return {"status": "service_consumer_migration_required"}
    if config.get("enabled") is not True or config.get("host_id") != identity.get("host_id"):
        raise ValueError("service host configuration/identity mismatch")
    host, installation = identity["host_id"], identity["installation_id"]
    if (re.fullmatch(r"[a-z][a-z0-9-]{2,63}", host) is None
            or re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", installation) is None):
        raise ValueError("invalid service installation namespace")
    namespace = f".agent/hosts/{host}/supervisor/{installation}"
    cache = _mailbox(home, config)
    snapshot = _fetch(cache)
    request = _blob(cache, snapshot, namespace + "/control.json")
    activation = home / updater.UPDATER_ROOT / "service-activation.json"
    if not activation.exists():
        historical = (hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
                      if request is not None else None)
        updater._atomic(activation, {"snapshot": snapshot, "historical_digest": historical})
    if request is None or request.get("action") not in SERVICE_ACTIONS:
        return {"status": "no_service_request"}
    fields = {"version", "id", "action", "target_host_id", "target_installation_id"}
    if request["action"] == "self_update":
        fields.add("candidate_sha")
        updater._sha(request.get("candidate_sha"))
    if (set(request) != fields or type(request["version"]) is not int or request["version"] != 1
            or not isinstance(request["id"], str) or updater._ID.fullmatch(request["id"]) is None
            or request["target_host_id"] != host or request["target_installation_id"] != installation):
        raise ValueError("invalid exact-installation service request")
    directory = home / updater.UPDATER_ROOT / "requests"
    directory.mkdir(mode=0o700, exist_ok=True)
    updater._check_directory(directory)
    ledger = directory / (request["id"] + ".json")
    digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    if _read(activation).get("historical_digest") == digest:
        if not ledger.exists():
            updater._atomic(ledger, {"digest": digest, "state": "completed",
                                    "result": {"status": "historical_fenced"}})
    saved = _read(ledger) if ledger.exists() else None
    if saved is not None:
        if saved.get("digest") != digest:
            raise ValueError("immutable service request id reused")
        result = saved.get("result") or {"status": "unknown", "task_replay": False}
        if result["status"] == "historical_fenced":
            ack = _blob(cache, snapshot, f"{namespace}/acks/{request['id']}.json")
            if ack is not None:
                return {"status": "historical_service_request_fenced", "id": request["id"]}
    if saved is None or saved.get("state") == "waiting":
        if saved is None:
            ack = _blob(cache, _fetch(cache), f"{namespace}/acks/{request['id']}.json")
            if ack is not None:
                if ack.get("id") != request["id"] or ack.get("action") != request["action"]:
                    raise ValueError("conflicting historical service acknowledgement")
                return {"status": "service_request_already_acknowledged", "id": request["id"]}
            updater._atomic(ledger, {"digest": digest, "state": "claimed"})
        if request["action"] in ("host_diagnostics", "status"):
            result = {"status": "completed", "diagnostics": observation(home)}
        elif (state / "disabled.json").exists() or (state / "disabled.json").is_symlink():
            result = {"status": "disabled", "changed": False}
        elif request["action"] == "restart":
            result = recover(home, force_restart=True)
        else:
            location = state / updater.INTENTS
            location.mkdir(mode=0o700, exist_ok=True)
            updater._check_directory(location)
            intent = {"version": 1, "request_id": request["id"],
                      "candidate_sha": request["candidate_sha"], "target_host_id": host,
                      "target_installation_id": installation, "status": "pending_external_controller"}
            target = location / (request["id"] + ".json")
            try:
                updater._atomic(target, intent)
            except FileExistsError:
                if _read(target) != intent:
                    raise ValueError("conflicting pinned service update intent")
            result = {"status": "queued", "candidate_sha": request["candidate_sha"]}
        phase = "waiting" if result["status"] == "waiting_for_active_leases" else "completed"
        _write(ledger, {"digest": digest, "state": phase, "result": result})
    payload = {"version": 1, "id": request["id"], "action": request["action"],
               "target_host_id": host, "target_installation_id": installation,
               "request_digest": digest, **result}
    _publish(cache, {f"{namespace}/results/{request['id']}.json": payload,
                     f"{namespace}/acks/{request['id']}.json": payload})
    return {"status": "service_response_published", "id": request["id"], "result": result["status"]}
